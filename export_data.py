#!/usr/bin/env python3
"""
Export the local JRE SQLite database to data.json for GitHub Pages.

Usage
-----
    python export_data.py                        # jre_data.db → data.json
    python export_data.py --db other.db          # custom DB
    python export_data.py --out docs/data.json   # custom output path
    python export_data.py --no-minutes           # skip per-minute data (smaller file)

Workflow
--------
1. Run the Flask server locally:  python server.py
2. Click "Sync YouTube" to fetch real JRE transcripts.
3. Run this script:               python export_data.py
4. Commit & push data.json:       git add data.json && git push
5. GitHub Pages loads data.json automatically on next visit.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Must match STOPWORDS in jre_analyzer/analyzer.py
STOPWORDS: frozenset[str] = frozenset(
    "a an the and or but if in on at to of for is it he she they we "
    "you i me my his her its our their be was were been have has had "
    "do does did will would could should may might just like yeah so "
    "that this with from by what when where who how no not".split()
)


def export(db_path: Path, out_path: Path, include_minutes: bool = True) -> None:
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        print("Run 'python server.py' and sync YouTube episodes first.")
        return

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # ── Episodes ────────────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT video_id, title, upload_date, episode_number, duration_seconds
        FROM episodes
        WHERE indexed_at IS NOT NULL
        ORDER BY upload_date DESC, episode_number DESC
    """).fetchall()

    episodes = [dict(r) for r in rows]

    if not episodes:
        print("No indexed episodes found.")
        print("Run 'python server.py', click 'Sync YouTube', then re-run this script.")
        con.close()
        return

    video_ids = [ep["video_id"] for ep in episodes]
    placeholders = ",".join("?" * len(video_ids))

    # ── Word frequencies ─────────────────────────────────────────────────────
    word_counts: dict[str, dict[str, int]] = {}
    for row in con.execute(f"""
        SELECT video_id, word, count
        FROM word_frequencies
        WHERE video_id IN ({placeholders})
        ORDER BY video_id
    """, video_ids).fetchall():
        if row["word"] in STOPWORDS:
            continue
        vid = row["video_id"]
        if vid not in word_counts:
            word_counts[vid] = {}
        word_counts[vid][row["word"]] = row["count"]

    # ── Minute frequencies ───────────────────────────────────────────────────
    minute_counts: dict[str, dict[str, dict[str, int]]] = {}
    if include_minutes:
        for row in con.execute(f"""
            SELECT video_id, minute, word, count
            FROM minute_frequencies
            WHERE video_id IN ({placeholders})
            ORDER BY video_id, minute
        """, video_ids).fetchall():
            if row["word"] in STOPWORDS:
                continue
            vid = row["video_id"]
            m   = str(row["minute"])
            if vid not in minute_counts:
                minute_counts[vid] = {}
            if m not in minute_counts[vid]:
                minute_counts[vid][m] = {}
            minute_counts[vid][m][row["word"]] = row["count"]

    con.close()

    # ── Write JSON ───────────────────────────────────────────────────────────
    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "episodes":     episodes,
        "word_counts":  word_counts,
        "minute_counts": minute_counts,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Exported {len(episodes)} episodes → {out_path}  ({size_mb:.1f} MB)")
    print()
    print("Next steps:")
    print("  git add data.json")
    print("  git commit -m 'Update JRE data'")
    print("  git push")
    print()
    print("GitHub Pages will serve data.json automatically.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export JRE DB to static data.json")
    parser.add_argument("--db",  default="jre_data.db",  type=Path, help="SQLite DB path")
    parser.add_argument("--out", default="data.json",    type=Path, help="Output JSON path")
    parser.add_argument("--no-minutes", action="store_true", help="Skip per-minute data (smaller file)")
    args = parser.parse_args()
    export(args.db, args.out, include_minutes=not args.no_minutes)
