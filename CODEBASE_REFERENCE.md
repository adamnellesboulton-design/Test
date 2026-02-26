# Codebase Reference Notes

This document is a quick reference for working on the JRE Transcript Analyzer codebase across separate context windows.

## High-level architecture

- **`server.py`**: Flask API + static file host for the browser UI.
- **`static/index.html`**: Main web UI (uploads, search, charts, table rendering).
- **`jre_analyzer/database.py`**: SQLite data access layer (episodes, per-episode word frequencies, per-minute frequencies).
- **`jre_analyzer/fetch_transcripts.py`**: Transcript text parsing helpers, including date extraction.
- **`jre_analyzer/analyzer.py`**: Tokenization + indexing logic to build frequency tables.
- **`jre_analyzer/search.py`**: Search/matching logic and result aggregation (single keyword, phrase, multi-keyword, set operations).
- **`jre_analyzer/fair_value.py`**: Probability model/fair-value computations for mention-count markets.
- **`jre_analyzer/visualize.py`**: Optional plotting for CLI charts.
- **`main.py`**: CLI entrypoint for upload/index/search/info flows.

## Request flow (web)

1. Browser uploads transcript(s) to `POST /api/upload`.
2. Server parses transcript into timestamped segments and inferred duration.
3. Episode is inserted into SQLite; indexing runs immediately.
4. Indexing writes:
   - episode-level word counts (`word_frequencies` table)
   - minute-level word counts (`minute_frequencies` table)
5. `GET /api/search` performs keyword/phrase logic, returns counts + averages + fair-value bucket stats.
6. `GET /api/minutes` and `GET /api/context` return chart/KWIC detail for selected episode(s).

## Data model essentials

From `database.py` behavior, core persisted entities are:

- **episodes**: metadata + serialized transcript JSON + duration + indexed timestamp.
- **word frequencies**: `(episode_id, word, count)` for total per-episode mentions.
- **minute frequencies**: `(episode_id, minute, word, count)` for timeline charts.

`indexed_at` indicates whether frequency tables are already built.

## Search behavior summary

- Single-token query uses token-level matching rules (exact/plural/compound policy in `search.py` + `analyzer.py` docs).
- Phrase query (`" "` present) searches raw transcript text.
- Comma-separated keywords create per-keyword result sets and merged/intersection output depending on mode:
  - `mode=or`: union behavior
  - `mode=and`: intersection behavior
- For multi single-word OR searches, adjacent-occurrence dedup is applied to avoid double-counting sequences like "Joe Biden" as two mentions.

## Fair-value outputs

`fair_value.py` can return Poisson/empirical/negative-binomial/zero-inflated variants depending on sample size and overdispersion signals. API responses include:

- expected rate and moments
- selected model label
- bucket probabilities for `P(N >= k)` where `k = 1..MAX_BUCKET`

## CLI commands (`main.py`)

- `upload`: parse + store transcript then index episode.
- `index`: index all unindexed episodes.
- `search`: show episode table, rolling averages, optional charts/minute breakdown.
- `info`: print database summary and recent episodes.

## Operational notes

- DB path defaults to `jre_data.db` (`DB_PATH` env var can override).
- Server runs default Flask dev mode at `http://localhost:5000`.
- Static UI is served from `static/`.
- Transcript parsing is resilient to decode errors (`errors="replace"`).

## Recent bug fix note

In `server.py` upload handler, the API documented support for multipart `episode_date[]`, but the value was ignored and only auto-detection from transcript content was used. The handler now:

1. reads `episode_date[]`
2. uses submitted date when present
3. falls back to transcript date extraction when not present

This keeps implementation aligned with API docs and expected UI/API behavior.


## Recent hardening + streamlining updates

- `server.py` now centralizes request parsing helpers for:
  - `mode` validation (`or`/`and` only),
  - comma-split keyword term parsing,
  - optional episode-id list parsing,
  - per-term search result construction.
- Shared helper usage removed duplicated parsing logic across `/api/search`, `/api/minutes`, and `/api/context`.
- API validation is now more consistent:
  - invalid mode values return `400`,
  - empty parsed keyword sets return `400` on all keyword-driven endpoints.

These changes reduce repeated logic, improve maintainability, and tighten input validation behavior without changing core search semantics.
