"""
Word frequency analyzer.

Given a transcript (list of {start, text} segments) this module:
 1. Tokenises each segment into lowercase alphabetic words (no stemming).
 2. Builds per-episode word counts.
 3. Builds per-minute word counts (minute = floor(start / 60)).

No speaker filtering is applied — all words from all speakers are counted,
matching the market resolution rules where any speaker's words count.

Matching rules (applied at search time, not index time)
-------------------------------------------------------
For a search term T, a token counts as a match if:
  - Exact:      token == T
  - Plural:     token == T + "s"  or  token == T + "es"
  - Compound:   T is a substring of token, AND token is not merely
                T + a common derivational suffix (e.g. "joyful" ≠ compound
                for "joy", but "killjoy" is).

Words are stored raw (lowercase, no stemming) so that all matching forms
are available for retrieval.
"""

import json
import logging
import re
from collections import defaultdict

from .database import Database

logger = logging.getLogger(__name__)

STOPWORDS: frozenset[str] = frozenset(
    "a an the and or but if in on at to of for is it he she they we "
    "you i me my his her its our their be was were been have has had "
    "do does did will would could should may might just like yeah so "
    "that this with from by what when where who how no not".split()
)


def tokenize(text: str) -> list[str]:
    """
    Extract lowercase alphabetic tokens from text.
    No stemming — raw words are stored so search-time matching can apply
    the exact plural/compound rules.

        "drugs"    → ["drugs"]
        "killjoy"  → ["killjoy"]
        "DMT"      → ["dmt"]
        "drug's"   → ["drug", "s"]   (apostrophe stripped)
    """
    return re.findall(r"[a-z]+", text.lower())


def build_frequencies(transcript: list[dict]) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    """
    Returns:
        episode_freq  : {word: total_count}
        minute_freq   : {minute_int: {word: count}}
    """
    episode_freq: dict[str, int] = defaultdict(int)
    minute_freq: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for seg in transcript:
        start = seg.get("start", 0) or 0
        text  = seg.get("text", "") or ""
        minute = int(start // 60)
        for word in tokenize(text):
            episode_freq[word] += 1
            minute_freq[minute][word] += 1

    return dict(episode_freq), {m: dict(v) for m, v in minute_freq.items()}


def index_episode(db: Database, episode_id: int) -> bool:
    """
    Load the stored transcript for `episode_id`, build frequency tables and
    save them back to the database.  Returns True if indexing succeeded.
    """
    ep = db.get_episode(episode_id)
    if ep is None:
        logger.error("Episode %s not found in database", episode_id)
        return False

    try:
        transcript = json.loads(ep["transcript_json"])
    except (json.JSONDecodeError, TypeError):
        logger.error("Invalid transcript JSON for episode %s", episode_id)
        return False

    episode_freq, minute_freq = build_frequencies(transcript)

    db.upsert_word_frequencies(episode_id, episode_freq)
    db.upsert_minute_frequencies(episode_id, minute_freq)
    db.mark_indexed(episode_id)

    total_words = sum(episode_freq.values())
    logger.info(
        "Indexed episode %s: %d unique words, %d total words, %d minutes",
        episode_id,
        len(episode_freq),
        total_words,
        len(minute_freq),
    )
    return True


def index_all(db: Database) -> int:
    """Index all episodes that have not yet been indexed. Returns count indexed."""
    episodes = db.get_all_episodes()
    count = 0
    for ep in episodes:
        if ep.get("indexed_at"):
            continue
        if index_episode(db, ep["id"]):
            count += 1
    return count


def per_minute_rate(count: int, duration_seconds: int) -> float:
    """Mentions per minute given a total count and episode duration."""
    if not duration_seconds:
        return 0.0
    return count / (duration_seconds / 60.0)
