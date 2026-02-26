"""
Keyword search layer.

Market resolution rules implemented here
-----------------------------------------
For a search term T, a word in the transcript counts as a mention if:

  1. Exact match          token == T
  2. Plural               token == T + "s"  or  T + "es"
  3. Compound word        T appears as a substring of token AND the token
                          is not merely T + a derivational suffix.
                          e.g. "killjoy" counts for "joy"  (kill + joy)
                               "joyful"  does NOT count    (joy + suffix -ful)

Possessive apostrophes (drug's) are stripped at tokenization time, so the
base word "drug" is stored and matched directly.

No speaker attribution — all words from any speaker count.
No confidence intervals — counts are exact totals from the transcript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .analyzer import per_minute_rate
from .database import Database

# ── Compound-vs-derivation filter ────────────────────────────────────────────
# If the search term T appears at the *start* of a longer word and what
# follows is one of these suffixes, it is a derived form, not a compound.
# e.g.  joy + ful   → "joyful"    → NOT a match
#        joy + stick → "joystick"  → IS  a match ("stick" is a real word)
_VOWELS: frozenset[str] = frozenset("aeiou")

# Explicit (word, term) pairs where the compound heuristic fires but the
# embedded term carries a completely different historical meaning — not a
# genuine modern compound.  "-teen" in number words comes from Old English
# "tēon" (ten), not from the modern word "teen" (teenager).
_FALSE_COMPOUNDS: frozenset[tuple[str, str]] = frozenset([
    ("thirteen",  "teen"), ("fourteen",  "teen"), ("fifteen",  "teen"),
    ("sixteen",   "teen"), ("seventeen", "teen"), ("eighteen", "teen"),
    ("nineteen",  "teen"),

    # "fun" should not match the Latin-root family "fundamental*".
    ("fundamental",   "fun"), ("fundamentals",  "fun"),
    ("fundamentally", "fun"), ("fundamentalist", "fun"),
    ("fundamentalists", "fun"), ("fundamentalism", "fun"),

    # "fun" should also not match unrelated "funct-*" / "fund-*" roots.
    ("function", "fun"), ("functions", "fun"),
    ("functional", "fun"), ("functionally", "fun"),
    ("functionality", "fun"), ("functioning", "fun"),
    ("fund", "fun"), ("funds", "fun"),
    ("funded", "fun"), ("funding", "fun"),
])

_DERIVATIONAL_SUFFIXES: frozenset[str] = frozenset([
    "ful", "fully", "ness", "nesses",
    "ly",
    "ous", "ously", "ousness",
    "ish",
    "tion", "tions", "sion", "sions",
    "al", "ial", "ials", "ially",
    "ive", "ives", "ively",
    "ment", "ments",
    "less", "lessly", "lessness",
    "dom", "hood", "ship", "ships",
    "ity", "ities",
    "ize", "ized", "izes", "izing",
    "ise", "ised", "ises", "ising",
    "ify", "ified", "ifying",
    "ate", "ated", "ating", "ation", "ations",
    "ing", "ings",
    "ed",
    "er", "ers",
    "est",
    "ier", "iest",
    "en", "ens",
    "ward", "wards",
    "wise",
])

# Colloquial/diminutive endings (often nouny/adjective forms) that should not
# be treated as standalone compound tails when appended directly to a term.
# e.g. fun + sies -> "funsies" (not a compound mention of "fun").
_DIMINUTIVE_SUFFIXES: frozenset[str] = frozenset([
    "y", "ie", "ies",
])

# Term-specific root/stem families that begin with the letters of a term but are
# semantically unrelated and should never count as compounds.
_FALSE_PREFIX_STEMS: dict[str, tuple[str, ...]] = {
    "fun": (
        "funn",       # funnel, funneling, funny, ...
        "fund",       # fund, funding, fundraise, ...
        "funct",      # function, functionality, ...
    ),
}

def is_valid_match(word: str, term: str) -> bool:
    """
    Return True if `word` is a valid match for `term` under market rules.

    Examples (term = "joy"):
        "joy"       → True  (exact)
        "joys"      → True  (plural)
        "joyes"     → True  (plural -es)
        "killjoy"   → True  (compound, term at end)
        "joyful"    → False (term + derivational suffix "ful")
        "joyfully"  → False (term + "fully")

    Examples (term = "ass"):
        "ass"          → True  (exact)
        "asses"        → True  (plural)
        "assmunch"     → True  (compound: ass + munch, consonant-initial suffix)
        "asshole"      → True  (compound: ass + hole, consonant-initial suffix)
        "badass"       → True  (compound, term at end)
        "embarrassing" → False (term embedded mid-word with derivational suffix)
        "declassify"   → False (term embedded mid-word with derivational suffix)
        "assign"       → False (Latin prefix "as-" + root, vowel-initial suffix)
        "assume"       → False (Latin prefix "as-" + root, vowel-initial suffix)

    Examples (term = "fun"):
        "fun"          → True  (exact)
        "funhouse"     → True  (compound)
        "fundamental"  → False (false-compound blocklist)
        "function"     → False (false-compound blocklist)
        "funded"       → False (false-compound blocklist)

    Examples (term = "amen"):
        "amen"       → True  (exact)
        "amens"      → True  (plural)
        "amendment"  → False (term at start of a longer root)
        "parliament" → False (term embedded mid-word)
        "tournament" → False (term embedded mid-word)
    """
    if len(word) < len(term):
        return False

    # 0. Explicit false-compound blocklist
    if (word, term) in _FALSE_COMPOUNDS:
        return False

    # 0b. Term-specific stem families that should not count as compounds.
    for stem in _FALSE_PREFIX_STEMS.get(term, ()):
        if word.startswith(stem):
            return False

    # 1. Exact
    if word == term:
        return True

    # 2. Plural
    # +es always accepted (asses, churches, …).
    # +s only accepted when the term does NOT end in "e": adding "s" to an
    # "e"-ending word produces a 3rd-person-singular verb form (collapses,
    # breathes, …), not a plural.  Genuine "e"-word plurals already end in
    # "es" (horses, races) and are caught by the +es branch above.
    if word == term + "es":
        return True
    if word == term + "s" and not term.endswith("e"):
        return True

    # 3. Compound
    pos = word.find(term)
    if pos < 0:
        return False

    after = word[pos + len(term):]

    if pos == 0:
        # Term at start of word.
        # Accept as a compound only when the suffix looks like an independent
        # word, not a derivational morpheme:
        #   - suffix must be at least as long as the term (rules out short
        #     fragments like "et" in "asset")
        #   - suffix must start with a consonant — this filters out Latin-prefix
        #     false positives (assign, assume, assist, assault, assemble…) where
        #     the letters after "ass" always begin with a vowel because they come
        #     from Latin "ad-" assimilation, not the English word "ass".
        #   - suffix must not itself be a derivational suffix
        if not after:
            return False
        # Doubled-consonant inflection (drug→drugged, run→running)
        if after[0] == term[-1] and after[1:] in _DERIVATIONAL_SUFFIXES:
            return False
        # Diminutive/colloquial inflections (fun→funsies, dog→doggie, ...)
        # are not compounds.
        if after[0] == term[-1] and after[1:] in _DIMINUTIVE_SUFFIXES:
            return False
        if after in _DIMINUTIVE_SUFFIXES:
            return False
        if after.startswith("s") and after[1:] in _DIMINUTIVE_SUFFIXES:
            return False
        if after in _DERIVATIONAL_SUFFIXES:
            return False
        if len(after) >= len(term) and after[0] not in _VOWELS:
            return True
        return False

    # Term is in the middle or at the end.
    # Require the prefix (word[:pos]) to be at least as long as the term itself
    # to avoid false positives from short consonant clusters.
    # e.g. "ass" in "class" (pos=2, prefix="cl" len=2 < 3) → rejected.
    #      "ass" in "badass" (pos=3, prefix="bad" len=3 >= 3) → accepted.
    if pos < len(term):
        return False
    # Only allow the term to be at the end of the word, or followed by a simple
    # plural suffix.  Derivational suffixes (e.g. -ing in "embarrassing",
    # -ifying in "declassifying") are NOT accepted here — they mean the term is
    # embedded inside a longer root, not that it's a compound component.
    if after and after not in ("s", "es"):
        return False
    return True


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class EpisodeResult:
    episode_id: int
    title: str
    episode_date: Optional[str]
    episode_number: Optional[int]
    duration_seconds: int
    count: int
    per_minute: float


@dataclass
class MinuteResult:
    minute: int
    count: int


@dataclass
class SearchResult:
    keyword: str
    episodes: list[EpisodeResult] = field(default_factory=list)

    avg_last_1:   Optional[float] = None
    avg_last_5:   Optional[float] = None
    avg_last_20:  Optional[float] = None
    avg_last_50:  Optional[float] = None
    avg_last_100: Optional[float] = None

    avg_pm_last_1:   Optional[float] = None
    avg_pm_last_5:   Optional[float] = None
    avg_pm_last_20:  Optional[float] = None
    avg_pm_last_50:  Optional[float] = None
    avg_pm_last_100: Optional[float] = None

    def episode_by_id(self, episode_id: int) -> Optional[EpisodeResult]:
        for ep in self.episodes:
            if ep.episode_id == episode_id:
                return ep
        return None


# ── Public search function ────────────────────────────────────────────────────

def search(
    db: Database,
    keyword: str,
    episode_ids: Optional[list[int]] = None,
) -> SearchResult:
    """
    Search for `keyword` across all indexed episodes (or a subset).

    Applies exact, plural, and compound matching per the market rules.
    episode_ids filters which episodes to include (None = all).
    """
    term = keyword.strip().lower()

    # Pull all word_frequency rows where the word contains the term
    raw_rows = db.get_words_containing(term, episode_ids=episode_ids)

    # Aggregate per episode
    ep_counts: dict[int, int] = {}
    ep_meta: dict[int, dict] = {}

    for row in raw_rows:
        if not is_valid_match(row["word"], term):
            continue
        eid = row["episode_id"]
        ep_counts[eid] = ep_counts.get(eid, 0) + row["count"]
        if eid not in ep_meta:
            ep_meta[eid] = row

    # Build list of all indexed episodes (so zero-count episodes appear too)
    all_eps = db.get_episode_list_indexed(episode_ids=episode_ids)

    episodes: list[EpisodeResult] = []
    for ep_row in all_eps:
        eid = ep_row["id"]
        dur = ep_row["duration_seconds"] or 0
        cnt = ep_counts.get(eid, 0)
        episodes.append(
            EpisodeResult(
                episode_id=eid,
                title=ep_row["title"],
                episode_date=ep_row["episode_date"],
                episode_number=ep_row["episode_number"],
                duration_seconds=dur,
                count=cnt,
                per_minute=per_minute_rate(cnt, dur),
            )
        )

    result = SearchResult(keyword=term, episodes=episodes)
    _compute_averages(result)
    return result


# ── Average helpers ───────────────────────────────────────────────────────────

def _rolling_avg(episodes: list[EpisodeResult], n: int) -> Optional[float]:
    subset = episodes[:n]
    if not subset:
        return None
    return sum(ep.count for ep in subset) / len(subset)


def _rolling_avg_pm(episodes: list[EpisodeResult], n: int) -> Optional[float]:
    subset = [ep for ep in episodes[:n] if ep.duration_seconds > 0]
    if not subset:
        return None
    return sum(ep.per_minute for ep in subset) / len(subset)


def _compute_averages(result: SearchResult) -> None:
    eps = result.episodes  # newest-first

    for attr, n in [
        ("avg_last_1",   1),
        ("avg_last_5",   5),
        ("avg_last_20",  20),
        ("avg_last_50",  50),
        ("avg_last_100", 100),
    ]:
        setattr(result, attr, _rolling_avg(eps, n))

    result.avg_pm_last_1   = _rolling_avg_pm(eps, 1)
    result.avg_pm_last_5   = _rolling_avg_pm(eps, 5)
    result.avg_pm_last_20  = _rolling_avg_pm(eps, 20)
    result.avg_pm_last_50  = _rolling_avg_pm(eps, 50)
    result.avg_pm_last_100 = _rolling_avg_pm(eps, 100)


# ── Multi-keyword merge ───────────────────────────────────────────────────────

def intersect_results(label: str, results: list[SearchResult]) -> SearchResult:
    """
    AND-merge: only count episodes where ALL keywords have at least one mention.

    For qualifying episodes the count is the sum of all individual keyword counts
    (so FV models "how much combined discussion" given co-occurrence).
    Episodes where any keyword has count == 0 are zeroed out, pulling down the
    lambda estimate and pushing up the zero-inflation term.
    """
    if not results:
        return SearchResult(keyword=label)
    if len(results) == 1:
        r = SearchResult(keyword=label, episodes=results[0].episodes)
        _compute_averages(r)
        return r

    kw_counts: list[dict[int, int]] = [
        {ep.episode_id: ep.count for ep in res.episodes}
        for res in results
    ]

    merged_episodes: list[EpisodeResult] = []
    for ep in results[0].episodes:
        eid = ep.episode_id
        if all(kw.get(eid, 0) > 0 for kw in kw_counts):
            cnt = sum(kw.get(eid, 0) for kw in kw_counts)
        else:
            cnt = 0
        merged_episodes.append(EpisodeResult(
            episode_id=eid,
            title=ep.title,
            episode_date=ep.episode_date,
            episode_number=ep.episode_number,
            duration_seconds=ep.duration_seconds,
            count=cnt,
            per_minute=per_minute_rate(cnt, ep.duration_seconds),
        ))

    merged = SearchResult(keyword=label, episodes=merged_episodes)
    _compute_averages(merged)
    return merged


def merge_results(label: str, results: list[SearchResult]) -> SearchResult:
    """
    Merge multiple SearchResults (one per keyword) by summing episode counts.

    Episode order and metadata are taken from the first result (all results
    cover the same set of indexed episodes in the same newest-first order).
    The merged per_minute rate is recomputed from the summed count.
    """
    if not results:
        return SearchResult(keyword=label)
    if len(results) == 1:
        results[0] = SearchResult(
            keyword=label,
            episodes=results[0].episodes,
        )
        _compute_averages(results[0])
        return results[0]

    # Sum counts per episode across all keyword results
    combined_counts: dict[int, int] = {}
    for res in results:
        for ep in res.episodes:
            combined_counts[ep.episode_id] = (
                combined_counts.get(ep.episode_id, 0) + ep.count
            )

    # Rebuild episodes in the canonical order from the first result
    merged_episodes: list[EpisodeResult] = []
    for ep in results[0].episodes:
        cnt = combined_counts.get(ep.episode_id, 0)
        merged_episodes.append(
            EpisodeResult(
                episode_id=ep.episode_id,
                title=ep.title,
                episode_date=ep.episode_date,
                episode_number=ep.episode_number,
                duration_seconds=ep.duration_seconds,
                count=cnt,
                per_minute=per_minute_rate(cnt, ep.duration_seconds),
            )
        )

    merged = SearchResult(keyword=label, episodes=merged_episodes)
    _compute_averages(merged)
    return merged


def search_multi_adjacent(
    db: Database,
    label: str,
    terms: list[str],
    individual_results: list[SearchResult],
    mode: str = "or",
    episode_ids: Optional[list[int]] = None,
) -> SearchResult:
    """
    Multi-keyword merge that counts adjacent keyword occurrences as one mention.

    When two or more searched terms appear as consecutive tokens in the
    transcript (e.g. "Joe Biden" for terms ["joe", "biden"]), the entire
    contiguous run counts as ONE mention rather than one per keyword.
    Non-adjacent occurrences of each keyword still count individually.

    For AND mode the same zero-episode filter as intersect_results applies:
    episodes missing any keyword are zeroed out.

    individual_results must be the pre-computed per-keyword SearchResults
    (used for fast pre-filtering without re-querying the database).
    """
    # Build per-keyword episode count maps from the pre-computed results.
    kw_ep_counts = [
        {ep.episode_id: ep.count for ep in res.episodes}
        for res in individual_results
    ]

    # For AND mode: derive the set of qualifying episodes (all keywords present).
    if mode == "and":
        sets = [{eid for eid, c in kw.items() if c > 0} for kw in kw_ep_counts]
        qualifying_ids: set[int] = set.intersection(*sets) if sets else set()

    all_eps = db.get_episode_list_indexed(episode_ids=episode_ids)
    episodes: list[EpisodeResult] = []

    for ep_row in all_eps:
        eid = ep_row["id"]
        dur = ep_row["duration_seconds"] or 0

        # AND filter: zero out episodes missing any keyword.
        if mode == "and" and eid not in qualifying_ids:
            episodes.append(EpisodeResult(
                episode_id=eid, title=ep_row["title"],
                episode_date=ep_row["episode_date"],
                episode_number=ep_row["episode_number"],
                duration_seconds=dur, count=0, per_minute=0.0,
            ))
            continue

        # Fast path: skip transcript scan for episodes with no keyword hits at all.
        if not any(kw.get(eid, 0) > 0 for kw in kw_ep_counts):
            episodes.append(EpisodeResult(
                episode_id=eid, title=ep_row["title"],
                episode_date=ep_row["episode_date"],
                episode_number=ep_row["episode_number"],
                duration_seconds=dur, count=0, per_minute=0.0,
            ))
            continue

        # Scan the raw transcript counting contiguous runs of keyword matches.
        # Each run = 1 mention regardless of how many terms appear in the run.
        # in_run resets between segments so words across segment boundaries
        # are not treated as adjacent.
        segments = db.get_transcript(eid)
        count = 0
        for seg in segments:
            in_run = False
            tokens = re.findall(r"[a-z]+", seg.get("text", "").lower())
            for token in tokens:
                if any(is_valid_match(token, t) for t in terms):
                    if not in_run:
                        count += 1
                        in_run = True
                else:
                    in_run = False

        episodes.append(EpisodeResult(
            episode_id=eid, title=ep_row["title"],
            episode_date=ep_row["episode_date"],
            episode_number=ep_row["episode_number"],
            duration_seconds=dur, count=count,
            per_minute=per_minute_rate(count, dur),
        ))

    result = SearchResult(keyword=label, episodes=episodes)
    _compute_averages(result)
    return result


# ── Minute breakdown ──────────────────────────────────────────────────────────

def get_minute_breakdown(
    db: Database, keyword: str, episode_id: int
) -> list[MinuteResult]:
    """Return per-minute counts for a keyword within a specific episode."""
    term = keyword.strip().lower()
    rows = db.get_minute_words_containing(term, episode_id)

    # Aggregate per minute, filtering by valid match
    minute_counts: dict[int, int] = {}
    for row in rows:
        if not is_valid_match(row["word"], term):
            continue
        m = row["minute"]
        minute_counts[m] = minute_counts.get(m, 0) + row["count"]

    return [
        MinuteResult(minute=m, count=c)
        for m, c in sorted(minute_counts.items())
    ]


def get_minute_breakdown_multi_adjacent(
    db: Database,
    terms: list[str],
    episode_id: int,
) -> dict[int, int]:
    """
    Per-minute mention counts for a multi-keyword OR search with adjacent
    deduplication.  Contiguous tokens matching any of `terms` count as ONE
    mention per run, consistent with search_multi_adjacent.
    """
    segments = db.get_transcript(episode_id)
    minute_counts: dict[int, int] = {}
    for seg in segments:
        minute = int(seg.get("start", 0) // 60)
        in_run = False
        tokens = re.findall(r"[a-z]+", seg.get("text", "").lower())
        for token in tokens:
            if any(is_valid_match(token, t) for t in terms):
                if not in_run:
                    minute_counts[minute] = minute_counts.get(minute, 0) + 1
                    in_run = True
            else:
                in_run = False
    return minute_counts


# ── Phrase search (multi-word) ────────────────────────────────────────────────

def _phrase_pattern(phrase: str) -> re.Pattern:
    """Word-boundary regex for a multi-word phrase (case-insensitive)."""
    return re.compile(r"\b" + re.escape(phrase.strip().lower()) + r"\b", re.IGNORECASE)


def phrase_search(
    db: Database,
    phrase: str,
    episode_ids: Optional[list[int]] = None,
) -> SearchResult:
    """
    Count occurrences of a multi-word phrase by scanning raw transcript text.
    Returns a SearchResult compatible with single-keyword results.
    """
    pattern = _phrase_pattern(phrase)
    all_eps = db.get_episode_list_indexed(episode_ids=episode_ids)
    episodes: list[EpisodeResult] = []

    for ep_row in all_eps:
        eid = ep_row["id"]
        segments = db.get_transcript(eid)
        count = sum(len(pattern.findall(seg.get("text", ""))) for seg in segments)
        dur = ep_row["duration_seconds"] or 0
        episodes.append(EpisodeResult(
            episode_id=eid,
            title=ep_row["title"],
            episode_date=ep_row["episode_date"],
            episode_number=ep_row["episode_number"],
            duration_seconds=dur,
            count=count,
            per_minute=per_minute_rate(count, dur),
        ))

    result = SearchResult(keyword=phrase.strip().lower(), episodes=episodes)
    _compute_averages(result)
    return result


def get_phrase_minute_breakdown(
    db: Database, phrase: str, episode_id: int
) -> list[MinuteResult]:
    """Per-minute counts for a phrase, derived from raw transcript segments."""
    pattern = _phrase_pattern(phrase)
    segments = db.get_transcript(episode_id)
    minute_counts: dict[int, int] = {}
    for seg in segments:
        cnt = len(pattern.findall(seg.get("text", "")))
        if cnt:
            minute = int(seg.get("start", 0) // 60)
            minute_counts[minute] = minute_counts.get(minute, 0) + cnt
    return [MinuteResult(minute=m, count=c) for m, c in sorted(minute_counts.items())]


# ── Context (KWIC) ────────────────────────────────────────────────────────────

def get_context(
    db: Database,
    keyword: str,
    episode_id: int,
    context_chars: int = 100,
) -> list[dict]:
    """
    Return KWIC (keyword-in-context) snippets for *keyword* in *episode_id*.

    Works for both single words (using is_valid_match rules) and phrases.
    Each hit: {minute, second, prefix, match, suffix}
    """
    term = keyword.strip().lower()
    is_phrase = " " in term

    if is_phrase:
        pattern = _phrase_pattern(term)
    else:
        # Match any token containing the term as a substring, then filter
        pattern = re.compile(r"\b\w*" + re.escape(term) + r"\w*\b", re.IGNORECASE)

    segments = db.get_transcript(episode_id)
    hits: list[dict] = []

    for seg in segments:
        text = seg.get("text", "")
        for m in pattern.finditer(text):
            if not is_phrase and not is_valid_match(m.group().lower(), term):
                continue
            s = max(0, m.start() - context_chars)
            e = min(len(text), m.end() + context_chars)
            prefix = ("…" if s > 0 else "") + text[s:m.start()]
            suffix = text[m.end():e] + ("…" if e < len(text) else "")
            ts = seg.get("start", 0)
            hits.append({
                "minute": int(ts // 60),
                "second": int(ts % 60),
                "prefix": prefix,
                "match":  m.group(),
                "suffix": suffix,
            })

    return hits
