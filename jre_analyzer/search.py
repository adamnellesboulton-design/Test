"""
Keyword search layer.

Returns structured result objects that can be displayed in the CLI or passed
to the visualisation / fair-value modules.

Speaker-filter confidence intervals
-------------------------------------
Transcripts are pre-filtered so that only Joe's segments are stored (see
``fetch_transcripts.filter_joe_segments``).  The heuristic has an accuracy of
~65-70 % without audio diarisation.  We model this as:

    precision  P(actually Joe | classified Joe) ≈ 0.68
    recall     P(classified Joe | actually Joe) ≈ 0.68

For each observed count ``c`` we compute a 95 % confidence interval on the
**true Joe-only mention count** in two steps:

1. Garwood exact Poisson CI on ``c`` → ``[obs_lo, obs_hi]``.
   (Falls back to a Wald normal approximation if scipy is unavailable.)

2. Scale to the true count:
     true_lo = obs_lo × precision / recall   (conservative)
     true_hi = obs_hi / recall               (generous upper: full Poisson
                                              spread plus missed mentions)

The resulting ``[count_lo, count_hi]`` is exposed on every ``EpisodeResult``
and propagated to per-minute rates and rolling averages.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .analyzer import per_minute_rate, stem_word
from .database import Database

# ── Speaker-filter accuracy constants ────────────────────────────────────────
# Source: docstring in fetch_transcripts.filter_joe_segments()
#   "Accuracy is ~65-70 % without audio diarisation"
# We model precision ≈ recall ≈ midpoint of that range.
_SPEAKER_PRECISION = 0.68   # P(actually Joe  | classified as Joe)
_SPEAKER_RECALL    = 0.68   # P(classified Joe | actually Joe)

# Try to import scipy for the exact Garwood CI
try:
    from scipy.stats import chi2 as _chi2
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class EpisodeResult:
    video_id: str
    title: str
    upload_date: Optional[str]
    episode_number: Optional[int]
    duration_seconds: int
    count: int          # keyword mentions in Joe-attributed segments (observed)
    per_minute: float   # observed mentions per minute

    # 95 % CI on true Joe-only mention count (accounting for filter FP/FN)
    count_lo: float = 0.0
    count_hi: float = 0.0

    # CI on per-minute rate (same bounds scaled by duration)
    per_minute_lo: float = 0.0
    per_minute_hi: float = 0.0


@dataclass
class MinuteResult:
    minute: int
    count: int


@dataclass
class SearchResult:
    keyword: str
    episodes: list[EpisodeResult] = field(default_factory=list)

    # Rolling averages of raw mention count (None if not enough data)
    avg_last_1:   Optional[float] = None
    avg_last_5:   Optional[float] = None
    avg_last_20:  Optional[float] = None
    avg_last_50:  Optional[float] = None
    avg_last_100: Optional[float] = None

    # Rolling averages of per-minute mention rate (duration-normalized)
    avg_pm_last_1:   Optional[float] = None
    avg_pm_last_5:   Optional[float] = None
    avg_pm_last_20:  Optional[float] = None
    avg_pm_last_50:  Optional[float] = None
    avg_pm_last_100: Optional[float] = None

    # 95 % CI bounds on each rolling average (same speaker-filter correction)
    avg_last_1_lo:    Optional[float] = None
    avg_last_1_hi:    Optional[float] = None
    avg_last_5_lo:    Optional[float] = None
    avg_last_5_hi:    Optional[float] = None
    avg_last_20_lo:   Optional[float] = None
    avg_last_20_hi:   Optional[float] = None
    avg_last_50_lo:   Optional[float] = None
    avg_last_50_hi:   Optional[float] = None
    avg_last_100_lo:  Optional[float] = None
    avg_last_100_hi:  Optional[float] = None

    def episode_by_id(self, video_id: str) -> Optional[EpisodeResult]:
        for ep in self.episodes:
            if ep.video_id == video_id:
                return ep
        return None


# ── Public search function ────────────────────────────────────────────────────

def search(db: Database, keyword: str) -> SearchResult:
    """
    Search for `keyword` across all indexed episodes.

    The keyword is stemmed with the same algorithm used at index time, so
    "drugs", "drug", and "drugged" all resolve to the same DB key.
    Populates rolling averages and speaker-filter confidence intervals.
    """
    keyword = stem_word(keyword.strip().lower())
    raw_rows = db.search_word_by_episode(keyword)

    episodes: list[EpisodeResult] = []
    for row in raw_rows:
        dur   = row["duration_seconds"] or 0
        count = row["count"] or 0
        lo, hi = _count_ci(count)
        episodes.append(
            EpisodeResult(
                video_id=row["video_id"],
                title=row["title"],
                upload_date=row["upload_date"],
                episode_number=row["episode_number"],
                duration_seconds=dur,
                count=count,
                per_minute=per_minute_rate(count, dur),
                count_lo=lo,
                count_hi=hi,
                per_minute_lo=per_minute_rate(lo, dur),
                per_minute_hi=per_minute_rate(hi, dur),
            )
        )

    result = SearchResult(keyword=keyword, episodes=episodes)
    _compute_averages(result)
    return result


# ── CI helper ─────────────────────────────────────────────────────────────────

def _count_ci(c: int) -> tuple[float, float]:
    """
    95 % CI on the true Joe-only mention count given observed count *c*.

    Step 1 – Garwood exact Poisson CI on the observed count (or Wald fallback).
    Step 2 – Scale to the true count using the speaker-filter parameters:
        true_lo = obs_lo × precision / recall
        true_hi = obs_hi / recall
    """
    # ── Garwood CI (exact for Poisson) ───────────────────────────────────────
    if _HAS_SCIPY:
        if c == 0:
            obs_lo, obs_hi = 0.0, float(_chi2.ppf(0.975, 2) / 2)
        else:
            obs_lo = float(_chi2.ppf(0.025, 2 * c) / 2)
            obs_hi = float(_chi2.ppf(0.975, 2 * c + 2) / 2)
    else:
        # Wald normal approximation (adequate for c ≥ 5)
        half   = 1.96 * math.sqrt(max(c, 1))
        obs_lo = max(0.0, c - half)
        obs_hi = c + half

    # ── Scale by speaker-filter precision and recall ──────────────────────────
    true_lo = obs_lo * _SPEAKER_PRECISION / _SPEAKER_RECALL
    true_hi = obs_hi / _SPEAKER_RECALL
    return true_lo, true_hi


# ── Average helpers ───────────────────────────────────────────────────────────

def _rolling_avg(episodes: list[EpisodeResult], n: int) -> Optional[float]:
    subset = episodes[:n]
    if not subset:
        return None
    return sum(ep.count for ep in subset) / len(subset)


def _rolling_avg_ci(
    episodes: list[EpisodeResult], n: int
) -> tuple[Optional[float], Optional[float]]:
    """
    95 % CI on the rolling mean of true Joe-only counts over the last *n*
    episodes.  Uses the mean of the per-episode CI bounds (conservative but
    straightforward — assumes independence across episodes).
    """
    subset = episodes[:n]
    if not subset:
        return None, None
    lo = sum(ep.count_lo for ep in subset) / len(subset)
    hi = sum(ep.count_hi for ep in subset) / len(subset)
    return lo, hi


def _rolling_avg_pm(episodes: list[EpisodeResult], n: int) -> Optional[float]:
    """Average per-minute mention rate, skipping episodes with unknown duration."""
    subset = [ep for ep in episodes[:n] if ep.duration_seconds > 0]
    if not subset:
        return None
    return sum(ep.per_minute for ep in subset) / len(subset)


def _compute_averages(result: SearchResult) -> None:
    eps = result.episodes  # already newest-first

    for attr, n in [
        ("avg_last_1",   1),
        ("avg_last_5",   5),
        ("avg_last_20",  20),
        ("avg_last_50",  50),
        ("avg_last_100", 100),
    ]:
        setattr(result, attr, _rolling_avg(eps, n))
        lo, hi = _rolling_avg_ci(eps, n)
        setattr(result, f"{attr}_lo", lo)
        setattr(result, f"{attr}_hi", hi)

    result.avg_pm_last_1   = _rolling_avg_pm(eps, 1)
    result.avg_pm_last_5   = _rolling_avg_pm(eps, 5)
    result.avg_pm_last_20  = _rolling_avg_pm(eps, 20)
    result.avg_pm_last_50  = _rolling_avg_pm(eps, 50)
    result.avg_pm_last_100 = _rolling_avg_pm(eps, 100)


# ── Minute breakdown ──────────────────────────────────────────────────────────

def get_minute_breakdown(db: Database, keyword: str, video_id: str) -> list[MinuteResult]:
    """Return per-minute counts for a keyword within a specific episode."""
    keyword = stem_word(keyword.strip().lower())
    rows = db.search_word_by_minute(keyword, video_id)
    return [MinuteResult(minute=r["minute"], count=r["count"]) for r in rows]
