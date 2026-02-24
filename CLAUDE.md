# CLAUDE.md — JRE Keyword Analyzer

This file provides a comprehensive reference for AI assistants working in this
repository. Read it before making any changes.

---

## Project Purpose

**JRE Keyword Analyzer** is a tool for tracking how often keywords are
mentioned across Joe Rogan Experience (JRE) podcast transcripts. The primary
use case is helping Polymarket prediction-market traders estimate fair values
for "Will keyword X be mentioned ≥ N times on JRE?" markets.

The application has two interfaces:

- **Web UI** — Flask server + vanilla-JS single-page app (`static/index.html`)
- **CLI** — `main.py` command-line tool

---

## Repository Layout

```
/
├── main.py               # CLI entry point (upload / index / search / info)
├── server.py             # Flask web server (all REST API endpoints)
├── export_data.py        # Legacy script to export DB → data.json (outdated schema)
├── requirements.txt      # Python dependencies
├── Dockerfile            # Python 3.11-slim image; DB at /app/data/jre_data.db
├── docker-compose.yml    # Single-service compose with named volume for DB
├── Procfile              # Railway/Heroku: gunicorn server:app
├── package.json          # Only used for `npx serve` (static-site fallback)
├── .gitignore            # Ignores *.db, __pycache__, venv, charts/
├── static/
│   └── index.html        # Full SPA — all CSS and JS inlined in one file
└── jre_analyzer/
    ├── __init__.py
    ├── analyzer.py        # Tokenization + frequency index builder
    ├── database.py        # SQLite persistence layer (Database class)
    ├── fair_value.py      # Polymarket fair-value probability calculator
    ├── fetch_transcripts.py  # .txt transcript file parser
    ├── search.py          # Keyword search with matching rules
    └── visualize.py       # Matplotlib chart generation (CLI only)
```

---

## Tech Stack

| Layer        | Technology                                                      |
|--------------|-----------------------------------------------------------------|
| Language     | Python 3.11+                                                    |
| Web framework| Flask 3.0+, Gunicorn 21+                                        |
| Database     | SQLite (stdlib `sqlite3`, WAL mode, `check_same_thread=False`)  |
| Statistics   | NumPy, SciPy (`scipy.stats.nbinom`)                             |
| Charts (CLI) | Matplotlib                                                      |
| Frontend     | Vanilla HTML/CSS/JS — **no build step**, no npm packages        |
| Chart lib    | Chart.js 4.4.0 loaded from CDN in `static/index.html`          |
| Deployment   | Docker / Railway / Heroku-style (`Procfile`)                    |

---

## Development Setup

```bash
# Install dependencies (no build step required)
pip install -r requirements.txt

# Run the web server (localhost:5000)
python server.py

# Or use the CLI
python main.py --help
```

There is **no `.env` file** and **no secrets required** to run locally. The
only runtime configuration is:

| Variable | Default          | Purpose                            |
|----------|------------------|------------------------------------|
| `DB_PATH`| `jre_data.db`    | Path to the SQLite database file   |
| `PORT`   | `5000`           | Server listen port                 |

### Docker

```bash
docker-compose up --build
# Database is persisted in Docker volume 'jre_data' at /app/data/jre_data.db
```

---

## CLI Commands (`main.py`)

```bash
python main.py upload <file.txt> --title "JRE #2200 - Guest" --date 2026-02-24
python main.py index
python main.py search "DMT"
python main.py search "aliens" --lookback 50 --show-chart
python main.py info
```

All commands accept `--db <path>` to point at a non-default database.

---

## REST API (`server.py`)

| Method | Route                         | Description                                     |
|--------|-------------------------------|-------------------------------------------------|
| GET    | `/`                           | Serve `static/index.html`                       |
| GET    | `/api/status`                 | Total episode count + latest episode            |
| GET    | `/api/episodes`               | All episodes (metadata only, no transcripts)    |
| POST   | `/api/upload`                 | Upload one or more `.txt` transcript files      |
| DELETE | `/api/episode/<id>`           | Delete episode + all its frequency data         |
| GET    | `/api/search`                 | Keyword search with fair-value output           |
| GET    | `/api/minutes`                | Per-minute mention breakdown for one episode    |
| GET    | `/api/context`                | KWIC (keyword-in-context) snippets              |
| POST   | `/api/reindex`                | Drop + rebuild entire frequency index           |

