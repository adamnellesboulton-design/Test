#!/usr/bin/env python3
"""
Flask web server for the JRE Transcript Analyzer.

Run:
    python server.py
Then open: http://localhost:5000
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_all
from jre_analyzer.search import search, get_minute_breakdown
from jre_analyzer.fair_value import calculate_fair_value, recommended_pmf, recommended_sf, MAX_BUCKET

app = Flask(__name__, static_folder="static", static_url_path="/static")

DB_PATH = Path("jre_data.db")
db = Database(db_path=DB_PATH)

# Track background sync state
_sync_status = {"running": False, "message": "Idle", "added": 0, "rate_limited": False}
_sync_lock = threading.Lock()


# ── Static files ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── API: status ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    total = db.count_episodes()
    eps = db.get_all_episodes(limit=1)
    latest = eps[0] if eps else None
    return jsonify({
        "total_episodes": total,
        "latest_episode": latest["title"] if latest else None,
        "latest_date": latest["upload_date"] if latest else None,
        "sync": _sync_status,
    })


# ── API: sync ────────────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    with _sync_lock:
        if _sync_status["running"]:
            return jsonify({"error": "Sync already running"}), 409

    n = request.json.get("episodes", 100) if request.json else 100

    def _run():
        with _sync_lock:
            _sync_status["running"] = True
            _sync_status["message"] = f"Fetching up to {n} episodes…"
            _sync_status["added"] = 0
        try:
            from jre_analyzer.fetch_transcripts import sync_episodes
            summary = sync_episodes(db, max_episodes=n)
            indexed = index_all(db)
            with _sync_lock:
                _sync_status["added"] = summary["added"]
                _sync_status["transcripts_ok"] = summary["transcripts_ok"]
                _sync_status["transcripts_missing"] = summary["transcripts_missing"]
                _sync_status["rate_limited"] = summary.get("rate_limited", False)
                if summary.get("rate_limited"):
                    _sync_status["message"] = (
                        f"YouTube rate-limit hit after {summary['added']} episodes "
                        f"({summary['transcripts_ok']} with transcripts). "
                        f"Indexed {indexed}. Re-run sync after 11 am UTC to continue."
                    )
                else:
                    _sync_status["message"] = (
                        f"Done. {summary['added']} new episodes added "
                        f"({summary['transcripts_ok']} with transcripts, "
                        f"{summary['transcripts_missing']} missing), "
                        f"{indexed} indexed."
                    )
        except Exception as exc:
            with _sync_lock:
                _sync_status["message"] = f"Error: {exc}"
        finally:
            with _sync_lock:
                _sync_status["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"started": True})


# ── API: seed demo data ──────────────────────────────────────────────────────

@app.route("/api/seed", methods=["POST"])
def api_seed():
    """Seed the DB with synthetic demo data (no YouTube needed)."""
    import random, math as _math
    from datetime import date, timedelta

    random.seed(42)
    keyword = (request.json or {}).get("keyword", "dmt")
    n = (request.json or {}).get("episodes", 100)

    def _make_transcript(kw, count, dur_min=180):
        segs, t = [], 0.0
        dur_sec = dur_min * 60
        times = set(int(random.uniform(0, dur_sec)) for _ in range(count))
        filler = "yeah man that is like you know interesting right think really people the a because so and but what joe rogan".split()
        while t < dur_sec:
            words = random.choices(filler, k=10)
            if int(t) in times:
                words[0] = kw
            segs.append({"start": t, "duration": 6.0, "text": " ".join(words)})
            t += 6.0
        return segs

    for i in range(n):
        ep_num = 2200 - i
        spike = random.random() < 0.08
        count = random.randint(8, 20) if spike else max(0, int(random.gauss(3.0, _math.sqrt(3.0))))
        dur = random.randint(120, 240)
        vid = f"demo_{ep_num:04d}"
        title = f"Joe Rogan Experience #{ep_num}"
        ep_date = (date(2025, 1, 15) - timedelta(weeks=i)).strftime("%Y-%m-%d")
        db.upsert_episode(vid, title, ep_date, dur * 60, _make_transcript(keyword, count, dur))

    indexed = index_all(db)
    return jsonify({"seeded": n, "indexed": indexed, "keyword": keyword})


# ── API: search ──────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    lookback = int(request.args.get("lookback", 20))
    result = search(db, keyword)

    episodes = [
        {
            "video_id":       ep.video_id,
            "title":          ep.title,
            "upload_date":    ep.upload_date,
            "episode_number": ep.episode_number,
            "duration_seconds": ep.duration_seconds,
            "count":          ep.count,
            "per_minute":     round(ep.per_minute, 4),
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

    def _r(v, digits=4):
        return round(v, digits) if v is not None else None

    averages_per_min = {
        "last_1":   _r(result.avg_pm_last_1),
        "last_5":   _r(result.avg_pm_last_5),
        "last_20":  _r(result.avg_pm_last_20),
        "last_50":  _r(result.avg_pm_last_50),
        "last_100": _r(result.avg_pm_last_100),
    }

    fv = calculate_fair_value(result, lookback=lookback)
    rec_pmf = recommended_pmf(fv)
    rec_sf  = recommended_sf(fv)

    fair_value = {
        "lambda":            round(fv.lambda_estimate, 4),
        "mean":              round(fv.mean, 4),
        "std_dev":           round(math.sqrt(fv.variance), 4),
        "model":             ("neg-binomial" if fv.overdispersed and fv.negbin_pmf
                              else ("empirical" if fv.lookback_episodes >= 10 else "poisson")),
        "lookback_episodes": fv.lookback_episodes,
        "reference_minutes": round(fv.reference_minutes, 1) if fv.reference_minutes else None,
        "buckets": [
            {
                "n":         k,
                "label":     f"{k}+" if k == MAX_BUCKET else str(k),
                "pmf":       round(rec_pmf.get(k, 0), 6),
                "sf":        round(rec_sf.get(k, 0), 6),
                "pct":       round(rec_sf.get(k, 0) * 100, 2),
            }
            for k in range(MAX_BUCKET + 1)
        ],
    }

    return jsonify({
        "keyword":          keyword,
        "episodes":         episodes,
        "averages":         averages,
        "averages_per_min": averages_per_min,
        "fair_value":       fair_value,
    })


# ── API: reindex ─────────────────────────────────────────────────────────────

@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    """
    Clear all frequency data and rebuild the index from stored transcripts.

    Needed any time the tokenizer changes (e.g. after adding stemming), so
    that searched keywords match what is actually stored in the DB.
    """
    db.reset_index()
    indexed = index_all(db)
    return jsonify({"reindexed": indexed})


# ── API: per-minute breakdown ────────────────────────────────────────────────

@app.route("/api/minutes")
def api_minutes():
    keyword  = request.args.get("keyword", "").strip()
    video_id = request.args.get("video_id", "").strip()
    if not keyword or not video_id:
        return jsonify({"error": "keyword and video_id required"}), 400

    result = search(db, keyword)
    ep = result.episode_by_id(video_id)
    minute_data = get_minute_breakdown(db, keyword, video_id)

    if not minute_data:
        return jsonify({"video_id": video_id, "keyword": keyword, "minutes": []})

    minutes = [r.minute for r in minute_data]
    full_range = list(range(min(minutes), max(minutes) + 1))
    count_map = {r.minute: r.count for r in minute_data}

    return jsonify({
        "video_id": video_id,
        "keyword":  keyword,
        "title":    ep.title if ep else video_id,
        "minutes":  [
            {"minute": m, "count": count_map.get(m, 0)}
            for m in full_range
        ],
    })


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("JRE Analyzer running at http://localhost:5000")
    app.run(debug=True, port=5000, use_reloader=False)
