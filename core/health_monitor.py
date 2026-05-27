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
    """Verify trading briefing is loaded."""
    try:
        from core.state_manager import load_briefing
        
        briefing = load_briefing()
        if not briefing:
            return False, "No briefing found"
        
        approved = len(briefing.get("approved_stocks", []))
        watchlist = len(briefing.get("watchlist", []))
        
        if approved + watchlist == 0:
            return False, "Briefing has no stocks"
        
        return True, f"Briefing OK ({approved} war room, {watchlist} watchlist)"
    except Exception as e:
        return False, str(e)


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
    
    return health


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_preflight_checks()
    print(result.report())
    exit(0 if result.passed() else 1)
