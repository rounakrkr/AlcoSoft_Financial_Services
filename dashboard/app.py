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
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

DB_PATH         = os.path.join(_ROOT, "data", "alcosoft.db")
REFLECTION_DB_PATH = os.path.join(_ROOT, "data", "reflection.db")
BRIEFING_PATH   = os.path.join(_ROOT, "data", "session_briefing.json")
REFLECTIONS_DIR = os.path.join(_ROOT, "data", "reflections")

# Adaptive Learning System
try:
    from reflection.reflection_engine import (
        get_all_signal_stats,
        get_all_time_window_stats,
        get_all_symbol_stats,
    )
    from reflection.adaptive_config_updater import get_adaptive_config_summary
    ADAPTIVE_AVAILABLE = True
except ImportError:
    ADAPTIVE_AVAILABLE = False


def _reflection_db_query(query: str, params: tuple = ()) -> list:
    if not os.path.exists(REFLECTION_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(REFLECTION_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


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
        with open(BRIEFING_PATH, encoding="utf-8-sig") as f:
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
        with open(ref_path, encoding="utf-8-sig") as f:
            reflection = json.load(f)

    total = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    cap_path = os.path.join(_ROOT, "data", "live_capital.json")
    capital_display = float(cfg("risk", "paper_capital", 10000))
    if os.path.exists(cap_path):
        try:
            with open(cap_path, encoding="utf-8-sig") as f:
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


@app.route("/api/margin-status")
def api_margin_status():
    """
    🔥 NEW: Margin monitoring endpoint.
    Returns current margin deployment status.
    """
    try:
        from core.order_executor import get_margin_status
        margin = get_margin_status()
        allow_margin = cfg("risk", "allow_margin", False)
        forced_buy = cfg("risk", "forced_buy_margin", False)

        return jsonify({
            "ok": True,
            "margin_enabled": allow_margin,
            "forced_buy_enabled": forced_buy,
            "real_capital": margin.get("real_capital", 0),
            "margin_leverage": margin.get("margin_leverage", 1.0),
            "total_available": margin.get("total_available_with_margin", 0),
            "deployed": margin.get("current_position_value", 0),
            "unrealized_pnl": margin.get("unrealized_pnl", 0),
            "effective_capital": margin.get("effective_capital", 0),
            "margin_used": margin.get("margin_used", 0),
            "margin_pct": round(margin.get("margin_pct", 0), 2),
            "remaining_margin": margin.get("remaining_margin", 0),
            "is_over_leveraged": margin.get("is_over_leveraged", False),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "margin_enabled": cfg("risk", "allow_margin", False),
            "forced_buy_enabled": cfg("risk", "forced_buy_margin", False),
        })


@app.route("/api/emergency-squareoff", methods=["POST"])
def api_emergency_squareoff():
    """
    🚨 EMERGENCY: Close all open positions immediately.
    Requires POST to prevent accidental clicks.
    """
    import asyncio
    try:
        from core.emergency_squareoff import emergency_square_off_all

        result = asyncio.run(emergency_square_off_all())
        return jsonify({
            "ok": True,
            "status": result["status"],
            "closed_count": result["closed_count"],
            "failed_count": result["failed_count"],
            "timestamp": result["timestamp"],
            "details": result["details"],
        })
    except Exception as e:
        import logging
        logging.error(f"Emergency squareoff failed: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


@app.route("/api/adaptive")
def api_adaptive():
    if not ADAPTIVE_AVAILABLE:
        return jsonify({"ok": False, "error": "Adaptive engine not available"}), 503

    signals = get_all_signal_stats()
    windows = get_all_time_window_stats()
    symbols = get_all_symbol_stats()
    config_summary = get_adaptive_config_summary()

    total_trades = sum(s.get("total_trades", 0) for s in signals)
    total_wins = sum(s.get("winning_trades", 0) for s in signals)
    overall_win_rate = round((total_wins / total_trades * 100) if total_trades > 0 else 0.0, 1)

    multiplier_rows = _reflection_db_query(
        "SELECT multiplier_type, multiplier_key, multiplier_value, raw_calculated_value, sample_size, confidence_strength, last_updated "
        "FROM multiplier_history ORDER BY multiplier_type, multiplier_key"
    )

    change_history = _reflection_db_query(
        "SELECT multiplier_type, multiplier_key, previous_value, new_value, raw_calculated_value, "
        "sample_size, confidence_strength, reason_source, timestamp "
        "FROM multiplier_change_log ORDER BY timestamp DESC LIMIT 50"
    )

    config_history = _reflection_db_query(
        "SELECT config_date, changes_made, timestamp "
        "FROM config_history ORDER BY timestamp DESC LIMIT 20"
    )

    reflection = {}
    ref_path = os.path.join(REFLECTIONS_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.json")
    if os.path.exists(ref_path):
        try:
            with open(ref_path, encoding="utf-8-sig") as f:
                reflection = json.load(f)
        except Exception:
            reflection = {}

    avg_confidence = 0.0
    if multiplier_rows:
        avg_confidence = round(
            sum(row.get("confidence_strength", 0) for row in multiplier_rows) / len(multiplier_rows),
            2,
        )

    alerts = []
    for row in multiplier_rows:
        if row.get("sample_size", 0) < 20 and row.get("confidence_strength", 0) < 0.3:
            alerts.append({
                "level": "warning",
                "message": f"Low confidence: {row.get('multiplier_type')} {row.get('multiplier_key')} only has {row.get('sample_size', 0)} samples",
            })
        if row.get("multiplier_value", 0) < 0.6 and row.get("sample_size", 0) < 30:
            alerts.append({
                "level": "danger",
                "message": f"Multiplier suppressed fast: {row.get('multiplier_type')} {row.get('multiplier_key')} dropped to {row.get('multiplier_value', 0):.2f} with low samples",
            })

    strong_periods = [w for w in windows if w.get("win_rate", 0) >= 55]
    weak_periods = [w for w in windows if w.get("win_rate", 0) < 50]

    # Build adaptive alerts
    for stats in signals:
        if stats.get("total_trades", 0) < 10 and stats.get("win_rate", 0) < 50:
            alerts.append({
                "level": "warning",
                "message": f"Low sample size: {stats.get('signal_name')} has only {stats.get('total_trades', 0)} trades",
            })
        if stats.get("win_rate", 0) < 40:
            alerts.append({
                "level": "danger",
                "message": f"{stats.get('signal_name')} suppressed due to low win rate ({stats.get('win_rate', 0):.1f}%)",
            })

    for symbol in symbols:
        if symbol.get("sl_hit_freq", 0) > 60 and symbol.get("volatility_profile", "").upper() == "HIGH":
            alerts.append({
                "level": "alert",
                "message": f"Abnormal volatility: {symbol.get('symbol')} has SL hit frequency {symbol.get('sl_hit_freq', 0):.1f}%",
            })

    return jsonify({
        "ok": True,
        "signals": signals,
        "time_windows": windows,
        "symbols": symbols,
        "config_summary": config_summary,
        "overall_win_rate": overall_win_rate,
        "multiplier_history": multiplier_rows,
        "change_history": change_history,
        "config_history": config_history,
        "average_confidence": avg_confidence,
        "strong_periods": strong_periods,
        "weak_periods": weak_periods,
        "alerts": alerts,
        "reflection": reflection,
    })


# ════════════════════════════════════════════════════════════
#   COGNITION LAB BLUEPRINT — Separate Research Dashboard
# ════════════════════════════════════════════════════════════
#
# Creates dedicated routes for cognitive observation insights:
# - /cognition/status — current cognition system status
# - /cognition/cycles/today — today's observation cycles
# - /cognition/hypotheses — active hypotheses
# - /cognition/predictions/accuracy — prediction accuracy metrics
# - /cognition/daily-reflection — today's reflection
#
# Keeps main dashboard (/) execution-focused and lightweight.
# ════════════════════════════════════════════════════════════

try:
    from dashboard.cognition_lab import cognition_lab
    app.register_blueprint(cognition_lab)
    logger = __import__("logging").getLogger(__name__)
    logger.info("🧠 Cognition Lab dashboard registered at /cognition")
except Exception as e:
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"Cognition Lab not available: {e}")


if __name__ == "__main__":
    app.run(debug=False, port=5000)
