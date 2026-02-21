#!/usr/bin/env python3
"""
JRE Transcript Analyzer — CLI entry point.

Commands
--------
  upload  Parse and store a transcript .txt file.
  index   Build word-frequency tables for all un-indexed episodes.
  search  Search for a keyword and display stats + charts.
  info    Show database summary.

Usage examples
--------------
  python main.py upload transcript.txt --title "JRE #2200 - Guest Name" --date 2026-02-24
  python main.py index
  python main.py search "DMT"
  python main.py search "aliens" --lookback 50 --show-chart
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
from jre_analyzer.fetch_transcripts import parse_transcript_txt
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

def cmd_upload(args: argparse.Namespace, db: Database) -> None:
    path = Path(args.file)
    if not path.exists():
        print(f"[error] File not found: {path}")
        sys.exit(1)

    content = path.read_text(encoding="utf-8", errors="replace")
    segments, duration = parse_transcript_txt(content)

    if not segments:
        print("[error] No segments parsed — check the file format.")
        sys.exit(1)

    title = args.title or path.stem
    episode_id = db.insert_episode(
        title=title,
        transcript=segments,
        episode_date=args.date,
        filename=path.name,
        duration_seconds=duration,
    )
    print(f"Stored episode id={episode_id}: {title!r} ({len(segments)} segments)")
    print("Indexing…")
    index_episode(db, episode_id)
    print("Done.")


def cmd_index(args: argparse.Namespace, db: Database) -> None:
    total = db.count_episodes()
    if total == 0:
        print("No episodes in database. Run 'upload' first.")
        return

    print(f"Indexing word frequencies for un-indexed episodes ({total} total in DB)…")
    count = index_all(db)
    print(f"Done. {count} episodes indexed.")


def cmd_search(args: argparse.Namespace, db: Database) -> None:
    keyword = args.keyword
    lookback = args.lookback

    print(_header(f"Keyword search: \"{keyword}\""))
    result = search(db, keyword)

    if not result.episodes:
        print("No indexed episodes found. Run 'upload' then 'index' first.")
        return

    print(f"\n{'Episode':>10}  {'Date':>12}  {'#Mentions':>10}  {'per min':>8}  {'Title'}")
    print("─" * 90)
    for ep in result.episodes[:args.top]:
        ep_num = f"#{ep.episode_number}" if ep.episode_number else f"id{ep.episode_id}"
        date   = ep.episode_date or "unknown"
        pmin   = f"{ep.per_minute:.3f}"
        title  = ep.title[:45] + ("…" if len(ep.title) > 45 else "")
        count_str = _c(str(ep.count), Fore.RED if ep.count > 0 else Fore.WHITE) if HAS_COLOR else str(ep.count)
        print(f"{ep_num:>10}  {date:>12}  {count_str:>10}  {pmin:>8}  {title}")

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
        if val is not None:
            bar = "█" * min(40, int(val * 5))
            print(f"  {label} : {val:6.2f}  {bar}")
        else:
            print(f"  {label} : (insufficient data)")

    fv = calculate_fair_value(result, lookback=lookback)
    print(_header("Polymarket fair values"))
    print(format_fair_value_table(fv))

    if args.show_chart or args.save_chart:
        plot_episode_trend(result, show=args.show_chart, save=args.save_chart)
        from jre_analyzer.fair_value import recommended_pmf
        plot_fair_value(keyword, recommended_pmf(fv), show=args.show_chart, save=args.save_chart)

    if args.episode_id or args.minute_chart:
        eid = args.episode_id
        if not eid and result.episodes:
            eid = result.episodes[0].episode_id
            print(f"\n(Using most recent episode: id={eid})")

        if eid:
            minute_data = get_minute_breakdown(db, keyword, eid)
            ep_obj  = result.episode_by_id(eid)
            ep_title = ep_obj.title if ep_obj else str(eid)

            print(_header(f"Per-minute breakdown: {ep_title[:60]}"))
            if minute_data:
                print(f"\n{'Minute':>8}  {'Count':>8}")
                print("─" * 22)
                for m in minute_data:
                    bar = "█" * m.count
                    print(f"{m.minute:>8}  {m.count:>8}  {bar}")
            else:
                print("  (keyword not mentioned in this episode)")

            if args.minute_chart or args.show_chart or args.save_chart:
                plot_minute_breakdown(
                    result, eid, minute_data,
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
        print(f"    [{indexed}] {ep['episode_date'] or '????-??-??'}  {num:>6}  {ep['title'][:55]}")


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

    # upload
    u = sub.add_parser("upload", help="Parse and store a transcript .txt file")
    u.add_argument("file", help="Path to transcript .txt file")
    u.add_argument("--title", default=None, help="Episode title")
    u.add_argument("--date", default=None, help="Episode date (YYYY-MM-DD)")

    # index
    sub.add_parser("index", help="Build word-frequency index for stored episodes")

    # search
    sr = sub.add_parser("search", help="Search for a keyword")
    sr.add_argument("keyword", help="Word or phrase to search for")
    sr.add_argument("--top", type=int, default=20, help="Episodes to show in table")
    sr.add_argument("--lookback", type=int, default=20,
                    help="Episodes to use for fair-value calculation (default 20)")
    sr.add_argument("--episode-id", type=int, default=None,
                    help="Episode ID for per-minute breakdown")
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
        if args.command == "upload":
            cmd_upload(args, db)
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
