# CLAUDE.md — JRE Transcript Analyzer

This file provides guidance for AI assistants (Claude and others) working in this repository. It describes the codebase structure, development workflows, and conventions to follow.

---

## Project Overview

This is a **JRE (Joe Rogan Experience) transcript analysis tool** designed for use alongside Polymarket prediction markets. It tracks keyword mentions across podcast episodes and uses statistical models (Poisson, Negative Binomial, Zero-Inflated Negative Binomial) to estimate fair-value probabilities for keyword-frequency markets.

**Key capabilities:**
- Parse and store podcast transcript `.txt` files in SQLite
- Full-text keyword search with exact, plural, and compound-word matching (per Polymarket rules)
- Statistical fair-value estimation for prediction markets
- Per-minute breakdowns and rolling averages
- Visualization via Matplotlib (PNG charts) and an interactive Chart.js web frontend
- Dual interface: REST API web server + CLI

---

## Repository Structure

```
/
├── main.py                  # CLI entry point (argparse)
├── server.py                # Flask REST API server
├── export_data.py           # Export DB to data.json (GitHub Pages)
├── index.html               # Frontend SPA (Vanilla JS + Chart.js)
├── requirements.txt         # Python dependencies
├── package.json             # npm config (static-serve fallback)
├── Dockerfile               # Python 3.11-slim container
├── docker-compose.yml       # Docker Compose with persistent volume
├── Procfile                 # Railway deployment command
├── .gitignore
│
└── jre_analyzer/            # Core package
    ├── __init__.py
    ├── analyzer.py          # Tokenization and word-frequency indexing
    ├── database.py          # SQLite ORM layer (custom, no SQLAlchemy)
    ├── search.py            # Keyword search engine + rolling stats
    ├── fair_value.py        # Polymarket probability models
    ├── fetch_transcripts.py # Transcript text parser
    └── visualize.py         # Matplotlib chart generation
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| Web framework | Flask ≥ 3.0.0 |
| WSGI server | Gunicorn ≥ 21.2.0 |
| Database | SQLite (WAL mode, custom ORM) |
| Statistics | NumPy ≥ 1.26.0, SciPy ≥ 1.11.0 |
| Visualization | Matplotlib ≥ 3.8.0 |
| CLI formatting | Tabulate, Colorama |
| Frontend | Vanilla JS, Chart.js v4.4.0 (CDN) |
| Containerization | Docker, Docker Compose |
| Deployment | Railway (Procfile), Docker |

---

## Architecture

```
SPA Frontend (index.html)
  └── HTTP/JSON (REST API)
        └── Flask Server (server.py)
              ├── Analyzer (analyzer.py)   — tokenize, build indexes
              ├── Search Engine (search.py) — keyword matching + stats
              ├── Fair Value (fair_value.py) — probability models
              └── Database (database.py)   — SQLite ORM (WAL mode)

CLI (main.py) → same business logic modules → same Database layer
```

All business logic lives in `jre_analyzer/`. Both `server.py` (web) and `main.py` (CLI) are thin shells that delegate into the package.

---

## Database Schema

SQLite database (`jre_data.db` by default, `/app/data/jre_data.db` in Docker).

```sql
episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    episode_date     TEXT,              -- ISO YYYY-MM-DD
    filename         TEXT,
    uploaded_at      TEXT,              -- ISO datetime
    duration_seconds INTEGER DEFAULT 0,
    episode_number   INTEGER,           -- Parsed from title
    transcript_json  TEXT NOT NULL DEFAULT '[]',
    indexed_at       TEXT               -- Set after indexing
)

word_frequencies (
    episode_id  INTEGER,
    word        TEXT,
    count       INTEGER DEFAULT 0,
    PRIMARY KEY (episode_id, word)
)

minute_frequencies (
    episode_id  INTEGER,
    minute      INTEGER,
    word        TEXT,
    count       INTEGER DEFAULT 0,
    PRIMARY KEY (episode_id, minute, word)
)

-- Indexes
idx_wf_word  ON word_frequencies(word)
idx_mf_word  ON minute_frequencies(word)
idx_ep_date  ON episodes(episode_date)
idx_ep_num   ON episodes(episode_number)
```

**ORM conventions:**
- Custom Python ORM, not SQLAlchemy.
- Row factory returns dict-like objects.
- WAL mode + foreign key enforcement enabled on every connection.
- `check_same_thread=False` — connection is not thread-safe by design; Flask uses per-request connections.

---

## Running the Application

### Development (local)

```bash
# Install dependencies
pip install -r requirements.txt

