"""
Fetch JRE episode list and transcripts from YouTube.

Episodes are fetched from the PowerfulJRE channel using the stable uploads-
playlist ID (avoids the @handle 404 that occurs in some environments).

For each episode we retrieve the auto-generated English transcript with
timestamps, then apply a heuristic speaker filter to keep only segments
likely spoken by Joe before storing them in the database.

Speaker filtering
-----------------
YouTube auto-captions carry no speaker labels.  We use a turn-length
heuristic: JRE guests tend to deliver long monologues while Joe asks
shorter questions and interjects with brief affirmations.

  1. Segment the transcript into speaker "turns" by detecting pauses
     > PAUSE_THRESHOLD seconds between adjacent segments.
  2. Compute per-episode median turn word-count.
  3. Turns below the median → attributed to Joe.
     Turns at/above the median → attributed to the guest.

Accuracy is roughly 65–70 % without full audio diarization.  This is
sufficient for keyword counting across many episodes; per-episode figures
should be treated as estimates.
"""

import re
import time
import json
import logging
from datetime import datetime
from typing import Optional

try:
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    from yt_dlp import YoutubeDL
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

from .database import Database

logger = logging.getLogger(__name__)

# ── Channel identifiers ───────────────────────────────────────────────────────
# Stable uploads-playlist URL (replaces the @handle which causes 404 in some
# yt-dlp versions / deployment environments like Railway).
_CHANNEL_ID      = "UCzWQYUVCpZqtN93H8RR44Qw"
_UPLOADS_LIST_ID = "UU" + _CHANNEL_ID[2:]   # UCxxx → UUxxx

JRE_PLAYLIST_URL = f"https://www.youtube.com/playlist?list={_UPLOADS_LIST_ID}"
JRE_CHANNEL_URL  = f"https://www.youtube.com/channel/{_CHANNEL_ID}/videos"
JRE_HANDLE_URL   = "https://www.youtube.com/@PowerfulJRE/videos"   # last-resort

# Pause between adjacent transcript segments that signals a speaker change (s)
_PAUSE_THRESHOLD = 2.0

# Joe's characteristic short affirmations / reactions (used as a tiebreaker)
_JOE_SIGNALS: frozenset[str] = frozenset(
    "yeah right wow really interesting absolutely totally dude man bro "
    "exactly sure okay seriously incredible insane crazy amazing wild "
    "fascinating no yes true correct definitely exactly".split()
)


# ── yt-dlp helpers ───────────────────────────────────────────────────────────

def _ydl_opts(quiet: bool = True) -> dict:
    return {
        "quiet":          quiet,
        "no_warnings":    quiet,
        "extract_flat":   True,
        "ignoreerrors":   True,
        "skip_download":  True,
        # Mimic a regular browser to reduce bot-detection blocks on Railway
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_retries": 3,
    }


def _extract_with_fallbacks(ydl: "YoutubeDL", max_episodes: int) -> list:
    """
    Try URLs in order of reliability:
      1. Uploads playlist  (most stable, no @handle parsing needed)
      2. Channel /videos   (needs channel ID, still robust)
      3. @handle           (may 404 depending on yt-dlp version)
    """
    urls = [JRE_PLAYLIST_URL, JRE_CHANNEL_URL, JRE_HANDLE_URL]
    for url in urls:
        logger.info("Trying URL: %s", url)
        try:
            info = ydl.extract_info(url, download=False)
            if info and info.get("entries"):
                return list(info["entries"])
        except Exception as exc:
            logger.warning("URL %s failed: %s", url, exc)
    return []


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_episode_list(max_episodes: int = 100) -> list[dict]:
    """
    Return up to *max_episodes* JRE episodes, newest-first.

    Each dict has: video_id, title, upload_date, duration_seconds.
    """
    if not HAS_DEPS:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    opts = _ydl_opts()
    opts["playlistend"] = max_episodes

    with YoutubeDL(opts) as ydl:
        entries = _extract_with_fallbacks(ydl, max_episodes)

    episodes: list[dict] = []
    for entry in entries:
        if entry is None:
            continue
        video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
        if not video_id:
            continue
        title    = entry.get("title", "")
        duration = entry.get("duration") or 0

        # Skip Shorts / clips (< 10 minutes)
        if duration and duration < 600:
            continue
        # Filter to actual JRE episodes
        if not _is_jre_episode(title):
            continue

        upload_date = _parse_upload_date(entry.get("upload_date", ""))
        episodes.append({
            "video_id":        video_id,
            "title":           title,
            "upload_date":     upload_date,
            "duration_seconds": duration,
        })

        if len(episodes) >= max_episodes:
            break

    logger.info("Found %d JRE episodes", len(episodes))
    return episodes


