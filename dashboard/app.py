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

    chart_trades = _db_query("""
        SELECT symbol, pnl, status, exit_time, entry_time
        FROM trades
        WHERE date = ? AND pnl IS NOT NULL
          AND status IN ('CLOSED', 'STOPPED')
        ORDER BY COALESCE(exit_time, entry_time) ASC
        LIMIT 50
    """, (today,))

    labels, pnls, cumulative = [], [], []
    running = 0.0
    for i, t in enumerate(chart_trades):
        pnl = float(t.get("pnl") or 0)
        labels.append(t.get("symbol", f"T{i+1}"))
        pnls.append(round(pnl, 2))
        running += pnl
        cumulative.append(round(running, 2))

    circuit_breakers = {}
    market_msg = ""
    order_verify = {}
    feed_stats = {}
    try:
        from core.circuit_breaker import get_status as cb_status
        circuit_breakers = cb_status()
    except Exception:
        pass
    try:
        from core.market_calendar import market_status_message
        _, market_msg = market_status_message()
    except Exception:
        market_msg = "—"
    try:
        from core.order_verifier import get_verification_report
        order_verify = get_verification_report()
    except Exception:
        pass
    try:
        from core.data_fetcher import get_feed_stats
        feed_stats = get_feed_stats()
    except Exception:
        pass

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
        "charts": {
            "trade_labels":    labels,
            "trade_pnl":       pnls,
            "cumulative_pnl":  cumulative,
            "wins":            stats.get("winning_trades", 0),
            "losses":          stats.get("losing_trades", 0),
            "circuit_breakers": circuit_breakers,
            "market_status":   market_msg,
            "order_verify":    order_verify,
            "feed": {
                "subscribed": len(feed_stats.get("subscribed", [])),
                "tick_total": sum(feed_stats.get("tick_counts", {}).values()),
            },
        },
    })


if __name__ == "__main__":
    app.run(debug=False, port=5000)
