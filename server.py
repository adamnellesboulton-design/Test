#!/usr/bin/env python3
"""
Flask web server for the JRE Transcript Analyzer.

Run:
    python server.py
Then open: http://localhost:5000

Auto-sync
---------
A background thread fires every day at 12:00 UTC (one hour after the YouTube
transcript rate-limit resets at 11 am UTC).  It fetches the most recent
episodes that are not yet in the database, so the data stays current without
any manual intervention.
"""

from __future__ import annotations

import math
import threading
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from jre_analyzer.database import Database
from jre_analyzer.analyzer import index_all
from jre_analyzer.search import search, get_minute_breakdown
from jre_analyzer.fair_value import calculate_fair_value, recommended_pmf, recommended_sf, MAX_BUCKET

app = Flask(__name__, static_folder="static", static_url_path="/static")

DB_PATH = Path("jre_data.db")
db = Database(db_path=DB_PATH)

# ── Sync state ───────────────────────────────────────────────────────────────

_sync_status: dict = {
    "running":      False,
    "message":      "Idle",
    "added":        0,
    "rate_limited": False,
}
_sync_lock = threading.Lock()

# How many episodes the daily auto-sync fetches.  Already-stored episodes are
# skipped, so this just needs to be larger than the typical daily release count
# (JRE publishes 3–5 episodes per week).
_AUTO_SYNC_EPISODES = 20


# ── Shared sync helper ───────────────────────────────────────────────────────

def _run_sync(n: int, label: str = "Sync") -> None:
    """
    Fetch up to *n* episodes and index them.  Must be called from a thread
    that has already set ``_sync_status["running"] = True``.
    """
    from jre_analyzer.fetch_transcripts import sync_episodes

    try:
        summary = sync_episodes(db, max_episodes=n)
        indexed = index_all(db)
        with _sync_lock:
            _sync_status["added"]               = summary["added"]
            _sync_status["transcripts_ok"]      = summary["transcripts_ok"]
            _sync_status["transcripts_missing"] = summary["transcripts_missing"]
            _sync_status["rate_limited"]        = summary.get("rate_limited", False)
            if summary.get("rate_limited"):
                _sync_status["message"] = (
                    f"{label}: YouTube rate-limit after {summary['added']} episodes "
                    f"({summary['transcripts_ok']} with transcripts). "
                    f"Indexed {indexed}. Will retry tomorrow at noon UTC."
                )
            else:
                _sync_status["message"] = (
                    f"{label}: {summary['added']} new episodes added "
                    f"({summary['transcripts_ok']} with transcripts, "
                    f"{summary['transcripts_missing']} missing), "
                    f"{indexed} indexed."
                )
    except Exception as exc:
        with _sync_lock:
            _sync_status["message"] = f"{label} error: {exc}"
    finally:
        with _sync_lock:
            _sync_status["running"] = False


# ── Daily auto-sync scheduler ────────────────────────────────────────────────

def _seconds_until_noon_utc() -> float:
    """Return seconds until the next 12:00:00 UTC."""
    now  = datetime.now(timezone.utc)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now >= noon:
        noon += timedelta(days=1)
    return (noon - now).total_seconds()


def _next_noon_utc() -> datetime:
    now  = datetime.now(timezone.utc)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if now >= noon:
        noon += timedelta(days=1)
    return noon


def _auto_sync_loop() -> None:
    """Daemon thread: trigger a sync every day at 12:00 UTC."""
    import logging
    logger = logging.getLogger(__name__)

    while True:
        wait = _seconds_until_noon_utc()
        next_dt = _next_noon_utc().strftime("%Y-%m-%d %H:%M UTC")
        logger.info("Auto-sync: next run at %s (in %.0f s)", next_dt, wait)
        _time.sleep(wait)

        with _sync_lock:
            if _sync_status["running"]:
                logger.info("Auto-sync: skipping — a sync is already running")
                continue
            _sync_status["running"] = True
            _sync_status["message"] = (
                f"Auto-sync: fetching up to {_AUTO_SYNC_EPISODES} new episodes…"
            )
            _sync_status["added"] = 0

        logger.info("Auto-sync: starting daily fetch (%d episodes)", _AUTO_SYNC_EPISODES)
        _run_sync(_AUTO_SYNC_EPISODES, label="Auto-sync")
        logger.info("Auto-sync: finished — %s", _sync_status["message"])


# Start the scheduler as soon as the module loads (survives Flask reloads
# because we guard with use_reloader=False at the bottom).
_scheduler_thread = threading.Thread(
    target=_auto_sync_loop, daemon=True, name="auto-sync-scheduler"
)
_scheduler_thread.start()


# ── Startup fill ──────────────────────────────────────────────────────────────
# If there are fewer than 100 episodes in the DB on first launch, fill up
# automatically without any user action.

_FILL_TARGET = 100

