"""
Polymarket fair-value calculator for JRE keyword mention markets.

Approach
--------
We model the number of keyword mentions in an episode as a Poisson random
variable with rate λ estimated from the historical data.

  λ  =  mean mentions per episode over the chosen lookback window

The Polymarket market typically resolves as:
  "Will keyword X be mentioned AT LEAST N times?"  → YES/NO

So for each bucket n ∈ {0, 1, 2, …, 10} we compute:
  P(mentions == n)   using Poisson PMF
  P(mentions >= n)   using Poisson survival function (for ≥ markets)

We also provide a negative-binomial fit as an alternative when the data is
overdispersed (variance > mean), which is common for names/topics that are
bursty.

Additionally, an "empirical" distribution is returned: the fraction of the
lookback episodes where the count fell in each bucket.  This is the most
robust estimate when the distribution is non-standard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

try:
    import numpy as np
    from scipy import stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from .search import SearchResult


MAX_BUCKET = 10   # counts above this are grouped into a "10+" bucket


@dataclass
class FairValueResult:
    keyword: str
    lambda_estimate: float          # Poisson λ (mean mentions/episode, normalized)
    lookback_episodes: int
    mean: float
    variance: float
    overdispersed: bool             # True → negative-binomial may be better
    # Median episode duration (minutes) used as normalization reference.
    # None means raw counts were used (no duration data available).
    reference_minutes: Optional[float] = None

    # P(mentions == n) for n = 0..MAX_BUCKET, key MAX_BUCKET means "≥ MAX_BUCKET"
    poisson_pmf:    dict[int, float]
    # P(mentions >= n)
    poisson_sf:     dict[int, float]
    # Empirical fraction of episodes with exactly n mentions (n=MAX_BUCKET means ≥MAX_BUCKET)
    empirical_pmf:  dict[int, float]
    # Empirical P(mentions >= n)
    empirical_sf:   dict[int, float]
    # Negative-binomial PMF (if scipy available and data is overdispersed)
    negbin_pmf:     Optional[dict[int, float]]
    negbin_sf:      Optional[dict[int, float]]


def calculate_fair_value(
    result: SearchResult,
    lookback: int = 20,
) -> FairValueResult:
    """
    Calculate Polymarket fair-value probabilities for the next JRE episode.

    Parameters
    ----------
    result   : SearchResult from search.search()
    lookback : Number of most-recent episodes to use for estimation (default 20)
    """
    # Use the N most-recent episodes (result.episodes is newest-first)
    all_eps = result.episodes[:lookback]

    # ── Normalize by episode duration ────────────────────────────────────────
    # Use per-minute mention rate × median episode duration as the effective
    # count, so short and long episodes are treated on equal footing.
    eps_with_dur = [ep for ep in all_eps if ep.duration_seconds > 0]
    ref_minutes: Optional[float] = None

    if eps_with_dur:
        dur_sorted = sorted(ep.duration_seconds for ep in eps_with_dur)
        ref_minutes = dur_sorted[len(dur_sorted) // 2] / 60.0
        # Effective counts: what you'd expect in a reference-length episode
        eff_counts = [ep.per_minute * ref_minutes for ep in eps_with_dur]
        # Empirical PMF needs integer buckets — round to nearest whole mention
        int_counts = [max(0, round(c)) for c in eff_counts]
        n = len(eff_counts)
    else:
        # No duration data — fall back to raw counts
        eff_counts = [float(ep.count) for ep in all_eps]
        int_counts = [ep.count for ep in all_eps]
        n = len(eff_counts)

    if n == 0:
        # No data — return uniform over 0..MAX_BUCKET
        uniform = 1.0 / (MAX_BUCKET + 1)
        uniform_pmf = {i: uniform for i in range(MAX_BUCKET + 1)}
        uniform_sf  = {i: 1.0 - sum(uniform_pmf[j] for j in range(i)) for i in range(MAX_BUCKET + 1)}
        return FairValueResult(
            keyword=result.keyword,
            lambda_estimate=0.0,
            lookback_episodes=0,
            mean=0.0, variance=0.0, overdispersed=False,
            reference_minutes=ref_minutes,
            poisson_pmf=uniform_pmf, poisson_sf=uniform_sf,
            empirical_pmf=uniform_pmf, empirical_sf=uniform_sf,
            negbin_pmf=None, negbin_sf=None,
        )

    mean = sum(eff_counts) / n
    variance = sum((c - mean) ** 2 for c in eff_counts) / max(n - 1, 1)
    lam = mean if mean > 0 else 1e-9
    overdispersed = variance > mean * 1.2   # 20% tolerance

    poisson_pmf = _poisson_pmf_dict(lam)
    poisson_sf  = _sf_from_pmf(poisson_pmf)
    empirical_pmf = _empirical_pmf(int_counts, n)
    empirical_sf  = _sf_from_pmf(empirical_pmf)

    negbin_pmf: Optional[dict[int, float]] = None
    negbin_sf:  Optional[dict[int, float]] = None
    if HAS_SCIPY and overdispersed and variance > 0 and mean > 0:
        negbin_pmf = _negbin_pmf_dict(mean, variance)
        if negbin_pmf:
            negbin_sf = _sf_from_pmf(negbin_pmf)

    return FairValueResult(
        keyword=result.keyword,
        lambda_estimate=lam,
        lookback_episodes=n,
        mean=mean,
        variance=variance,
        overdispersed=overdispersed,
        reference_minutes=ref_minutes,
        poisson_pmf=poisson_pmf,
        poisson_sf=poisson_sf,
        empirical_pmf=empirical_pmf,
        empirical_sf=empirical_sf,
        negbin_pmf=negbin_pmf,
        negbin_sf=negbin_sf,
    )


def recommended_pmf(fv: FairValueResult) -> dict[int, float]:
    """
    Return the PMF we recommend using for fair-value pricing:
    - If overdispersed AND scipy available AND negbin converged → negative-binomial
    - Else if enough data (≥ 10 eps) → empirical
    - Else → Poisson
    """
    if fv.overdispersed and fv.negbin_pmf is not None:
        return fv.negbin_pmf
    if fv.lookback_episodes >= 10:
        return fv.empirical_pmf
    return fv.poisson_pmf


def recommended_sf(fv: FairValueResult) -> dict[int, float]:
    """Survival function (P >= n) for the recommended model."""
    pmf = recommended_pmf(fv)
    return _sf_from_pmf(pmf)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _poisson_pmf(lam: float, k: int) -> float:
    """Poisson PMF P(X=k)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0


