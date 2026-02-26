# JRE Main

JRE Main is a transcript analytics app for exploring keyword and phrase mentions in uploaded Joe Rogan Experience transcript text files.

## What it does

- Upload one or many transcript `.txt` files.
- Parse timestamped transcript segments.
- Build per-episode and per-minute word-frequency indexes.
- Search for:
  - single words,
  - phrases,
  - comma-separated multi-keyword queries.
- Compute fair-value probability buckets for mention-count style markets.
- Show minute-level timeline and keyword-in-context snippets.

## Architecture

- `server.py`: Flask API + static asset hosting.
- `static/index.html`: browser UI.
- `jre_analyzer/database.py`: SQLite persistence and query layer.
- `jre_analyzer/fetch_transcripts.py`: transcript/date parsing.
- `jre_analyzer/analyzer.py`: tokenization + index writes.
- `jre_analyzer/search.py`: matching and aggregation logic.
- `jre_analyzer/fair_value.py`: distribution model and probabilities.
- `main.py`: CLI entry point.

## Run locally

### Requirements

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Start web server

```bash
python server.py
```

Open: `http://localhost:5000`

### CLI entry point

```bash
python main.py info
python main.py upload <path/to/transcript.txt> --title "JRE #2200" --date 2026-02-24
python main.py search "aliens" --lookback 50
```

## API notes

### `GET /api/search`

Query params:

- `keyword` (required): single term, phrase, or comma-separated terms.
- `lookback` (optional): integer or `all`.
- `mode` (optional): `or` or `and`.
- `episode_ids` (optional): comma-separated integer IDs.

Behavior:

- Invalid `mode` now returns HTTP 400 with a descriptive message.
- Empty keyword lists now return HTTP 400 consistently across search/context/minute endpoints.

### `GET /api/minutes`

Query params:

- `keyword` (required)
- `episode_id` (required, integer)
- `mode` (optional): `or` or `and`

### `GET /api/context`

Query params:

- `keyword` (required)
- `episode_id` (required, integer)

## Development checks

```bash
python -m compileall -q .
```

## Environment

- Default DB path: `jre_data.db`
- Override with env var: `DB_PATH`