### `/api/search` query parameters

| Parameter    | Default | Notes                                                    |
|--------------|---------|----------------------------------------------------------|
| `keyword`    | —       | Required. Comma-separated for multi-keyword queries.     |
| `lookback`   | `20`    | Episodes used for fair-value calculation.                |
| `mode`       | `or`    | `"or"` (union) or `"and"` (intersection).               |
| `episode_ids`| —       | Optional comma-separated episode ID filter.              |

---

## Database Schema (`jre_analyzer/database.py`)

```sql
episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,           -- e.g. "JRE #2200 – Guest Name"
    episode_date     TEXT,                    -- ISO YYYY-MM-DD, nullable
    filename         TEXT,
    uploaded_at      TEXT,                    -- ISO datetime (UTC)
    duration_seconds INTEGER DEFAULT 0,       -- from last transcript timestamp
    episode_number   INTEGER,                 -- parsed from title (#NNNN)
    transcript_json  TEXT NOT NULL DEFAULT '[]',  -- JSON [{start, text}, …]
    indexed_at       TEXT                     -- NULL until indexed
)

word_frequencies (
    episode_id  INTEGER NOT NULL,
    word        TEXT NOT NULL,                -- lowercase, no stemming
    count       INTEGER NOT NULL,
    PRIMARY KEY (episode_id, word)
)

minute_frequencies (
    episode_id  INTEGER NOT NULL,
    minute      INTEGER NOT NULL,             -- floor(start_seconds / 60)
    word        TEXT NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (episode_id, minute, word)
)
```

Indexes: `idx_wf_word`, `idx_mf_word`, `idx_ep_date`, `idx_ep_num`.

Episodes are always ordered **newest-first** (`episode_date DESC NULLS LAST, id DESC`).

---

## Core Business Logic

### Transcript Format

Uploaded `.txt` files must have this format (produced by yt-dlp or manual export):

```
Episode Date: February 5, 2026

Starting point is 00:04:46
This is the spoken text for this segment...

Starting point is 00:05:04
More spoken text here...
```

- `Episode Date: Month D, YYYY` is auto-detected from the first 4000 characters.
- All segments before (and including) the JRE intro phrase
  `"train by day, joe rogan podcast by night, all day"` are stripped.
- Duration is estimated as `last_timestamp_seconds + 60`.

### Keyword Matching Rules (`jre_analyzer/search.py:is_valid_match()`)

A stored word counts as a match for search term `T` if:

1. **Exact**: `word == T`
2. **Plural**: `word == T + "es"` always; `word == T + "s"` only if `T` does
   not end in `"e"` (avoids matching 3rd-person verbs like "breathes").
3. **Compound**: `T` appears as a substring of `word`, AND:
   - If `T` is at the **start** of `word`: the suffix must be ≥ len(T) long,
     must start with a consonant (filters Latin-prefix false positives), and
     must not be a derivational suffix (e.g., "joyful" is rejected for "joy").
   - If `T` is in the **middle or end**: the prefix before `T` must be ≥
     len(T) long; the suffix after `T` must be empty, `"s"`, or `"es"` only.
4. **False-compound blocklist**: explicit `(word, term)` pairs where the
   heuristic would fire but the etymology is unrelated (e.g., "thirteen" for
   "teen").

Words are stored **raw (lowercase, no stemming)** at index time. All matching
happens at query time.

**No speaker attribution** — all words from any speaker count equally.

### Multi-keyword Queries

- Comma-separate terms: `"joe, biden"` or `"million, billion"`.
- **OR mode** (default): counts per episode are summed across keywords.
- **AND mode**: only episodes where every keyword has ≥ 1 mention; count is
  the sum of all keyword counts.