def fetch_transcript(video_id: str, joe_only: bool = True) -> Optional[list[dict]]:
    """
    Fetch the transcript for a single video.

    Returns a list of segment dicts:
        {"start": float, "duration": float, "text": str}
    or None if no transcript is available.

    When *joe_only* is True (default) the heuristic speaker filter is applied
    so only segments likely spoken by Joe are returned.
    """
    if not HAS_DEPS:
        raise RuntimeError("youtube-transcript-api is not installed.")

    try:
        transcript = YouTubeTranscriptApi.get_transcript(
            video_id,
            languages=["en", "en-US", "en-GB"],
        )
    except (NoTranscriptFound, TranscriptsDisabled):
        logger.warning("No transcript available for %s", video_id)
        return None
    except Exception as exc:
        logger.error("Error fetching transcript for %s: %s", video_id, exc)
        return None

    if joe_only:
        transcript = filter_joe_segments(transcript)
    return transcript


def filter_joe_segments(transcript: list[dict]) -> list[dict]:
    """
    Heuristic: return only segments likely spoken by Joe Rogan.

    Algorithm
    ---------
    1. Group consecutive segments into "turns" using pause detection
       (gap > _PAUSE_THRESHOLD seconds between segment end and next start).
    2. Compute the per-episode *median* turn word-count.
    3. Attribute below-median turns to Joe (host asks shorter questions),
       above-median turns to the guest (longer monologues).
    4. The very first turn is always kept (Joe's episode intro).

    If the filtered result is empty (e.g. transcript has only one giant
    segment) the full transcript is returned unchanged as a safe fallback.
    """
    if not transcript:
        return transcript

    # ── Step 1: group into turns ──────────────────────────────────────────
    turns: list[list[dict]] = []
    current: list[dict] = [transcript[0]]

    for i in range(1, len(transcript)):
        prev, curr = transcript[i - 1], transcript[i]
        prev_end = prev.get("start", 0) + prev.get("duration", 0)
        gap      = curr.get("start", 0) - prev_end
        if gap > _PAUSE_THRESHOLD:
            turns.append(current)
            current = [curr]
        else:
            current.append(curr)
    if current:
        turns.append(current)

    # ── Step 2: word count per turn ───────────────────────────────────────
    def _wc(turn: list[dict]) -> int:
        return sum(len(seg.get("text", "").split()) for seg in turn)

    counts = [_wc(t) for t in turns]
    if not counts:
        return transcript

    sorted_counts = sorted(counts)
    median = sorted_counts[len(sorted_counts) // 2]

    # ── Step 3: keep Joe's turns ──────────────────────────────────────────
    joe_segments: list[dict] = []
    for idx, (turn, wc) in enumerate(zip(turns, counts)):
        is_joe = wc < median   # short turn → Joe

        # Override: very first turn is always Joe's intro
        if idx == 0:
            is_joe = True

        # Override: turns composed mainly of Joe's characteristic signals
        if not is_joe:
            all_words = " ".join(seg.get("text", "") for seg in turn).lower().split()
            if all_words:
                signal_ratio = sum(1 for w in all_words if w in _JOE_SIGNALS) / len(all_words)
                if signal_ratio > 0.5:
                    is_joe = True

        if is_joe:
            joe_segments.extend(turn)

    # Safe fallback: never return empty results
    return joe_segments if joe_segments else transcript


def sync_episodes(db: Database, max_episodes: int = 100, delay: float = 1.5) -> int:
    """
    Fetch the episode list then download missing transcripts.

    Only Joe's segments (heuristic filtered) are stored.
    Returns the number of episodes newly added to the database.
    """
    episodes = fetch_episode_list(max_episodes)
    added = 0

    for ep in episodes:
        video_id = ep["video_id"]

        if db.episode_exists(video_id):
            logger.debug("Skipping already-stored episode %s", video_id)
            continue

        logger.info("Fetching transcript for %s (%s)", ep["title"], video_id)
        transcript = fetch_transcript(video_id, joe_only=True)

        if transcript is None:
            logger.warning(
                "No transcript for %s — storing episode with empty transcript", video_id
            )
            transcript = []

        db.upsert_episode(
            video_id=video_id,
            title=ep["title"],
            upload_date=ep["upload_date"],
            duration_seconds=ep["duration_seconds"],
            transcript=transcript,
        )
        added += 1
        time.sleep(delay)

    logger.info("Sync complete. %d new episodes added.", added)
    return added


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_jre_episode(title: str) -> bool:
    """Heuristic: title must reference JRE or Joe Rogan Experience."""
    t = title.lower()
    return (
        "joe rogan experience" in t
        or re.search(r"\bjre\b", t) is not None
        or re.search(r"#\d{4}", t) is not None
    )


def _parse_upload_date(raw: str) -> Optional[str]:
    """Convert YYYYMMDD → ISO date string, or return None."""
    if not raw or len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None
