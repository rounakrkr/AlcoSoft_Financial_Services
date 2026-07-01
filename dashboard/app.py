# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   dashboard/app.py — Flask Control Center
#   Run: python dashboard/app.py  →  http://localhost:5000
# ============================================================

import os
import sqlite3
import sys
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv

# Project root on path (core.trading_settings)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

load_dotenv(os.path.join(_ROOT, ".env"))

from core.auth_manager import (
    load_user,
    load_user_from_request,
    authenticate_user,
    admin_required,
    log_auth_event
)

from core.trading_settings import (
    load_settings,
    save_settings,
    validate_updates,
    get_field_schema,
    get as cfg,
)
from core.strategy_sets import load_strategy_sets, normalize_set_key
from core.safe_io import safe_read_json
from core.state_manager import (
    load_briefing,
    get_trading_session_state,
    lock_entries,
    resume_entries,
    initialize_db,
)

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

def _load_or_create_secret_key() -> str:
    """P2-8 FIX: persist the Flask secret so admins are not logged out on every
    restart. Prefer FLASK_SECRET_KEY env; otherwise use a locally persisted key."""
    key = os.environ.get("FLASK_SECRET_KEY")
    if key:
        return key
    path = os.path.join(_ROOT, "data", ".flask_secret")
    try:
        if os.path.exists(path):
            with open(path) as f:
                existing = f.read().strip()
            if existing:
                return existing
        os.makedirs(os.path.dirname(path), exist_ok=True)
        generated = os.urandom(32).hex()
        with open(path, "w") as f:
            f.write(generated)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return generated
    except Exception:
        return os.urandom(24).hex()


app.config['SECRET_KEY'] = _load_or_create_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.user_loader(load_user)

# Configure logging to show INFO level messages for debugging
import logging
if not app.debug:
    app.logger.setLevel(logging.INFO)
else:
    app.logger.setLevel(logging.DEBUG)
login_manager.request_loader(load_user_from_request)

@app.before_request
def require_login():
    # Only allow unauthenticated access to login, static files
    if request.endpoint in ['login', 'static']:
        return
    if not current_user.is_authenticated:
        return login_manager.unauthorized()

@app.errorhandler(Exception)
def handle_api_error(error):
    """Catch unhandled exceptions and return JSON for all requests."""
    import traceback
    
    app.logger.exception(f"Unhandled error: {error}")
    
    # Return JSON for all error responses (error.html template does not exist)
    return jsonify({
        "ok": False,
        "error": str(error),
        "type": error.__class__.__name__,
    }), 500

@app.route('/favicon.ico')
def favicon():
    """Return empty response for favicon requests to suppress 404 errors."""
    return '', 204

# Initialize database schema on startup
initialize_db()

DB_PATH         = os.path.join(_ROOT, "data", "alcosoft.db")
REFLECTION_DB_PATH = os.path.join(_ROOT, "data", "reflection.db")
BRIEFING_PATH   = os.path.join(_ROOT, "data", "session_briefing.json")
FEED_STATS_PATH = os.path.join(_ROOT, "data", "feed_stats.json")
REFLECTIONS_DIR = os.path.join(_ROOT, "data", "reflections")

# Adaptive Learning System
try:
    from reflection.reflection_engine import (
        get_all_signal_stats,
        get_all_time_window_stats,
        get_all_symbol_stats,
        get_signal_execution_policy,
    )
    from reflection.adaptive_config_updater import get_adaptive_config_summary
    ADAPTIVE_AVAILABLE = True
except ImportError:
    ADAPTIVE_AVAILABLE = False


