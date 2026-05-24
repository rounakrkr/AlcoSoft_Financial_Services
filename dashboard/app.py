# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   dashboard/app.py — Flask Control Center
#   Run separately: python dashboard/app.py
# ============================================================

import json
import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DB_PATH        = "data/alcosoft.db"
BRIEFING_PATH  = "data/session_briefing.json"
LEARNINGS_PATH = "data/learnings.json"
REFLECTIONS_DIR = "data/reflections"


# ── Helper ────────────────────────────────────────────────────
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


# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    today = datetime.now().strftime("%Y-%m-%d")

    # Today's stats
    stats_rows = _db_query("""
        SELECT * FROM daily_stats WHERE date = ?
    """, (today,))
    stats = stats_rows[0] if stats_rows else {
        "total_trades": 0, "winning_trades": 0,
        "losing_trades": 0, "gross_pnl": 0.0
    }

    # Open positions
    positions = _db_query("""
        SELECT symbol, entry_price, stop_loss, quantity,
               strategy, entry_time, trading_mode
        FROM trades WHERE status = 'OPEN'
        ORDER BY id DESC
    """)

    # Recent trades (last 10)
    trades = _db_query("""
        SELECT symbol, action, entry_price, exit_price,
               pnl, status, strategy, entry_time, exit_time
        FROM trades
        ORDER BY id DESC LIMIT 10
    """)

    # Session briefing
    briefing = {}
    if os.path.exists(BRIEFING_PATH):
        with open(BRIEFING_PATH) as f:
            briefing = json.load(f)

    # War room log (today, last 10)
    war_log = _db_query("""
        SELECT agent, symbol, verdict, confidence,
               reasons, concern, timestamp, round_number
        FROM war_room_log
        WHERE timestamp LIKE ?
        ORDER BY id DESC LIMIT 10
    """, (f"{today}%",))

    # Latest reflection
    reflection = {}
    ref_path = os.path.join(REFLECTIONS_DIR, f"{today}.json")
    if os.path.exists(ref_path):
        with open(ref_path) as f:
            reflection = json.load(f)

    # Win rate
    total   = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    return jsonify({
        "timestamp":    datetime.now().strftime("%H:%M:%S"),
        "trading_mode": os.getenv("TRADING_MODE", "PAPER"),
        "strategy":     os.getenv("STRATEGY_TYPE", "INTRADAY"),
        "capital":      float(os.getenv("CAPITAL", 10000)),
        "stats": {
            **stats,
            "win_rate": win_pct,
        },
        "positions":   positions,
        "trades":      trades,
        "briefing":    briefing,
        "war_log":     war_log,
        "reflection":  reflection,
    })


if __name__ == "__main__":
    print("🚀 AlcoSoft Dashboard starting at http://localhost:5000")
    app.run(debug=False, port=5000)