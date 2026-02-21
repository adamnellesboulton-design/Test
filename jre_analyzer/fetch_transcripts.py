"""
Fetch JRE episode list and transcripts from YouTube.

Episodes are fetched from the PowerfulJRE channel. For each episode we
retrieve the auto-generated (or manual) English transcript with timestamps
via youtube-transcript-api.  The raw entries are stored in the database as-is
so that per-minute analysis can be performed later.
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

# PowerfulJRE channel – the main upload destination for full episodes
JRE_CHANNEL_URL = "https://www.youtube.com/@PowerfulJRE/videos"
# Fallback search query when channel scraping is needed
JRE_SEARCH_QUERY = "Joe Rogan Experience"


def _ydl_opts(quiet: bool = True) -> dict:
    return {
        "quiet": quiet,
        "no_warnings": quiet,
        "extract_flat": True,          # don't download, just list
        "ignoreerrors": True,
        "skip_download": True,
    }


def fetch_episode_list(max_episodes: int = 100) -> list[dict]:
    """
    Return a list of dicts with keys: video_id, title, upload_date, duration_seconds.
    Episodes are returned newest-first.
    """
    if not HAS_DEPS:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")

    episodes = []
    opts = _ydl_opts()
    opts["playlistend"] = max_episodes

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(JRE_CHANNEL_URL, download=False)
        if info is None:
            raise RuntimeError(f"Could not fetch channel info from {JRE_CHANNEL_URL}")

        entries = info.get("entries") or []
        for entry in entries:
            if entry is None:
                continue
            video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
            title = entry.get("title", "")
            # Skip shorts / clips (duration is a hint; title matching is a fallback)
            duration = entry.get("duration") or 0
            if duration and duration < 600:   # less than 10 minutes → skip
                continue
            # Only keep episodes that look like JRE episodes
            if not _is_jre_episode(title):
                continue

            upload_date_raw = entry.get("upload_date", "")  # YYYYMMDD
            upload_date = _parse_upload_date(upload_date_raw)

            episodes.append({
                "video_id": video_id,
                "title": title,
                "upload_date": upload_date,
                "duration_seconds": duration,
            })

            if len(episodes) >= max_episodes:
                break

    logger.info("Found %d JRE episodes from channel", len(episodes))
    return episodes


def _is_jre_episode(title: str) -> bool:
    """Heuristic: title must reference JRE or Joe Rogan Experience."""
    t = title.lower()
    return (
        "joe rogan experience" in t
        or re.search(r"\bjre\b", t) is not None
        or re.search(r"#\d{4}", t) is not None   # episode number like #2100
    )


def _parse_upload_date(raw: str) -> Optional[str]:
    """Convert YYYYMMDD → ISO date string, or return None."""
    if not raw or len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_transcript(video_id: str) -> Optional[list[dict]]:
    """
    Fetch the transcript for a single video.

    Returns a list of segment dicts:
        {"start": float, "duration": float, "text": str}
    or None if no transcript is available.

    We filter segments to Joe's speech only (we cannot cleanly separate
    speakers automatically, so we return the full transcript and mark it as
    unfiltered — the caller can apply guest-filtering heuristics if desired).
    """
    if not HAS_DEPS:
        raise RuntimeError("youtube-transcript-api is not installed.")

    try:
        transcript = YouTubeTranscriptApi.get_transcript(
            video_id,
            languages=["en", "en-US", "en-GB"],
        )
        return transcript
    except (NoTranscriptFound, TranscriptsDisabled):
        logger.warning("No transcript available for %s", video_id)
        return None
    except Exception as exc:  # pragma: no cover
        logger.error("Error fetching transcript for %s: %s", video_id, exc)
        return None


def sync_episodes(db: Database, max_episodes: int = 100, delay: float = 1.5) -> int:
    """
    High-level function: fetch episode list, then download missing transcripts.

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
        transcript = fetch_transcript(video_id)

        if transcript is None:
            logger.warning("No transcript for %s — storing episode with empty transcript", video_id)
            transcript = []

        db.upsert_episode(
            video_id=video_id,
            title=ep["title"],
            upload_date=ep["upload_date"],
            duration_seconds=ep["duration_seconds"],
            transcript=transcript,
        )
        added += 1
        time.sleep(delay)   # be polite to YouTube

    logger.info("Sync complete. %d new episodes added.", added)
    return added
