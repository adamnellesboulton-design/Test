"""
SQLite persistence layer.

Schema
------
episodes
    video_id        TEXT PK
    title           TEXT
    upload_date     TEXT   (ISO format YYYY-MM-DD, nullable)
    duration_seconds INTEGER
    episode_number  INTEGER (parsed from title, nullable)
    transcript_json TEXT   (JSON-encoded list of {start, duration, text})
    indexed_at      TEXT   (ISO datetime when we processed word frequencies)

word_frequencies
    video_id        TEXT   (FK → episodes.video_id)
    word            TEXT
    count           INTEGER
    PRIMARY KEY (video_id, word)

minute_frequencies
    video_id        TEXT
    minute          INTEGER   (floor(start / 60))
    word            TEXT
    count           INTEGER
    PRIMARY KEY (video_id, minute, word)
"""

import json
import re
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "jre_data.db"


class Database:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._con
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                video_id         TEXT PRIMARY KEY,
                title            TEXT NOT NULL,
                upload_date      TEXT,
                duration_seconds INTEGER,
                episode_number   INTEGER,
                transcript_json  TEXT NOT NULL DEFAULT '[]',
                indexed_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS word_frequencies (
                video_id  TEXT NOT NULL,
                word      TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (video_id, word)
            );

            CREATE TABLE IF NOT EXISTS minute_frequencies (
                video_id  TEXT NOT NULL,
                minute    INTEGER NOT NULL,
                word      TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (video_id, minute, word)
            );

            CREATE INDEX IF NOT EXISTS idx_wf_word ON word_frequencies(word);
            CREATE INDEX IF NOT EXISTS idx_mf_word ON minute_frequencies(word);
            CREATE INDEX IF NOT EXISTS idx_ep_date ON episodes(upload_date);
            CREATE INDEX IF NOT EXISTS idx_ep_num  ON episodes(episode_number);
        """)
        self._con.commit()

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def upsert_episode(
        self,
        video_id: str,
        title: str,
        upload_date: Optional[str],
        duration_seconds: int,
        transcript: list[dict],
    ) -> None:
        episode_number = _parse_episode_number(title)
        self._con.execute(
            """
            INSERT INTO episodes (video_id, title, upload_date, duration_seconds,
                                  episode_number, transcript_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title            = excluded.title,
                upload_date      = excluded.upload_date,
                duration_seconds = excluded.duration_seconds,
                episode_number   = excluded.episode_number,
                transcript_json  = excluded.transcript_json
            """,
            (video_id, title, upload_date, duration_seconds,
             episode_number, json.dumps(transcript)),
        )
        self._con.commit()

    def episode_exists(self, video_id: str) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM episodes WHERE video_id = ?", (video_id,)
        ).fetchone()
        return row is not None

    def get_episode(self, video_id: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM episodes WHERE video_id = ?", (video_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_episodes(self, limit: int = 0) -> list[dict]:
        """Return episodes ordered newest-first (by upload_date then episode_number)."""
        sql = """
            SELECT * FROM episodes
            ORDER BY upload_date DESC, episode_number DESC
        """
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._con.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_recent_episode_ids(self, n: int) -> list[str]:
        rows = self._con.execute(
            """
            SELECT video_id FROM episodes
            ORDER BY upload_date DESC, episode_number DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [r["video_id"] for r in rows]

    def mark_indexed(self, video_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "UPDATE episodes SET indexed_at = ? WHERE video_id = ?",
            (now, video_id),
        )
        self._con.commit()

    def count_episodes(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    # ------------------------------------------------------------------
    # Word frequencies
    # ------------------------------------------------------------------

    def upsert_word_frequencies(self, video_id: str, freq: dict[str, int]) -> None:
        """Bulk-insert / replace per-episode word counts."""
        self._con.executemany(
            """
            INSERT INTO word_frequencies (video_id, word, count)
            VALUES (?, ?, ?)
            ON CONFLICT(video_id, word) DO UPDATE SET count = excluded.count
            """,
            [(video_id, word, count) for word, count in freq.items()],
        )
        self._con.commit()

    def upsert_minute_frequencies(
        self, video_id: str, minute_freq: dict[int, dict[str, int]]
    ) -> None:
        """minute_freq: {minute_int: {word: count}}"""
        rows = []
        for minute, freq in minute_freq.items():
            for word, count in freq.items():
                rows.append((video_id, minute, word, count))
        self._con.executemany(
            """
            INSERT INTO minute_frequencies (video_id, minute, word, count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(video_id, minute, word) DO UPDATE SET count = excluded.count
            """,
            rows,
        )
        self._con.commit()

    # ------------------------------------------------------------------
    # Search queries
    # ------------------------------------------------------------------

    def search_word_by_episode(self, word: str) -> list[dict]:
        """
        Return per-episode frequency for `word`, ordered newest-first.
        Each row: video_id, title, upload_date, episode_number, count, duration_seconds
        """
        word = word.lower()
        rows = self._con.execute(
            """
            SELECT e.video_id,
                   e.title,
                   e.upload_date,
                   e.episode_number,
                   e.duration_seconds,
                   COALESCE(wf.count, 0) AS count
            FROM episodes e
            LEFT JOIN word_frequencies wf
                   ON wf.video_id = e.video_id AND wf.word = ?
            WHERE e.indexed_at IS NOT NULL
            ORDER BY e.upload_date DESC, e.episode_number DESC
            """,
            (word,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_word_by_minute(self, word: str, video_id: str) -> list[dict]:
        """
        Return per-minute frequency for `word` within a single episode.
        Each row: minute, count
        """
        word = word.lower()
        rows = self._con.execute(
            """
            SELECT minute, count
            FROM minute_frequencies
            WHERE video_id = ? AND word = ?
            ORDER BY minute
            """,
            (video_id, word),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_episode_count_for_word(self, word: str, video_id: str) -> int:
        word = word.lower()
        row = self._con.execute(
            "SELECT count FROM word_frequencies WHERE video_id = ? AND word = ?",
            (video_id, word),
        ).fetchone()
        return row["count"] if row else 0

    def reset_index(self) -> None:
        """
        Drop all word/minute frequency data and clear indexed_at on every
        episode so index_all() will re-process them.

        Call this after a tokenizer change (e.g. adding stemming) so that
        the stored frequencies are rebuilt with the new algorithm.
        """
        self._con.executescript("""
            DELETE FROM word_frequencies;
            DELETE FROM minute_frequencies;
            UPDATE episodes SET indexed_at = NULL;
        """)
        self._con.commit()
        logger.info("Frequency index cleared; episodes queued for re-indexing")

    def close(self) -> None:
        self._con.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_episode_number(title: str) -> Optional[int]:
    """Try to extract the episode number from titles like '#2100' or 'Episode 2100'."""
    m = re.search(r"#(\d{3,5})", title)
    if m:
        return int(m.group(1))
    m = re.search(r"episode\s+(\d{3,5})", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None
