"""
Keyword search layer.

Returns structured result objects that can be displayed in the CLI or passed
to the visualisation / fair-value modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .analyzer import per_minute_rate
from .database import Database


@dataclass
class EpisodeResult:
    video_id: str
    title: str
    upload_date: Optional[str]
    episode_number: Optional[int]
    duration_seconds: int
    count: int                      # total keyword mentions in episode
    per_minute: float               # mentions per minute


@dataclass
class MinuteResult:
    minute: int
    count: int


@dataclass
class SearchResult:
    keyword: str
    episodes: list[EpisodeResult] = field(default_factory=list)

    # Rolling averages (None if not enough data)
    avg_last_1:   Optional[float] = None
    avg_last_5:   Optional[float] = None
    avg_last_20:  Optional[float] = None
    avg_last_50:  Optional[float] = None
    avg_last_100: Optional[float] = None

    def episode_by_id(self, video_id: str) -> Optional[EpisodeResult]:
        for ep in self.episodes:
            if ep.video_id == video_id:
                return ep
        return None


def search(db: Database, keyword: str) -> SearchResult:
    """
    Search for `keyword` across all indexed episodes.
    Populates rolling averages over the N most-recent episodes.
    """
    keyword = keyword.strip().lower()
    raw_rows = db.search_word_by_episode(keyword)

    episodes: list[EpisodeResult] = []
    for row in raw_rows:
        dur = row["duration_seconds"] or 0
        count = row["count"] or 0
        episodes.append(
            EpisodeResult(
                video_id=row["video_id"],
                title=row["title"],
                upload_date=row["upload_date"],
                episode_number=row["episode_number"],
                duration_seconds=dur,
                count=count,
                per_minute=per_minute_rate(count, dur),
            )
        )

    result = SearchResult(keyword=keyword, episodes=episodes)
    _compute_averages(result)
    return result


def _rolling_avg(episodes: list[EpisodeResult], n: int) -> Optional[float]:
    subset = episodes[:n]
    if not subset:
        return None
    return sum(ep.count for ep in subset) / len(subset)


def _compute_averages(result: SearchResult) -> None:
    eps = result.episodes  # already newest-first
    result.avg_last_1   = _rolling_avg(eps, 1)
    result.avg_last_5   = _rolling_avg(eps, 5)
    result.avg_last_20  = _rolling_avg(eps, 20)
    result.avg_last_50  = _rolling_avg(eps, 50)
    result.avg_last_100 = _rolling_avg(eps, 100)


def get_minute_breakdown(db: Database, keyword: str, video_id: str) -> list[MinuteResult]:
    """Return per-minute counts for a keyword within a specific episode."""
    keyword = keyword.strip().lower()
    rows = db.search_word_by_minute(keyword, video_id)
    return [MinuteResult(minute=r["minute"], count=r["count"]) for r in rows]