def _startup_fill() -> None:
    """On startup, fill the DB up to _FILL_TARGET episodes if needed."""
    import logging
    logger = logging.getLogger(__name__)

    count = db.count_episodes()
    if count >= _FILL_TARGET:
        logger.info("Startup: %d episodes already in DB — no fill needed", count)
        return

    logger.info("Startup: %d episodes in DB — auto-filling up to %d", count, _FILL_TARGET)
    with _sync_lock:
        if _sync_status["running"]:
            return
        _sync_status["running"] = True
        _sync_status["message"] = f"Starting up — fetching episodes from YouTube…"
        _sync_status["added"]   = 0

    _run_sync(_FILL_TARGET, label="Startup")
    logger.info("Startup fill complete — %s", _sync_status["message"])


_fill_thread = threading.Thread(target=_startup_fill, daemon=True, name="startup-fill")
_fill_thread.start()


# ── Static files ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── API: status ──────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    total  = db.count_episodes()
    eps    = db.get_all_episodes(limit=1)
    latest = eps[0] if eps else None
    return jsonify({
        "total_episodes":  total,
        "latest_episode":  latest["title"]       if latest else None,
        "latest_date":     latest["upload_date"]  if latest else None,
        "sync":            _sync_status,
        "next_auto_sync":  _next_noon_utc().strftime("%Y-%m-%d %H:%M UTC"),
    })


# ── API: search ───────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    lookback = int(request.args.get("lookback", 20))
    result   = search(db, keyword)

    def _r(v, digits=4):
        return round(v, digits) if v is not None else None

    episodes = [
        {
            "video_id":         ep.video_id,
            "title":            ep.title,
            "upload_date":      ep.upload_date,
            "episode_number":   ep.episode_number,
            "duration_seconds": ep.duration_seconds,
            "count":            ep.count,
            "count_lo":         _r(ep.count_lo, 2),
            "count_hi":         _r(ep.count_hi, 2),
            "per_minute":       _r(ep.per_minute),
            "per_minute_lo":    _r(ep.per_minute_lo),
            "per_minute_hi":    _r(ep.per_minute_hi),
        }
        for ep in result.episodes
    ]

    averages = {
        "last_1":    result.avg_last_1,
        "last_5":    result.avg_last_5,
        "last_20":   result.avg_last_20,
        "last_50":   result.avg_last_50,
        "last_100":  result.avg_last_100,
        # 95 % CI bounds on each average (speaker-filter corrected)
        "last_1_lo":   _r(result.avg_last_1_lo),
        "last_1_hi":   _r(result.avg_last_1_hi),
        "last_5_lo":   _r(result.avg_last_5_lo),
        "last_5_hi":   _r(result.avg_last_5_hi),
        "last_20_lo":  _r(result.avg_last_20_lo),
        "last_20_hi":  _r(result.avg_last_20_hi),
        "last_50_lo":  _r(result.avg_last_50_lo),
        "last_50_hi":  _r(result.avg_last_50_hi),
        "last_100_lo": _r(result.avg_last_100_lo),
        "last_100_hi": _r(result.avg_last_100_hi),
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
        "lambda_ci_lo":      round(fv.lambda_ci_lo, 4),
        "lambda_ci_hi":      round(fv.lambda_ci_hi, 4),
        "mean":              round(fv.mean, 4),
        "std_dev":           round(math.sqrt(fv.variance), 4),
        "model":             (
            "neg-binomial" if fv.overdispersed and fv.negbin_pmf
            else ("empirical" if fv.lookback_episodes >= 10 else "poisson")
        ),
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


# ── API: reindex ──────────────────────────────────────────────────────────────

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


# ── API: per-minute breakdown ─────────────────────────────────────────────────

@app.route("/api/minutes")
def api_minutes():
    keyword  = request.args.get("keyword", "").strip()
    video_id = request.args.get("video_id", "").strip()
    if not keyword or not video_id:
        return jsonify({"error": "keyword and video_id required"}), 400

    result      = search(db, keyword)
    ep          = result.episode_by_id(video_id)
    minute_data = get_minute_breakdown(db, keyword, video_id)

    if not minute_data:
        return jsonify({"video_id": video_id, "keyword": keyword, "minutes": []})

    minutes    = [r.minute for r in minute_data]
    full_range = list(range(min(minutes), max(minutes) + 1))
    count_map  = {r.minute: r.count for r in minute_data}

    return jsonify({
        "video_id": video_id,
        "keyword":  keyword,
        "title":    ep.title if ep else video_id,
        "minutes":  [
            {"minute": m, "count": count_map.get(m, 0)}
            for m in full_range
        ],
    })


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("JRE Analyzer running at http://localhost:5000")
    print(f"Next auto-sync: {_next_noon_utc().strftime('%Y-%m-%d %H:%M UTC')}")
    app.run(debug=True, port=5000, use_reloader=False)
