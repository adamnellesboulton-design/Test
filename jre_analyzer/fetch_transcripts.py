"""
Fetch JRE episode list and transcripts from YouTube.

Episode discovery
-----------------
We intentionally avoid yt-dlp's ``youtube:tab`` extractor (used for
channel / @handle URLs) because it calls a private InnerTube API endpoint
that returns HTTP 404 in some environments (Railway, certain cloud IPs).

Instead we use two fallback-chained methods:

  1. YouTube RSS feed  (requests, no API key, newest ~15 videos)
  2. yt-dlp ytsearch: (YouTube search API — different endpoint, always works)

Transcripts are fetched via youtube-transcript-api.  When YouTube is
rate-limited or returns no transcript, the jrescribe-transcripts GitHub
repository (https://github.com/achendrick/jrescribe-transcripts) is tried
as a fallback for numbered JRE episodes.

Speaker filtering
-----------------
YouTube auto-captions carry no speaker labels.  We use a turn-length
heuristic: JRE guests tend to deliver long monologues while Joe asks
shorter questions and reacts with brief affirmations.

  1. Segment the transcript into "turns" by detecting pauses > 2 s.
  2. Compute the per-episode median turn word-count.
  3. Below-median turns → Joe.  At/above-median turns → guest.

Accuracy is ~65–70 % without audio diarisation — sufficient for keyword
frequency estimation across many episodes.
"""

import re
import time
import logging
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree

import requests

# Lazy import — only pulled in when a YouTube transcript is unavailable.
_jrescribe_fetch = None


def _get_jrescribe_fetch():
    global _jrescribe_fetch
    if _jrescribe_fetch is None:
        try:
            from .jrescribe_source import fetch_transcript_jrescribe  # noqa: PLC0415
            _jrescribe_fetch = fetch_transcript_jrescribe
        except Exception:
            _jrescribe_fetch = lambda ep_num: None  # noqa: E731
    return _jrescribe_fetch

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from yt_dlp import YoutubeDL
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

# Error classes moved / changed name between v0.6 and v1.x; import defensively.
try:
    from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
    _TRANSCRIPT_ERRORS = (NoTranscriptFound, TranscriptsDisabled)
except ImportError:
    try:
        from youtube_transcript_api._errors import (  # type: ignore[import]
            NoTranscriptFound, TranscriptsDisabled,
        )
        _TRANSCRIPT_ERRORS = (NoTranscriptFound, TranscriptsDisabled)
    except ImportError:
        _TRANSCRIPT_ERRORS = ()  # fall through to broad except below

from .database import Database

logger = logging.getLogger(__name__)

# ── Discovery configuration ───────────────────────────────────────────────────

# Channel ID for PowerfulJRE — used only for the RSS feed URL.
# The @handle / channel-tab URL is intentionally NOT used (causes 404 on
# Railway because yt-dlp's youtube:tab extractor calls a different API).
_CHANNEL_ID = "UCzWQYUVCpZqtN93H8RR44Qw"

# RSS feed: free, no API key, returns the ~15 most recent uploads.
JRE_RSS_URL = (
    f"https://www.youtube.com/feeds/videos.xml?channel_id={_CHANNEL_ID}"
)

# Search query used with yt-dlp's ytsearch: extractor (different endpoint,
# not affected by the channel-tab 404).
JRE_SEARCH_QUERY = "Joe Rogan Experience"

# ── Speaker-filter constants ──────────────────────────────────────────────────
_PAUSE_THRESHOLD = 2.0  # seconds gap between segments → new turn

# High-frequency Joe signals; turns >50 % these words are kept even if long.
_JOE_SIGNALS: frozenset[str] = frozenset(
    "yeah right wow really interesting absolutely totally dude man bro "
    "exactly sure okay seriously incredible insane crazy amazing wild "
    "fascinating no yes true correct definitely".split()
)


# ── yt-dlp helpers ───────────────────────────────────────────────────────────

