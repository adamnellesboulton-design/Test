"""
Transcript text-file parser.

Parses uploaded .txt transcripts with the format produced by tools like
yt-dlp or manual export:

    Starting point is 00:04:46
    This is the argument that the carnivore people...

    Starting point is 00:05:04
    Well, that was the thing they would always say...

Each "Starting point is HH:MM:SS" line marks the beginning of a new segment.
All text between two such markers belongs to the first marker's timestamp.

Returns
-------
parse_transcript_txt(content) → (segments, duration_seconds)
    segments         : list of {"start": float, "text": str}
    duration_seconds : int  (last timestamp + small padding, rough estimate)
"""

import re
from typing import Optional

_TIMESTAMP_RE = re.compile(
    r"starting point is\s+(\d{1,2}):(\d{2}):(\d{2})",
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r"episode\s+date\s*:\s*([a-z]+)\s+(\d{1,2}),?\s*(\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def extract_episode_date(content: str) -> Optional[str]:
    """
    Scan the transcript content for a line like:
        Episode Date: February 5, 2026
    and return an ISO-format date string (YYYY-MM-DD), or None if not found.
    Only scans the first 4000 characters so it stays fast for large files.
    """
    m = _DATE_RE.search(content[:4000])
    if not m:
        return None
    month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
    month = _MONTHS.get(month_str.lower())
    if not month:
        return None
    try:
        return f"{int(year_str):04d}-{month:02d}-{int(day_str):02d}"
    except ValueError:
        return None


def parse_transcript_txt(content: str) -> tuple[list[dict], int]:
    """
    Parse a transcript .txt file.

    Returns (segments, duration_seconds).
    """
    segments: list[dict] = []
    current_start: Optional[float] = None
    current_lines: list[str] = []
    last_start: float = 0.0

    for line in content.splitlines():
        m = _TIMESTAMP_RE.match(line.strip())
        if m:
            # Flush previous segment
            if current_start is not None and current_lines:
                text = " ".join(current_lines).strip()
                if text:
                    segments.append({"start": current_start, "text": text})

            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            current_start = float(h * 3600 + mi * 60 + s)
            last_start = current_start
            current_lines = []
        elif current_start is not None:
            stripped = line.strip()
            if stripped:
                current_lines.append(stripped)

    # Flush last segment
    if current_start is not None and current_lines:
        text = " ".join(current_lines).strip()
        if text:
            segments.append({"start": current_start, "text": text})

    # Estimate duration as last timestamp + 60 s padding
    duration = int(last_start) + 60 if last_start else 0

    return segments, duration
