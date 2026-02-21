#!/usr/bin/env python3
"""
Demo script — loads synthetic JRE episode data and runs a full search + chart.

Run this without any YouTube API access to verify the analysis pipeline works:
    python demo.py

It seeds 100 fake episodes with realistic mention distributions for several
keywords, then runs a keyword search, prints the fair-value table, and saves
trend/fair-value charts to ./charts/.
"""

from __future__ import annotations

import json
import math
import random
import tempfile
from pathlib import Path

# ── Seed random for reproducibility ────────────────────────────────────────
random.seed(42)

# ── Bootstrap database ─────────────────────────────────────────────────────
from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_all
from jre_analyzer.search import search, get_minute_breakdown
from jre_analyzer.fair_value import calculate_fair_value, format_fair_value_table
from jre_analyzer.visualize import plot_episode_trend, plot_fair_value, plot_minute_breakdown

DB_PATH = Path("demo_jre.db")


def make_fake_transcript(keyword: str, mention_count: int, duration_min: int = 180) -> list[dict]:
    """Generate a fake transcript with `mention_count` occurrences of `keyword`."""
    segments = []
    duration_sec = duration_min * 60
    words_per_seg = 12

    # Sprinkle keyword mentions randomly across the duration
    mention_times = sorted(random.uniform(0, duration_sec) for _ in range(mention_count))
    mention_set = set(int(t) for t in mention_times)

    t = 0.0
    while t < duration_sec:
        filler = ["yeah", "man", "that", "is", "like", "you", "know",
                  "interesting", "right", "think", "really", "people",
                  "the", "a", "because", "so", "and", "but", "what"]
        words = random.choices(filler, k=words_per_seg)

        # If a keyword mention falls in this second, inject it
        if int(t) in mention_set:
            words[random.randint(0, len(words) - 1)] = keyword

        segments.append({
            "start": t,
            "duration": 6.0,
            "text": " ".join(words),
        })
        t += 6.0

    return segments


def seed_database(db: Database, keyword: str, n_episodes: int = 100) -> None:
    """Seed DB with synthetic episodes whose keyword mentions follow a realistic distribution."""
    print(f"Seeding {n_episodes} fake episodes for keyword '{keyword}'…")

    # Simulate bursty mention pattern: Poisson λ=3 base, occasional spikes
    lam = 3.0
    for i in range(n_episodes):
        ep_num = 2200 - i   # descending so newest first
        # Realistic: most episodes mention keyword ~3 times, some not at all, rare spikes
        spike = random.random() < 0.08   # 8% chance of spike episode
        if spike:
            count = random.randint(8, 20)
        else:
            count = max(0, int(random.gauss(lam, math.sqrt(lam))))

        duration_min = random.randint(120, 240)
        video_id = f"fake_{ep_num:04d}"
        title = f"Joe Rogan Experience #{ep_num}"

        # Approximate date (newest episode = 2025-01-15, then ~weekly)
        from datetime import date, timedelta
        ep_date = date(2025, 1, 15) - timedelta(weeks=i)
        upload_date = ep_date.strftime("%Y-%m-%d")

        transcript = make_fake_transcript(keyword, count, duration_min)

        db.upsert_episode(
            video_id=video_id,
            title=title,
            upload_date=upload_date,
            duration_seconds=duration_min * 60,
            transcript=transcript,
        )


def main():
    keyword = "DMT"

    print("=" * 70)
    print("  JRE Transcript Analyzer — DEMO MODE")
    print("  (synthetic data, no YouTube connection required)")
    print("=" * 70)

    db = Database(db_path=DB_PATH)

    seed_database(db, keyword.lower(), n_episodes=100)

    print("\nIndexing word frequencies…")
    indexed = index_all(db)
    print(f"  Indexed {indexed} episodes.")

    print(f"\nSearching for '{keyword}'…")
    result = search(db, keyword)

    # Print rolling averages
    print(f"\n  Total episodes with data : {len(result.episodes)}")
    for label, val in [
        ("Last  1", result.avg_last_1),
        ("Last  5", result.avg_last_5),
        ("Last 20", result.avg_last_20),
        ("Last 50", result.avg_last_50),
        ("Last100", result.avg_last_100),
    ]:
        if val is not None:
            bar = "█" * min(30, round(val * 3))
            print(f"  {label} avg : {val:6.2f}  {bar}")
        else:
            print(f"  {label} avg : —")

    # Fair value
    fv = calculate_fair_value(result, lookback=20)
    print(format_fair_value_table(fv))

    # Charts
    from jre_analyzer.fair_value import recommended_pmf
    print("\nGenerating charts → ./charts/")
    plot_episode_trend(result, show=False, save=True)
    plot_fair_value(keyword, recommended_pmf(fv), show=False, save=True)

    # Per-minute chart for the most-recent episode
    if result.episodes:
        vid = result.episodes[0].video_id
        minute_data = get_minute_breakdown(db, keyword.lower(), vid)
        plot_minute_breakdown(result, vid, minute_data, show=False, save=True)

    print("\nDemo complete.  Charts saved to ./charts/")
    db.close()


if __name__ == "__main__":
    main()