- **Adjacent deduplication** (single-word multi-keyword only): consecutive
  tokens that all match any searched term count as **one** mention, not one
  per keyword. E.g. "Joe Biden" = 1 mention for `["joe", "biden"]`.
- **Phrase search** (term containing a space): scans raw transcript text with
  a word-boundary regex rather than the frequency index.

### Indexing Pipeline

1. `parse_transcript_txt()` → list of `{start, text}` segments.
2. `tokenize()` → `re.findall(r"[a-z]+", text.lower())` (no stemming).
3. `build_frequencies()` → `{word: count}` + `{minute: {word: count}}`.
4. Results written to `word_frequencies` and `minute_frequencies` tables.
5. Episode `indexed_at` is stamped with UTC datetime.

Indexing happens automatically on upload via the web API. The CLI requires a
separate `python main.py index` step.

### Fair Value Models (`jre_analyzer/fair_value.py`)

Model selection (best → fallback), applied per search:

| Priority | Model                          | Condition                                               |
|----------|--------------------------------|---------------------------------------------------------|
| 1        | Zero-Inflated Neg-Binomial (ZINB) | Overdispersed AND zero_fraction ≥ 0.25 AND π ≥ 0.05 |
| 2        | Negative Binomial              | Overdispersed (variance > mean × 1.2)                  |
| 3        | Empirical                      | lookback_episodes ≥ 10                                  |
| 4        | Poisson                        | Fallback                                                |

- **Overdispersed**: `variance > mean * 1.2`.
- Duration normalization: counts are scaled to a reference duration (median
  episode length) before fitting to account for varying episode lengths.
- `MAX_BUCKET = 25`; bucket 25 means "≥ 25 mentions".

---

## Frontend (`static/index.html`)

- **Single self-contained file** — all CSS variables, styles, and JavaScript
  are inline. No build tools, no npm, no bundler.
- Dark theme with CSS custom properties (`--bg`, `--surface`, `--card`, etc.).
- Chart.js is loaded from CDN (`cdn.jsdelivr.net`).
- OR/AND mode toggle is hidden by default and shown via JS when 2+ keywords
  are entered.
- All API calls go to the same origin (`/api/…`).

When editing `static/index.html`, keep all styles and scripts inline — do not
split into separate files unless explicitly requested.

---

## Stopwords

The stopwords list is defined in **two places** and must be kept in sync:

- `jre_analyzer/analyzer.py:STOPWORDS` — used during indexing.
- `export_data.py:STOPWORDS` — used during static export.

---

## Testing

There is **no test suite** in this repository. When adding tests, use
`pytest` (not included in `requirements.txt`; add it if needed).

---

## Linting / Formatting

There is no linting or formatting configuration (no `pyproject.toml`,
`.flake8`, or `.pylintrc`). The code style follows standard Python conventions:

- Type annotations throughout.
- `from __future__ import annotations` at the top of each module.
- Dataclasses (`@dataclass`) for data structures.
- `Optional[X]` for nullable values.
- Module-level `logger = logging.getLogger(__name__)`.

---

## Deployment

### Railway / Heroku

```
Procfile: web: gunicorn server:app --bind 0.0.0.0:$PORT
```

Set `DB_PATH` to a path on a persistent volume (e.g. `/app/data/jre_data.db`).

### Docker

```bash
docker-compose up --build
```

The `Dockerfile` uses `python:3.11-slim`, installs from `requirements.txt`,
exposes `$PORT` (default 5000), and runs 2 Gunicorn workers.

---

## Known Caveats

- **`export_data.py` is outdated**: it references a `video_id` column that no
  longer exists in the current schema (current schema uses integer `id`). This
  script will fail against the live database and should be updated or removed
  before use.
- **No authentication**: the API has no auth. Anyone with network access can
  upload, delete, or reindex data.
- **SQLite concurrency**: the database uses WAL mode and
  `check_same_thread=False`. This works for low-traffic use; a high-traffic
  deployment would benefit from a proper RDBMS.
- **Phrase search performance**: `phrase_search()` and `get_context()` scan
  raw transcript JSON for every episode on every query. Large databases with
  many episodes will be slow for phrase queries.
