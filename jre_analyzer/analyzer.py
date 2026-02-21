"""
Word frequency analyzer.

Given a transcript (list of {start, duration, text} segments) this module:
 1. Tokenises each segment into lowercase alphabetic words.
 2. Builds per-episode word counts.
 3. Builds per-minute word counts (minute = floor(start / 60)).

Joe vs guest speaker filtering
-------------------------------
YouTube auto-captions don't label speakers.  A common heuristic for long-form
podcasts is that Joe is the speaker for roughly the first few minutes of most
segments and speaks slightly more than guests.  Without diarisation we apply
NO filtering and treat the whole transcript as Joe's words — this is the same
data available to Polymarket market resolvers who typically count total mentions
regardless of speaker.  If diarised transcripts become available they can be
passed in with segments already filtered to Joe's turns.
"""

import json
import logging
import re
from collections import defaultdict
from typing import Optional

from .database import Database

logger = logging.getLogger(__name__)

# ── Stemmer ───────────────────────────────────────────────────────────────────
# snowballstemmer (PyPI: snowballstemmer) maps inflected forms to their root so
# that "drugs", "drug", "drugged" all index and search as "drug".  Falls back
# to identity if the package is not installed (no stemming, existing behaviour).
try:
    import snowballstemmer as _sb
    _stemmer = _sb.stemmer("english")
    def stem_word(word: str) -> str:
        return _stemmer.stemWord(word)
    logger.debug("Snowball English stemmer loaded")
except ImportError:  # pragma: no cover
    def stem_word(word: str) -> str:  # type: ignore[misc]
        return word
    logger.warning("snowballstemmer not installed — plurals will not be collapsed")

# Words to ignore when building the general index (not applied to keyword search)
STOPWORDS: frozenset[str] = frozenset(
    "a an the and or but if in on at to of for is it he she they we "
    "you i me my his her its our their be was were been have has had "
    "do does did will would could should may might just like yeah so "
    "that this with from by what when where who how no not".split()
)


def tokenize(text: str) -> list[str]:
    """
    Lower-case alphabetic tokens, stemmed to their English root form.

    Examples (with snowballstemmer installed):
        "drugs" → ["drug"]
        "running" → ["run"]
        "psychedelics" → ["psychedel"]
        "DMT" → ["dmt"]   (acronyms are unaffected — no common suffix)
    """
    return [stem_word(w) for w in re.findall(r"[a-z]+", text.lower())]


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
        text = seg.get("text", "") or ""
        minute = int(start // 60)
        words = tokenize(text)
        for word in words:
            episode_freq[word] += 1
            minute_freq[minute][word] += 1

    # Convert defaultdicts to plain dicts for serialisation
    return dict(episode_freq), {m: dict(v) for m, v in minute_freq.items()}


def index_episode(db: Database, video_id: str) -> bool:
    """
    Load the stored transcript for `video_id`, build frequency tables and save
    them back to the database.  Returns True if indexing succeeded.
    """
    ep = db.get_episode(video_id)
    if ep is None:
        logger.error("Episode %s not found in database", video_id)
        return False

    try:
        transcript = json.loads(ep["transcript_json"])
    except (json.JSONDecodeError, TypeError):
        logger.error("Invalid transcript JSON for %s", video_id)
        return False

    episode_freq, minute_freq = build_frequencies(transcript)

    db.upsert_word_frequencies(video_id, episode_freq)
    db.upsert_minute_frequencies(video_id, minute_freq)
    db.mark_indexed(video_id)

    total_words = sum(episode_freq.values())
    logger.info(
        "Indexed %s: %d unique words, %d total words, %d minutes of content",
        video_id,
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
        if index_episode(db, ep["video_id"]):
            count += 1
    return count


def per_minute_rate(count: int, duration_seconds: int) -> float:
    """Mentions per minute given a total count and episode duration."""
    if not duration_seconds:
        return 0.0
    return count / (duration_seconds / 60.0)