# Start Flask dev server (port 5000)
python server.py

# Open http://localhost:5000
```

### CLI

```bash
# Upload a transcript file
python main.py upload transcript.txt --title "JRE #1234" --date 2024-01-15

# Build/rebuild word-frequency indexes
python main.py index

# Search for a keyword
python main.py search "bitcoin" --lookback 20

# Database summary
python main.py info

# Custom DB path
python main.py --db /path/to/custom.db <command>
```

**CLI flags:**
- `--db` — path to SQLite DB file (default: `jre_data.db`)
- `--title` — episode title override
- `--date` — episode date `YYYY-MM-DD`
- `--top N` — episodes to show (default: 20)
- `--lookback N` — episodes used for fair-value model (default: 20)
- `--episode-id N` — show per-minute breakdown for a specific episode
- `--show-chart` — display charts interactively
- `--save-chart` — save charts to `./charts/` (default: enabled)
- `--minute-chart` — generate per-minute chart

### Docker

```bash
# Build and start (with persistent DB volume)
docker compose up --build

# Or run directly
docker build -t jre-analyzer .
docker run -p 5000:5000 -v jre_data:/app/data jre-analyzer
```

### Production (Railway)

Deployed via the `Procfile`:
```
web: gunicorn server:app --bind 0.0.0.0:$PORT
```

---

## REST API Reference

All endpoints return JSON.

### `GET /api/status`
Database summary: total episodes, latest episode info.

### `GET /api/episodes`
List all uploaded episodes (metadata only, no transcript bodies).

### `POST /api/upload`
Upload one or more `.txt` transcript files.

```
Content-Type: multipart/form-data
Fields:
  files[]         File[]   (required) .txt transcript files
  title[]         string[] (optional) episode titles
  episode_date[]  string[] (optional) ISO dates YYYY-MM-DD

Response:
  { "created": [...], "errors": [...] }
```

### `DELETE /api/episode/<id>`
Delete an episode and its associated word/minute-frequency rows.

### `GET /api/search`
Keyword search with statistics and fair-value output.

```
Query params:
  keyword     string   (required) single term or comma-separated phrases
  lookback    int      (default 20) episodes for statistical model
  mode        string   "or" (default) | "and" for multi-keyword
  episode_ids string   (optional) comma-separated IDs to filter

Response shape:
  {
    "keyword": string,
    "mode": "or" | "and",
    "episodes": [ { episode_id, title, episode_date, count, per_minute, ... } ],
    "averages": { "last_1", "last_5", "last_20", "last_50", "last_100" },
    "averages_per_min": { ... },
    "fair_value": {
      "lambda", "mean", "std_dev", "model",
      "pi_estimate", "zero_fraction", "lookback_episodes",
      "reference_minutes",
      "buckets": [ { "n", "label", "pmf", "sf", "pct" } ]
    },
    "per_keyword": [...],
    "per_keyword_fv": [...]
  }
