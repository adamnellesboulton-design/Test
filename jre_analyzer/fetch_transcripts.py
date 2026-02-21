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
