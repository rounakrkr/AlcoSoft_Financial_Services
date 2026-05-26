# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   dashboard/app.py — Flask Control Center
#   Run: python dashboard/app.py  →  http://localhost:5000
# ============================================================

import json
import os
import sqlite3
import sys
from datetime import datetime

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

# Project root on path (core.trading_settings)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

load_dotenv(os.path.join(_ROOT, ".env"))

from core.trading_settings import (
    load_settings,
    save_settings,
    validate_updates,
    FIELD_SCHEMA,
    get as cfg,
)

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

DB_PATH         = os.path.join(_ROOT, "data", "alcosoft.db")
BRIEFING_PATH   = os.path.join(_ROOT, "data", "session_briefing.json")
REFLECTIONS_DIR = os.path.join(_ROOT, "data", "reflections")


def _db_query(query: str, params: tuple = ()) -> list:
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "settings": load_settings(),
        "schema": FIELD_SCHEMA,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    cleaned, errors = validate_updates(body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    merged = save_settings(cleaned)
    return jsonify({"ok": True, "settings": merged})


@app.route("/api/status")
def api_status():
    today = datetime.now().strftime("%Y-%m-%d")

    stats_rows = _db_query("SELECT * FROM daily_stats WHERE date = ?", (today,))
    stats = stats_rows[0] if stats_rows else {
        "total_trades": 0, "winning_trades": 0,
        "losing_trades": 0, "gross_pnl": 0.0,
    }

    positions = _db_query("""
        SELECT symbol, entry_price, stop_loss, quantity,
               strategy, entry_time, trading_mode
        FROM trades WHERE status = 'OPEN'
        ORDER BY id DESC
    """)

    trades = _db_query("""
        SELECT symbol, action, entry_price, exit_price,
               pnl, status, strategy, entry_time, exit_time
        FROM trades ORDER BY id DESC LIMIT 10
    """)

    briefing = {}
    if os.path.exists(BRIEFING_PATH):
        with open(BRIEFING_PATH, encoding="utf-8") as f:
            briefing = json.load(f)

    war_log = _db_query("""
        SELECT agent, symbol, verdict, confidence,
               reasons, concern, timestamp, round_number
        FROM war_room_log
        WHERE timestamp LIKE ?
        ORDER BY id DESC LIMIT 10
    """, (f"{today}%",))

    reflection = {}
    ref_path = os.path.join(REFLECTIONS_DIR, f"{today}.json")
    if os.path.exists(ref_path):
        with open(ref_path, encoding="utf-8") as f:
            reflection = json.load(f)

    total = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    cap_path = os.path.join(_ROOT, "data", "live_capital.json")
    capital_display = float(cfg("risk", "paper_capital", 10000))
    if os.path.exists(cap_path):
        try:
            with open(cap_path, encoding="utf-8") as f:
                cap_data = json.load(f)
                capital_display = cap_data.get("capital", capital_display)
        except Exception:
            pass

    st = load_settings().get("strategy", {})

    return jsonify({
        "timestamp":    datetime.now().strftime("%H:%M:%S"),
        "trading_mode": os.getenv("TRADING_MODE", "PAPER"),
        "strategy":     st.get("strategy_type", "INTRADAY"),
        "capital":      capital_display,
        "stats": {**stats, "win_rate": win_pct},
        "positions":   positions,
        "trades":      trades,
        "briefing":    briefing,
        "war_log":     war_log,
        "reflection":  reflection,
    })


if __name__ == "__main__":
    print("AlcoSoft Dashboard → http://localhost:5000")
    print("Settings editor  → http://localhost:5000/settings")
    app.run(debug=False, port=5000)