```

### `GET /api/minutes`
Per-minute keyword breakdown for a specific episode.
```
Query params: keyword, episode_id
```

### `GET /api/context`
KWIC (keyword-in-context) display for a specific episode.
```
Query params: keyword, episode_id, window (default 5)
```

### `POST /api/reindex`
Clear and rebuild all word-frequency and minute-frequency indexes.

---

## Key Modules

### `jre_analyzer/analyzer.py`
- `Analyzer` class: tokenizes transcript text and builds `word_frequencies` and `minute_frequencies`.
- Tokenization: lowercases, strips punctuation, keeps alphanumeric + hyphens. Hyphenated compounds are indexed both as a compound and as individual words.
- `index_episode(episode_id)` — main entry point, reads `transcript_json`, counts words per minute.

### `jre_analyzer/database.py`
- `Database` class: wraps SQLite connection, manages schema creation.
- Methods: `get_episode`, `get_all_episodes`, `save_episode`, `delete_episode`, `get_word_frequencies`, `get_minute_frequencies`, `upsert_word_frequency`, etc.
- Always call `db.close()` or use as a context manager to avoid WAL lock issues.

### `jre_analyzer/search.py`
- `SearchEngine` class: performs keyword lookups across `word_frequencies`.
- Supports exact matching, automatic plural detection, and compound-word matching.
- Matching rules follow Polymarket resolution criteria (documented inline).
- `search(keyword, lookback, mode, episode_ids)` returns a `SearchResult` dataclass.
- Rolling averages computed over last 1, 5, 20, 50, 100 episodes.

### `jre_analyzer/fair_value.py`
- Implements three probability models for count data:
  - **Poisson** — standard count model
  - **Negative Binomial (NB)** — overdispersed count model
  - **Zero-Inflated Negative Binomial (ZINB)** — for series with many zero-count episodes
- `compute_fair_value(counts, lookback, reference_minutes)` selects the best-fit model automatically.
- Returns a `FairValueResult` dataclass with PMF buckets, CDF/SF values, and market-style percentage labels.

### `jre_analyzer/fetch_transcripts.py`
- `parse_transcript(text)` — parses raw `.txt` transcript files into a list of `{minute: int, text: str}` dicts.
- Expects transcript lines in `[MM:SS]` or `[HH:MM:SS]` timestamp format.

### `jre_analyzer/visualize.py`
- `generate_chart(search_result, ...)` — creates Matplotlib PNG charts of keyword frequency over time.
- Saves to `./charts/` or returns as a bytes buffer for API responses.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `jre_data.db` | Path to the SQLite database file |
| `PORT` | `5000` | Port for web server / static server |
| `FLASK_ENV` | (Flask default) | Set to `production` in prod |

No `.env` file is required. Variables are passed via shell or Docker environment.

---

## Code Conventions

### Python style
- Python 3.11+. Use type hints on all new functions.
- Dataclasses (`@dataclass`) for structured return types (e.g., `SearchResult`, `FairValueResult`, `EpisodeResult`).
- Defensive null checks are expected — episodes may lack dates or episode numbers.
- All DB operations go through `database.py`. No raw SQL in `server.py` or `main.py`.
- Keep business logic inside `jre_analyzer/`; keep `server.py` and `main.py` as thin routing/CLI shells.

### Keyword matching
- All keyword matching must follow Polymarket resolution rules documented in `search.py`.
- Do not silently broaden matches (e.g., substring matching). Only exact, plural, and compound forms are valid.
- Changes to matching logic require updating the inline documentation in `search.py`.

### Statistical models
- The fair-value model selection logic is in `fair_value.py`. Understand the ZINB/NB/Poisson selection criteria before modifying.
- `reference_minutes` normalizes counts across episodes of different lengths. Always pass it when computing per-minute fair values.

### Frontend (index.html)
- Single-file SPA — all HTML, CSS, and JS live in `index.html` (also mirrored to `static/index.html`).
- No build step. Chart.js loaded from CDN.
- Dark-themed CSS using CSS custom properties (`--bg-primary`, `--text-primary`, etc.).
- Keep JavaScript vanilla — do not introduce npm-bundled frameworks.

### Database
- Always enable WAL mode and foreign keys (done in `Database.__init__`).
- Prefer bulk upserts over individual inserts when indexing transcripts.
- Do not store transcript text in `word_frequencies` — only counts. Full text stays in `transcript_json` on the `episodes` row.

---

## Testing

There is no automated test suite currently. When adding features:
- Test transcript upload and indexing via the CLI (`python main.py upload ... && python main.py index`).
- Verify search results against known keyword counts manually.
- For statistical model changes, validate PMF bucket totals sum to ~1.0.
- Chart generation can be spot-checked with `--show-chart` in the CLI.

When a test suite is added, place tests in a `tests/` directory and use `pytest`.

---

## Deployment Notes

### Docker
- The database is stored in a Docker volume (`jre_data`) mounted at `/app/data/`.
- Do not store the DB inside the container image layer.
- 2 Gunicorn workers by default; increase for higher concurrency.

### Railway
- Set `DB_PATH` environment variable to a persistent disk path.
- The `PORT` variable is automatically injected by Railway.

### GitHub Pages (static export)
- Run `python export_data.py` to generate `data.json`.
- Deploy `index.html` + `data.json` as a static site.

---

## Common Tasks

**Add a new API endpoint:**
1. Add route in `server.py`.
2. Add business logic to the appropriate module in `jre_analyzer/`.
3. Update `database.py` if new DB queries are needed.
4. Update this file's API reference section.

**Change keyword matching behavior:**
1. Edit `jre_analyzer/search.py`.
2. Update the Polymarket rules documentation in the function docstring.
3. Re-run indexing (`python main.py index` or `POST /api/reindex`) after changes.

**Add a new statistical model:**
1. Implement the PMF and SF functions in `jre_analyzer/fair_value.py`.
2. Add model selection logic to `compute_fair_value()`.
3. Update the `FairValueResult.model` field documentation.

**Update the frontend:**
1. Edit `index.html`.
2. Copy to `static/index.html` to keep them in sync: `cp index.html static/index.html`.

**Export data for static hosting:**
```bash
python export_data.py
```