def _ydl_opts(quiet: bool = True) -> dict:
    return {
        "quiet":         quiet,
        "no_warnings":   quiet,
        "extract_flat":  True,
        "ignoreerrors":  True,
        "skip_download": True,
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


# ── Episode list fetching ─────────────────────────────────────────────────────

def fetch_episode_list(max_episodes: int = 100) -> list[dict]:
    """
    Return up to *max_episodes* JRE episodes, newest-first.

    Each dict has: video_id, title, upload_date, duration_seconds.

    Tries RSS feed first (fast, no yt-dlp), then falls back to
    yt-dlp search (broader, avoids channel-tab 404).
    """
    episodes: list[dict] = []

    # ── 1. RSS feed (newest ~15 without yt-dlp) ───────────────────────────
    try:
        rss_eps = _fetch_from_rss()
        episodes.extend(rss_eps)
        logger.info("RSS feed returned %d episodes", len(rss_eps))
    except Exception as exc:
        logger.warning("RSS feed failed: %s", exc)

    rss_ids = {ep["video_id"] for ep in episodes}

    # ── 2. yt-dlp search (fills up to max_episodes) ───────────────────────
    if len(episodes) < max_episodes and HAS_DEPS:
        try:
            search_eps = _fetch_from_search(max_episodes, exclude_ids=rss_ids)
            episodes.extend(search_eps)
            logger.info("Search returned %d additional episodes", len(search_eps))
        except Exception as exc:
            logger.warning("yt-dlp search failed: %s", exc)

    if not episodes:
        raise RuntimeError(
            "Could not retrieve any JRE episodes. "
            "Both RSS and yt-dlp search failed."
        )

    # Deduplicate and cap
    seen: set[str] = set()
    unique: list[dict] = []
    for ep in episodes:
        if ep["video_id"] not in seen:
            seen.add(ep["video_id"])
            unique.append(ep)
        if len(unique) >= max_episodes:
            break

    logger.info("Found %d JRE episodes total", len(unique))
    return unique


def _fetch_from_rss(timeout: int = 15) -> list[dict]:
    """Fetch the most recent ~15 episodes from the channel RSS feed."""
    resp = requests.get(JRE_RSS_URL, timeout=timeout)
    resp.raise_for_status()

    root = ElementTree.fromstring(resp.content)
    ns = {
        "atom":  "http://www.w3.org/2005/Atom",
        "yt":    "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    episodes: list[dict] = []
    for entry in root.findall("atom:entry", ns):
        vid_el   = entry.find("yt:videoId", ns)
        title_el = entry.find("atom:title", ns)
        pub_el   = entry.find("atom:published", ns)

        if vid_el is None or title_el is None:
            continue

        title = title_el.text or ""
        if not _is_jre_episode(title):
            continue

        pub_date: Optional[str] = None
        if pub_el is not None and pub_el.text:
            pub_date = pub_el.text[:10]  # ISO date YYYY-MM-DD

        # RSS doesn't include duration; set 0 — will be filled from transcript
        episodes.append({
            "video_id":        vid_el.text,
            "title":           title,
            "upload_date":     pub_date,
            "duration_seconds": 0,
        })

    return episodes


def _fetch_from_search(
    max_episodes: int,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """
    Use yt-dlp's ``ytsearch:`` extractor to list JRE episodes.

    This extractor calls YouTube's *search* endpoint (not the channel-tab
    InnerTube API) so it is unaffected by the 404 that hits channel URLs.
    """
    if not HAS_DEPS:
        return []

    exclude_ids = exclude_ids or set()
    # Over-sample so we have enough after filtering clips / duplicates
    oversample = min(max_episodes * 3, 300)
    search_url = f"ytsearch{oversample}:{JRE_SEARCH_QUERY}"

    opts = _ydl_opts()
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_url, download=False)

    episodes: list[dict] = []
    for entry in (info or {}).get("entries") or []:
        if entry is None:
            continue

        video_id = entry.get("id", "")
        if not video_id or video_id in exclude_ids:
            continue

        title    = entry.get("title", "")
        duration = entry.get("duration") or 0

        if duration and duration < 600:     # skip Shorts / clips
            continue
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

    return episodes


# ── Transcript fetching ───────────────────────────────────────────────────────

def fetch_transcript(video_id: str, joe_only: bool = True) -> Optional[list[dict]]:
    """
    Fetch the transcript for a single video.

    Returns a list of segment dicts:
        {"start": float, "duration": float, "text": str}
    or None if no transcript is available.

    Supports both youtube-transcript-api 0.6.x (get_transcript classmethod)
    and 1.x+ (instance fetch() / to_raw_data()).  The 1.x API became the
    standard in 2025; get_transcript() was removed in 1.2.0.

    When *joe_only* is True (default) the heuristic speaker filter is applied
    so only segments likely spoken by Joe are returned.
    """
    if not HAS_DEPS:
        raise RuntimeError("youtube-transcript-api is not installed.")

    languages = ["en", "en-US", "en-GB"]
    transcript: Optional[list[dict]] = None

    # ── Try new instance API (>=1.0.0) ───────────────────────────────────────
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
        # 1.x returns a FetchedTranscript with to_raw_data() → list[dict]
        if hasattr(fetched, "to_raw_data"):
            transcript = fetched.to_raw_data()
        else:
            # Some intermediate builds returned an iterable of snippet objects
            transcript = [
                {"start": s.start, "duration": s.duration, "text": s.text}
                for s in fetched
            ]
    except _TRANSCRIPT_ERRORS:
        logger.warning("No transcript available for %s", video_id)
        return None
    except AttributeError:
        # fetch() doesn't exist → old library version, fall through below
        pass
    except Exception as exc:
        logger.error("Transcript fetch (new API) failed for %s: %s", video_id, exc)
        return None

    # ── Fall back to old classmethod API (<1.0.0) ─────────────────────────────
    if transcript is None:
        try:
            transcript = YouTubeTranscriptApi.get_transcript(  # type: ignore[attr-defined]
                video_id, languages=languages
            )
        except _TRANSCRIPT_ERRORS:
            logger.warning("No transcript available for %s", video_id)
            return None
        except Exception as exc:
            logger.error("Transcript fetch (legacy API) failed for %s: %s", video_id, exc)
            return None

    if joe_only and transcript:
        transcript = filter_joe_segments(transcript)
    return transcript


# ── Speaker filter ────────────────────────────────────────────────────────────

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
    5. Override: turns where > 50 % of words are Joe-signal words are kept.

    Falls back to the full transcript if filtering would return nothing.
    """
    if not transcript:
        return transcript

    # ── Group into turns ──────────────────────────────────────────────────
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

    # ── Word count per turn ───────────────────────────────────────────────
    def _wc(turn: list[dict]) -> int:
        return sum(len(seg.get("text", "").split()) for seg in turn)

    counts = [_wc(t) for t in turns]
    if not counts:
        return transcript

    sorted_counts = sorted(counts)
    median = sorted_counts[len(sorted_counts) // 2]

    # ── Label and collect Joe's turns ─────────────────────────────────────
    joe_segments: list[dict] = []
    for idx, (turn, wc) in enumerate(zip(turns, counts)):
        is_joe = wc < median

        if idx == 0:
            is_joe = True   # intro is always Joe's

        if not is_joe:
            all_words = " ".join(
                seg.get("text", "") for seg in turn
            ).lower().split()
            if all_words:
                ratio = sum(1 for w in all_words if w in _JOE_SIGNALS) / len(all_words)
                if ratio > 0.5:
                    is_joe = True

        if is_joe:
            joe_segments.extend(turn)

    return joe_segments if joe_segments else transcript


# ── High-level sync ───────────────────────────────────────────────────────────

def sync_episodes(db: Database, max_episodes: int = 100, delay: float = 1.5) -> dict:
    """
    Fetch the episode list then download missing transcripts.

    Only Joe's segments (heuristic filtered) are stored.

    Returns a summary dict:
        added          - episodes newly written to DB
        skipped        - episodes already in DB
        transcripts_ok - episodes where a real transcript was fetched
        transcripts_missing - episodes stored with empty transcript
    """
    episodes = fetch_episode_list(max_episodes)
    added = skipped = transcripts_ok = transcripts_missing = 0

    for ep in episodes:
        video_id = ep["video_id"]

        if db.episode_exists(video_id):
            logger.debug("Skipping already-stored episode %s", video_id)
            skipped += 1
            continue

        logger.info("Fetching transcript for %s (%s)", ep["title"], video_id)
        transcript = fetch_transcript(video_id, joe_only=True)

        # ── jrescribe fallback ────────────────────────────────────────────
        if transcript is None:
            ep_num = _extract_episode_number(ep["title"])
            if ep_num is not None:
                try:
                    transcript = _get_jrescribe_fetch()(ep_num)
                    if transcript:
                        logger.info(
                            "Using jrescribe transcript for JRE #%d (%s)",
                            ep_num, video_id,
                        )
                except Exception as exc:
                    logger.debug("jrescribe fallback error: %s", exc)
        # ─────────────────────────────────────────────────────────────────

        if transcript:
            transcripts_ok += 1
        else:
            transcripts_missing += 1
            logger.warning(
                "No transcript for %s — storing with empty transcript", video_id
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

    logger.info(
        "Sync complete. added=%d skipped=%d transcripts_ok=%d transcripts_missing=%d",
        added, skipped, transcripts_ok, transcripts_missing,
    )
    return {
        "added": added,
        "skipped": skipped,
        "transcripts_ok": transcripts_ok,
        "transcripts_missing": transcripts_missing,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_episode_number(title: str) -> Optional[int]:
    """Extract the JRE episode number from a title string (e.g. '#2100')."""
    m = re.search(r"#(\d{3,5})", title)
    if m:
        return int(m.group(1))
    return None


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