def _reflection_db_query(query: str, params: tuple = ()) -> list:
    if not os.path.exists(REFLECTION_DB_PATH):
        return []
    conn = None
    try:
        # P3-6 FIX: busy_timeout so concurrent engine writes don't cause an instant
        # "database is locked" that silently returns [].
        conn = sqlite3.connect(REFLECTION_DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _db_query(query: str, params: tuple = ()) -> list:
    if not os.path.exists(DB_PATH):
        return []
    conn = None
    try:
        # P2-7 FIX: busy_timeout so concurrent engine writes don't cause an instant
        # "database is locked" that silently returns [].
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _configured_strategy_sets() -> list:
    try:
        config = load_strategy_sets()
        return list(config.buy_sets) + list(config.sell_sets)
    except Exception:
        return []


def _strategy_set_performance_rows(signal_stats: list[dict], multiplier_rows: list[dict]) -> list[dict]:
    set_defs = _configured_strategy_sets()
    if not set_defs:
        return signal_stats

    stats_by_name = {row.get("signal_name"): row for row in signal_stats}
    multipliers = {
        row.get("multiplier_key"): row
        for row in multiplier_rows
        if row.get("multiplier_type") == "signal"
    }
    settings = load_settings()
    strategy_set_settings = settings.get("strategy_sets", {})
    adaptive_safety_blocks_execution = bool(
        settings.get("risk", {}).get("adaptive_safety_blocks_execution", False)
    )

    rows = []
    for set_def in set_defs:
        stats = stats_by_name.get(set_def.name, {})
        multiplier = multipliers.get(normalize_set_key(set_def.name), {})
        execution_policy = get_signal_execution_policy(set_def.name) if ADAPTIVE_AVAILABLE else {}
        rows.append({
            "signal_name": set_def.name,
            "set_name": set_def.name,
            "side": set_def.side.upper(),
            "conditions": list(set_def.conditions),
            "priority": set_def.priority,
            "base_confidence": set_def.base_confidence,
            "confidence_weight": set_def.confidence_weight,
            "notes": set_def.notes,
            "total_trades": stats.get("total_trades", 0),
            "winning_trades": stats.get("winning_trades", 0),
            "losing_trades": stats.get("losing_trades", 0),
            "win_rate": stats.get("win_rate", 0.0),
            "avg_profit": stats.get("avg_profit", 0.0),
            "avg_loss": stats.get("avg_loss", 0.0),
            "avg_rr": stats.get("avg_rr", 0.0),
            "avg_drawdown": stats.get("avg_drawdown", 0.0),
            "multiplier": multiplier.get("multiplier_value", 1.0),
            "confidence": multiplier.get("confidence_strength", 0.0),
            "execution_policy": execution_policy,
            "execution_suppressed": bool(execution_policy.get("suppressed")),
            "execution_blocking_enabled": adaptive_safety_blocks_execution,
            "strategy_set_enabled": bool(strategy_set_settings.get(set_def.name, True)),
            "execution_scope": execution_policy.get("scope", ""),
        })

    return rows


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        user = authenticate_user(username, password)
        if user:
            login_user(user, remember=False)
            return redirect(url_for('index'))
        else:
            flash("Invalid credentials")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    log_auth_event("LOGOUT", current_user.username, True)
    logout_user()
    return redirect(url_for('login'))

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
        "schema": get_field_schema(),
    })


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_settings_post():
    body = request.get_json(silent=True) or {}
    cleaned, errors = validate_updates(body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # P2-4 FIX: guard risk-critical, hot-reloaded settings while positions are open.
    # A single edit can flip margin/leverage/daily-loss or disable an exit strategy set
    # mid-session with immediate effect. Require explicit confirmation in that case.
    RISKY_KEYS = {
        "allow_margin", "margin_leverage", "position_size_margin", "forced_buy_margin",
        "max_daily_loss_percent", "max_risk_per_trade", "enable_risk_based_position_sizing",
    }
    try:
        touches_risky = False
        for section, vals in (cleaned or {}).items():
            if isinstance(vals, dict) and (set(vals.keys()) & RISKY_KEYS):
                touches_risky = True
            if section == "strategy_sets":
                touches_risky = True  # enabling/disabling exit sets affects open positions
        if touches_risky and len(_db_query(
            "SELECT 1 FROM trades WHERE status = 'OPEN' AND quantity > 0 LIMIT 1"
        )) > 0:
            if not body.get("confirm_risky_change"):
                return jsonify({
                    "ok": False,
                    "requires_confirmation": True,
                    "error": "Risk-critical settings change while positions are OPEN. "
                             "Re-submit with confirm_risky_change=true to apply.",
                }), 409
    except Exception as guard_err:
        app.logger.warning("Risky-change guard check failed: %s", guard_err)

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
        SELECT symbol, entry_price, stop_loss, trailing_sl,
               target_price, quantity, strategy, confidence,
               kotak_sl_order_id, entry_time, trading_mode
        FROM trades WHERE status = 'OPEN' AND quantity > 0
        ORDER BY id DESC
    """)

    trades = _db_query("""
        SELECT symbol, action, entry_price, exit_price,
               pnl, status, strategy, notes, entry_time, exit_time
        FROM trades ORDER BY id DESC LIMIT 10
    """)
    for trade in trades:
        raw_status = str(trade.get("status") or "").upper()
        exit_reason = str(trade.get("notes") or "").strip()
        trade["raw_status"] = raw_status
        if raw_status == "STOPPED":
            trade["status"] = "CLOSED"
            trade["exit_reason"] = exit_reason or "STOPLOSS"
        else:
            trade["exit_reason"] = exit_reason

    # Load briefing using centralized state_manager (uses validation)
    briefing = load_briefing() or {}
    
    # Add validation status for dashboard display
    if briefing:
        from core.state_manager import validate_briefing
        is_valid, reason = validate_briefing(briefing)
        briefing["_validation_status"] = "VALID" if is_valid else f"INVALID: {reason}"
    else:
        briefing = {}
        briefing["_validation_status"] = "MISSING"

    agent_decisions = _db_query("""
        SELECT agent, symbol, verdict, confidence,
               reasons, concern, timestamp, round_number
        FROM agent_decision_log
        WHERE timestamp LIKE ?
        ORDER BY id DESC LIMIT 10
    """, (f"{today}%",))

    ref_path = os.path.join(REFLECTIONS_DIR, f"{today}.json")
    reflection = safe_read_json(
        ref_path,
        {},
        expected_type=dict,
        label="dashboard reflection",
        log=app.logger,
    )

    total = stats.get("total_trades", 0)
    winners = stats.get("winning_trades", 0)
    win_pct = round((winners / total * 100) if total > 0 else 0)

    try:
        from core.order_executor import get_capital_snapshot
        capital_snapshot = get_capital_snapshot()
    except Exception as exc:
        app.logger.warning("Capital snapshot unavailable: %s", exc, exc_info=True)
        fallback_capital = float(cfg("risk", "paper_capital", 10000))
        capital_snapshot = {
            "mode": os.getenv("TRADING_MODE", "PAPER"),
            "starting_capital": fallback_capital,
            "account_equity": None,
            "gross_exposure": None,
            "margin_blocked": None,
            "free_margin": None,
            "remaining_buying_power": None,
            "margin_utilization": None,
            "closed_pnl": stats.get("gross_pnl", 0.0),
            "unrealized_pnl": None,
            "margin_enabled": False,
            "margin_leverage": 1.0,
        }

    st = load_settings().get("strategy", {})
    trading_state = get_trading_session_state()

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
    gross_profit = 0.0
    gross_loss = 0.0
    for i, t in enumerate(chart_trades):
        pnl = float(t.get("pnl") or 0)
        labels.append(t.get("symbol", f"T{i+1}"))
        pnls.append(round(pnl, 2))
        running += pnl
        cumulative.append(round(running, 2))
        if pnl > 0:
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)

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
    feed_stats = safe_read_json(
        FEED_STATS_PATH,
        {},
        expected_type=dict,
        label="feed stats",
        log=app.logger,
    )
    if not feed_stats:
        try:
            from core.data_fetcher import get_feed_stats
            feed_stats = get_feed_stats()
        except Exception:
            pass

    return jsonify({
        "timestamp":    datetime.now().strftime("%H:%M:%S"),
        "trading_mode": os.getenv("TRADING_MODE", "PAPER"),
        "trading_state": trading_state,
        "strategy":     st.get("strategy_type", "INTRADAY"),
        "capital":      capital_snapshot.get("free_margin"),
        "capital_snapshot": capital_snapshot,
        "stats": {**stats, "win_rate": win_pct},
        "positions":   positions,
        "trades":      trades,
        "briefing":    briefing,
        "agent_decisions": agent_decisions,
        "reflection":  reflection,
        "charts": {
            "trade_labels":    labels,
            "trade_pnl":       pnls,
            "cumulative_pnl":  cumulative,
            "wins":            round(gross_profit, 2),
            "losses":          round(gross_loss, 2),
            "circuit_breakers": circuit_breakers,
            "market_status":   market_msg,
            "order_verify":    order_verify,
            "feed": {
                "subscribed": len(feed_stats.get("subscribed", [])),
                "tick_total": feed_stats.get("tick_total", sum(feed_stats.get("tick_counts", {}).values())),
                "updated_at": feed_stats.get("updated_at"),
            },
        },
    })


@app.route("/api/trading-state", methods=["GET", "POST"])
def api_trading_state():
    if request.method == "GET":
        return jsonify({"ok": True, "state": get_trading_session_state()})

    # Ensure admin for state changes
    if not current_user.is_authenticated or current_user.role not in ['admin', 'emergency_admin']:
        return jsonify({"ok": False, "error": "Admin required"}), 403

    body = request.get_json(silent=True) or {}
    
    # Payload confirmation required
    if body.get("confirm_action") not in ["RESUME", "LOCK"]:
        return jsonify({"ok": False, "error": "Explicit confirmation payload required"}), 400

    action = str(body.get("action") or "").strip().lower()
    if action == "resume":
        state = resume_entries("DASHBOARD_RESUME_TRADING")
        return jsonify({"ok": True, "state": state})
    if action in {"lock", "flat_lock"}:
        state = lock_entries("DASHBOARD_MANUAL_LOCK")
        return jsonify({"ok": True, "state": state})
    return jsonify({"ok": False, "error": "Unknown trading-state action"}), 400


@app.route("/api/margin-status")
def api_margin_status():
    """
    🔥 NEW: Margin monitoring endpoint.
    Returns current margin deployment status.
    """
    try:
        from core.order_executor import get_capital_snapshot, get_margin_status
        margin = get_margin_status()
        capital_snapshot = get_capital_snapshot()
        allow_margin = cfg("risk", "allow_margin", False)
        forced_buy = cfg("risk", "forced_buy_margin", False)

        return jsonify({
            "ok": True,
            "margin_enabled": allow_margin,
            "forced_buy_enabled": forced_buy,
            "capital_snapshot": capital_snapshot,
            "account_equity": margin.get("account_equity", 0),
            "free_margin": margin.get("free_margin", 0),
            "gross_exposure": margin.get("gross_exposure", 0),
            "margin_blocked": margin.get("margin_blocked", 0),
            "remaining_buying_power": margin.get("remaining_buying_power", 0),
            "margin_utilization": round(margin.get("margin_utilization", 0), 2),
            "margin_leverage": margin.get("margin_leverage", 1.0),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "margin_enabled": cfg("risk", "allow_margin", False),
            "forced_buy_enabled": cfg("risk", "forced_buy_margin", False),
        })


@app.route("/api/emergency-squareoff", methods=["POST"])
@admin_required
def api_emergency_squareoff():
    """
    🚨 EMERGENCY: Close all open positions immediately.
    Requires POST to prevent accidental clicks.
    """
    body = request.get_json(silent=True) or {}
    if body.get("confirm_action") != "SQUARE_OFF":
        return jsonify({"ok": False, "error": "Explicit confirmation payload required"}), 400

    trading_mode = os.getenv("TRADING_MODE", "PAPER")

    try:
        from core.state_manager import lock_entries, get_trading_session_state, get_open_positions, mark_liquidating
        import logging
        import time
        
        logger = logging.getLogger(__name__)

        # Get initial count of open positions
        initial_open_count = len(get_open_positions())

        if initial_open_count == 0:
            # No positions to close, but still lock entries so the system pauses
            lock_entries("EMERGENCY_SQUAREOFF_NO_OPEN_POSITIONS")
            return jsonify({
                "ok": True,
                "status": "COMPLETED",
                "closed_count": 0,
                "failed_count": 0,
                "details": [],
                "timestamp": datetime.now().isoformat(),
            })

        # 1. Trigger the main thread to do the squareoff
        # The main thread has the websocket connected for live prices
        mark_liquidating("EMERGENCY_SQUAREOFF_REQUESTED")
        logger.info("Emergency squareoff requested - delegated to main thread.")

        # P2-2 FIX: detect whether the engine is actually alive. If its heartbeat is
        # stale, the delegated request would never be consumed — so fall back to an
        # in-process squareoff (uses yfinance/entry-price fallback for pricing).
        engine_alive = False
        try:
            hb = safe_read_json(
                os.path.join(_ROOT, "data", "engine_heartbeat.json"), {},
                expected_type=dict, label="engine heartbeat", log=app.logger,
            )
            engine_alive = (time.time() - float(hb.get("ts", 0))) < 90
        except Exception:
            engine_alive = False

        # 2. Wait for main thread to process it (terminal reasons: SUCCESS/PARTIAL/FAILED)
        timeout = 15
        start_time = time.time()
        completed = False
        terminal_reason = ""

        if engine_alive:
            while time.time() - start_time < timeout:
                state = get_trading_session_state()
                reason = state.get("reason", "")
                if any(tag in reason for tag in (
                    "EMERGENCY_SQUAREOFF_SUCCESS",
                    "EMERGENCY_SQUAREOFF_PARTIAL",
                    "EMERGENCY_SQUAREOFF_FAILED",
                    "EMERGENCY_SQUAREOFF_TRIGGER_COMPLETE",
                )):
                    completed = True
                    terminal_reason = reason
                    break
                time.sleep(0.5)

        if not completed:
            # Engine dead or did not respond in time → last-resort in-process squareoff.
            logger.warning(
                "Engine %s; performing DIRECT in-process emergency squareoff as fallback.",
                "did not confirm" if engine_alive else "appears DEAD (stale heartbeat)",
            )
            try:
                from core.emergency_squareoff import emergency_square_off_all
                fb = emergency_square_off_all()
                terminal_reason = f"DASHBOARD_FALLBACK_{fb.get('status', 'UNKNOWN')}"
                completed = fb.get("status") in ("SUCCESS", "PARTIAL")
            except Exception as fb_err:
                logger.error("Dashboard fallback squareoff failed: %s", fb_err, exc_info=True)

        # 3. Calculate results based on remaining open positions
        final_open_count = len(get_open_positions())
        closed_count = initial_open_count - final_open_count
        failed_count = final_open_count

        status = "COMPLETED" if final_open_count == 0 else ("PARTIAL" if closed_count > 0 else "FAILED")
        response_data = {
            "ok": True,
            "status": status,
            "engine_alive": engine_alive,
            "reason": terminal_reason,
            "closed_count": max(0, closed_count),
            "failed_count": max(0, failed_count),
            "details": [],
            "timestamp": datetime.now().isoformat(),
        }

        logger.info(f"Squareoff complete. Closed: {response_data['closed_count']}, Failed: {response_data['failed_count']}")
        return jsonify(response_data)

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Emergency squareoff signal failed: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e) if str(e) else "Failed to execute squareoff. Please check the system log.",
        }), 500


@app.route("/api/adaptive")
def api_adaptive():
    if not ADAPTIVE_AVAILABLE:
        return jsonify({"ok": False, "error": "Adaptive engine not available"}), 503

    raw_signals = get_all_signal_stats()
    windows = get_all_time_window_stats()
    symbols = get_all_symbol_stats()
    config_summary = get_adaptive_config_summary()

    multiplier_rows = _reflection_db_query(
        "SELECT multiplier_type, multiplier_key, multiplier_value, raw_calculated_value, sample_size, confidence_strength, last_updated "
        "FROM multiplier_history ORDER BY multiplier_type, multiplier_key"
    )
    signals = _strategy_set_performance_rows(raw_signals, multiplier_rows)

    total_trades = sum(s.get("total_trades", 0) for s in signals)
    total_wins = sum(s.get("winning_trades", 0) for s in signals)
    overall_win_rate = round((total_wins / total_trades * 100) if total_trades > 0 else 0.0, 1)

    change_history = _reflection_db_query(
        "SELECT multiplier_type, multiplier_key, previous_value, new_value, raw_calculated_value, "
        "sample_size, confidence_strength, reason_source, timestamp "
        "FROM multiplier_change_log ORDER BY timestamp DESC LIMIT 50"
    )

    config_history = _reflection_db_query(
        "SELECT config_date, changes_made, timestamp "
        "FROM config_history ORDER BY timestamp DESC LIMIT 20"
    )

    ref_path = os.path.join(REFLECTIONS_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.json")
    reflection = safe_read_json(
        ref_path,
        {},
        expected_type=dict,
        label="adaptive reflection",
        log=app.logger,
    )

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
        trade_count = stats.get("total_trades", 0)
        set_name = stats.get("set_name") or stats.get("signal_name")
        if 0 < trade_count < 10 and stats.get("win_rate", 0) < 50:
            alerts.append({
                "level": "warning",
                "message": f"Low sample size: {set_name} has only {trade_count} trades",
            })
        policy = stats.get("execution_policy") or {}
        if policy.get("suppressed"):
            alerts.append({
                "level": "danger",
                "message": policy.get("reason") or f"{set_name} execution-suppressed",
            })
        elif trade_count >= 10 and stats.get("win_rate", 0) < 40:
            alerts.append({
                "level": "warning",
                "message": f"{set_name} historically weak ({stats.get('win_rate', 0):.1f}%) but not execution-blocked without recent confirmation",
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
        "strategy_sets": signals,
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