def _poisson_pmf_dict(lam: float) -> dict[int, float]:
    pmf: dict[int, float] = {}
    tail = 0.0
    for k in range(MAX_BUCKET):
        pmf[k] = _poisson_pmf(lam, k)
        tail += pmf[k]
    pmf[MAX_BUCKET] = max(0.0, 1.0 - tail)
    return pmf


def _empirical_pmf(counts: list[int], n: int) -> dict[int, float]:
    pmf: dict[int, float] = {k: 0.0 for k in range(MAX_BUCKET + 1)}
    for c in counts:
        bucket = min(c, MAX_BUCKET)
        pmf[bucket] += 1.0 / n
    return pmf


def _negbin_pmf_dict(mean: float, variance: float) -> Optional[dict[int, float]]:
    """
    Fit a Negative-Binomial via method of moments:
        p = mean / variance
        r = mean² / (variance - mean)
    """
    if not HAS_SCIPY:
        return None
    if variance <= mean:
        return None
    try:
        p = mean / variance
        r = mean * p / (1 - p)
        if r <= 0 or not (0 < p < 1):
            return None
        pmf: dict[int, float] = {}
        tail = 0.0
        for k in range(MAX_BUCKET):
            v = stats.nbinom.pmf(k, r, p)
            pmf[k] = float(v)
            tail += float(v)
        pmf[MAX_BUCKET] = max(0.0, 1.0 - tail)
        return pmf
    except Exception:
        return None


def _sf_from_pmf(pmf: dict[int, float]) -> dict[int, float]:
    """P(X >= n) for n = 0..MAX_BUCKET from a PMF dict."""
    sf: dict[int, float] = {}
    cumulative = 0.0
    for k in sorted(pmf):
        sf[k] = max(0.0, min(1.0, 1.0 - cumulative))
        cumulative += pmf[k]
    return sf


def format_fair_value_table(fv: FairValueResult) -> str:
    """Return a formatted ASCII table for CLI display."""
    rec_pmf = recommended_pmf(fv)
    rec_sf  = recommended_sf(fv)

    lines = [
        f"\nKeyword : {fv.keyword!r}",
        f"Lookback: last {fv.lookback_episodes} episodes",
        f"Mean    : {fv.mean:.2f} mentions/episode",
        f"Std dev : {math.sqrt(fv.variance):.2f}",
        f"Model   : {'Neg-Binomial' if fv.overdispersed and fv.negbin_pmf else ('Empirical' if fv.lookback_episodes >= 10 else 'Poisson')}",
        f"λ (Poisson) = {fv.lambda_estimate:.3f}",
        "",
        f"{'Count':>6}  {'P(= N)':>10}  {'P(>= N)':>10}  {'Fair value (≥N YES)':>20}",
        "-" * 54,
    ]
    for k in range(MAX_BUCKET + 1):
        label = f"{k}+" if k == MAX_BUCKET else str(k)
        pmf_v = rec_pmf.get(k, 0.0)
        sf_v  = rec_sf.get(k, 0.0)
        fv_pct = sf_v * 100
        lines.append(
            f"{label:>6}  {pmf_v:>10.4f}  {sf_v:>10.4f}  {fv_pct:>19.1f}%"
        )

    return "\n".join(lines)
