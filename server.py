#!/usr/bin/env python3
"""
Flask web server for the JRE Transcript Analyzer.

Run:
    python server.py
Then open: http://localhost:5000

All transcript data is user-uploaded via the web UI (.txt files).
No automatic YouTube fetching. All uploads are shared across users.
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_episode, index_all
from jre_analyzer.search import (
    search, get_minute_breakdown, merge_results, intersect_results,
    phrase_search, get_phrase_minute_breakdown, get_context,
    get_context_multi_adjacent,
    search_multi_adjacent, get_minute_breakdown_multi_adjacent,
)
from jre_analyzer.fair_value import calculate_fair_value, recommended_pmf, recommended_sf, MAX_BUCKET
from jre_analyzer.fetch_transcripts import parse_transcript_txt, extract_episode_date

app = Flask(__name__, static_folder="static", static_url_path="/static")

DB_PATH = Path(os.environ.get("DB_PATH", "jre_data.db"))
db = Database(db_path=DB_PATH)
VALID_MODES = {"or", "and"}
INLINE_INDEX_MAX_FILES = int(os.environ.get("INLINE_INDEX_MAX_FILES", "1"))


def _index_episodes_background(episode_ids: list[int]) -> None:
    """Index uploaded episodes on a separate DB connection."""
    bg_db = Database(db_path=DB_PATH)
    try:
        for episode_id in episode_ids:
            index_episode(bg_db, episode_id)
    finally:
        bg_db.close()


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify({"error": exc.description or exc.name}), exc.code
    return exc


@app.errorhandler(Exception)
def handle_unexpected_exception(exc: Exception):
    if request.path.startswith("/api/"):
        app.logger.exception("Unhandled API error", exc_info=exc)
        return jsonify({"error": "Internal server error"}), 500
    raise exc


def _parse_mode(raw_mode: str) -> str | None:
    mode = raw_mode.strip().lower()
    return mode if mode in VALID_MODES else None


def _parse_terms(keyword: str) -> list[str]:
    return [kw.strip() for kw in keyword.split(",") if kw.strip()]


def _parse_episode_ids(ep_ids_raw: str) -> list[int] | None:
    cleaned = ep_ids_raw.strip()
    if not cleaned:
        return None
    return [int(x) for x in cleaned.split(",") if x.strip()]


def _build_individual_results(terms: list[str], episode_ids: list[int] | None):
    return [
        phrase_search(db, t, episode_ids=episode_ids) if " " in t
        else search(db, t, episode_ids=episode_ids)
        for t in terms
    ]


# ── Static files ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── API: status ───────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    total = db.count_episodes()
    eps   = db.get_all_episodes(limit=1)
    latest = eps[0] if eps else None
    return jsonify({
        "total_episodes": total,
        "latest_title":   latest["title"]        if latest else None,
        "latest_date":    latest["episode_date"]  if latest else None,
    })


# ── API: episodes list ────────────────────────────────────────────────────────

@app.route("/api/episodes")
def api_episodes():
    """Return all uploaded episodes with metadata (no transcript body)."""
    eps = db.get_all_episodes()
    return jsonify([
        {
            "id":               ep["id"],
            "title":            ep["title"],
            "episode_date":     ep["episode_date"],
            "episode_number":   ep["episode_number"],
            "filename":         ep["filename"],
            "uploaded_at":      ep["uploaded_at"],
            "duration_seconds": ep["duration_seconds"],
            "indexed":          ep["indexed_at"] is not None,
        }
        for ep in eps
    ])


# ── API: upload transcripts ───────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Upload one or more .txt transcript files.

    Multipart form fields:
      files[]      — one or more .txt files (required)
      title[]      — episode title for each file (optional; defaults to filename)
      episode_date[] — ISO date YYYY-MM-DD for each file (optional)

    Returns list of created episode objects.
    """
    files = request.files.getlist("files[]")
    titles = request.form.getlist("title[]")
    episode_dates = request.form.getlist("episode_date[]")

    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided"}), 400

    created = []
    errors  = []
    deferred_episode_ids: list[int] = []
    inline_index = len(files) <= INLINE_INDEX_MAX_FILES

    for i, f in enumerate(files):
        filename = f.filename or f"upload_{i+1}.txt"
        title    = titles[i] if i < len(titles) and titles[i].strip() else Path(filename).stem

        try:
            content = f.read().decode("utf-8", errors="replace")
        except Exception as exc:
            errors.append({"filename": filename, "error": f"Could not read file: {exc}"})
            continue

        # Prefer explicitly submitted date; otherwise auto-detect from transcript
        # content ("Episode Date: Month D, YYYY").
        submitted_date = episode_dates[i].strip() if i < len(episode_dates) else ""
        date = submitted_date or extract_episode_date(content)

        if not content.strip():
            errors.append({"filename": filename, "error": "File is empty"})
            continue

        try:
            segments, duration = parse_transcript_txt(content)
        except Exception as exc:
            errors.append({"filename": filename, "error": f"Parse error: {exc}"})
            continue

        try:
            episode_id = db.insert_episode(
                title=title,
                transcript=segments,
                episode_date=date,
                filename=filename,
                duration_seconds=duration,
            )

            if inline_index:
                indexed = index_episode(db, episode_id)
            else:
                indexed = False
                deferred_episode_ids.append(episode_id)
            ep = db.get_episode(episode_id)
            if ep is None:
                raise RuntimeError("Episode saved but could not be reloaded")

            created.append({
                "id":               ep["id"],
                "title":            ep["title"],
                "episode_date":     ep["episode_date"],
                "episode_number":   ep["episode_number"],
                "filename":         ep["filename"],
                "uploaded_at":      ep["uploaded_at"],
                "duration_seconds": ep["duration_seconds"],
                "segment_count":    len(segments),
                "indexed":          bool(indexed),
            })
        except Exception as exc:
            app.logger.exception("Failed to store/index uploaded transcript", exc_info=exc)
            errors.append({"filename": filename, "error": f"Storage/index error: {exc}"})
            continue

    if deferred_episode_ids:
        threading.Thread(
            target=_index_episodes_background,
            args=(deferred_episode_ids,),
            daemon=True,
        ).start()

    return jsonify({
        "created": created,
        "errors": errors,
        "indexing_deferred": bool(deferred_episode_ids),
    })


