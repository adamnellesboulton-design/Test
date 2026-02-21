#!/usr/bin/env python3
"""
JRE Transcript Analyzer — CLI entry point.

Commands
--------
  sync    Fetch the last N episodes from YouTube and store transcripts.
  index   Build word-frequency tables for all un-indexed episodes.
  search  Search for a keyword and display stats + charts.
  info    Show database summary.

Usage examples
--------------
  python main.py sync --episodes 100
  python main.py index
  python main.py search "DMT"
  python main.py search "aliens" --lookback 50 --show-chart
  python main.py search "Trump" --episode-id dQw4w9WgXcQ --minute-chart
  python main.py info
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_all, index_episode
from jre_analyzer.search import search, get_minute_breakdown
from jre_analyzer.fair_value import calculate_fair_value, format_fair_value_table
from jre_analyzer.visualize import (
    plot_episode_trend,
    plot_minute_breakdown,
    plot_fair_value,
)

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Colour helpers
# ------------------------------------------------------------------

def _c(text: str, color: str) -> str:
    if not HAS_COLOR:
        return text
    return f"{color}{text}{Style.RESET_ALL}"


def _header(text: str) -> str:
    return _c(f"\n{'─' * 70}\n  {text}\n{'─' * 70}", Fore.CYAN)


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace, db: Database) -> None:
    try:
        from jre_analyzer.fetch_transcripts import sync_episodes
    except ImportError as e:
        print(f"[error] Missing dependency: {e}")
        print("        Run: pip install -r requirements.txt")
        sys.exit(1)

    print(f"Syncing up to {args.episodes} episodes…")
    added = sync_episodes(db, max_episodes=args.episodes, delay=args.delay)
    print(f"\nDone. {added} new episodes added. Run 'index' to build frequency tables.")


def cmd_index(args: argparse.Namespace, db: Database) -> None:
    total = db.count_episodes()
    if total == 0:
        print("No episodes in database. Run 'sync' first.")
        return

    print(f"Indexing word frequencies for un-indexed episodes ({total} total in DB)…")
    count = index_all(db)
    print(f"Done. {count} episodes indexed.")


def cmd_search(args: argparse.Namespace, db: Database) -> None:
    keyword = args.keyword
    lookback = args.lookback

    # ── Episode trend ──────────────────────────────────────────────
    print(_header(f"Keyword search: \"{keyword}\""))
    result = search(db, keyword)

    if not result.episodes:
        print("No indexed episodes found. Run 'sync' then 'index' first.")
        return

    # Summary table
    print(f"\n{'Episode':>10}  {'Date':>12}  {'#Mentions':>10}  {'per min':>8}  {'Title'}")
    print("─" * 90)
    for ep in result.episodes[:args.top]:
        ep_num = f"#{ep.episode_number}" if ep.episode_number else ep.video_id[:8]
        date   = ep.upload_date or "unknown"
        pmin   = f"{ep.per_minute:.3f}"
        title  = ep.title[:45] + ("…" if len(ep.title) > 45 else "")
        count_str = _c(str(ep.count), Fore.RED if ep.count > 0 else Fore.WHITE) if HAS_COLOR else str(ep.count)
        print(f"{ep_num:>10}  {date:>12}  {count_str:>10}  {pmin:>8}  {title}")

    # Rolling averages
    print()
    print("Rolling averages (mentions/episode):")
    avgs = [
        ("Last  1 ep ", result.avg_last_1),
        ("Last  5 eps", result.avg_last_5),
        ("Last 20 eps", result.avg_last_20),
        ("Last 50 eps", result.avg_last_50),
        ("Last100 eps", result.avg_last_100),
    ]
    for label, val in avgs:
        bar = ""
        if val is not None:
            filled = min(40, int(val * 5))
            bar = "█" * filled
            print(f"  {label} : {val:6.2f}  {bar}")
        else:
            print(f"  {label} : (insufficient data)")

    # ── Fair value ─────────────────────────────────────────────────
    fv = calculate_fair_value(result, lookback=lookback)
    print(_header("Polymarket fair values"))
    print(format_fair_value_table(fv))

    # ── Charts ─────────────────────────────────────────────────────
    if args.show_chart or args.save_chart:
        plot_episode_trend(result, show=args.show_chart, save=args.save_chart)
        plot_fair_value(
            keyword,
            _get_recommended_pmf(fv),
            show=args.show_chart,
            save=args.save_chart,
        )

    # ── Per-minute breakdown for a specific episode ────────────────
    if args.episode_id or args.minute_chart:
        vid = args.episode_id
        if not vid and result.episodes:
            # Default to most recent episode
            vid = result.episodes[0].video_id
            print(f"\n(Using most recent episode: {vid})")

        if vid:
            minute_data = get_minute_breakdown(db, keyword, vid)
            ep_obj = result.episode_by_id(vid)
            ep_title = ep_obj.title if ep_obj else vid

            print(_header(f"Per-minute breakdown: {ep_title[:60]}"))
            if minute_data:
                print(f"\n{'Minute':>8}  {'Count':>8}")
                print("─" * 22)
                for m in minute_data:
                    bar = "█" * m.count
                    print(f"{m.minute:>8}  {m.count:>8}  {bar}")
            else:
                print(f"  (keyword not mentioned in this episode)")

            if args.minute_chart or args.show_chart or args.save_chart:
                plot_minute_breakdown(
                    result, vid, minute_data,
                    show=args.show_chart or args.minute_chart,
                    save=args.save_chart or args.minute_chart,
                )


def cmd_info(args: argparse.Namespace, db: Database) -> None:
    print(_header("Database info"))
    total = db.count_episodes()
    eps = db.get_all_episodes(limit=5)
    print(f"  Total episodes stored : {total}")
    print(f"  Most recent 5:")
    for ep in eps:
        indexed = "✓" if ep.get("indexed_at") else "○"
        num = f"#{ep['episode_number']}" if ep.get("episode_number") else ""
        print(f"    [{indexed}] {ep['upload_date'] or '????-??-??'}  {num:>6}  {ep['title'][:55]}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_recommended_pmf(fv) -> dict[int, float]:
    from jre_analyzer.fair_value import recommended_pmf
    return recommended_pmf(fv)


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jre-analyzer",
        description="JRE Transcript Analyzer — track keyword mentions for Polymarket",
    )
    p.add_argument("--db", default="jre_data.db", help="Path to SQLite database")

    sub = p.add_subparsers(dest="command", required=True)

    # sync
    s = sub.add_parser("sync", help="Fetch episodes and transcripts from YouTube")
    s.add_argument("--episodes", type=int, default=100, help="Max episodes to fetch")
    s.add_argument("--delay", type=float, default=1.5, help="Seconds between requests")

    # index
    sub.add_parser("index", help="Build word-frequency index for stored episodes")

    # search
    sr = sub.add_parser("search", help="Search for a keyword")
    sr.add_argument("keyword", help="Word or phrase to search for")
    sr.add_argument("--top", type=int, default=20, help="Episodes to show in table")
    sr.add_argument("--lookback", type=int, default=20,
                    help="Episodes to use for fair-value calculation (default 20)")
    sr.add_argument("--episode-id", default=None,
                    help="YouTube video ID for per-minute breakdown")
    sr.add_argument("--show-chart", action="store_true",
                    help="Open charts interactively (requires display)")
    sr.add_argument("--save-chart", action="store_true", default=True,
                    help="Save charts to ./charts/ (default: on)")
    sr.add_argument("--minute-chart", action="store_true",
                    help="Generate and save per-minute chart for latest episode")

    # info
    sub.add_parser("info", help="Show database summary")

    return p


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    db = Database(db_path=args.db)

    try:
        if args.command == "sync":
            cmd_sync(args, db)
        elif args.command == "index":
            cmd_index(args, db)
        elif args.command == "search":
            cmd_search(args, db)
        elif args.command == "info":
            cmd_info(args, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
