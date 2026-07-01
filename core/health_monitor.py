# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/health_monitor.py — Production Health & Diagnostics
#   Pre-flight checks, system diagnostics, live mode validation
# ============================================================

import logging
import os
import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

from core.trading_settings import get as cfg

class HealthCheck:
    """System health check result."""
    def __init__(self):
        self.checks: Dict[str, bool] = {}
        self.errors: Dict[str, str] = {}
        self.warnings: List[str] = []
        self.timestamp = datetime.now().isoformat()
    
    def passed(self) -> bool:
        return all(self.checks.values()) and not self.errors
    
    def report(self) -> str:
        lines = [
            f"\n{'='*70}",
            f"SYSTEM HEALTH REPORT — {self.timestamp}",
            f"{'='*70}",
        ]
        
        # Passed checks
        passed = [k for k, v in self.checks.items() if v]
        if passed:
            lines.append(f"\n✅ PASSED ({len(passed)}):")
            for check in passed:
                lines.append(f"   • {check}")
        
        # Failed checks
        failed = [k for k, v in self.checks.items() if not v]
        if failed:
            lines.append(f"\n❌ FAILED ({len(failed)}):")
            for check in failed:
                lines.append(f"   • {check}")
                if check in self.errors:
                    lines.append(f"     Error: {self.errors[check]}")
        
        # Warnings
        if self.warnings:
            lines.append(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                lines.append(f"   • {warning}")
        
        lines.append(f"\n{'='*70}\n")
        return "\n".join(lines)


def check_broker_connection() -> Tuple[bool, str]:
    """Test Kotak Neo connection."""
    try:
        from core.kotak_client import get_client, is_session_alive
        
        client = get_client()
        if not is_session_alive():
            return False, "Session health check failed"
        
        return True, "Connected"
    except Exception as e:
        return False, str(e)


def check_database() -> Tuple[bool, str]:
    """Verify database is accessible and contains data."""
    try:
        from core.state_manager import get_open_positions, get_today_stats
        
        # Try to access DB
        positions = get_open_positions()
        stats = get_today_stats()
        
        return True, f"DB OK ({len(positions)} open positions)"
    except Exception as e:
        return False, str(e)


def check_briefing() -> Tuple[bool, str]:
    """Verify trading briefing is loaded and valid."""
    try:
        from core.state_manager import load_briefing, validate_briefing
        
        briefing = load_briefing()
        
        # Use comprehensive validation
        is_valid, reason = validate_briefing(briefing)
        if not is_valid:
            return False, f"Briefing invalid: {reason}"
        
        approved = len(briefing.get("approved_stocks", []))
        watchlist = len(briefing.get("watchlist", []))
        total = approved + watchlist
        
        return True, f"Briefing OK ({approved} cognition, {watchlist} watchlist, {total} total)"
    except Exception as e:
        return False, f"Error validating briefing: {str(e)[:80]}"


def check_live_feed(strict: bool = False) -> Tuple[bool, str]:
    """Verify market data is flowing."""
    try:
        from core.data_fetcher import get_all_symbols, has_enough_history, get_feed_stats
        
        stats = get_feed_stats()
        symbols = get_all_symbols() or stats.get("subscribed", [])
        tick_total = sum(stats.get("tick_counts", {}).values())

        if not symbols:
            if strict:
                return False, "No symbols subscribed"
            return True, "Feed not started yet (starts after briefing load)"

        if strict and tick_total == 0:
            return False, f"Subscribed to {len(symbols)} symbols but no ticks yet"

        min_candles = int(cfg("market_data", "health_min_ws_candles", 4))
        ready_count = sum(
            1 for s in symbols
            if has_enough_history(s, min_candles=min_candles)
        )

        if ready_count == 0:
            if tick_total > 0:
                return True, (
                    f"Ticks flowing ({tick_total} total); "
                    f"building candles ({min_candles}+ needed for health OK)"
                )
            return False, (
                f"No symbol has {min_candles}+ live candles yet "
                f"({len(symbols)} subscribed)"
            )

        return True, (
            f"Feed OK ({ready_count}/{len(symbols)} have {min_candles}+ candles)"
        )
    except Exception as e:
        return False, str(e)


def check_api_credentials() -> Tuple[bool, str]:
    """Verify all API keys are configured."""
    required = {
        "KOTAK_CONSUMER_KEY": "Kotak broker",
        "KOTAK_MOBILE_NUMBER": "Kotak broker",
        "KOTAK_UCC": "Kotak broker",
        "KOTAK_MPIN": "Kotak broker",
        "KOTAK_TOTP_SECRET": "Kotak broker",
        "GEMINI_API_KEY": "Google Gemini (Fundamental)",
        "GROQ_API_KEY": "Groq (Risk Manager)",
        "OPENROUTER_KEY_1": "OpenRouter (Reflection)",
        "OPENROUTER_KEY_2": "OpenRouter (Mediator)",
        "OPENROUTER_KEY_3": "OpenRouter (Fundamental)",
        "OPENROUTER_KEY_4": "OpenRouter (Technical/Standby)",
    }
    
    missing = [k for k, v in required.items() if not os.getenv(k)]

    placeholders = []
    for key in required:
        val = (os.getenv(key) or "").strip().lower()
        if not val:
            continue
        if "your_" in val or val.endswith("_here") or val == "changeme":
            placeholders.append(key)

    if missing:
        return False, f"Missing: {', '.join(missing)}"
    if placeholders:
        return False, f"Placeholder values (set real keys): {', '.join(placeholders)}"

    return True, "All API keys configured"


def check_market_hours() -> Tuple[bool, str]:
    """Trading day + session window (weekends & NSE holidays excluded)."""
    from core.market_calendar import market_status_message
    return market_status_message()


def check_capital() -> Tuple[bool, str]:
    """Verify capital is sufficient."""
    try:
        from core.order_executor import _get_available_capital
        
        capital = _get_available_capital()
        min_capital = 500
        
        if capital < min_capital:
            return False, f"Capital ₹{capital:.0f} below minimum ₹{min_capital}"
        
        return True, f"Capital ₹{capital:.0f}"
    except Exception as e:
        return False, str(e)


def check_max_daily_loss() -> Tuple[bool, str]:
    """Verify we haven't exceeded daily loss limit."""
    try:
        from core.order_executor import check_max_daily_loss as loss_limit_hit
        
        if loss_limit_hit():
            return False, "Max daily loss exceeded"
        
        return True, "Daily loss OK"
    except Exception as e:
        return False, str(e)


def run_preflight_checks() -> HealthCheck:
    """Run all preflight checks before trading."""
    health = HealthCheck()
    
    logger.info("🔍 Running preflight health checks...")
    
    checks = [
        ("API Credentials", check_api_credentials),
        ("Broker Connection", check_broker_connection),
        ("Database", check_database),
        ("Trading Briefing", check_briefing),
        ("Live Feed", check_live_feed),
        ("Market Hours", check_market_hours),
        ("Available Capital", check_capital),
        ("Daily Loss Limit", check_max_daily_loss),
    ]
    
    for name, check_fn in checks:
        try:
            passed, msg = check_fn()
            health.checks[name] = passed
            status = "✅" if passed else "❌"
            logger.info(f"{status} {name}: {msg}")
        except Exception as e:
            health.checks[name] = False
            health.errors[name] = str(e)
            logger.error(f"❌ {name}: {e}")
    
    logger.info(health.report())
    return health


def continuous_monitoring() -> HealthCheck:
    """Lightweight health check during trading."""
    health = HealthCheck()

    # Reconcile any LIVE orders still awaiting broker confirmation
    try:
        import os
        if os.getenv("TRADING_MODE", "PAPER") == "LIVE":
            from core.order_verifier import reconcile_pending_orders
            recon = reconcile_pending_orders()
            pending = recon.get("still_pending", 0)
            if pending:
                health.warnings.append(
                    f"{pending} order(s) still pending broker verification"
                )
    except Exception as e:
        logger.warning(f"Order reconciliation skipped: {e}")
    
    checks = [
        ("Broker Connection", check_broker_connection),
        ("Database", check_database),
        ("Live Feed", lambda: check_live_feed(strict=True)),
        ("Daily Loss Limit", check_max_daily_loss),
    ]
    
    for name, check_fn in checks:
        try:
            passed, msg = check_fn()
            health.checks[name] = passed
            if not passed:
                health.errors[name] = msg
                logger.warning(f"⚠️  {name}: {msg}")
        except Exception as e:
            health.checks[name] = False
            health.errors[name] = str(e)
            logger.error(f"❌ {name}: {e}")

    # ── S1 FIX: ACT on failures, don't just log them ──────────────────────────
    # Previously this watchdog only appended warnings/errors and nothing consumed
    # the result, so a dead feed/broker went unhandled while positions sat
    # unmonitored. Now we self-heal (reconnect / restart feed) and escalate.
    try:
        _act_on_health(health)
    except Exception as e:
        logger.error(f"Health escalation handler failed: {e}", exc_info=True)

    return health


# Escalation state (module-level, throttles alerts and tracks streaks)
_consec_fail: Dict[str, int] = {}
_last_alert_ts: Dict[str, float] = {}
_HEALTH_ALERT_COOLDOWN_SEC = 300  # don't re-alert the same subsystem more than every 5 min


def _throttled_alert(key: str, message: str):
    now = time.time()
    if now - _last_alert_ts.get(key, 0.0) < _HEALTH_ALERT_COOLDOWN_SEC:
        return
    _last_alert_ts[key] = now
    try:
        from core.alerts import alert_critical
        alert_critical(message)
    except Exception as e:
        logger.error(f"alert_critical failed for {key}: {e}")


def _act_on_health(health: "HealthCheck"):
    """Self-heal + escalate on continuous-monitoring failures (S1)."""
    import os as _os

    has_open_positions = False
    market_open = False
    try:
        from core.state_manager import get_open_positions
        has_open_positions = len(get_open_positions()) > 0
    except Exception:
        pass
    try:
        from core.data_fetcher import _is_market_open
        market_open = _is_market_open()
    except Exception:
        pass

    # Track consecutive failures per subsystem
    for name, passed in health.checks.items():
        _consec_fail[name] = 0 if passed else _consec_fail.get(name, 0) + 1

    # 1) BROKER CONNECTION dead → re-authenticate (also rebinds the feed via R1 fix)
    if not health.checks.get("Broker Connection", True):
        logger.error("🔧 Health: broker connection down — attempting force_reconnect()")
        try:
            from core.kotak_client import force_reconnect
            force_reconnect()
            logger.info("✅ Health: force_reconnect() completed")
        except Exception as e:
            logger.error(f"force_reconnect() failed: {e}")
        _throttled_alert(
            "broker",
            f"🚨 Broker connection DOWN ({_consec_fail.get('Broker Connection', 1)}x). "
            f"Auto re-auth attempted. Open positions: {has_open_positions}.",
        )

    # 2) LIVE FEED dead during market hours → restart feed; escalate if positions open
    if market_open and not health.checks.get("Live Feed", True):
        logger.error("🔧 Health: live feed stalled during market hours — restarting feed")
        try:
            from core.data_fetcher import restart_live_feed
            restart_live_feed()
        except Exception as e:
            logger.error(f"restart_live_feed() failed: {e}")
        if has_open_positions:
            _throttled_alert(
                "feed",
                f"🚨 Live feed STALLED with OPEN positions "
                f"({_consec_fail.get('Live Feed', 1)}x). Software stop-loss is BLIND. "
                f"Feed restart attempted — verify broker/network immediately.",
            )

    # 3) DAILY LOSS breached → order_executor.check_max_daily_loss already halts trading.
    #    Escalate so the operator is notified.
    if not health.checks.get("Daily Loss Limit", True):
        _throttled_alert(
            "daily_loss",
            "🚨 Max daily loss breached — new entries halted by circuit breaker. "
            "Review open positions.",
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_preflight_checks()
    print(result.report())
    exit(0 if result.passed() else 1)


def check_system_health():
    """Check overall system health status."""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        from core.state_manager import get_open_positions
        from core.trading_settings import get as cfg
        
        health = {
            "status": "OK",
            "open_positions": len(get_open_positions()),
            "max_positions": cfg("strategy", "max_open_positions", 2),
            "capital": cfg("risk", "paper_capital", 100000),
            "errors": []
        }
        
        # Check if we're approaching position limit
        if health["open_positions"] >= health["max_positions"]:
            health["status"] = "WARNING"
            health["errors"].append("Position limit approaching")
        
        return health
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "ERROR", "errors": [str(e)]}
