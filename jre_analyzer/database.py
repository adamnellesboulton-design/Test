"""
SQLite persistence layer.

Schema
------
episodes
    id               INTEGER PRIMARY KEY AUTOINCREMENT
    title            TEXT NOT NULL        (user-supplied, e.g. "JRE #2100 – Guest Name")
    episode_date     TEXT                 (ISO YYYY-MM-DD, nullable — the episode's air date)
    filename         TEXT                 (original uploaded filename)
    uploaded_at      TEXT                 (ISO datetime when file was uploaded to this app)
    duration_seconds INTEGER              (derived from last transcript timestamp)
    episode_number   INTEGER              (parsed from title, nullable)
    transcript_json  TEXT DEFAULT '[]'   (JSON list of {start, text})
    indexed_at       TEXT                 (ISO datetime when word frequencies were built)

word_frequencies
    episode_id  INTEGER  (FK → episodes.id)
    word        TEXT     (lowercase, un-stemmed)
    count       INTEGER
    PRIMARY KEY (episode_id, word)

minute_frequencies
    episode_id  INTEGER
    minute      INTEGER  (floor(start / 60))
    word        TEXT
    count       INTEGER
    PRIMARY KEY (episode_id, minute, word)
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
        self._con.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                title            TEXT NOT NULL,
                episode_date     TEXT,
                filename         TEXT,
                uploaded_at      TEXT,
                duration_seconds INTEGER DEFAULT 0,
                episode_number   INTEGER,
                transcript_json  TEXT NOT NULL DEFAULT '[]',
                indexed_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS word_frequencies (
                episode_id  INTEGER NOT NULL,
                word        TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (episode_id, word)
            );

            CREATE TABLE IF NOT EXISTS minute_frequencies (
                episode_id  INTEGER NOT NULL,
                minute      INTEGER NOT NULL,
                word        TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (episode_id, minute, word)
            );

            CREATE INDEX IF NOT EXISTS idx_wf_word     ON word_frequencies(word);
            CREATE INDEX IF NOT EXISTS idx_mf_word     ON minute_frequencies(word);
            CREATE INDEX IF NOT EXISTS idx_ep_date     ON episodes(episode_date);
            CREATE INDEX IF NOT EXISTS idx_ep_num      ON episodes(episode_number);
        """)
        self._con.commit()

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def insert_episode(
        self,
        title: str,
        transcript: list[dict],
        episode_date: Optional[str] = None,
        filename: Optional[str] = None,
        duration_seconds: int = 0,
    ) -> int:
        """Insert a new episode. Returns the new episode id."""
        episode_number = _parse_episode_number(title)
        uploaded_at = datetime.now(timezone.utc).isoformat()
        cur = self._con.execute(
            """
            INSERT INTO episodes (title, episode_date, filename, uploaded_at,
                                  duration_seconds, episode_number, transcript_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, episode_date, filename, uploaded_at,
             duration_seconds, episode_number, json.dumps(transcript)),
        )
        self._con.commit()
        return cur.lastrowid

    def delete_episode(self, episode_id: int) -> bool:
        """Delete an episode and all its frequency data. Returns True if found."""
        row = self._con.execute(
            "SELECT id FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return False
        self._con.executescript(f"""
            DELETE FROM word_frequencies   WHERE episode_id = {episode_id};
            DELETE FROM minute_frequencies WHERE episode_id = {episode_id};
            DELETE FROM episodes           WHERE id         = {episode_id};
        """)
        self._con.commit()
        return True

    def get_episode(self, episode_id: int) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_episodes(self, limit: int = 0) -> list[dict]:
        """Return episodes ordered newest-first (by episode_date then id)."""
        sql = """
            SELECT * FROM episodes
            ORDER BY episode_date DESC NULLS LAST, id DESC
        """
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._con.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def mark_indexed(self, episode_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "UPDATE episodes SET indexed_at = ? WHERE id = ?",
            (now, episode_id),
        )
        self._con.commit()

    def count_episodes(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    # ------------------------------------------------------------------
    # Word frequencies
    # ------------------------------------------------------------------

    def upsert_word_frequencies(self, episode_id: int, freq: dict[str, int]) -> None:
        """Bulk-insert / replace per-episode word counts."""
        self._con.executemany(
            """
            INSERT INTO word_frequencies (episode_id, word, count)
            VALUES (?, ?, ?)
            ON CONFLICT(episode_id, word) DO UPDATE SET count = excluded.count
            """,
            [(episode_id, word, count) for word, count in freq.items()],
        )
        self._con.commit()

    def upsert_minute_frequencies(
        self, episode_id: int, minute_freq: dict[int, dict[str, int]]
    ) -> None:
        """minute_freq: {minute_int: {word: count}}"""
        rows = []
        for minute, freq in minute_freq.items():
            for word, count in freq.items():
                rows.append((episode_id, minute, word, count))
        self._con.executemany(
            """
            INSERT INTO minute_frequencies (episode_id, minute, word, count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(episode_id, minute, word) DO UPDATE SET count = excluded.count
            """,
            rows,
        )
        self._con.commit()

    # ------------------------------------------------------------------
    # Search queries
    # ------------------------------------------------------------------

    def get_words_containing(
        self,
        term: str,
        episode_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """
        Return all word_frequencies rows where the word equals, starts with,
        or contains `term`.  The caller filters using is_valid_match().

        Returns rows: episode_id, word, count (plus episode metadata).
        """
        term = term.lower()
        like_pat = f"%{term}%"

        if episode_ids is not None:
            placeholders = ",".join("?" * len(episode_ids))
            sql = f"""
                SELECT e.id          AS episode_id,
                       e.title,
                       e.episode_date,
                       e.episode_number,
                       e.duration_seconds,
                       wf.word,
                       wf.count
                FROM word_frequencies wf
                JOIN episodes e ON e.id = wf.episode_id
                WHERE wf.word LIKE ?
                  AND e.id IN ({placeholders})
                  AND e.indexed_at IS NOT NULL
            """
            rows = self._con.execute(sql, [like_pat] + list(episode_ids)).fetchall()
        else:
            sql = """
                SELECT e.id          AS episode_id,
                       e.title,
                       e.episode_date,
                       e.episode_number,
                       e.duration_seconds,
                       wf.word,
                       wf.count
                FROM word_frequencies wf
                JOIN episodes e ON e.id = wf.episode_id
                WHERE wf.word LIKE ?
                  AND e.indexed_at IS NOT NULL
            """
            rows = self._con.execute(sql, (like_pat,)).fetchall()

        return [dict(r) for r in rows]

    def get_episode_list_indexed(
        self, episode_ids: Optional[list[int]] = None
    ) -> list[dict]:
        """Return metadata for all indexed episodes (or a subset), newest-first."""
        if episode_ids is not None:
            placeholders = ",".join("?" * len(episode_ids))
            sql = f"""
                SELECT id, title, episode_date, episode_number, duration_seconds
                FROM episodes
                WHERE indexed_at IS NOT NULL AND id IN ({placeholders})
                ORDER BY episode_date DESC NULLS LAST, id DESC
            """
            rows = self._con.execute(sql, list(episode_ids)).fetchall()
        else:
            rows = self._con.execute("""
                SELECT id, title, episode_date, episode_number, duration_seconds
                FROM episodes
                WHERE indexed_at IS NOT NULL
                ORDER BY episode_date DESC NULLS LAST, id DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_minute_words_containing(
        self, term: str, episode_id: int
    ) -> list[dict]:
        """
        Return per-minute word rows for all words containing `term` in one episode.
        Returns rows: minute, word, count
        """
        like_pat = f"%{term.lower()}%"
        rows = self._con.execute(
            """
            SELECT minute, word, count
            FROM minute_frequencies
            WHERE episode_id = ? AND word LIKE ?
            ORDER BY minute
            """,
            (episode_id, like_pat),
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_index(self) -> None:
        """Drop all frequency data and clear indexed_at so episodes get re-indexed."""
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
