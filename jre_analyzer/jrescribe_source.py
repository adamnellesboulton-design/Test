"""
Fetch JRE transcripts from the jrescribe-transcripts GitHub repository.

https://github.com/achendrick/jrescribe-transcripts

Used as a fallback when the YouTube Transcript API is rate-limited or
unavailable.  Episodes are stored as Markdown files with ``<timemark />``
timestamp tags and plain-text dialogue between them.

The format between consecutive ``<timemark seconds="N" />`` tags is::

    <timemark seconds="0" />
    Dialogue text paragraph

    <timemark seconds="60" />
    More dialogue text
    ...

File naming conventions tried in order (newest → oldest):
  {episode_number}.md
  p{episode_number}.md
  jre{episode_number}.md
"""

from __future__ import annotations

import re
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_RAW_BASE = (
    "https://raw.githubusercontent.com/achendrick/jrescribe-transcripts/master"
)
_TIMEOUT = 20

# Naming conventions to try, newest first
_FILENAME_PATTERNS = ["{n}.md", "p{n}.md", "jre{n}.md"]

# <timemark seconds="123" /> or <timemark seconds='123'/>
_TIMEMARK_RE = re.compile(
    r"<timemark\s+seconds=[\"']?(\d+)[\"']?\s*/?>", re.IGNORECASE
)

# Strip any remaining HTML / Vue component tags
_TAG_RE = re.compile(r"<[^>]+>")

# Vue interpolation {{ ... }}
_VUE_RE = re.compile(r"\{\{[^}]*\}\}")


# ── Public API ────────────────────────────────────────────────────────────────


def fetch_transcript_jrescribe(episode_number: int) -> Optional[list[dict]]:
    """
    Fetch a JRE transcript from the jrescribe-transcripts GitHub repo.

    Tries several file-naming conventions in order of likelihood and returns
    the first successful result.

    Returns a list of segment dicts compatible with the youtube-transcript-api
    format::

        [{"start": float, "duration": float, "text": str}, ...]

    Returns ``None`` if the episode is not available in the repo.
    """
    for pattern in _FILENAME_PATTERNS:
        filename = pattern.format(n=episode_number)
        url = f"{_RAW_BASE}/{filename}"
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            if resp.status_code != 200:
                continue

            segments = _parse_transcript(resp.text)
            if segments:
                logger.info(
                    "jrescribe: loaded %d segments for JRE #%d from %s",
                    len(segments),
                    episode_number,
                    filename,
                )
                return segments

            # File exists but contains only metadata (transcription: false)
            logger.debug(
                "jrescribe: %s exists but has no transcript segments", filename
            )
            return None

        except requests.RequestException as exc:
            logger.debug("jrescribe: request failed for %s: %s", url, exc)

    logger.debug("jrescribe: no transcript found for JRE #%d", episode_number)
    return None


# ── Parsing ───────────────────────────────────────────────────────────────────


def _parse_transcript(content: str) -> list[dict]:
    """
    Convert a jrescribe Markdown file into a list of segment dicts.

    ``re.split()`` on a capturing group alternates between text and capture::

        [pre_text, sec1, text1, sec2, text2, ...]

    We zip adjacent ``(sec, text)`` pairs and compute duration as the gap to
    the following timemark (defaulting to 60 s for the last segment).
    """
    parts = _TIMEMARK_RE.split(content)
    # parts[0] = frontmatter / header (skip)
    # parts[1::2] = seconds strings
    # parts[2::2] = text blocks

    segments: list[dict] = []

    i = 1  # index of the first seconds-value
    while i + 1 < len(parts):
        seconds_str = parts[i].strip()
        text_block = parts[i + 1]

        try:
            start = float(seconds_str)
        except ValueError:
            i += 2
            continue

        # Duration = gap to the next timemark, or a 60-second default
        if i + 2 < len(parts):
            try:
                duration = max(1.0, float(parts[i + 2].strip()) - start)
            except (ValueError, IndexError):
                duration = 60.0
        else:
            duration = 60.0

        text = _clean_text(text_block)
        if text:
            segments.append(
                {"start": start, "duration": duration, "text": text}
            )

        i += 2

    return segments


def _clean_text(raw: str) -> str:
    """
    Strip HTML/Vue tags, Vue interpolation, and excess whitespace from a
    raw text block.  Returns plain dialogue text, or ``''`` if nothing remains.
    """
    text = _TAG_RE.sub(" ", raw)   # remove <tags>
    text = _VUE_RE.sub(" ", text)  # remove {{ vue }}
    text = " ".join(text.split())  # collapse whitespace
    return text.strip()
