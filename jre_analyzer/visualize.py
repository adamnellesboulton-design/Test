"""
Visualisation module.

Produces two figures:
  1. Episode trend chart  — bar chart of keyword counts per episode with
     rolling-average lines (last-1, 5, 20, 50, 100).
  2. Minute breakdown chart — bar chart of counts per minute for a chosen episode.

Both functions save to PNG files and optionally display interactively.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

try:
    import matplotlib
    matplotlib.use("Agg")           # non-interactive backend; switch to TkAgg if GUI needed
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

from .search import SearchResult, MinuteResult

OUTPUT_DIR = Path(__file__).parent.parent / "charts"


def _ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def plot_episode_trend(
    result: SearchResult,
    show: bool = False,
    save: bool = True,
    max_episodes: int = 100,
) -> Optional[Path]:
    """
    Bar chart of keyword count per episode (newest on the right) with overlay
    lines for rolling averages.

    Returns the path of the saved PNG, or None if matplotlib is unavailable.
    """
    if not HAS_MPL:
        print("matplotlib is not installed — cannot plot.")
        return None

    # Oldest → newest for left-to-right reading
    eps = list(reversed(result.episodes[:max_episodes]))
    if not eps:
        print("No episode data to plot.")
        return None

    counts = [ep.count for ep in eps]
    labels = [_short_label(ep) for ep in eps]
    x = np.arange(len(eps))

    fig, ax = plt.subplots(figsize=(max(14, len(eps) * 0.22), 6))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    # Bars
    bar_color = "#0f3460"
    bars = ax.bar(x, counts, color=bar_color, width=0.7, zorder=2, label="Count per episode")

    # Highlight bars with non-zero counts
    for bar, count in zip(bars, counts):
        if count > 0:
            bar.set_color("#e94560")

    # Rolling average lines
    avg_configs = [
        (result.avg_last_1,   1,   "#f5f5f5", "Last 1  avg"),
        (result.avg_last_5,   5,   "#00b4d8", "Last 5  avg"),
        (result.avg_last_20,  20,  "#90e0ef", "Last 20 avg"),
        (result.avg_last_50,  50,  "#ffd166", "Last 50 avg"),
        (result.avg_last_100, 100, "#06d6a0", "Last 100 avg"),
    ]
    for avg_val, _, color, label in avg_configs:
        if avg_val is not None:
            ax.axhline(
                y=avg_val, color=color, linewidth=1.4, linestyle="--",
                alpha=0.85, label=f"{label}: {avg_val:.2f}", zorder=3,
            )

    ax.set_xlabel("Episode", color="#cccccc", fontsize=9)
    ax.set_ylabel("Mentions", color="#cccccc", fontsize=9)
    ax.set_title(
        f'JRE keyword: "{result.keyword}"  |  mention count per episode',
        color="#ffffff", fontsize=12, fontweight="bold",
    )
    ax.tick_params(colors="#aaaaaa")
    ax.spines[:].set_color("#444455")
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # X-axis labels — show every Nth label to avoid overcrowding
    step = max(1, len(eps) // 20)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(labels[::step], rotation=45, ha="right", fontsize=7, color="#aaaaaa")

    # Legend
    leg = ax.legend(
        loc="upper left", framealpha=0.3, fontsize=8,
        facecolor="#0d0d1a", labelcolor="#ffffff",
    )

    plt.tight_layout()

    out_path = None
    if save:
        out_dir = _ensure_output_dir()
        safe_kw = result.keyword.replace(" ", "_").replace("/", "-")
        out_path = out_dir / f"trend_{safe_kw}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        print(f"Saved trend chart → {out_path}")

    if show:
        plt.show()

    plt.close(fig)
    return out_path


def plot_minute_breakdown(
    result: SearchResult,
    episode_id: int,
    minute_data: list[MinuteResult],
    show: bool = False,
    save: bool = True,
) -> Optional[Path]:
    """
    Bar chart of keyword count per minute for a specific episode.
    """
    if not HAS_MPL:
        print("matplotlib is not installed — cannot plot.")
        return None

    if not minute_data:
        print("No per-minute data available for this episode.")
        return None

    ep = result.episode_by_id(episode_id)
    ep_label = ep.title if ep else str(episode_id)

    minutes = [r.minute for r in minute_data]
    counts  = [r.count  for r in minute_data]

    # Fill gaps so we get a continuous x-axis
    if minutes:
        full_range = list(range(min(minutes), max(minutes) + 1))
        count_map = {r.minute: r.count for r in minute_data}
        full_counts = [count_map.get(m, 0) for m in full_range]
    else:
        full_range, full_counts = [], []

    x = np.arange(len(full_range))

    fig, ax = plt.subplots(figsize=(max(12, len(full_range) * 0.18), 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    ax.bar(x, full_counts, color="#e94560", width=0.8, zorder=2)

    mean_val = sum(full_counts) / len(full_counts) if full_counts else 0
    if mean_val > 0:
        ax.axhline(
            y=mean_val, color="#ffd166", linewidth=1.2,
            linestyle="--", alpha=0.8, label=f"Episode avg: {mean_val:.2f}/min",
        )
        ax.legend(framealpha=0.3, fontsize=8, facecolor="#0d0d1a", labelcolor="#ffffff")

    ax.set_xlabel("Minute", color="#cccccc", fontsize=9)
    ax.set_ylabel("Mentions", color="#cccccc", fontsize=9)
    ax.set_title(
        f'"{result.keyword}" per minute  |  {ep_label[:70]}',
        color="#ffffff", fontsize=10, fontweight="bold",
    )
    ax.tick_params(colors="#aaaaaa")
    ax.spines[:].set_color("#444455")
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    step = max(1, len(full_range) // 20)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(
        [str(full_range[i]) for i in range(0, len(full_range), step)],
        rotation=45, ha="right", fontsize=7, color="#aaaaaa",
    )

    plt.tight_layout()

    out_path = None
    if save:
        out_dir = _ensure_output_dir()
        safe_kw = result.keyword.replace(" ", "_").replace("/", "-")
        out_path = out_dir / f"minutes_{safe_kw}_{episode_id}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        print(f"Saved minute chart → {out_path}")

    if show:
        plt.show()

    plt.close(fig)
    return out_path


def plot_fair_value(
    keyword: str,
    probabilities: dict[int, float],
    show: bool = False,
    save: bool = True,
) -> Optional[Path]:
    """
    Bar chart of Polymarket fair-value probabilities for each mention bucket (0-10+).
    """
    if not HAS_MPL:
        print("matplotlib is not installed — cannot plot.")
        return None

    buckets = sorted(probabilities.keys())
    probs   = [probabilities[b] * 100 for b in buckets]
    labels  = [str(b) if b < 10 else "10+" for b in buckets]

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    colors = ["#0f3460" if b == 0 else "#e94560" for b in buckets]
    bars = ax.bar(range(len(buckets)), probs, color=colors, width=0.6, zorder=2)

    for bar, prob in zip(bars, probs):
        if prob >= 1.0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5,
                f"{prob:.1f}%",
                ha="center", va="bottom",
                color="#ffffff", fontsize=8,
            )

    ax.set_xticks(range(len(buckets)))
    ax.set_xticklabels(labels, color="#aaaaaa")
    ax.set_xlabel("Mention count in next episode", color="#cccccc", fontsize=9)
    ax.set_ylabel("Fair-value probability (%)", color="#cccccc", fontsize=9)
    ax.set_title(
        f'Polymarket fair value  |  "{keyword}" mentions in next JRE episode',
        color="#ffffff", fontsize=11, fontweight="bold",
    )
    ax.tick_params(colors="#aaaaaa")
    ax.spines[:].set_color("#444455")
    ax.set_ylim(0, max(probs) * 1.15 if probs else 100)

    plt.tight_layout()

    out_path = None
    if save:
        out_dir = _ensure_output_dir()
        safe_kw = keyword.replace(" ", "_").replace("/", "-")
        out_path = out_dir / f"fairvalue_{safe_kw}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        print(f"Saved fair-value chart → {out_path}")

    if show:
        plt.show()

    plt.close(fig)
    return out_path


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _short_label(ep) -> str:
    if ep.episode_number:
        return f"#{ep.episode_number}"
    if ep.episode_date:
        return ep.episode_date[5:]  # MM-DD
    return f"id{ep.episode_id}"