# ── API: delete episode ───────────────────────────────────────────────────────

@app.route("/api/episode/<int:episode_id>", methods=["DELETE"])
def api_delete_episode(episode_id: int):
    deleted = db.delete_episode(episode_id)
    if not deleted:
        return jsonify({"error": "Episode not found"}), 404
    return jsonify({"deleted": episode_id})


# ── API: search ───────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    lookback_raw = request.args.get("lookback", "20").strip().lower()
    if lookback_raw == "all":
        lookback = None
    else:
        try:
            lookback = int(lookback_raw)
        except ValueError:
            return jsonify({"error": "Invalid lookback"}), 400
    mode_raw = request.args.get("mode", "or")
    mode = _parse_mode(mode_raw)
    if mode is None:
        return jsonify({"error": "Invalid mode. Use 'or' or 'and'."}), 400

    # Optional episode filter — comma-separated IDs
    ep_ids_raw = request.args.get("episode_ids", "")
    try:
        episode_ids = _parse_episode_ids(ep_ids_raw)
    except ValueError:
        return jsonify({"error": "Invalid episode_ids"}), 400

    # Support comma-separated multi-keyword queries (e.g. "million, billion")
    # Phrases (terms containing spaces) are searched against raw transcript text.
    terms = _parse_terms(keyword)
    if not terms:
        return jsonify({"error": "keyword required"}), 400
    individual_results = _build_individual_results(terms, episode_ids)
    # For multi-keyword single-word queries, deduplicate adjacent occurrences:
    # "Joe Biden" counts as 1 mention, not 2.
    if len(terms) > 1 and all(" " not in t for t in terms):
        result = search_multi_adjacent(
            db, keyword, terms, individual_results,
            mode=mode, episode_ids=episode_ids,
        )
    else:
        result = (intersect_results(keyword, individual_results) if mode == "and"
                  else merge_results(keyword, individual_results))

    # Badge metrics (rolling averages + FV) should reflect overall source
    # mentions across all keywords, not adjacency-deduped runs.
    # Keep `result` for episode/table/chart counting semantics.
    badge_result = (
        intersect_results(keyword, individual_results)
        if mode == "and"
        else merge_results(keyword, individual_results)
    ) if len(terms) > 1 else result

    def _r(v, digits=4):
        return round(v, digits) if v is not None else None

    episodes = [
        {
            "episode_id":       ep.episode_id,
            "title":            ep.title,
            "episode_date":     ep.episode_date,
            "episode_number":   ep.episode_number,
            "duration_seconds": ep.duration_seconds,
            "count":            ep.count,
            "per_minute":       _r(ep.per_minute),
        }
        for ep in result.episodes
    ]

    averages = {
        "last_1":   badge_result.avg_last_1,
        "last_5":   badge_result.avg_last_5,
        "last_20":  badge_result.avg_last_20,
        "last_50":  badge_result.avg_last_50,
        "last_100": badge_result.avg_last_100,
    }

    averages_per_min = {
        "last_1":   _r(badge_result.avg_pm_last_1),
        "last_5":   _r(badge_result.avg_pm_last_5),
        "last_20":  _r(badge_result.avg_pm_last_20),
        "last_50":  _r(badge_result.avg_pm_last_50),
        "last_100": _r(badge_result.avg_pm_last_100),
    }

    fv_lookback = len(badge_result.episodes) if lookback is None else lookback
    fv      = calculate_fair_value(badge_result, lookback=fv_lookback)
    rec_pmf = recommended_pmf(fv)
    rec_sf  = recommended_sf(fv)

    fair_value = {
        "lambda":            round(fv.lambda_estimate, 4),
        "mean":              round(fv.mean, 4),
        "std_dev":           round(math.sqrt(fv.variance), 4),
        "model":             (
            "zero-inflated"  if fv.zero_inflated  and fv.zinb_pmf
            else "neg-binomial" if fv.overdispersed and fv.negbin_pmf
            else ("empirical" if fv.lookback_episodes >= 10 else "poisson")
        ),
        "pi_estimate":       round(fv.pi_estimate, 3) if fv.pi_estimate is not None else None,
        "zero_fraction":     round(sum(1 for ep in badge_result.episodes[:fv_lookback] if ep.count == 0) / max(fv.lookback_episodes, 1), 3),
        "lookback_episodes": fv.lookback_episodes,
        "reference_minutes": round(fv.reference_minutes, 1) if fv.reference_minutes else None,
        "buckets": [
            {
                "n":     k,
                "label": f"{k}+" if k == MAX_BUCKET else str(k),
                "pmf":   round(rec_pmf.get(k, 0), 6),
                "sf":    round(rec_sf.get(k, 0), 6),
                "pct":   round(rec_sf.get(k, 0) * 100, 2),
            }
            for k in range(1, MAX_BUCKET + 1)  # Start at 1; P(≥0)=100% is trivially obvious
        ],
    }

    per_keyword = []
    per_keyword_fv = []
    for t, res in zip(terms, individual_results):
        per_keyword.append({
            "keyword": t,
            "episodes": [
                {"episode_id": ep.episode_id, "count": ep.count, "per_minute": _r(ep.per_minute)}
                for ep in res.episodes
            ],
            "averages": {
                "last_1":   res.avg_last_1,
                "last_5":   res.avg_last_5,
                "last_20":  res.avg_last_20,
                "last_50":  res.avg_last_50,
                "last_100": res.avg_last_100,
            },
            "averages_per_min": {
                "last_1":   _r(res.avg_pm_last_1),
                "last_5":   _r(res.avg_pm_last_5),
                "last_20":  _r(res.avg_pm_last_20),
                "last_50":  _r(res.avg_pm_last_50),
                "last_100": _r(res.avg_pm_last_100),
            },
        })
        kw_fv_lookback = len(res.episodes) if lookback is None else lookback
        kw_fv    = calculate_fair_value(res, lookback=kw_fv_lookback)
        kw_sf    = recommended_sf(kw_fv)
        per_keyword_fv.append({
            "keyword": t,
            "buckets": [
                {
                    "n":     k,
                    "label": f"{k}+" if k == MAX_BUCKET else str(k),
                    "pct":   round(kw_sf.get(k, 0) * 100, 2),
                }
                for k in range(1, MAX_BUCKET + 1)
            ],
        })

    return jsonify({
        "keyword":            keyword,
        "mode":               mode,
        "episodes":           episodes,
        "averages":           averages,
        "averages_per_min":   averages_per_min,
        "fair_value":         fair_value,
        "per_keyword":        per_keyword,
        "per_keyword_fv":     per_keyword_fv,
    })


