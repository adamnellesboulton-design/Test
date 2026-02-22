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
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_episode, index_all
from jre_analyzer.search import search, get_minute_breakdown, merge_results
from jre_analyzer.fair_value import calculate_fair_value, recommended_pmf, recommended_sf, MAX_BUCKET
from jre_analyzer.fetch_transcripts import parse_transcript_txt, extract_episode_date

app = Flask(__name__, static_folder="static", static_url_path="/static")

DB_PATH = Path(os.environ.get("DB_PATH", "jre_data.db"))
db = Database(db_path=DB_PATH)


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

    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided"}), 400

    created = []
    errors  = []

    for i, f in enumerate(files):
        filename = f.filename or f"upload_{i+1}.txt"
        title    = titles[i] if i < len(titles) and titles[i].strip() else Path(filename).stem

        try:
            content = f.read().decode("utf-8", errors="replace")
        except Exception as exc:
            errors.append({"filename": filename, "error": f"Could not read file: {exc}"})
            continue

        # Auto-detect date from transcript content ("Episode Date: Month D, YYYY")
        date = extract_episode_date(content)

        if not content.strip():
            errors.append({"filename": filename, "error": "File is empty"})
            continue

        try:
            segments, duration = parse_transcript_txt(content)
        except Exception as exc:
            errors.append({"filename": filename, "error": f"Parse error: {exc}"})
            continue

        episode_id = db.insert_episode(
            title=title,
            transcript=segments,
            episode_date=date,
            filename=filename,
            duration_seconds=duration,
        )

        # Index immediately (fast for a single episode)
        index_episode(db, episode_id)

        ep = db.get_episode(episode_id)
        created.append({
            "id":               ep["id"],
            "title":            ep["title"],
            "episode_date":     ep["episode_date"],
            "episode_number":   ep["episode_number"],
            "filename":         ep["filename"],
            "uploaded_at":      ep["uploaded_at"],
            "duration_seconds": ep["duration_seconds"],
            "segment_count":    len(segments),
            "indexed":          True,
        })

    return jsonify({"created": created, "errors": errors})


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

    lookback = int(request.args.get("lookback", 20))

    # Optional episode filter — comma-separated IDs
    ep_ids_raw = request.args.get("episode_ids", "").strip()
    episode_ids = None
    if ep_ids_raw:
        try:
            episode_ids = [int(x) for x in ep_ids_raw.split(",") if x.strip()]
        except ValueError:
            return jsonify({"error": "Invalid episode_ids"}), 400

    # Support comma-separated multi-keyword queries (e.g. "million, billion")
    terms = [kw.strip() for kw in keyword.split(",") if kw.strip()]
    if not terms:
        return jsonify({"error": "keyword required"}), 400
    individual_results = [search(db, t, episode_ids=episode_ids) for t in terms]
    result = merge_results(keyword, individual_results)

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
        "last_1":   result.avg_last_1,
        "last_5":   result.avg_last_5,
        "last_20":  result.avg_last_20,
        "last_50":  result.avg_last_50,
        "last_100": result.avg_last_100,
    }

    averages_per_min = {
        "last_1":   _r(result.avg_pm_last_1),
        "last_5":   _r(result.avg_pm_last_5),
        "last_20":  _r(result.avg_pm_last_20),
        "last_50":  _r(result.avg_pm_last_50),
        "last_100": _r(result.avg_pm_last_100),
    }

    fv      = calculate_fair_value(result, lookback=lookback)
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

    return jsonify({
        "keyword":          keyword,
        "episodes":         episodes,
        "averages":         averages,
        "averages_per_min": averages_per_min,
        "fair_value":       fair_value,
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

    terms = [kw.strip() for kw in keyword.split(",") if kw.strip()]
    individual_results = [search(db, t, episode_ids=[eid]) for t in terms]
    result = merge_results(keyword, individual_results)
    ep     = result.episode_by_id(eid)

    # Merge per-minute breakdowns across all keywords
    merged_counts: dict[int, int] = {}
    for t in terms:
        for mr in get_minute_breakdown(db, t, eid):
            merged_counts[mr.minute] = merged_counts.get(mr.minute, 0) + mr.count

    if not merged_counts:
        return jsonify({"episode_id": eid, "keyword": keyword, "minutes": []})

    minutes    = list(merged_counts.keys())
    full_range = list(range(min(minutes), max(minutes) + 1))
    count_map  = merged_counts

    return jsonify({
        "episode_id": eid,
        "keyword":    keyword,
        "title":      ep.title if ep else str(eid),
        "minutes":    [
            {"minute": m, "count": count_map.get(m, 0)}
            for m in full_range
        ],
    })


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