# ── API: per-minute breakdown ─────────────────────────────────────────────────

@app.route("/api/minutes")
def api_minutes():
    keyword    = request.args.get("keyword", "").strip()
    episode_id = request.args.get("episode_id", "").strip()
    if not keyword or not episode_id:
        return jsonify({"error": "keyword and episode_id required"}), 400

    try:
        eid = int(episode_id)
    except ValueError:
        return jsonify({"error": "episode_id must be an integer"}), 400

    mode_raw = request.args.get("mode", "or")
    mode = _parse_mode(mode_raw)
    if mode is None:
        return jsonify({"error": "Invalid mode. Use 'or' or 'and'."}), 400

    terms = _parse_terms(keyword)
    if not terms:
        return jsonify({"error": "keyword required"}), 400

    individual_results = _build_individual_results(terms, [eid])
    result = merge_results(keyword, individual_results)
    ep     = result.episode_by_id(eid)

    # Per-keyword per-minute breakdowns (always simple sum — used for per-kw bars)
    per_keyword_minutes: list[dict] = []
    for t in terms:
        breakdown = (
            get_phrase_minute_breakdown(db, t, eid) if " " in t
            else get_minute_breakdown(db, t, eid)
        )
        kw_map: dict[int, int] = {}
        for mr in breakdown:
            kw_map[mr.minute] = kw_map.get(mr.minute, 0) + mr.count
        per_keyword_minutes.append({"keyword": t, "minutes": kw_map})

    # Merged per-minute counts: use adjacent-dedup for multi-word OR queries
    # so the minute-chart bars match the main episode count.
    if len(terms) > 1 and all(" " not in t for t in terms) and mode != "and":
        merged_counts: dict[int, int] = get_minute_breakdown_multi_adjacent(db, terms, eid)
    else:
        merged_counts = {}
        for kw_entry in per_keyword_minutes:
            for minute, cnt in kw_entry["minutes"].items():
                merged_counts[minute] = merged_counts.get(minute, 0) + cnt

    # Always return a full timeline from t=0 through the episode end minute so
    # minute charts/rolling averages start at the true beginning and end at the
    # last data point of the selected episode.
    duration_seconds = (ep.duration_seconds if ep else 0) or 0
    duration_last_minute = max(0, math.ceil(duration_seconds / 60) - 1)
    last_minute_with_data = max(merged_counts.keys(), default=0)
    timeline_end_minute = max(duration_last_minute, last_minute_with_data)
    full_range = list(range(0, timeline_end_minute + 1))

    return jsonify({
        "episode_id":  eid,
        "keyword":     keyword,
        "title":       ep.title if ep else str(eid),
        "minutes":     [{"minute": m, "count": merged_counts.get(m, 0)} for m in full_range],
        "per_keyword": per_keyword_minutes,
    })


# ── API: context (KWIC) ───────────────────────────────────────────────────────

@app.route("/api/context")
def api_context():
    keyword    = request.args.get("keyword", "").strip()
    episode_id = request.args.get("episode_id", "").strip()
    if not keyword or not episode_id:
        return jsonify({"error": "keyword and episode_id required"}), 400
    try:
        eid = int(episode_id)
    except ValueError:
        return jsonify({"error": "episode_id must be an integer"}), 400

    terms = _parse_terms(keyword)
    if not terms:
        return jsonify({"error": "keyword required"}), 400

    if len(terms) > 1 and all(" " not in t for t in terms):
        all_hits = get_context_multi_adjacent(db, terms, eid)
    else:
        all_hits: list[dict] = []
        for t in terms:
            all_hits.extend(get_context(db, t, eid))

    all_hits.sort(key=lambda h: (h["minute"], h["second"]))
    return jsonify({"episode_id": eid, "keyword": keyword, "hits": all_hits})


# ── API: reindex ──────────────────────────────────────────────────────────────

@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    db.reset_index()
    indexed = index_all(db)
    return jsonify({"reindexed": indexed})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("JRE Analyzer running at http://localhost:5000")
    app.run(debug=True, port=5000, use_reloader=False)
