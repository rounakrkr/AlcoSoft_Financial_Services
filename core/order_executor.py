# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/order_executor.py — Order Placement & Management
#   Changes: SL-M order on Kotak after BUY, trading_symbol fix,
#   profit targets, signal-set exit, trailing SL,
#   max daily loss check, squareoff flag
# ============================================================

import logging
import os
import threading
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from core.token_validator import (
    validate_and_fix_session_before_order,
    ensure_trade_token_on_client,
    JWTTokenValidator,
    diagnose_token_health,
)
from core.state_manager import (
    save_open_position, close_position, get_open_positions,
    update_trailing_sl, update_sl_order_id,
    update_tsl_activation_state, get_tsl_activation_state,
    get_today_gross_pnl,
    entries_are_enabled,
    lock_entries,
    mark_liquidating,
    mark_position_reconciliation_pending,
)
from core.audit_logger import (
    audit_order_placed, audit_position_closed, audit_system_error,
)
from core.trading_settings import get as cfg
from core.safe_io import safe_float, safe_int
from core.circuit_breaker import get_breaker
from core.api_resilience import call_broker_api
from core.order_verifier import (
    record_order_sent,
    wait_for_order_verification,
    wait_for_sl_verification,
)
from reflection.reflection_engine import record_trade
from core.alerts import alert_critical

load_dotenv()
logger = logging.getLogger(__name__)


class OrderExecutionError(Exception):
    """Raised when a LIVE broker order fails — trips order circuit breaker."""

def get_nse_tick_size(price: float) -> float:
    """Returns the NSE tick size based on the revised 2025 rules."""
    if price < 250:
        return 0.01
    elif price <= 1000:
        return 0.05
    elif price <= 5000:
        return 0.10
    elif price <= 10000:
        return 0.50
    elif price <= 20000:
        return 1.00
    else:
        return 5.00

def round_to_tick(price: float) -> float:
    """Rounds a price to its nearest valid NSE tick size."""
    tick = get_nse_tick_size(price)
    return round(price / tick) * tick


# ── Secrets / mode stay in .env ───────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")
INTRADAY_SQUAREOFF = dt_time(15, 15)
_capital_cache: float = 10000.0
_capital_last_update: float = 0.0
CAPITAL_CACHE_TTL = 300
_order_lock = threading.RLock()
_capital_api_failures: int = 0
_CAPITAL_API_FAILURE_ALERT_THRESHOLD = 3

# ── Squareoff flag — prevents repeated calls after 3:15 ──────
# _squareoff_done_date guards against the flag sticking across days: it is reset
# whenever the calendar date rolls over so EOD squareoff fires every trading day.
_squareoff_done = False
_squareoff_done_date = None
RISK_REDUCING_SELL_REASONS = {
    "STOPLOSS",
    "TRAILING_SL",
    "TARGET",
    "SQUAREOFF",
    "EMERGENCY_SQUAREOFF",
    "MANUAL_SQUAREOFF",
}

# ── Configuration validation ──────────────────────────────────
_config_validated = False
_config_warnings = []

# ── F002: Broker SL recovery — per-symbol backoff state ─────────────────────
# Tracks recovery attempts for positions missing a broker-side SL-M order.
# State per symbol: {last_attempt_ts, next_delay_sec, attempt_count}
# Prevents API spam and log flooding during prolonged broker outages.
_sl_recovery_state: dict[str, dict] = {}
_SL_RECOVERY_INITIAL_DELAY_SEC: int = 30    # First retry 30s after failure
_SL_RECOVERY_MAX_DELAY_SEC:     int = 300   # Cap at 5 minutes between retries
_SL_RECOVERY_BACKOFF_FACTOR:    int = 2     # Double delay each time
_SL_RECOVERY_LOG_EVERY_N:       int = 5     # Log a summary every N failures


def _is_risk_reducing_sell(reason: str) -> bool:
    return str(reason or "").strip().upper() in RISK_REDUCING_SELL_REASONS


def _extract_broker_fill_price(row: dict | None) -> float:
    if not isinstance(row, dict):
        return 0.0
    for key in (
        "avgPrc",
        "avgPrice",
        "averagePrice",
        "average_price",
        "trdPrice",
        "tradedPrice",
        "fillPrice",
        "price",
        "prc",
    ):
        price = safe_float(row.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def _resolve_liquidation_price(
    position: dict,
    live_prices: dict[str, float] | None,
) -> tuple[float, str]:
    symbol = str(position.get("symbol", "")).strip().upper()
    current = safe_float((live_prices or {}).get(symbol), 0.0)
    if current > 0:
        return current, "ws_ltp"

    try:
        from core.data_fetcher import get_latest_tick
        tick = get_latest_tick(symbol)
        if tick:
            tick_price = safe_float(tick.get("ltp"), 0.0)
            if tick_price > 0:
                return tick_price, "latest_tick"
    except Exception:
        logger.debug("Latest tick lookup failed during liquidation for %s", symbol, exc_info=True)

    # Attempt 3: yfinance fallback for squareoff pricing
    try:
        import yfinance as yf
        nse_ticker = f'{symbol}.NS'
        data = yf.Ticker(nse_ticker, session=None)
        if data and data.info:
            price = safe_float(
                data.info.get('currentPrice') or data.info.get('regularMarketPrice'), 0.0
            )
            if price > 0:
                logger.info('Liquidation price for %s from yfinance: Rs%s', symbol, price)
                return price, 'yfinance_fallback'
    except Exception as e:
        logger.debug('yfinance liquidation fallback failed for %s: %s', symbol, e)

    return 0.0, "missing_quote"


def validate_allocation_config() -> list:
    """
    Validate capital allocation configuration.
    Returns list of warnings/errors found.
    Should be called at startup and when settings change.
    """
    global _config_validated, _config_warnings
    warnings = []
    
    from core.strategy import MAX_POSITIONS
    
    try:
        # Read settings
        allow_margin = cfg("risk", "allow_margin", False)
        margin_leverage = safe_float(cfg("risk", "margin_leverage", 2.0), 2.0)
        position_size_margin = safe_float(cfg("risk", "position_size_margin", 0.75), 0.75)
        risk_pct = safe_float(cfg("risk", "max_risk_per_trade", 0.02), 0.02)
        max_open = MAX_POSITIONS
        paper_capital = safe_float(cfg("risk", "paper_capital", 10000), 10000)
        
        # Margin validation
        if allow_margin:
            if margin_leverage < 1.0 or margin_leverage > 5.0:
                warnings.append(
                    f"⚠️ margin_leverage={margin_leverage} out of range [1.0-5.0], will be clamped"
                )
            
            if position_size_margin < 0.10 or position_size_margin > 1.0:
                warnings.append(
                    f"⚠️ position_size_margin={position_size_margin} out of range [0.10-1.0], will be clamped"
                )
            
            # Check if allocation is realistic
            per_position_budget = (paper_capital * margin_leverage * position_size_margin) / max_open
            if per_position_budget < paper_capital * 0.01:  # Less than 1% per position
                warnings.append(
                    f"⚠️ Per-position budget (₹{per_position_budget:.0f}) is very small; "
                    f"only {max_open} positions × 1% minimum = {max_open}% portfolio usage"
                )
        
        # Position count validation
        if max_open < 1 or max_open > 10:
            warnings.append(
                f"⚠️ max_open_positions={max_open} out of range [1-10]"
            )
        
        # Risk validation
        if risk_pct < 0.005 or risk_pct > 0.10:
            warnings.append(
                f"⚠️ risk_pct={risk_pct*100:.2f}% out of typical range [0.5%-10%]"
            )
        
        # Capital validation
        if paper_capital < 5000:
            warnings.append(
                f"⚠️ paper_capital=₹{paper_capital} is very small, may not support {max_open} positions"
            )
        
        # Combined validation
        if allow_margin and max_open > 1:
            total_allocation_pct = position_size_margin * 100
            if total_allocation_pct < 50:
                warnings.append(
                    f"⚠️ position_size_margin={position_size_margin*100:.0f}% means less than "
                    f"{total_allocation_pct:.0f}% of margin is used across all {max_open} positions"
                )
        
        _config_warnings = warnings
        _config_validated = True
        
        if warnings:
            logger.warning(f"⚠️ Configuration warnings ({len(warnings)}):")
            for w in warnings:
                logger.warning(w)
        else:
            logger.info("✅ Allocation configuration valid")
        
        return warnings
        
    except Exception as e:
        error_msg = f"❌ Configuration validation failed: {e}"
        logger.error(error_msg, exc_info=True)
        return [error_msg]


# NOTE: Validation is now called explicitly via validate_allocation_config() 
# when needed (startup, pre-market check, etc.), NOT at module load time.
# This avoids circular import issues with core.strategy during initialization.



# ════════════════════════════════════════════════════════════
#   CAPITAL ALLOCATION HELPERS (NEW - Multi-Constraint Model)
# ════════════════════════════════════════════════════════════

def calculate_total_buying_power() -> dict:
    """
    Calculate total buying power considering margin.
    Returns dict with detailed breakdown.
    
    Returns: {
        'real_capital': float,           # Actual capital available
        'margin_leverage': float,        # 1.0 (no margin) to 5.0x
        'total_buying_power': float,     # real_capital × margin_leverage
        'position_size_margin': float,   # % of portfolio for positions (10-100%)
        'allow_margin': bool,            # Is margin enabled?
    }
    """
    real_capital = _capital_base_for_sizing()
    allow_margin = cfg("risk", "allow_margin", False)
    
    if allow_margin:
        margin_leverage = safe_float(cfg("risk", "margin_leverage", 2.0), 2.0)
        margin_leverage = max(1.0, min(5.0, margin_leverage))
    else:
        margin_leverage = 1.0
    
    position_size_margin = safe_float(cfg("risk", "position_size_margin", 0.75), 0.75)
    position_size_margin = max(0.10, min(1.0, position_size_margin))
    
    total_buying_power = real_capital * margin_leverage
    
    return {
        'real_capital': real_capital,
        'margin_leverage': margin_leverage,
        'total_buying_power': total_buying_power,
        'position_size_margin': position_size_margin,
        'allow_margin': allow_margin,
    }


def _capital_base_for_sizing() -> float:
    """
    Account capital base for sizing.

    Open positions are handled by the deployment/leverage constraint. In PAPER
    mode, using cash-after-open-positions here collapses buying power to zero
    as soon as a margin-sized position exists.
    """
    if TRADING_MODE == "PAPER":
        initial = max(0.0, safe_float(cfg("risk", "paper_capital", 10000), 10000.0))
        closed_pnl = safe_float(get_today_gross_pnl(), 0.0)
        return max(0.0, initial + closed_pnl)

    return max(0.0, safe_float(_get_available_capital(), 0.0))


def calculate_per_position_budget(buying_power_info: dict, max_open_positions: int = 4) -> dict:
    """
    Calculate per-position budget allocation.
    Divides total buying power among max_open_positions.
    
    Budget = (total_buying_power × position_size_margin) / max_open_positions
    
    Returns: {
        'total_buying_power': float,        # From input
        'position_size_margin': float,      # From input
        'max_open_positions': int,          # From input
        'portfolio_allocation_pct': float,  # Used by all positions
        'per_position_budget': float,       # Budget for each position
    }
    """
    max_open_positions = max(1, min(10, safe_int(max_open_positions, 4)))
    
    total_buying_power = safe_float(buying_power_info.get('total_buying_power', 0.0), 0.0)
    position_size_margin = safe_float(buying_power_info.get('position_size_margin', 0.75), 0.75)
    
    portfolio_allocation_pct = position_size_margin * 100  # What % of total to use
    allocated_capital = total_buying_power * position_size_margin
    per_position_budget = allocated_capital / max_open_positions
    
    return {
        'total_buying_power': total_buying_power,
        'position_size_margin': position_size_margin,
        'max_open_positions': max_open_positions,
        'portfolio_allocation_pct': portfolio_allocation_pct,
        'per_position_budget': per_position_budget,
    }


def analyze_quantity_constraints(
    price: float,
    stop_loss: float,
    risk_pct: float,
    real_capital: float,
    per_position_budget: float,
    deployed_now: float = 0.0,
    total_buying_power: float = None,
) -> dict:
    """
    Analyze binding constraints on quantity (2026-06-01 REVISED):
    
    THREE CONSTRAINTS (capital constraint removed to honor margin configuration):
    1. Risk constraint (max_loss / stop_dist)
    2. Allocation constraint (per_position_budget / price)
    3. Leverage constraint (total_buying_power availability)
    
    Over-leverage validation is performed separately as a safety net.
    
    Returns: {
        'risk_qty': float,              # From risk tolerance
        'allocation_qty': float,        # From per-position budget
        'leverage_qty': float,          # From leverage limit
        'final_qty': int,               # MIN of 3 constraints (as integer)
        'limiting_constraint': str,     # Which constraint was tightest
        'all_constraints': dict,        # Debug info for all values
    }
    """
    price = safe_float(price, 0.0)
    stop_loss = safe_float(stop_loss, 0.0)
    risk_pct = max(0.0, safe_float(risk_pct, 0.0))
    real_capital = max(0.0, safe_float(real_capital, 0.0))
    per_position_budget = max(0.0, safe_float(per_position_budget, 0.0))
    deployed_now = max(0.0, safe_float(deployed_now, 0.0))
    
    # CONSTRAINT 1: Risk constraint
    max_loss = real_capital * risk_pct
    stop_dist = abs(price - stop_loss)
    if stop_dist <= 0:
        stop_dist = max(1.0, price * 0.01)
    risk_qty = max_loss / stop_dist if stop_dist > 0 else 0
    
    # CONSTRAINT 2: Allocation constraint
    allocation_qty = per_position_budget / price if price > 0 else 0
    
    # CONSTRAINT 3: Leverage constraint (can't deploy more than total buying power)
    if total_buying_power is None:
        total_buying_power = real_capital  # Fallback (no margin)
    total_buying_power = max(0.0, safe_float(total_buying_power, 0.0))
    available_to_deploy = max(0.0, total_buying_power - deployed_now)
    leverage_qty = available_to_deploy / price if price > 0 else 0
    
    # Take minimum of 3 binding constraints
    quantities = {
        'risk': risk_qty,
        'allocation': allocation_qty,
        'leverage': leverage_qty,
    }
    
    final_qty = max(0, int(min(quantities.values())))
    
    # Identify which constraint was limiting
    if final_qty <= 0:
        limiting_constraint = 'insufficient_all'
    else:
        # Find which constraint produced the minimum
        min_qty = final_qty
        limiting_constraint = 'unknown'
        for constraint_name, constraint_qty in quantities.items():
            if int(constraint_qty) == min_qty:
                limiting_constraint = constraint_name
                break
    
    return {
        'risk_qty': risk_qty,
        'allocation_qty': allocation_qty,
        'leverage_qty': leverage_qty,
        'final_qty': final_qty,
        'limiting_constraint': limiting_constraint,
        'all_constraints': quantities,
    }


def get_allocation_metrics() -> dict:
    """
    Get current allocation metrics for dashboard.
    Shows real-time deployment and constraints.
    
    Returns: {
        'real_capital': float,
        'total_buying_power': float,
        'margin_leverage': float,
        'deployed_capital': float,
        'available_for_new_position': float,
        'available_real_capital': float,
        'position_count': int,
        'max_open_positions': int,
        'per_position_budget': float,
        'portfolio_allocation_pct': float,
        'current_leverage_ratio': float,
        'margin_usage_pct': float,
        'can_open_new_position': bool,
        'reason_if_cannot': str,
    }
    """
    from core.strategy import MAX_POSITIONS
    
    bp_info = calculate_total_buying_power()
    budget_info = calculate_per_position_budget(bp_info, MAX_POSITIONS)
    margin_status = get_margin_status()
    
    real_capital = bp_info['real_capital']
    total_buying_power = bp_info['total_buying_power']
    margin_leverage = bp_info['margin_leverage']
    
    deployed_capital = safe_float(margin_status.get('current_position_value'), 0.0)
    available_for_new = total_buying_power - deployed_capital
    
    # Calculate real capital usage
    deployed_entry_value = safe_float(margin_status.get('entry_position_value'), 0.0)
    available_real_capital = max(0, real_capital - deployed_entry_value)
    
    position_count = len(get_open_positions())
    per_position_budget = budget_info['per_position_budget']
    portfolio_allocation_pct = budget_info['portfolio_allocation_pct']
    
    current_leverage = (deployed_capital / real_capital) if real_capital > 0 else 1.0
    current_leverage = max(1.0, current_leverage)
    margin_usage_pct = max(0, (current_leverage - 1.0) * 100)
    
    can_open = position_count < MAX_POSITIONS and available_for_new > 0
    reason = ""
    if position_count >= MAX_POSITIONS:
        reason = f"Max positions reached ({MAX_POSITIONS})"
    elif available_for_new <= 0:
        reason = "No buying power available"
    
    return {
        'real_capital': real_capital,
        'total_buying_power': total_buying_power,
        'margin_leverage': margin_leverage,
        'deployed_capital': deployed_capital,
        'available_for_new_position': available_for_new,
        'available_real_capital': available_real_capital,
        'position_count': position_count,
        'max_open_positions': MAX_POSITIONS,
        'per_position_budget': per_position_budget,
        'portfolio_allocation_pct': portfolio_allocation_pct,
        'current_leverage_ratio': current_leverage,
        'margin_usage_pct': margin_usage_pct,
        'can_open_new_position': can_open,
        'reason_if_cannot': reason,
    }


# ════════════════════════════════════════════════════════════
#   POSITION SIZING
# ════════════════════════════════════════════════════════════

def calculate_quantity(symbol: str, price: float, stop_loss: float, risk_pct: float = None) -> int:
    """
    🚀 REDESIGNED (FX11-A): Dynamic Broker Margin Architecture.
    Calculates quantity using real API-discovered margin blocks instead of static leverage.
    Fails CLOSED if API is unreachable in LIVE mode.
    """
    price = safe_float(price, 0.0)
    stop_loss = safe_float(stop_loss, 0.0)
    
    if price <= 0:
        logger.error("Quantity rejected: invalid price %r", price)
        return 0
    
    if risk_pct is None:
        risk_pct = safe_float(cfg("risk", "max_risk_per_trade", 0.02), 0.02)
    risk_pct = max(0.0, min(1.0, safe_float(risk_pct, 0.02)))
    
    from core.strategy import MAX_POSITIONS
    
    bp_info = calculate_total_buying_power()
    real_capital = bp_info['real_capital']
    allow_margin = bp_info['allow_margin']
    position_size_margin = bp_info['position_size_margin']
    
    per_position_equity_budget = (real_capital * position_size_margin) / MAX_POSITIONS
    
    margin_per_share = price
    effective_leverage = 1.0
    margin_source = "CASH (No Margin)"

    if allow_margin:
        if TRADING_MODE == "LIVE":
            try:
                from core.kotak_client import get_client
                from core.api_resilience import call_broker_api
                from core.data_fetcher import resolve_instrument_tokens
                
                tokens = resolve_instrument_tokens([symbol])
                token = tokens[0].get("instrument_token") if tokens else None                
                if token:
                    client = get_client()
                    resp = call_broker_api(
                        client.margin_required,
                        exchange_segment="nse_cm",
                        price=str(price),
                        order_type="MKT",
                        product="MIS",
                        quantity="1",
                        instrument_token=str(token),
                        transaction_type="B"
                    )
                    if resp and isinstance(resp, dict) and "data" in resp:
                        data = resp["data"]
                        if "ordMrgn" in data and float(data["ordMrgn"]) > 0:
                            margin_per_share = float(data["ordMrgn"])
                            effective_leverage = price / margin_per_share
                            margin_source = "API (client.margin_required)"
                        else:
                            logger.error(f"❌ FX11-A: margin_required for {symbol} returned invalid ordMrgn. Failing CLOSED.")
                            return 0
                    else:
                        logger.error(f"❌ FX11-A: margin_required for {symbol} failed/timeout. Failing CLOSED.")
                        return 0
                else:
                    logger.error(f"❌ FX11-A: Missing instrument token for {symbol}. Failing CLOSED.")
                    return 0
            except Exception as e:
                logger.error(f"❌ FX11-A: Exception during margin_required for {symbol}: {e}. Failing CLOSED.")
                return 0
        else:
            configured_leverage = bp_info['margin_leverage']
            margin_per_share = price / configured_leverage
            effective_leverage = configured_leverage
            margin_source = "CONFIG (Paper Mode)"

    logger.info(f"📊 Margin Discovery [{symbol}]: ₹{margin_per_share:.2f}/share ({effective_leverage:.1f}x) [Source: {margin_source}]")

    margin_status = get_margin_status()
    free_margin = safe_float(margin_status.get('free_margin'), 0.0)
    
    max_loss = real_capital * risk_pct
    stop_dist = abs(price - stop_loss)
    if stop_dist <= 0:
        stop_dist = max(1.0, price * 0.01)
    
    risk_qty = max_loss / stop_dist if stop_dist > 0 else 0
    allocation_qty = per_position_equity_budget / margin_per_share if margin_per_share > 0 else 0
    leverage_qty = free_margin / margin_per_share if margin_per_share > 0 else 0
    
    enable_risk = cfg(
        "risk",
        "enable_risk_based_position_sizing",
        True
    )
    
    if enable_risk:
        quantities = {
            "risk": risk_qty,
            "allocation": allocation_qty,
            "leverage": leverage_qty,
        }
    else:
        quantities = {
            "allocation": allocation_qty,
            "leverage": leverage_qty,
        }
    
    final_qty = max(0, int(min(quantities.values())))
    
    limiting_constraint = min(quantities, key=quantities.get) if final_qty > 0 else "insufficient_all"

    forced_buy = cfg("risk", "forced_buy_margin", False)
    if final_qty <= 0 and forced_buy and allow_margin:
        if allocation_qty >= 1:
            final_qty = int(allocation_qty)
            limiting_constraint = "forced_buy_override"
            logger.warning(f"⚠️ FORCED BUY OVERRIDE | Buying {final_qty} using allocation budget.")

    if final_qty <= 0:
        logger.error(
            f"❌ INSUFFICIENT MARGIN/RISK FOR {symbol} | Price: ₹{price} | SL: ₹{stop_loss} | "
            f"Free Margin: ₹{free_margin:.0f} | Margin/Share: ₹{margin_per_share:.2f} | "
            f"Risk-qty: {int(risk_qty)} | Alloc-qty: {int(allocation_qty)} | Lever-qty: {int(leverage_qty)}"
        )
        return 0

    capital_deployed_if_buy = final_qty * margin_per_share
    
    logger.info(
        f"✅ Quantity calculated | {symbol} | Price: ₹{price} | Qty: {final_qty} | "
        f"Margin Blocked: ₹{capital_deployed_if_buy:.0f} | Limiting: {limiting_constraint} | "
        f"Free Margin Left: ₹{free_margin - capital_deployed_if_buy:.0f}"
    )
    
    return final_qty


def calculate_quantity_with_tranches(
    symbol: str,
    price: float, 
    stop_loss: float,
    risk_pct: float = None,
    max_tranches: int = 3
) -> dict:
    """
    🚀 ADVANCED: Calculate how many tranches/positions to buy when using forced margin.
    
    Returns: {
        'total_qty': int,           # Total shares across all tranches
        'num_tranches': int,        # How many separate buys to make
        'per_tranche_qty': int,     # Quantity per tranche
        'margin_used': float,       # Total margin deployed
        'margin_ratio': float,      # Margin % vs real capital
    }
    
    Example: With ₹800 capital and ₹1000 stock at 2x margin:
      → Can buy 1 share, but tranche mode calculates: 3 tranches × 0.5 shares each
      → Or: wait for small price dips, pyramid up
    """
    price = safe_float(price, 0.0)
    stop_loss = safe_float(stop_loss, 0.0)
    if price <= 0:
        return {
            'total_qty': 0,
            'num_tranches': 0,
            'per_tranche_qty': 0,
            'margin_used': 0.0,
            'margin_ratio': 0.0,
        }

    capital = max(0.0, safe_float(_get_available_capital(), 0.0))
    if risk_pct is None:
        risk_pct = safe_float(cfg("risk", "max_risk_per_trade", 0.02), 0.02)
    risk_pct = max(0.0, min(1.0, safe_float(risk_pct, 0.02)))
    max_tranches = max(1, min(10, safe_int(max_tranches, 3)))
    
    allow_margin = cfg("risk", "allow_margin", False)
    if not allow_margin:
        # No margin = no tranches, just single buy
        qty = calculate_quantity(symbol, price, stop_loss, risk_pct)
        return {
            'total_qty': qty,
            'num_tranches': 1,
            'per_tranche_qty': qty,
            'margin_used': qty * price - capital,
            'margin_ratio': 0.0,
        }
    
    margin_leverage = safe_float(cfg("risk", "margin_leverage", 2.0), 2.0)
    if margin_leverage < 1.0:
        logger.warning("⚠️ margin_leverage below 1.0, clamping to 1.0")
        margin_leverage = 1.0
    elif margin_leverage > 5.0:
        logger.warning("⚠️ margin_leverage above 5.0, clamping to 5.0")
        margin_leverage = 5.0

    position_size_pct = safe_float(cfg("risk", "position_size_margin", 0.75), 0.75)
    if position_size_pct < 0.10:
        logger.warning("⚠️ position_size_margin below 10%, clamping to 10%")
        position_size_pct = 0.10
    elif position_size_pct > 1.0:
        logger.warning("⚠️ position_size_margin above 100%, clamping to 100%")
        position_size_pct = 1.0

    capital_available = capital * margin_leverage
    
    # Calculate max buyable with margin
    max_buyable_qty = int((capital_available * position_size_pct) / price)
    
    # Break into tranches (pyramid strategy)
    # Each tranche gets equal allocation, but tries to maintain risk ratio
    num_tranches = min(max_tranches, max(1, int(max_buyable_qty / 2)))
    per_tranche_qty = max(1, int(max_buyable_qty / num_tranches))
    total_qty = per_tranche_qty * num_tranches
    
    capital_deployed = total_qty * price
    margin_used = max(0, capital_deployed - capital)
    margin_ratio = margin_used / capital if capital > 0 else 0
    
    logger.info(
        f"📊 TRANCHE CALC | Price: ₹{price} | Total: {total_qty} | "
        f"Tranches: {num_tranches} × {per_tranche_qty} | "
        f"Margin: ₹{margin_used:.0f} ({margin_ratio*100:.1f}%)"
    )
    
    return {
        'total_qty': total_qty,
        'num_tranches': num_tranches,
        'per_tranche_qty': per_tranche_qty,
        'margin_used': margin_used,
        'margin_ratio': margin_ratio,
    }


def calculate_stop_loss(price: float, direction: str = "LONG") -> float:
    price = safe_float(price, 0.0)
    if direction == "LONG":
        pct = max(0.0001, min(0.20, safe_float(cfg("risk", "long_stop_loss_percent", 0.008), 0.008)))
        raw = price * (1 - pct)
    else:
        pct = max(0.0001, min(0.20, safe_float(cfg("risk", "short_stop_loss_percent", 0.005), 0.005)))
        raw = price * (1 + pct)
    return round_to_tick(raw)


def calculate_target(entry: float, direction: str = "LONG") -> float:
    """Target based on asymmetric configuration."""
    entry = safe_float(entry, 0.0)
    if direction == "LONG":
        target_pct = max(0.001, min(0.50, safe_float(cfg("risk", "long_profit_target_percent", 0.015), 0.015)))
        raw = entry * (1 + target_pct)
    else:
        target_pct = max(0.001, min(0.50, safe_float(cfg("risk", "short_profit_target_percent", 0.025), 0.025)))
        raw = entry * (1 - target_pct)
    return round_to_tick(raw)


def _market_protection_pct(price: float) -> float:
    if price < 100:
        return 0.02
    if price <= 500:
        return 0.01
    return 0.005


def _broker_safe_limit_price(ltp: float, transaction: str, is_sl_order: bool = False) -> float:
    """
    Convert a desired market-style order into a limit order inside Kotak's
    protection band. Retail algo market orders are not allowed.
    
    NSE requires prices to be multiples of 0.05. This function ensures compliance.
    
    Args:
        is_sl_order: If True, use 3x the normal buffer for SL orders
                     to survive gap-downs.
    """
    ltp = max(safe_float(ltp, 0.0), 0.05)
    pct = _market_protection_pct(ltp)
    if is_sl_order:
        pct = pct * 3  # Wider buffer for SL orders to survive gap-downs
    
    if str(transaction).upper() == "B":
        adjusted = ltp * (1 + pct)
    else:
        adjusted = ltp * (1 - pct)
    
    # Round to nearest 0.05 multiple (NSE requirement)
    # e.g., 1198.97 → 1199.00, 1198.94 → 1198.95
    rounded = round_to_tick(adjusted)
    return max(0.05, rounded)


def _current_position_valuation() -> dict:
    try:
        from core.data_fetcher import get_latest_tick
    except Exception:
        get_latest_tick = None

    current_position_value = 0.0
    entry_position_value = 0.0
    unrealized_pnl = 0.0

    for p in get_open_positions():
        symbol = p.get("symbol", "")
        qty = safe_float(p.get("quantity", 0), 0.0)
        if qty <= 0:
            logger.warning(
                "Ignoring invalid open position valuation | symbol=%s qty=%r",
                symbol,
                p.get("quantity"),
            )
            continue
        entry_price = safe_float(p.get("entry_price", 0), 0.0)

        current_price = entry_price
        if get_latest_tick:
            try:
                tick = get_latest_tick(symbol)
                current_price = safe_float(tick.get("ltp", entry_price), entry_price) if tick else entry_price
            except Exception:
                current_price = entry_price

        entry_value = qty * entry_price
        current_value = qty * current_price

        entry_position_value += entry_value
        current_position_value += current_value
        unrealized_pnl += current_value - entry_value

    return {
        "entry_position_value": entry_position_value,
        "current_position_value": current_position_value,
        "unrealized_pnl": unrealized_pnl,
    }


# ════════════════════════════════════════════════════════════
#   BUY ORDER
# ════════════════════════════════════════════════════════════

def place_entry_order(
    symbol:         str,
    trading_symbol: str,
    entry_price:    float,
    stop_loss:      float = None,
    strategy:       str   = "",
    confidence:     int   = 0,
    product:        str   = "MIS",
    risk_pct:       float = None,
    direction:      str   = "LONG",
) -> dict:
    """
    🔧 FIXED VERSION: Validates session before attempting order.
    Prevents cascading failures from bad tokens.
    """
    symbol = str(symbol or "").strip().upper()
    if not entries_are_enabled():
        state = {}
        try:
            from core.state_manager import get_trading_session_state
            state = get_trading_session_state()
        except Exception:
            pass
        logger.warning(
            "BUY rejected for %s: entries disabled by session state %s (%s)",
            symbol,
            state.get("state", "UNKNOWN"),
            state.get("reason", ""),
        )
        return {}

    confidence_value = max(0.0, min(100.0, safe_float(confidence, 0.0)))
    min_confidence = max(0.0, min(100.0, safe_float(cfg("strategy", "min_confidence", 70), 70)))
    if confidence_value < min_confidence:
        logger.error(
            "BUY blocked for %s: confidence %.1f below min_confidence %.1f",
            symbol,
            confidence_value,
            min_confidence,
        )
        return {}

    if any(str(p.get("symbol", "")).upper() == symbol for p in get_open_positions()):
        logger.warning("BUY blocked for %s: local position already open", symbol)
        return {}

    breaker = get_breaker("order")
    if breaker.is_open():
        logger.error("🔴 Order circuit OPEN — blocking BUY for %s", symbol)
        return {}

    if TRADING_MODE == "LIVE" and not validate_and_fix_session_before_order():
        logger.error(
            f"❌ BUY BLOCKED: Session not ready for {symbol} "
            f"(token invalid or couldn't refresh)"
        )
        breaker._on_failure()
        return {}

    try:
        with _order_lock:
            return breaker.call(
                _place_entry_order_impl,
                symbol=symbol,
                trading_symbol=trading_symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                strategy=strategy,
                confidence=confidence_value,
                product=product,
                risk_pct=risk_pct,
                direction=direction,
                default={},
            )
    except OrderExecutionError as e:
        logger.error("❌ BUY blocked after failure: %s", e)
        return {}


def _place_entry_order_impl(
    symbol:         str,
    trading_symbol: str,
    entry_price:    float,
    stop_loss:      float = None,
    strategy:       str   = "",
    confidence:     int   = 0,
    product:        str   = "MIS",
    risk_pct:       float = None,
    direction:      str   = "LONG",
) -> dict:
    """Same as original, but with session validation before SL-M placement."""
    symbol = str(symbol or "").strip().upper()
    trading_symbol = trading_symbol or symbol
    entry_price = safe_float(entry_price, 0.0)
    confidence = int(max(0.0, min(100.0, safe_float(confidence, 0.0))))
    if entry_price <= 0:
        logger.error("BUY rejected for %s: invalid entry price %r", symbol, entry_price)
        return {}

    if any(str(p.get("symbol", "")).upper() == symbol for p in get_open_positions()):
        logger.warning("BUY rejected for %s: local position already open", symbol)
        return {}

    if stop_loss is None:
        stop_loss = calculate_stop_loss(entry_price, direction)
    else:
        stop_loss = safe_float(stop_loss, 0.0)
    invalid_sl = stop_loss <= 0
    if direction == "LONG" and stop_loss >= entry_price: invalid_sl = True
    elif direction == "SHORT" and stop_loss <= entry_price: invalid_sl = True
    if invalid_sl:
        logger.error("ENTRY rejected for %s: invalid stop loss %r", symbol, stop_loss)
        return {}

    quantity     = calculate_quantity(symbol, entry_price, stop_loss, risk_pct)
    if quantity <= 0:
        logger.error(
            "BUY rejected for %s: invalid/insufficient quantity %r | price=%s | available=%s",
            symbol,
            quantity,
            entry_price,
            _get_available_capital(),
        )
        return {}

    # 🔥 MARGIN SAFETY: Check if this order would over-leverage
    # F001 FIX: Use current_position_value (live market value) not entry_position_value.
    # The broker's actual margin consumption tracks current market value. Using entry
    # price understates deployment when positions have moved favourably, allowing
    # over-leverage beyond the configured ceiling.
    margin_status = get_margin_status()
    capital_deployed_if_buy = quantity * entry_price

    # current_position_value = Σ(live_price × qty) for all open positions
    # entry_position_value   = Σ(entry_price × qty) — what we paid, not what broker sees
    deployed_now_current = safe_float(margin_status.get('current_position_value'), 0.0)
    deployed_now_entry   = safe_float(margin_status.get('entry_position_value'), 0.0)
    real_capital         = safe_float(margin_status.get('account_equity'), 0.0)
    leverage             = safe_float(margin_status.get('margin_leverage'), 1.0)

    total_would_deploy  = deployed_now_current + capital_deployed_if_buy
    max_deployable      = real_capital * leverage
    would_over_leverage = total_would_deploy > max_deployable

    if deployed_now_current != deployed_now_entry:
        logger.debug(
            "F001: Entry vs current position value differ for %s | "
            "entry=₹%.0f current=₹%.0f | delta=₹%.0f",
            symbol,
            deployed_now_entry,
            deployed_now_current,
            deployed_now_current - deployed_now_entry,
        )

    if would_over_leverage:
        logger.error(
            "⚠️ OVER-LEVERAGE BLOCKED for %s | "
            "Current market deployment: ₹%.0f + This order: ₹%.0f = ₹%.0f > Max: ₹%.0f "
            "(real_capital=₹%.0f × leverage=%.1fx)",
            symbol,
            deployed_now_current,
            capital_deployed_if_buy,
            total_would_deploy,
            max_deployable,
            real_capital,
            leverage,
        )
        return {}

    target_price = calculate_target(entry_price, direction)

    try:
        from core.data_fetcher import get_candle_history as _gchs
        _entry_candles = len(_gchs(symbol, include_current=False))
    except Exception:
        _entry_candles = 0

    trade = {
        "symbol":           symbol,
        "trading_symbol":   trading_symbol,
        "quantity":         quantity,
        "entry_price":      entry_price,
        "stop_loss":        stop_loss,
        "target_price":     target_price,
        "action":           direction,
        "strategy":         strategy,
        "confidence":       confidence,
        "product":          product,
        "order_id":         None,
        "sl_order_id":      None,
        "opened_at":        datetime.now().isoformat(),
        "entry_ws_candles": _entry_candles,
    }

    if TRADING_MODE == "PAPER":
        trade["order_id"]    = f"PAPER-{symbol}-{datetime.now().strftime('%H%M%S')}"
        trade["sl_order_id"] = f"PAPER-SL-{symbol}-{datetime.now().strftime('%H%M%S')}"
        logger.info(
            f"📋 [PAPER] ENTRY ({direction}) | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | Target: ₹{target_price}"
        )

    elif TRADING_MODE == "LIVE":
        # Step 1 — Place ENTRY order
        trade["order_id"] = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = "B" if direction == "LONG" else "S",
            quantity       = quantity,
            price          = entry_price,
            order_type     = "L",
            product        = product,
        )
        if not trade["order_id"]:
            logger.error(f"❌ ENTRY order FAILED for {symbol}")
            raise OrderExecutionError(f"ENTRY order rejected for {symbol}")

        logger.info(f"✅ ENTRY order placed: {trade['order_id']} | Qty: {quantity}")
        
        record_order_sent(
            trade["order_id"],
            symbol,
            {"side": direction, "qty": quantity, "price": entry_price, "product": product},
        )
        verification = wait_for_order_verification(trade["order_id"], timeout_sec=45)
        if verification == "REJECTED":
            logger.error(
                f"❌ BUY explicitly rejected by broker for {symbol} — "
                f"not saving local position (order_id={trade['order_id']})"
            )
            raise OrderExecutionError(f"BUY rejected for {symbol}")
        elif verification == "TIMEOUT":
            # E2 FIX: don't blindly assume a timed-out BUY filled. Re-query the broker
            # order status once. If it explicitly REJECTED/CANCELLED, do NOT save a phantom
            # local position. If COMPLETE, proceed. If still pending/unknown, save it but
            # flag it and mark it reconciliation-pending so the reconciler verifies/cleans it.
            _resolved = "UNKNOWN"
            try:
                from core.order_verifier import (
                    fetch_kotak_order_row,
                    normalize_kotak_status,
                    extract_broker_fill_qty,
                )
                _row = fetch_kotak_order_row(trade["order_id"])
                if _row is not None:
                    _resolved = normalize_kotak_status(_row.get("ordSt"))
                    _filled = extract_broker_fill_qty(_row)
                    if _filled and _filled > 0 and _resolved not in ("COMPLETE",):
                        _resolved = "COMPLETE"
            except Exception as _recheck_err:
                logger.warning("BUY timeout re-check failed for %s: %s", symbol, _recheck_err)

            if _resolved in ("REJECTED", "CANCELLED"):
                logger.error(
                    f"❌ BUY timed out and broker status is {_resolved} for {symbol} — "
                    f"not saving local position (order_id={trade['order_id']})"
                )
                raise OrderExecutionError(f"BUY {_resolved} (post-timeout) for {symbol}")

            if _resolved == "COMPLETE":
                logger.info(
                    f"✅ BUY timeout resolved as COMPLETE on re-check for {symbol} "
                    f"(order_id={trade['order_id']})"
                )
            else:
                logger.warning(
                    f"⚠️ BUY verification timed out for {symbol} and broker status is "
                    f"'{_resolved}'. Saving as UNVERIFIED and flagging for reconciliation "
                    f"to prevent both double-buy and untracked phantom fills. "
                    f"(order_id={trade['order_id']})"
                )
                trade["notes"] = "UNVERIFIED: Broker confirmation timed out"
                trade["reconciliation_status"] = "RECONCILIATION_PENDING"

        logger.info(f"✅ BUY verified on broker | Symbol: {symbol} | Qty: {quantity} | Product: {product} | SL: ₹{stop_loss}")

        # ⚠️ DIAGNOSTIC: Check if BUY order is visible in positions (some exchanges need time)
        import time
        time.sleep(1)  # Small buffer for order settlement
        
        # Step 2 — Place SL-M SELL order on Kotak immediately
        # 🔧 NO PRE-VALIDATION: ensure_trade_token_on_client() already handles all token checks
        # Calling validate_and_fix_session_before_order() here causes DOUBLE token refresh!
        #
        time.sleep(1) 
        
        _SL_MAX_EXT_RETRIES = 3
        _SL_RETRY_DELAY_SEC = 2

        logger.info(
            f"🔄 Placing broker SL for {symbol} | "
            f"Qty: {quantity} | Trigger: ₹{stop_loss} | Trading symbol: {trading_symbol}"
        )

        for _sl_attempt in range(1, _SL_MAX_EXT_RETRIES + 1):
            try:
                trade["sl_order_id"] = _send_kotak_sl_order(
                    trading_symbol   = trading_symbol,
                    quantity         = quantity,
                    trigger_price    = stop_loss,
                    product          = product,
                    transaction_type = "S" if direction == "LONG" else "B",
                )
            except Exception as _sl_exc:
                logger.error(
                    f"❌ _send_kotak_sl_order exception (attempt {_sl_attempt}/{_SL_MAX_EXT_RETRIES}): {_sl_exc}",
                    exc_info=True,
                )
                trade["sl_order_id"] = None

            if trade["sl_order_id"]:
                logger.info(
                    f"🛡️ Broker SL placed | {symbol} | Qty: {quantity} | "
                    f"Trigger: ₹{stop_loss} | OrderID: {trade['sl_order_id']} "
                    f"(attempt {_sl_attempt}/{_SL_MAX_EXT_RETRIES})"
                )
                
                # F003: SL Order Verification
                sl_status = wait_for_sl_verification(trade["sl_order_id"])
                
                if sl_status == "REJECTED":
                    logger.error(f"❌ SL Order rejected by broker! Clearing ID and retrying...")
                    
                    alert_critical(
                        f"\nBroker Stop Loss Rejected\n\n"
                        f"Symbol: {symbol}\n\n"
                        f"Position is currently protected only by software stop-loss.\n\n"
                        f"Immediate review required."
                    )
                    
                    trade["sl_order_id"] = None
                    # Fall through to retry logic
                else:
                    break  # SL placed and verified — exit retry loop

            if _sl_attempt < _SL_MAX_EXT_RETRIES:
                logger.warning(
                    f"⚠️ SL attempt {_sl_attempt}/{_SL_MAX_EXT_RETRIES} failed/rejected for {symbol}. "
                    f"Retrying in {_SL_RETRY_DELAY_SEC}s..."
                )
                import time as _time_mod
                _time_mod.sleep(_SL_RETRY_DELAY_SEC)
                
                # F004 FIX: Validate token freshness before SL retry attempt
                try:
                    ensure_trade_token_on_client()
                    logger.debug(f"🔐 Token validated for SL retry (attempt {_sl_attempt + 1}/{_SL_MAX_EXT_RETRIES})")
                except Exception as _token_err:
                    logger.warning(f"⚠️ Token validation failed during SL retry: {_token_err}. Proceeding anyway...")

        # ── F002: SL-M failed after all retries ──────────────────────────────────────
        # CRITICAL: Do NOT abort or cancel the trade. The BUY is already filled on the
        # exchange. Cancelling a filled order is impossible. Raising an exception here
        # would create an orphan broker position with zero local visibility — strictly
        # worse than continuing with software-only protection.
        #
        # Instead: save the position (software SL stays fully active), flag it as
        # unprotected, alert the operator, and rely on the strategy loop's SL recovery
        # function to re-attempt broker-side SL placement on every subsequent iteration.
        # ─────────────────────────────────────────────────────────────────────────────
        if not trade["sl_order_id"]:
            logger.critical(
                f"🚨 CRITICAL: Broker SL-M FAILED for {symbol} after {_SL_MAX_EXT_RETRIES} attempts. "
                f"Position will be saved with SOFTWARE-ONLY protection. "
                f"Recovery loop will retry broker SL via kotak_sl_order_id=NULL detection. "
                f"BUY order_id: {trade['order_id']} | Entry: ₹{entry_price} | SL: ₹{stop_loss}"
            )
            # NOTE: No notes flag is written here. The absence of kotak_sl_order_id
            # is the sole, authoritative signal for the recovery loop. This avoids
            # storing machine state in a free-text field that other code paths may
            # overwrite (e.g. broker reconciliation quantity/entry repair writes notes).
            # Fire CRITICAL alert to operator immediately
            try:
                alert_critical(
                    f"🚨 Broker SL-M FAILED for {symbol} ({_SL_MAX_EXT_RETRIES} retries). "
                    f"Position held with SOFTWARE SL only (₹{stop_loss}). "
                    f"BUY: {trade['order_id']} | Entry: ₹{entry_price}. "
                    f"Strategy loop will retry broker SL automatically."
                )
            except Exception:
                pass
            # Permanent audit trail entry
            try:
                audit_system_error(
                    f"Broker SL-M placement failed for {symbol} after {_SL_MAX_EXT_RETRIES} attempts. "
                    f"Position saved with software-only SL. Recovery driven by kotak_sl_order_id=NULL. "
                    f"BUY order_id={trade['order_id']} entry={entry_price} sl={stop_loss}."
                )
            except Exception:
                pass

        logger.info(
            f"✅ [LIVE] BUY | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | Target: ₹{target_price} | "
            f"Broker SL: {'✅ ' + str(trade['sl_order_id']) if trade['sl_order_id'] else '❌ UNPROTECTED (recovery pending)'}"
        )
        _get_available_capital(force_refresh=True)

        audit_order_placed(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=entry_price,
            order_id=trade["order_id"],
            stop_loss=stop_loss,
            target=target_price,
        )

    save_open_position(trade)
    try:
        from core.alerts import alert_buy
        alert_buy(symbol, quantity, entry_price, strategy, trade.get("order_id") or "")
    except Exception:
        pass
    return trade


# ════════════════════════════════════════════════════════════
#   SELL ORDER
# ════════════════════════════════════════════════════════════

def place_sell_order(
    symbol:     str,
    exit_price: float,
    reason:     str = "SIGNAL",
    product:    str = "MIS",
    exit_price_source: str = "ws_ltp",
    quantity:   int | None = None,
) -> bool:
    """Place SELL. Risk-reducing exits bypass an open order circuit."""
    breaker = get_breaker("order")
    symbol = str(symbol or "").strip().upper()
    exit_price = safe_float(exit_price, 0.0)
    risk_reducing = _is_risk_reducing_sell(reason)
    circuit_open = breaker.is_open()
    if circuit_open and not risk_reducing:
        logger.error("🔴 Order circuit OPEN — blocking SELL for %s", symbol)
        return False
    if circuit_open and risk_reducing:
        logger.warning(
            "Order circuit OPEN, but allowing risk-reducing SELL for %s (%s)",
            symbol,
            reason,
        )

    try:
        with _order_lock:
            if risk_reducing:
                return _place_sell_order_impl(
                    symbol=symbol,
                    exit_price=exit_price,
                    reason=reason,
                    product=product,
                    exit_price_source=exit_price_source,
                    quantity=quantity,
                )
            return breaker.call(
                _place_sell_order_impl,
                symbol=symbol,
                exit_price=exit_price,
                reason=reason,
                product=product,
                exit_price_source=exit_price_source,
                quantity=quantity,
                default=False,
            )
    except OrderExecutionError as e:
        logger.error("❌ SELL blocked after failure: %s", e)
        return False


def _place_sell_order_impl(
    symbol:     str,
    exit_price: float,
    reason:     str = "SIGNAL",
    product:    str = "MIS",
    exit_price_source: str = "ws_ltp",
    quantity:   int | None = None,
) -> bool:

    symbol = str(symbol or "").strip().upper()
    exit_price = safe_float(exit_price, 0.0)
    if exit_price <= 0:
        logger.error("SELL rejected for %s: invalid exit price %r", symbol, exit_price)
        return False

    open_positions = get_open_positions()
    position = next((p for p in open_positions if str(p.get("symbol", "")).upper() == symbol), None)

    if not position:
        logger.warning(f"No open position for {symbol}")
        return False

    pos_quantity   = safe_int(position.get("quantity"), 0)
    direction      = position.get("action", "LONG")
    transaction_type = "S" if direction == "LONG" else "B"

    if quantity is None:
        quantity = pos_quantity
        
    if quantity <= 0 or quantity > pos_quantity:
        logger.error("SELL rejected for %s: invalid quantity %r (pos_qty: %r)", symbol, quantity, pos_quantity)
        return False
        
    # ← FIXED: use stored trading_symbol, not raw ticker
    trading_symbol = position.get("trading_symbol") or symbol
    success        = True
    final_exit_price = exit_price
    final_exit_price_source = str(exit_price_source or "unknown")

    if TRADING_MODE == "PAPER":
        logger.info(
            f"📋 [PAPER] SELL | {symbol} | Qty: {quantity} | "
            f"@ ₹{exit_price} | Reason: {reason}"
        )

    elif TRADING_MODE == "LIVE":
        # Enforce Cancel-Verify-Replace State Machine
        sl_order_id = position.get("kotak_sl_order_id")
        if sl_order_id:
            cancel_success = _cancel_kotak_order(sl_order_id)
            if cancel_success:
                logger.info(f"✅ Broker SL {sl_order_id} successfully cancelled before software sell.")

                # L4 FIX: the broker SL no longer exists. Clear kotak_sl_order_id in the DB
                # immediately so that if the software SELL below fails/times out (leaving the
                # position OPEN and NAKED), attempt_broker_sl_recovery re-arms a broker SL.
                # Previously the stale id remained, so recovery skipped the now-unprotected
                # position.
                try:
                    update_sl_order_id(symbol, "")
                except Exception:
                    logger.warning("Could not clear kotak_sl_order_id for %s after SL cancel", symbol)

                # Check for partial fills even on success
                try:
                    from core.order_verifier import fetch_kotak_order_row, extract_broker_fill_qty
                    row = fetch_kotak_order_row(sl_order_id)
                    filled_qty = extract_broker_fill_qty(row)
                    if filled_qty > 0:
                        quantity = max(0, quantity - filled_qty)
                        logger.warning(f"⚠️ Broker SL was partially filled ({filled_qty} shares). Adjusted software sell qty to {quantity}.")
                        if quantity <= 0:
                            logger.info(f"Broker SL completely filled prior to cancel confirmation. Aborting software sell.")
                            close_position(symbol, exit_price, reason, exit_price_source="broker_sl_partial_complete")
                            return True
                except Exception as e:
                    logger.error(f"Failed to verify partial fill status for cancelled order {sl_order_id}: {e}")
            
            else:
                # Cancel failed -> Status Verification State Machine
                logger.warning(f"⚠️ Failed to cancel broker SL {sl_order_id}. Initiating State Machine fallback...")
                try:
                    from core.order_verifier import fetch_kotak_order_row, extract_broker_fill_qty, normalize_kotak_status
                    row = fetch_kotak_order_row(sl_order_id)
                    if row is None:
                        raise TimeoutError("Status query returned None")
                        
                    ord_st = normalize_kotak_status(row.get("ordSt"))
                    filled_qty = extract_broker_fill_qty(row)
                    
                    if ord_st == "COMPLETE":
                        # State A
                        logger.info(f"STATE A: Broker SL {sl_order_id} already COMPLETE. Aborting software sell to prevent double exposure.")
                        close_position(symbol, exit_price, reason, exit_price_source="broker_sl_complete")
                        return True
                        
                    elif ord_st == "CANCELLED":
                        # State B (Cancelled)
                        logger.info(f"STATE B: Broker SL {sl_order_id} is CANCELLED.")
                        if filled_qty > 0:
                            quantity = max(0, quantity - filled_qty)
                            logger.warning(f"⚠️ Broker SL was partially filled ({filled_qty} shares). Adjusted software sell qty to {quantity}.")
                            if quantity <= 0:
                                close_position(symbol, exit_price, reason, exit_price_source="broker_sl_partial_complete")
                                return True
                        # Proceed to software sell
                        
                    elif ord_st == "REJECTED":
                        # State B2 (Rejected on creation - never existed)
                        logger.info(f"STATE B: Broker SL {sl_order_id} was REJECTED by broker.")
                        # Proceed to software sell
                    
                    elif ord_st in ("PENDING", "CANCEL_REJECTED"):
                        # State C (Open/Pending/Trigger_Pending/Cancel_Failed)
                        logger.error(f"🚨 CRITICAL ALERT (STATE C): Broker SL {sl_order_id} is still alive ({ord_st}). Aborting software sell to prevent naked short.")
                        return False
                        
                    else:
                        raise ValueError(f"Unknown normalized status: {ord_st}")
                        
                except (TimeoutError, ConnectionError, Exception) as e:
                    # State D (Timeout/Error)
                    logger.error(f"🚨 CRITICAL ALERT (STATE D): API Blackout during status query for {sl_order_id} ({e}). Aborting software sell to prevent blind shoot.")
                    return False

        order_id = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = transaction_type,
            quantity       = quantity,
            price          = _broker_safe_limit_price(exit_price, transaction_type),
            order_type     = "L",
            product        = product,
        )
        if not order_id:
            logger.error(f"❌ SELL order FAILED for {symbol}")
            raise OrderExecutionError(f"SELL order rejected for {symbol}")

        record_order_sent(
            order_id,
            symbol,
            {"side": "SELL", "qty": quantity, "price": exit_price, "reason": reason},
        )
        verification = wait_for_order_verification(order_id, timeout_sec=45)
        if verification == "REJECTED":
            logger.error(
                f"❌ SELL explicitly rejected by broker for {symbol} — "
                f"keeping local position open (order_id={order_id})"
            )
            raise OrderExecutionError(f"SELL rejected for {symbol}")
        elif verification == "TIMEOUT":
            logger.error(
                f"🚨 CRITICAL ALERT: SELL verification TIMED OUT for {symbol}. "
                f"Broker execution could not be verified. "
                f"Position remains OPEN locally. Manual intervention required. (order_id={order_id})"
            )
            raise OrderExecutionError(f"SELL verification timed out for {symbol}. Order status unconfirmed.")

        try:
            from core.order_verifier import fetch_kotak_order_row
            broker_row = fetch_kotak_order_row(order_id)
            broker_fill = _extract_broker_fill_price(broker_row)
            if broker_fill > 0:
                final_exit_price = broker_fill
                final_exit_price_source = "broker_fill"
            else:
                logger.warning(
                    "Broker SELL verified for %s but fill price unavailable; "
                    "recording requested quote %s from %s for reconciliation",
                    symbol,
                    exit_price,
                    final_exit_price_source,
                )
                final_exit_price_source = f"{final_exit_price_source}:broker_fill_missing"
        except Exception:
            logger.warning("Could not fetch broker fill price for %s", symbol, exc_info=True)
            final_exit_price_source = f"{final_exit_price_source}:broker_fill_lookup_failed"

    if success:
        entry = safe_float(position.get("entry_price"), 0.0)
        pnl = (final_exit_price - entry) * quantity if entry else None
        reconciliation_status = None
        if "broker_fill_missing" in final_exit_price_source or "broker_fill_lookup_failed" in final_exit_price_source:
            reconciliation_status = "RECONCILIATION_PENDING"

        if quantity < pos_quantity:
            from core.state_manager import partial_close_position
            partial_close_position(
                symbol,
                final_exit_price,
                quantity,
                reason,
                exit_price_source=final_exit_price_source,
                reconciliation_status=reconciliation_status,
            )
        else:
            close_position(
                symbol,
                final_exit_price,
                reason,
                exit_price_source=final_exit_price_source,
                reconciliation_status=reconciliation_status,
            )
        
        # ── Record trade outcome for reflection statistics ────
        try:
            from datetime import time as dt_time
            
            strategy_context = position.get("strategy", "UNKNOWN")
            confidence = position.get("confidence", 0)  # Now contains adaptive-adjusted confidence from signal
            
            # Calculate drawdown if available (estimate from position tracking)
            max_price = safe_float(position.get("max_price_during_hold"), final_exit_price)
            drawdown = max(0, (max_price - final_exit_price) / final_exit_price * 100) if final_exit_price > 0 else 0
            
            # Get current time window for statistics
            now = datetime.now().time()
            hour = now.hour
            minute = now.minute
            total_min = hour * 60 + minute
            
            if total_min >= 9*60+15 and total_min < 10*60:
                time_window = "9:15-10:00"
            elif total_min >= 10*60 and total_min < 11*60+30:
                time_window = "10:00-11:30"
            elif total_min >= 11*60+30 and total_min < 13*60:
                time_window = "11:30-1:00"
            elif total_min >= 13*60 and total_min < 14*60:
                time_window = "1:00-2:00"
            elif total_min >= 14*60 and total_min < 15*60+30:
                time_window = "2:00-3:30"
            else:
                time_window = "other"
            
            record_trade(
                signal_name=strategy_context,
                symbol=symbol,
                entry_price=entry,
                exit_price=final_exit_price,
                pnl=pnl,
                confidence=confidence,
                time_window=time_window,
                drawdown=drawdown,
                sl_hit=reason in ("STOPLOSS", "TRAILING_SL"),
                recovered=False,  # Could track this from position history
            )
            logger.debug(f"📊 Signal recorded: {strategy_context} | {symbol} | PL={pnl} | DD={drawdown:.1f}%")
        except Exception as e:
            logger.debug(f"Could not record signal outcome: {e}")
        
        try:
            from core.alerts import alert_sell
            alert_sell(symbol, quantity, final_exit_price, reason, pnl)
        except Exception:
            pass
        if TRADING_MODE == "LIVE":
            _get_available_capital(force_refresh=True)

    return success


# ════════════════════════════════════════════════════════════
#   EXIT CHECKS — Called every tick by strategy.py
# ════════════════════════════════════════════════════════════

def check_stop_losses(live_prices: dict[str, float]):
    """Checks software SL. Kotak SL-M is the backup."""
    for position in get_open_positions():
        symbol      = position["symbol"]
        trailing_sl = safe_float(position.get("trailing_sl"), 0.0)
        stop_loss   = safe_float(position.get("stop_loss"), 0.0)
        current     = safe_float(live_prices.get(symbol), 0.0)

        direction   = position.get("action", "LONG")

        if not current:
            continue

        # Use trailing SL if it exists and is better than original SL
        if direction == "LONG":
            active_sl = max(trailing_sl or 0, stop_loss or 0)
            sl_hit = current <= active_sl
        else: # SHORT
            active_sl = min(trailing_sl or float('inf'), stop_loss or float('inf'))
            if active_sl == float('inf'): active_sl = 0
            sl_hit = active_sl > 0 and current >= active_sl

        if active_sl and sl_hit:
            sl_type = "TRAILING_SL" if trailing_sl and (trailing_sl > stop_loss if direction == "LONG" else trailing_sl < stop_loss) else "STOPLOSS"
            logger.warning(
                f"🔴 {sl_type} HIT | {symbol} | "
                f"₹{current} {'≤' if direction == 'LONG' else '≥'} ₹{active_sl}"
            )
            place_sell_order(symbol, current, sl_type)


def check_profit_targets(live_prices: dict[str, float]):
    """Exits when price hits 2:1 target."""
    for position in get_open_positions():
        symbol  = position["symbol"]
        target  = safe_float(position.get("target_price"), 0.0)
        current = safe_float(live_prices.get(symbol), 0.0)
        direction = position.get("action", "LONG")

        if not current or not target:
            continue

        target_hit = current >= target if direction == "LONG" else current <= target

        if target_hit:
            logger.info(
                f"🎯 TARGET HIT | {symbol} | "
                f"₹{current} {'≥' if direction == 'LONG' else '≤'} Target ₹{target}"
            )
            place_sell_order(symbol, current, "TARGET")


def attempt_broker_sl_recovery():
    """
    F002 RECOVERY (v3 — final): Scans all open positions for missing broker-side
    SL-M orders and attempts to place them. Called every strategy loop iteration
    in LIVE mode.

    Recovery eligibility:
        A position is a recovery candidate when kotak_sl_order_id is empty/None
        in LIVE mode. This is the single authoritative field — no separate flag
        or notes annotation is written or read. This design is immune to
        notes-field overwrites by reconciliation or any future code that writes
        to notes for legitimate operator-facing purposes (Scenario C protection).

    Safety properties:
    - Circuit-breaker aware: skips ALL attempts when the order circuit is open,
      preventing the recovery loop from contributing to circuit trips that block
      legitimate risk-reducing sell orders.
    - Per-symbol exponential backoff: starts at 30s, doubles each failure up to
      a 300s cap. Eliminates API spam during prolonged broker outages.
    - Log-flood protected: WARNING on first attempt per cooldown cycle, DEBUG
      for cooldown skips, CRITICAL when a fresh attempt fails, WARNING summary
      every 5th failure.
    - Never creates more risk than the original condition: software SL remains
      fully active regardless of broker SL recovery status.

    Worst-case broker API load (30-minute outage, 10 positions):
        Schedule per symbol: 30s/90s/210s/450s/750s/1050s/1350s/1650s = 8 attempts
        Total: 8 x 10 symbols x 2 internal retries = 160 broker API calls.
        vs. 7,200 without backoff — 97.8% reduction.
    """
    import time as _t

    if TRADING_MODE != "LIVE":
        return  # Paper mode: no real broker SL orders

    # ── Guard 1: Circuit breaker ─────────────────────────────────────────────
    # If the order circuit is open, recovery attempts would fail immediately AND
    # each failure increments the breaker's counter, making it stay open longer.
    # More critically: an open order circuit already blocks new buy orders;
    # if recovery hammered it further, it would also delay HALF_OPEN recovery
    # which would prevent legitimate sell orders from going through.
    order_breaker = get_breaker("order")
    if order_breaker.is_open():
        logger.debug(
            "SL recovery skipped: order circuit OPEN (state=%s). "
            "Software SL remains active for all positions.",
            order_breaker.state.value,
        )
        return

    now_ts = _t.time()

    for position in get_open_positions():
        symbol      = str(position.get("symbol", "")).upper()
        sl_order_id = str(position.get("kotak_sl_order_id") or "").strip()

        # Recovery eligibility: kotak_sl_order_id is the authoritative signal.
        # Empty = no broker-side SL exists. No separate flag needed or used.
        # This is immune to notes-field overwrites by reconciliation or any
        # other code path that legitimately annotates the notes column.
        if sl_order_id:
            continue

        # ── Guard 2: Exponential backoff ────────────────────────────────────
        state = _sl_recovery_state.get(symbol)
        if state is None:
            # First time we've seen this symbol as unprotected in this session
            state = {
                "last_attempt_ts": 0.0,
                "next_delay_sec":  _SL_RECOVERY_INITIAL_DELAY_SEC,
                "attempt_count":   0,
            }
            _sl_recovery_state[symbol] = state

        elapsed = now_ts - state["last_attempt_ts"]
        if elapsed < state["next_delay_sec"]:
            # Still in cooldown — skip silently (no log to prevent flood)
            logger.debug(
                "SL recovery cooldown for %s: %.0fs remaining before next attempt "
                "(delay=%ds, attempt=%d)",
                symbol,
                state["next_delay_sec"] - elapsed,
                state["next_delay_sec"],
                state["attempt_count"],
            )
            continue

        # ── Cooldown elapsed: attempt recovery ──────────────────────────────
        stop_loss      = safe_float(position.get("stop_loss"), 0.0)
        trailing_sl    = safe_float(position.get("trailing_sl"), 0.0)
        quantity       = safe_int(position.get("quantity"), 0)
        trading_symbol = str(position.get("trading_symbol") or f"{symbol}-EQ")
        product        = str(position.get("product") or "MIS")
        active_trigger = max(stop_loss, trailing_sl) if trailing_sl else stop_loss

        if quantity <= 0 or active_trigger <= 0:
            logger.warning(
                "SL recovery skipped for %s: invalid position data "
                "(qty=%r trigger=%.2f)",
                symbol, quantity, active_trigger,
            )
            continue

        attempt_num = state["attempt_count"] + 1
        logger.warning(
            "🔄 SL recovery attempt #%d for %s | "
            "Qty: %d | Trigger: ₹%.2f | delay_was: %ds",
            attempt_num, symbol, quantity, active_trigger, state["next_delay_sec"],
        )

        # Update state BEFORE the call so a crash/exception still records the attempt
        state["last_attempt_ts"] = now_ts
        state["attempt_count"]   = attempt_num

        new_sl_id = None
        try:
            new_sl_id = _send_kotak_sl_order(
                trading_symbol=trading_symbol,
                quantity=quantity,
                trigger_price=active_trigger,
                product=product,
            )
        except Exception as exc:
            logger.critical(
                "🚨 SL recovery #%d EXCEPTION for %s: %s. "
                "Software SL at ₹%.2f active. Next attempt in %ds.",
                attempt_num, symbol, exc, active_trigger,
                min(state["next_delay_sec"] * _SL_RECOVERY_BACKOFF_FACTOR,
                    _SL_RECOVERY_MAX_DELAY_SEC),
            )

        if new_sl_id:
            # F003: Verify recovered SL order!
            sl_status = wait_for_sl_verification(new_sl_id)
            if sl_status == "REJECTED":
                logger.error(f"❌ SL Recovery Order rejected by broker! Backing off...")
                new_sl_id = None
                
        if new_sl_id:
            # ── SUCCESS ─────────────────────────────────────────────────────
            logger.info(
                "✅ SL recovery SUCCESS for %s after %d attempt(s) | "
                "New SL order: %s | Trigger: ₹%.2f",
                symbol, attempt_num, new_sl_id, active_trigger,
            )
            try:
                from core.state_manager import update_sl_order_id
                update_sl_order_id(symbol, new_sl_id)
                # kotak_sl_order_id now non-empty — position exits recovery
                # eligibility automatically on next loop iteration. No flag to clear.
            except Exception as upd_exc:
                logger.error(
                    "SL recovery: DB update failed for %s after success: %s",
                    symbol, upd_exc,
                )
            # Remove from backoff tracking — position is now protected
            _sl_recovery_state.pop(symbol, None)
            try:
                from core.alerts import send_alert
                send_alert(
                    f"✅ Broker SL-M recovered for {symbol} "
                    f"(attempt #{attempt_num}). "
                    f"SL order: {new_sl_id} | Trigger: ₹{active_trigger:.2f}",
                    severity="INFO",
                )
            except Exception:
                pass

        else:
            # ── FAILURE: apply exponential backoff ──────────────────────────
            new_delay = min(
                state["next_delay_sec"] * _SL_RECOVERY_BACKOFF_FACTOR,
                _SL_RECOVERY_MAX_DELAY_SEC,
            )
            state["next_delay_sec"] = new_delay

            # Log at CRITICAL on first failure; WARNING+summary every 5th;
            # DEBUG otherwise — prevents log flooding during sustained outages.
            if attempt_num == 1:
                logger.critical(
                    "🚨 SL recovery FAILED (attempt #1) for %s. "
                    "Software SL at ₹%.2f is the only active protection. "
                    "Next broker attempt in %ds.",
                    symbol, active_trigger, new_delay,
                )
            elif attempt_num % _SL_RECOVERY_LOG_EVERY_N == 0:
                logger.warning(
                    "⚠️ SL recovery still failing for %s "
                    "(attempt #%d, next in %ds). "
                    "Software SL at ₹%.2f remains active.",
                    symbol, attempt_num, new_delay, active_trigger,
                )
            else:
                logger.debug(
                    "SL recovery attempt #%d failed for %s. Next in %ds.",
                    attempt_num, symbol, new_delay,
                )


def _capital_fetch_window_open() -> bool:
    """
    Bug1 FIX: LIVE broker capital fetch is only meaningful on trading days
    within the 08:45–15:30 IST window. Outside it the broker returns an empty
    limits payload ({'Net':'0','availablecash':None}) that must NOT be treated
    as an API failure — doing so drove a nightly escalation loop plus a
    destructive force_reconnect() that tore down the WebSocket.
    """
    try:
        from core.market_calendar import is_trading_day
        now = datetime.now()
        if not is_trading_day(now.date()):
            return False
        return dt_time(8, 45) <= now.time() <= dt_time(15, 30)
    except Exception:
        return True  # fail-open: prefer fetching over silently blocking


def is_capital_fresh() -> bool:
    """True only when the last LIVE capital fetch succeeded (not a stale cache)."""
    return _capital_api_failures == 0 and _capital_cache > 0


def _get_available_capital(force_refresh: bool = False) -> float:
    """
    F005 FIX: Fetch broker account capital with observable failure handling.

    Failure behaviour:
    - On API failure (exception or None/zero response): increment
      _capital_api_failures counter and return the last known-good cache.
    - If _capital_api_failures reaches _CAPITAL_API_FAILURE_ALERT_THRESHOLD,
      fire a CRITICAL alert so the operator knows trading is running on a
      stale capital figure (or blocked entirely if cache is still 0.0).
    - A separate CRITICAL is raised immediately on ANY failure when
      _capital_cache == 0.0 (cache is still at its initial sentinel — no
      valid capital has ever been fetched this session), because in that
      state a failure means calculate_quantity() returns 0 for all trades.
    - On success: reset _capital_api_failures to 0.
    """
    global _capital_cache, _capital_last_update, _capital_api_failures
    import time

    if TRADING_MODE == "PAPER":
        return _calculate_paper_capital_available()

    now = time.time()

    # Bug1 FIX: don't hammer the broker capital API off-market. Outside the
    # 08:45–15:30 trading-day window serve the cache and skip the fetch entirely,
    # so an empty off-hours limits payload never counts as an API failure.
    if not _capital_fetch_window_open():
        return _capital_cache

    if not force_refresh and (now - _capital_last_update) < CAPITAL_CACHE_TTL:
        return _capital_cache   # valid cache — serve it

    # ── Attempt broker fetch ──────────────────────────────────────────────────
    fetch_ok = False
    try:
        from core.kotak_client import get_client
        client = get_client()
        limits = client.limits(segment="ALL", exchange="ALL", product="ALL")

        if isinstance(limits, dict) and limits.get("stCode") == 300015:
            # Market closed response — stale cache is fine, not a failure
            return _capital_cache

        if not isinstance(limits, dict):
            raise ValueError(
                "Capital API returned non-dict: " + repr(type(limits))
            )

        available = (
            limits.get("Net")
            or limits.get("availablecash")
            or limits.get("data", {}).get("Net")
        )
        available_value = safe_float(available, 0.0)
        if available_value > 0:
            _capital_cache = available_value
            _capital_last_update = now
            if _capital_api_failures > 0:
                logger.info(
                    "Capital API recovered after %d failure(s). "
                    "Available capital: Rs%.2f",
                    _capital_api_failures, _capital_cache,
                )
            _capital_api_failures = 0   # reset on success
            fetch_ok = True
            return _capital_cache
        else:
            # Bug1 FIX (defense-in-depth): an all-empty payload while the fetch
            # window is closed is an off-hours response, not a real failure.
            if not _capital_fetch_window_open():
                return _capital_cache
            raise ValueError(
                "Capital API returned zero/empty available field. "
                "limits=" + repr({k: limits.get(k) for k in ("Net", "availablecash", "data")})
            )

    except Exception as exc:
        _capital_api_failures += 1
        cache_is_zero = (_capital_cache <= 0.0)

        # Always log at WARNING (details for log review)
        logger.warning(
            "Capital API failure #%d: %s. "
            "Cache value: Rs%.2f (TTL: %ds old).",
            _capital_api_failures, exc,
            _capital_cache,
            int(now - _capital_last_update) if _capital_last_update > 0 else -1,
        )

        # Attempt session recovery on repeated failures (e.g. token expired overnight).
        # Bug1 FIX: only reconnect inside the trading-day window — an off-hours
        # reconnect is pointless and destructive (kills the WebSocket session).
        if (
            _capital_api_failures >= 5
            and _capital_api_failures % 5 == 0
            and _capital_fetch_window_open()
        ):
            try:
                logger.warning(
                    "🚨 Capital API has failed %d consecutive times. "
                    "Attempting silent session recovery/reconnect...",
                    _capital_api_failures
                )
                from core.kotak_client import force_reconnect as _force_reconnect
                _force_reconnect()
            except Exception as reconnect_err:
                logger.error("Session recovery failed during Capital API failure handling: %s", reconnect_err)

        # CRITICAL path 1: Cache still at initial 0.0 — all new orders will be
        # blocked with qty=0. Fire CRITICAL immediately on first failure.
        if cache_is_zero:
            logger.critical(
                "🚨 CAPITAL API FAILED and cache is ZERO (no valid capital "
                "fetched this session). ALL new buy orders will return qty=0 "
                "until capital API recovers. Failure #%d: %s",
                _capital_api_failures, exc,
            )
            try:
                alert_critical(
                    f"🚨 Capital API DOWN — no valid capital in cache. "
                    f"All new buy orders BLOCKED (qty=0). "
                    f"Failure #{_capital_api_failures}: {exc}"
                )
            except Exception:
                pass

        # CRITICAL path 2: Threshold crossed — stale cache, repeated failure
        elif _capital_api_failures == _CAPITAL_API_FAILURE_ALERT_THRESHOLD:
            logger.critical(
                "🚨 Capital API has failed %d consecutive times. "
                "Trading on STALE capital cache of Rs%.2f. "
                "New orders may be mis-sized or blocked if cache expires. "
                "Latest error: %s",
                _capital_api_failures, _capital_cache, exc,
            )
            try:
                alert_critical(
                    f"🚨 Capital API failed {_capital_api_failures}x consecutively. "
                    f"Running on stale cache: Rs{_capital_cache:.2f}. "
                    f"Error: {exc}"
                )
            except Exception:
                pass

        # Periodic reminder every 5 failures after threshold
        elif (
            _capital_api_failures > _CAPITAL_API_FAILURE_ALERT_THRESHOLD
            and _capital_api_failures % 5 == 0
        ):
            logger.critical(
                "🚨 Capital API still failing (failure #%d). "
                "Stale cache: Rs%.2f.",
                _capital_api_failures, _capital_cache,
            )

    return _capital_cache


def _calculate_paper_capital_available() -> float:
    """
    FIXED: Paper capital now properly deducted based on open positions.

    Formula: available = initial_capital - deployed_in_positions + closed_pnl
    """
    initial_capital = max(0.0, safe_float(cfg("risk", "paper_capital", 10000), 10000.0))

    # Sum deployment in all open positions
    positions = get_open_positions()
    deployed = sum(
        max(0.0, safe_float(p.get("quantity", 0), 0.0)) * safe_float(p.get("entry_price", 0), 0.0)
        for p in positions
    )

    # Sum P&L from all closed positions today
    closed_pnl = get_today_gross_pnl()

    # Available capital = initial - deployed + closed_pnl
    available = initial_capital - deployed + closed_pnl

    logger.debug(
        f"Paper capital: Initial={initial_capital} - Deployed={deployed} + ClosedPnL={closed_pnl} = Available={available}"
    )

    return max(0, available)  # Never go below zero


def get_margin_status() -> dict:
    """
    📊 MARGIN MONITORING: Returns current margin deployment status (FIXED).

    NOW ACCOUNTS FOR:
    - Current prices (not just entry prices)
    - Unrealized P&L
    - Closed P&L
    - Paper vs Live modes
    - FX11-B: Broker-Style Accounting replacing Available Cash.

    Returns: {
        'starting_capital': float,
        'account_equity': float,              # Base + Closed PnL + Unrealized PnL
        'gross_exposure': float,              # Current market value of all positions
        'margin_blocked': float,              # Total margin actively blocked
        'free_margin': float,                 # account_equity - margin_blocked
        'remaining_buying_power': float,      # free_margin * leverage
        'margin_utilization': float,          # % of equity blocked as margin
        'closed_pnl': float,
        'unrealized_pnl': float,
        'margin_leverage': float,
        'current_position_value': float,
        'entry_position_value': float
    }
    """
    from core.state_manager import get_today_stats
    stats = get_today_stats()

    # PnL / position valuation — computed up-front so the LIVE fallback below
    # can reconstruct start-of-day capital from open-position margin.
    valuation = _current_position_valuation()
    current_position_value = valuation["current_position_value"]
    entry_position_value = valuation["entry_position_value"]
    unrealized_pnl = valuation["unrealized_pnl"]
    closed_pnl = safe_float(get_today_gross_pnl(), 0.0)

    # Effective margin leverage
    margin_leverage = safe_float(cfg("risk", "margin_leverage", 2.0), 2.0)
    if not cfg("risk", "allow_margin", False):
        margin_leverage = 1.0

    # 1. Starting Capital
    if stats and stats.get("capital_start") is not None:
        starting_capital = max(0.0, safe_float(stats["capital_start"], 0.0))
    elif TRADING_MODE == "PAPER":
        starting_capital = safe_float(cfg("risk", "paper_capital", 10000), 10000.0)
    else:
        # LIVE + capital_start not persisted yet. Raw broker "available" is only
        # FREE margin — once a position is open it collapses to the leftover
        # (e.g. ₹34.62 after margin is blocked). Bug2 FIX: reconstruct true
        # start-of-day capital by adding back margin blocked by open positions.
        avail = safe_float(_get_available_capital(force_refresh=True), 0.0)
        blocked_by_positions = (
            entry_position_value / margin_leverage if margin_leverage > 0 else entry_position_value
        )
        starting_capital = max(0.0, avail + blocked_by_positions - closed_pnl)

    # 3. Account Equity
    account_equity = max(0.0, starting_capital + closed_pnl + unrealized_pnl)

    # 4. Gross Exposure
    gross_exposure = current_position_value

    # 5. Margin Blocked
    margin_blocked = gross_exposure / margin_leverage if margin_leverage > 0 else gross_exposure
    
    # 6. Free Margin
    free_margin = max(0.0, account_equity - margin_blocked)
    
    # 7. Remaining Buying Power
    remaining_buying_power = free_margin * margin_leverage
    
    # 8. Margin Utilization %
    margin_utilization = (margin_blocked / account_equity) * 100 if account_equity > 0 else 0.0
    
    return {
        'starting_capital': starting_capital,
        'account_equity': account_equity,
        'gross_exposure': gross_exposure,
        'margin_blocked': margin_blocked,
        'free_margin': free_margin,
        'remaining_buying_power': remaining_buying_power,
        'margin_utilization': margin_utilization,
        'closed_pnl': closed_pnl,
        'unrealized_pnl': unrealized_pnl,
        'margin_leverage': margin_leverage,
        'current_position_value': current_position_value,
        'entry_position_value': entry_position_value
    }


def get_capital_snapshot() -> dict:
    """
    FX11-B: Canonical capital breakdown for dashboard/API display.
    """
    margin = get_margin_status()
    
    return {
        "mode": TRADING_MODE,
        "starting_capital": round(margin['starting_capital'], 2),
        "account_equity": round(margin['account_equity'], 2),
        "gross_exposure": round(margin['gross_exposure'], 2),
        "margin_blocked": round(margin['margin_blocked'], 2),
        "free_margin": round(margin['free_margin'], 2),
        "remaining_buying_power": round(margin['remaining_buying_power'], 2),
        "margin_utilization": round(margin['margin_utilization'], 2),
        "closed_pnl": round(margin['closed_pnl'], 2),
        "unrealized_pnl": round(margin['unrealized_pnl'], 2),
        "margin_enabled": bool(cfg("risk", "allow_margin", False)),
        "margin_leverage": margin['margin_leverage']
    }


def update_trailing_stop_losses(live_prices: dict[str, float]):
    """
    Delayed TSL with configurable activation & mode.
    
    1. TSL only activates when profit reaches SL% × activation_ratio
    2. After activation, applies TSL mode (trailing or locked)
    3. Trailing mode: SL moves up as price rises, never down
    4. Locked mode: SL stays fixed at activation price
    """
    tsl_activation_ratio = max(1.0, min(2.0, safe_float(cfg("risk", "tsl_activation_ratio", 1.4), 1.4)))
    tsl_mode_is_trailing = bool(cfg("risk", "tsl_mode_after_activation", True))
    tsl_mode = "trailing" if tsl_mode_is_trailing else "locked"
    
    for position in get_open_positions():
        symbol      = position["symbol"]
        entry_price = safe_float(position.get("entry_price"), 0.0)
        initial_sl  = safe_float(position.get("stop_loss"), 0.0)
        current     = safe_float(live_prices.get(symbol), 0.0)
        current_tsl = safe_float(position.get("trailing_sl") or initial_sl, 0.0)

        if not current or not entry_price or not initial_sl:
            continue

        # ─────────────────────────────────────────────────────────
        # PROFIT TARGET PARTIAL EXIT
        # ─────────────────────────────────────────────────────────
        if bool(cfg("risk", "partial_profit_booking_enabled", False)):
            _notes = str(position.get("notes") or "")
            if "PARTIAL_PROFIT_DONE" not in _notes:
                direction = position.get("action", "LONG")
                
                # Fetch direction-specific targets
                if direction == "LONG":
                    _tgt_pct = safe_float(cfg("risk", "long_profit_target_percent", 0.015), 0.015)
                    profit_pct = (current - entry_price) / entry_price
                else:
                    _tgt_pct = safe_float(cfg("risk", "short_profit_target_percent", 0.025), 0.025)
                    profit_pct = (entry_price - current) / entry_price

                # R7: Direction-aware partial fraction (Long=0.25, Short=1.0)
                if direction == "LONG":
                    _fraction = safe_float(cfg("risk", "long_partial_profit_fraction",
                                              cfg("risk", "partial_profit_fraction", 1.0)), 1.0)
                else:
                    _fraction = safe_float(cfg("risk", "short_partial_profit_fraction",
                                              cfg("risk", "partial_profit_fraction", 1.0)), 1.0)

                _qty = int(position.get("quantity") or 0)
                
                if profit_pct >= _tgt_pct and _qty > 0:
                    _exit_qty = max(1, int(_qty * _fraction))
                    # L5 FIX: persist the PARTIAL_PROFIT_DONE guard BEFORE selling. If the
                    # notes write fails, skip the sell this cycle — otherwise a swallowed
                    # write meant the guard never stuck and the position was partially sold
                    # again on every subsequent loop.
                    _guard_ok = False
                    try:
                        from core.state_manager import update_position_notes
                        _new_notes = (_notes + " | PARTIAL_PROFIT_DONE").strip(" | ")
                        update_position_notes(symbol, _new_notes)
                        _guard_ok = True
                    except Exception as _guard_err:
                        logger.error(
                            "Partial-profit guard write failed for %s (%s); skipping partial "
                            "exit to avoid repeated sells", symbol, _guard_err,
                        )
                    if _guard_ok:
                        logger.info(
                            "[PartialProfit] %s Profit %.2f%% >= %.2f%% | %s | "
                            "Exiting %d/%d shares @ Rs%.2f (fraction=%.0f%%)",
                            direction, profit_pct * 100, _tgt_pct * 100, symbol, _exit_qty, _qty, current, _fraction * 100
                        )
                        place_sell_order(
                            symbol         = symbol,
                            exit_price     = current,
                            reason         = "PARTIAL_PROFIT_TARGET",
                            quantity       = _exit_qty,
                        )


        # ─────────────────────────────────────────────────────────
        # RSI EXIT: Exit position when RSI > threshold
        # Research-proven: fraction=1.0 (full exit) gives +31.9% vs +21.8% with half-exit
        # Controlled via trading_settings.json:
        #   "partial_exit_rsi_enabled": true
        #   "partial_exit_rsi_threshold": 72
        #   "partial_exit_fraction": 1.0   ← full exit (was 0.5 for half-exit)
        #   "partial_exit_mode": "full"
        # ─────────────────────────────────────────────────────────
        direction = position.get("action", "LONG")

        # ─────────────────────────────────────────────────────────
        # RSI EXIT: Exit position when RSI > threshold (Long) or RSI < threshold (Short)
        # ─────────────────────────────────────────────────────────
        if bool(cfg("risk", "partial_exit_rsi_enabled", False)):
            _notes = str(position.get("notes") or "")
            if "PARTIAL_EXIT_DONE" not in _notes:
                _long_rsi_thr  = safe_float(cfg("risk", "long_rsi_exit_threshold", 72.0), 72.0)
                _short_rsi_thr = safe_float(cfg("risk", "short_rsi_exit_threshold", 17.0), 17.0)
                _fraction = safe_float(cfg("risk", "partial_exit_fraction", 1.0), 1.0)

                _qty      = int(position.get("quantity") or 0)
                _live_rsi = None
                try:
                    from core.data_fetcher import get_candle_history
                    from core.strategy import _build_indicators
                    import pandas as pd
                    # L1 FIX: get_candle_history has no `max_candles` kwarg — the old
                    # call raised TypeError (swallowed below), so this RSI exit never
                    # fired. Fetch the history and slice the last 30 closed candles.
                    _hist = get_candle_history(symbol)
                    if _hist:
                        _hist = _hist[-30:]
                    if _hist and len(_hist) >= 15:
                        _df = pd.DataFrame(_hist)
                        _df.columns = [c.lower() for c in _df.columns]
                        _df = _build_indicators(_df)
                        if "rsi" in _df.columns and not _df["rsi"].isna().all():
                            _live_rsi = float(_df["rsi"].iloc[-1])
                except Exception:
                    pass
                
                if _live_rsi is not None and _qty > 0:
                    is_rsi_exit = False
                    if direction == "LONG" and _live_rsi >= _long_rsi_thr:
                        is_rsi_exit = True
                    elif direction == "SHORT" and _live_rsi <= _short_rsi_thr:
                        is_rsi_exit = True

                    if is_rsi_exit:
                        _exit_qty = max(1, int(_qty * _fraction))
                        # L5 FIX: persist the guard BEFORE selling; skip if it fails.
                        _guard_ok = False
                        try:
                            from core.state_manager import update_position_notes
                            _new_notes = (_notes + " | PARTIAL_EXIT_DONE").strip(" | ")
                            update_position_notes(symbol, _new_notes)
                            _guard_ok = True
                        except Exception as _guard_err:
                            logger.error(
                                "RSI-exit guard write failed for %s (%s); skipping to avoid "
                                "repeated sells", symbol, _guard_err,
                            )
                        if _guard_ok:
                            logger.info(
                                "[PartialExit] %s RSI=%.1f | %s | Exiting %d/%d shares @ Rs%.2f",
                                direction, _live_rsi, symbol, _exit_qty, _qty, current
                            )
                            place_sell_order(
                                symbol         = symbol,
                                exit_price     = current,
                                reason         = "PARTIAL_EXIT_RSI",
                                quantity       = _exit_qty,
                            )

        # Get current TSL activation state
        tsl_state        = get_tsl_activation_state(symbol)
        is_tsl_activated = tsl_state["tsl_activated"]

        # ─────────────────────────────────────────────────────────
        # PHASE 1: Check if TSL should be activated (not yet active)
        # ─────────────────────────────────────────────────────────
        if not is_tsl_activated:
            # Calculate activation threshold
            sl_percent = abs(entry_price - initial_sl) / entry_price
            if direction == "LONG":
                activation_threshold = entry_price + (entry_price * sl_percent * tsl_activation_ratio)
                activation_hit = current >= activation_threshold
            else:
                activation_threshold = entry_price - (entry_price * sl_percent * tsl_activation_ratio)
                activation_hit = current <= activation_threshold
            
            # Check if price reached activation threshold
            if activation_hit:
                # TSL activates!
                update_tsl_activation_state(symbol, True, current, tsl_mode)
                profit_pct = round(((current - entry_price) / entry_price) * 100, 2) if direction == "LONG" else round(((entry_price - current) / entry_price) * 100, 2)
                logger.info(
                    f"🎯 TSL ACTIVATED | {symbol} | "
                    f"Price ₹{current} | Entry ₹{entry_price} | "
                    f"Profit +{profit_pct}% | "
                    f"Mode: {tsl_mode.upper()}"
                )
                is_tsl_activated = True
        
        # ─────────────────────────────────────────────────────────
        # PHASE 2: Apply TSL based on current mode
        # ─────────────────────────────────────────────────────────
        if is_tsl_activated:
            tsl_pct = max(0.0001, min(0.20, safe_float(cfg("risk", "trailing_sl_percent", 0.008), 0.008)))
            
            if tsl_mode == "trailing":
                # Trailing mode: SL moves up with price, never down (LONG), or down with price, never up (SHORT)
                if direction == "LONG":
                    raw_tsl = current * (1 - tsl_pct)
                    new_tsl = round_to_tick(raw_tsl)
                    trail_valid = new_tsl > current_tsl
                else:
                    raw_tsl = current * (1 + tsl_pct)
                    new_tsl = round_to_tick(raw_tsl)
                    trail_valid = new_tsl < current_tsl if current_tsl > 0 else True
                
                if trail_valid:
                    update_trailing_sl(symbol, new_tsl)
                    logger.info(
                        f"📈 TRAILING SL | {symbol} | "
                        f"₹{current_tsl} → ₹{new_tsl}"
                    )

                    # Modify Kotak's SL order in LIVE mode
                    if TRADING_MODE == "LIVE":
                        sl_order_id = position.get("kotak_sl_order_id")
                        if sl_order_id:
                            _modify_sl_order(
                                sl_order_id,
                                new_tsl,
                                position["quantity"],
                                "S" if direction == "LONG" else "B"
                            )
            
            elif tsl_mode == "locked":
                # Locked mode: SL stays fixed at activation price (no update needed)
                pass




def check_max_daily_loss() -> bool:
    """Daily loss check based on initial trading capital (not available after position deployment)."""
    gross_pnl = get_today_gross_pnl()

    # L3 FIX: include UNREALIZED P&L of open positions. Using realized-only P&L
    # let an open position bleed far past max_daily_loss_percent before the halt
    # ever tripped (it only tripped once the loss was booked on close).
    try:
        unrealized_pnl = safe_float(_current_position_valuation().get("unrealized_pnl"), 0.0)
    except Exception:
        unrealized_pnl = 0.0
    total_pnl = safe_float(gross_pnl, 0.0) + unrealized_pnl
    
    # Use INITIAL capital for loss limit calculation, not available capital
    # (available capital gets reduced when positions are deployed, which incorrectly constrains the loss limit)
    if TRADING_MODE == "PAPER":
        initial_capital = safe_float(cfg("risk", "paper_capital", 100000), 100000.0)
    else:
        # F019 FIX: Use the persistent start-of-day capital established by FX06
        from core.state_manager import get_today_stats
        stats = get_today_stats()
        if stats and stats.get("capital_start") is not None:
            initial_capital = max(1.0, safe_float(stats["capital_start"], 0.0))
        else:
            # Bug2 FIX: capital_start not persisted yet — reuse get_margin_status()'s
            # reconstructed starting capital (adds back open-position margin) instead
            # of raw free margin, which would understate the loss limit after a trade.
            initial_capital = max(1.0, safe_float(get_margin_status().get("starting_capital"), 100000.0))
    
    max_daily_loss_pct = max(0.0, min(1.0, safe_float(cfg("risk", "max_daily_loss_percent", 0.05), 0.05)))
    max_daily_loss = -(initial_capital * max_daily_loss_pct)

    if total_pnl <= max_daily_loss:
        logger.warning(
            f"🚨 MAX DAILY LOSS HIT | "
            f"Total P&L: ₹{total_pnl:.2f} (realized ₹{gross_pnl:.2f} + unrealized ₹{unrealized_pnl:.2f}) | "
            f"Limit: ₹{max_daily_loss:.2f} | "
            f"No new trades today."
        )
        try:
            from core.circuit_breaker import halt_all_trading
            halt_all_trading(f"Max daily loss ₹{total_pnl:.2f}")
            alert_critical(
                f"Max daily loss hit: ₹{total_pnl:.2f} "
                f"(realized ₹{gross_pnl:.2f} + unrealized ₹{unrealized_pnl:.2f}, "
                f"limit ₹{max_daily_loss:.2f}). New trades halted."
            )
        except Exception:
            pass
        return True
    return False
 

def squareoff_all_intraday(live_prices: dict[str, float] | None = None, **kwargs):
    """Force-closes all MIS positions at 3:15 PM. Runs once per trading day."""
    global _squareoff_done, _squareoff_done_date

    # L2 FIX: reset the once-per-day guard when the date rolls over so a
    # continuously-running process still squares off every subsequent day.
    _today = datetime.now().date()
    if _squareoff_done_date != _today:
        _squareoff_done = False
        _squareoff_done_date = _today

    if _squareoff_done:
        return

    if datetime.now().time() < INTRADAY_SQUAREOFF:
        return

    from core.state_manager import get_trading_session_state, mark_liquidating, resume_entries

    initial_state = get_trading_session_state().get("state", "ACTIVE")
    was_active = (initial_state == "ACTIVE")

    if was_active:
        mark_liquidating("EOD_SQUAREOFF_STARTED")

    open_positions = get_open_positions()
    if not open_positions:
        _squareoff_done = True
        _squareoff_done_date = _today
        if was_active:
            resume_entries("EOD_SQUAREOFF_NO_OPEN_POSITIONS")
        return

    logger.warning(
        f"⏰ 3:15 PM — Squaring off {len(open_positions)} position(s)."
    )

    failures = []
    for position in open_positions:
        symbol  = position["symbol"]
        current, price_source = _resolve_liquidation_price(position, live_prices)
        if current <= 0:
            # F007 FIX: If WS feed is dead at 3:15 PM, we must still square off with a
            # marketable limit. E1 FIX: direction matters — a long exit SELLS (price low
            # to hit the bid) while a short exit BUYS-to-cover (price high to hit the ask).
            # Using entry*0.95 for a short cover would rest a buy limit BELOW market and
            # never fill, leaving the short open.
            _direction = str(position.get("action", "LONG")).upper()
            _fallback_mult = 0.95 if _direction == "LONG" else 1.05
            fallback = safe_float(position.get("entry_price"), 0.0) * _fallback_mult
            if fallback > 0:
                current = fallback
                price_source = "fallback_entry_price"
                logger.warning(
                    "⚠️ No live quote for %s during squareoff; using fallback price %s to force execution",
                    symbol, current
                )
            else:
                mark_position_reconciliation_pending(
                    symbol,
                    "SQUAREOFF_MISSING_EXIT_QUOTE",
                    "No live quote available and entry_price is 0; sell order was not sent",
                    exit_price_source="unknown",
                )
                logger.warning(
                    "Squareoff blocked for %s because live quote and entry_price are both 0",
                    symbol,
                )
                failures.append(symbol)
                continue

        if not place_sell_order(symbol, current, "SQUAREOFF", exit_price_source=price_source):
            failures.append(symbol)

    remaining = get_open_positions()
    if failures or remaining:
        logger.error(
            "Squareoff incomplete. Failed=%s Remaining=%s. Will retry on next loop.",
            failures,
            [p.get("symbol") for p in remaining],
        )
        _squareoff_done = False
        return

    _squareoff_done = True
    _squareoff_done_date = _today
    if was_active:
        resume_entries("EOD_SQUAREOFF_COMPLETE")
    else:
        logger.info("EOD squareoff complete. Preserving pre-existing lock state: %s", initial_state)


def get_portfolio_snapshot() -> dict:
    if TRADING_MODE == "PAPER":
        return {
            "mode":           "PAPER",
            "open_positions": get_open_positions(),
            "count":          len(get_open_positions()),
        }
    try:
        from core.kotak_client import get_client
        client   = get_client()
        limits   = client.limits(segment="ALL", exchange="ALL", product="ALL")
        holdings = client.holdings()
        return {"mode": "LIVE", "limits": limits, "holdings": holdings}
    except Exception as e:
        logger.error(f"Portfolio snapshot failed: {e}")
        return {}


# ════════════════════════════════════════════════════════════
#   KOTAK API CALLS (Internal)
# ════════════════════════════════════════════════════════════

def _parse_order_response(response) -> str | None:
    """
    CLEANER: Session refresh is done BEFORE place_order().
    Just parse the response.
    """
    if not response:
        logger.error(f"❌ Order response is None/empty")
        return None
        
    if not isinstance(response, dict):
        logger.error(f"❌ Order response is not dict: {type(response)} = {response}")
        return None
    
    # Check for errors FIRST
    if response.get("error") or response.get("Error") or response.get("Error Message"):
        logger.error("❌ Kotak order error response: %s", response)
        return None
    
    # Try to find order_id in different possible field names
    order_id = (
        response.get("nOrdNo")       # Kotak's primary field
        or response.get("order_id")
        or response.get("id")
        or response.get("ordNo")     # Alternative
        or response.get("orderNo")   # Another alternative
    )
    
    if order_id:
        logger.debug(f"✅ Extracted order_id: {order_id}")
        return str(order_id)
    
    logger.error(f"❌ No order_id found in response. Response keys: {list(response.keys()) if response else 'None'}")
    logger.error(f"   Full response: {response}")
    return None


def _send_kotak_order(
    trading_symbol: str,
    transaction:    str,
    quantity:       int,
    price:          float,
    order_type:     str = "L",
    product:        str = "MIS",
) -> str | None:
    """
    🔧 ENHANCED VERSION with pre-flight token validation.
    
    Execution Flow:
    1. Pre-flight: Validate session & token (prevent stCode=100008)
    2. Strip -EQ suffix from trading_symbol (Kotak order API wants raw ticker)
    3. Build order payload
    4. Execute order with 1x retry on auth error (not infinite loop!)
    5. Parse response carefully
    """

    # STEP 0: Handle trading symbol
    # 🔥 FIX (2026-05-27): Kotak order API REQUIRES the -EQ suffix!
    # It does NOT want bare symbols. Send as-is: "DIVISLAB-EQ" not "DIVISLAB"
    # Old code was wrong: "Kotak search uses SYMBOL-EQ but order API wants SYMBOL"
    
    # Keep the symbol WITH exchange suffix - that's what order API expects
    order_symbol = trading_symbol  # ← KEEP -EQ suffix!
    
    logger.info(
        f"📋 Preparing {transaction} order | Symbol: {order_symbol} | "
        f"Qty: {quantity} | Price: ₹{price}"
    )

    # STEP 1: PRE-FLIGHT TOKEN VALIDATION
    
    if not validate_and_fix_session_before_order():
        logger.error(
            f"❌ PRE-FLIGHT FAILED: Token not Trade-scoped or refreshable. "
            f"Aborting order."
        )
        health = diagnose_token_health()
        logger.error(f"Token health: {health}")
        return None

    normalized_order_type = str(order_type).upper()
    order_price = round_to_tick(safe_float(price, 0.0))
    if quantity <= 0 or order_price <= 0:
        logger.error(
            "Invalid broker order payload | symbol=%s transaction=%s qty=%r price=%r",
            order_symbol,
            transaction,
            quantity,
            price,
        )
        return None
    if normalized_order_type in ("MKT", "MARKET"):
        order_price = _broker_safe_limit_price(price, transaction)
        normalized_order_type = "L"
        logger.warning(
            "Converted market %s order for %s to broker-safe limit @ %s",
            transaction,
            order_symbol,
            order_price,
        )

    # STEP 2: Build Order Payload
    order_kwargs = dict(
        exchange_segment   = "nse_cm",  # ✅ Valid: "nse_cm" is accepted by NeoAPI 2.x
        product            = product,
        price              = str(order_price),
        order_type         = normalized_order_type,
        quantity           = str(quantity),
        validity           = "DAY",
        trading_symbol     = order_symbol,  # 🔥 USE SYMBOL WITH -EQ SUFFIX
        transaction_type   = transaction,
        amo                = "NO",
        disclosed_quantity = "0",
        market_protection  = "0",
        pf                 = "N",
        trigger_price      = "0",
    )

    # STEP 3: Execute with SMART Retry
    for attempt in range(2):
        attempt_num = attempt + 1
        
        try:
            # 🔐 CRITICAL: Ensure Trade token is set on client before order
            # This prevents NeoAPI from using stale View tokens
            try:
                if attempt == 0:
                    client = ensure_trade_token_on_client()
                else:
                    logger.warning(f"[Attempt 2/2] Auth error detected — forcing fresh session with Trade token...")
                    # Force reconnect by invalidating and getting fresh client
                    from core.kotak_client import force_reconnect as _force_reconnect
                    _force_reconnect()  # This destroys old instance
                    client = ensure_trade_token_on_client()  # Get fresh one
            except RuntimeError as e:
                if attempt == 0:
                    logger.warning(f"🔐 Token guarantee failed on attempt 1: {e} — will retry...")
                    continue
                else:
                    logger.error(f"🔐 Token guarantee failed on attempt 2: {e} — aborting")
                    return None
            
            # Verify tokens are still set before place_order
            try:
                cfg = client.api_client.configuration
                if not getattr(cfg, 'access_token', None):
                    logger.error(f"❌ access_token was reset before place_order!")
                    if attempt == 0:
                        logger.warning("Will retry with fresh session...")
                        continue
                    else:
                        return None
                if not getattr(cfg, 'edit_token', None):
                    logger.error(f"❌ edit_token was reset before place_order!")
                    if attempt == 0:
                        logger.warning("Will retry with fresh session...")
                        continue
                    else:
                        return None
            except Exception as e:
                logger.error(f"⚠️ Could not verify tokens before place_order: {e}", exc_info=True)
            
            # Verify configuration before order
            try:
                cfg = client.api_client.configuration
                access_tok = getattr(cfg, 'access_token', None)
                edit_tok = getattr(cfg, 'edit_token', None)
                tokens_match = (access_tok == edit_tok) if (access_tok and edit_tok) else "N/A"
                
                # Show token prefixes for debugging
                access_preview = f"{access_tok[:20]}..." if access_tok else "MISSING"
                edit_preview = f"{edit_tok[:20]}..." if edit_tok else "MISSING"
                
                logger.info(
                    f"📋 PRE-ORDER CONFIG DUMP (ATTEMPT {attempt_num}/2) | "
                    f"host={getattr(cfg, 'host', 'MISSING')} | "
                    f"access_token={access_preview} | "
                    f"edit_token={edit_preview} | "
                    f"tokens_match={tokens_match} | "
                    f"edit_sid={getattr(cfg, 'edit_sid', 'MISSING')} | "
                    f"serverId={getattr(cfg, 'serverId', 'MISSING')} | "
                    f"base_url={getattr(cfg, 'base_url', '')}"
                )
                
                # CRITICAL: Also log client.access_token and client.sid (object properties)
                client_access = getattr(client, 'access_token', None)
                client_sid = getattr(client, 'sid', None)
                logger.debug(
                    f"📋 CLIENT OBJECT STATE | "
                    f"client.access_token={f'{client_access[:20]}...' if client_access else 'MISSING'} | "
                    f"client.sid={client_sid[:20] if client_sid else 'MISSING'}... | "
                    f"Mismatch: access_token_cfg != access_token_obj={access_tok != client_access}"
                )
            except Exception as diag_e:
                logger.error(f"📋 Config dump failed: {diag_e}", exc_info=True)
            
            response = call_broker_api(client.place_order, **order_kwargs)
            
            # 📋 TEMPORARY DIAGNOSTIC: Log raw broker response
            logger.debug(f"📋 RAW BROKER RESPONSE: {response}")
            
            if response and isinstance(response, dict):
                stCode = response.get("stCode")
                errMsg = response.get("errMsg", "")
                logger.debug(f"Response stCode={stCode}, errMsg={errMsg}")
                
                if stCode == 100008 or "unauthorized" in errMsg.lower():
                    if attempt == 0:
                        logger.warning(
                            f"🔐 Auth error on attempt 1 (stCode={stCode}, errMsg={errMsg}) → "
                            f"will retry once with fresh session"
                        )
                        # DIAGNOSTIC: Log possible causes
                        logger.warning(
                            "🔴 stCode=100008 'unauthorized' can be caused by:\n"
                            "   CAUSE #1: CONSUMER_KEY is expired or invalid\n"
                            "       → Check Kotak Developer Portal for active app status\n"
                            "   CAUSE #2: IP Whitelist block (your server IP not registered)\n"
                            "       → Kotak prod requires registered IPs in Developer Portal\n"
                            "       → Check: Settings → App IP Whitelist\n"
                            "   CAUSE #3: Token scope degradation (already handled by retry)\n"
                            "       → Will retry with force_reconnect()...\n"
                        )
                        logger.debug(f"🔍 Triggering force_reconnect to get fresh tokens...")
                        continue
                    else:
                        logger.error(
                            f"🔐 Auth error persists after session refresh (attempt 2). "
                            f"Response: {response}. Aborting to prevent infinite loop."
                        )
                        return None
                
                if response.get("error") or response.get("Error"):
                    logger.error(f"Order rejected by broker (attempt {attempt_num}): {response}")
                    return None
            
            order_id = _parse_order_response(response)
            if order_id:
                logger.info(f"✅ Order placed (attempt {attempt_num}/2) | Order ID: {order_id}")
                return order_id
            
            logger.error(f"Order response invalid (attempt {attempt_num}): {response}")
            return None
        
        except (ConnectionError, OSError, TimeoutError) as e:
            if attempt == 0:
                logger.warning(f"Network error on attempt 1: {e} — retrying...")
            else:
                logger.error(f"Network error on attempt 2: {e}. Querying broker to check if order was placed...")
                # F013 FIX: Check if order actually got placed despite network error
                try:
                    from core.order_verifier import fetch_kotak_order_row
                    from core.broker_reconciliation import _fetch_order_report_rows
                    
                    # Search recent orders for matching order
                    recent_orders = _fetch_order_report_rows()
                    for broker_order in recent_orders:
                        if (str(broker_order.get("trading_symbol", "")).upper() == str(trading_symbol).upper() and 
                            int(broker_order.get("quantity", 0)) == int(quantity)):
                            matched_order_id = broker_order.get("nOrdNo") or broker_order.get("order_id")
                            if matched_order_id:
                                logger.critical(f"✅ Order WAS placed on broker! Found order: {matched_order_id}")
                                return matched_order_id
                except Exception as broker_query_err:
                    logger.debug(f"Broker query failed: {broker_query_err}. Treating as not placed.")
                
                logger.error(f"Aborting after network error on attempt 2. Order assumed NOT placed.")
                return None
        
        except Exception as e:
            logger.error(f"Order execution failed (attempt {attempt_num}): {e}", exc_info=True)
            if attempt == 0:
                logger.info("Attempting one retry...")
            else:
                logger.error("Retry failed. Aborting.")
                return None
    
    logger.error("Order failed after all retry attempts")
    return None


def _send_kotak_sl_order(
    trading_symbol: str,
    quantity:       int,
    trigger_price:  float,
    product:        str = "MIS",
    transaction_type: str = "S",
) -> str | None:
    """
    Places a SL limit order on Kotak.
    This is the broker-side protection —
    fires even if AlcoSoft is offline.
    """
    # STEP 0: Handle trading symbol
    # 🔥 FIX (2026-05-27): Kotak order API REQUIRES the -EQ suffix!
    order_symbol = trading_symbol  # ← KEEP -EQ suffix!
    software_trigger_price = safe_float(trigger_price, 0.0)
    quantity = safe_int(quantity, 0)
    if quantity <= 0 or software_trigger_price <= 0:
        logger.error(
            "Invalid SL order payload | symbol=%s qty=%r trigger=%r",
            order_symbol,
            quantity,
            software_trigger_price,
        )
        return None
        
    # Calculate Disaster Backup SL
    from core.trading_settings import get as cfg
    trigger_buffer_pct = safe_float(cfg("risk", "broker_sl_trigger_buffer_pct", 0.01), 0.01)
    limit_offset_pct = safe_float(cfg("risk", "broker_sl_limit_offset_pct", 0.0005), 0.0005)
    
    if transaction_type == "S":
        broker_trigger_price_raw = software_trigger_price * (1.0 - trigger_buffer_pct)
        broker_limit_price_raw = broker_trigger_price_raw * (1.0 - limit_offset_pct)
    else:
        broker_trigger_price_raw = software_trigger_price * (1.0 + trigger_buffer_pct)
        broker_limit_price_raw = broker_trigger_price_raw * (1.0 + limit_offset_pct)
    
    # Round to nearest valid tick size based on NSE dynamic pricing rules
    broker_trigger_price = round_to_tick(broker_trigger_price_raw)
    broker_limit_price = round_to_tick(broker_limit_price_raw)
        
    logger.debug(f"📋 SL order symbol: {order_symbol}")
    logger.info(
        f"🔄 [SL] Starting SL order placement | Symbol: {order_symbol} | Qty: {quantity} | "
        f"Software SL: ₹{software_trigger_price} | Broker Trigger: ₹{broker_trigger_price} | "
        f"Broker Limit: ₹{broker_limit_price}"
    )
    
    sl_kwargs = dict(
        exchange_segment   = "nse_cm",
        product            = product,
        price              = f"{broker_limit_price:.2f}",
        order_type         = "SL",
        quantity           = str(quantity),
        validity           = "DAY",
        trading_symbol     = order_symbol,  # 🔥 USE SYMBOL WITH -EQ SUFFIX
        transaction_type   = transaction_type,
        amo                = "NO",
        disclosed_quantity = "0",
        market_protection  = "0",
        pf                 = "N",
        trigger_price      = f"{broker_trigger_price:.2f}",
    )

    logger.info(f"📋 SL ORDER KWARGS (WILL SEND TO KOTAK):")
    for key, val in sl_kwargs.items():
        logger.info(f"    {key:20s} = {val}")
    logger.info(f"   ℹ️ If Kotak rejects: Try exchange_segment='NSE' instead of 'nse_cm'")
    logger.info(f"   ℹ️ If Kotak rejects: Check if product '{product}' supports SL orders (try 'CNC' if 'MIS' rejected)")

    # ✅ FIXED: Use sl_kwargs (not order_kwargs!)
    for attempt in range(2):
        try:
            # 🔐 CRITICAL: Ensure Trade token is set on client before order
            try:
                if attempt == 0:
                    logger.debug(f"[SL-M Attempt 1/2] Ensuring Trade token...")
                    client = ensure_trade_token_on_client()
                    logger.debug(f"[SL-M Attempt 1/2] ✅ Got client with Trade token")
                else:
                    logger.warning(f"[SL-M Attempt 2/2] Auth error detected — forcing fresh session with Trade token...")
                    from core.kotak_client import force_reconnect as _force_reconnect
                    _force_reconnect()
                    client = ensure_trade_token_on_client()
                    logger.debug(f"[SL-M Attempt 2/2] ✅ Got fresh client with Trade token")
            except (RuntimeError, Exception) as e:
                logger.error(f"🔐 SL-M: Token guarantee failed (Attempt {attempt+1}/2): {type(e).__name__}: {e}", exc_info=True)
                if attempt == 0:
                    logger.warning(f"   Will retry on attempt 2...")
                    continue
                else:
                    logger.error(f"   Both attempts failed — aborting SL-M order")
                    return None
            
            logger.debug(f"[SL-M Attempt {attempt + 1}/2] Sending SL-M order to Kotak...")
            
            # 📋 TEMPORARY DIAGNOSTIC: Dump configuration before order
            try:
                cfg = client.api_client.configuration
                logger.info(
                    f"📋 PRE-ORDER CONFIG DUMP (SL-M) | "
                    f"host={getattr(cfg, 'host', 'MISSING')} | "
                    f"access_token={'SET' if getattr(cfg, 'access_token', None) else 'MISSING'} | "
                    f"edit_token={'SET' if getattr(cfg, 'edit_token', None) else 'MISSING'} | "
                    f"edit_sid={getattr(cfg, 'edit_sid', 'MISSING')} | "
                    f"serverId={getattr(cfg, 'serverId', 'MISSING')} | "
                    f"base_url={getattr(cfg, 'base_url', '')}"
                )
            except Exception as diag_e:
                logger.error(f"📋 Config dump failed: {diag_e}")
            
            response = call_broker_api(client.place_order, **sl_kwargs)
            
            # 📋 TEMPORARY DIAGNOSTIC: Log raw broker response
            logger.debug(f"📋 RAW BROKER RESPONSE (SL-M): {response}")
            
            # 🔥 CRITICAL: Check if response is an error BEFORE parsing
            if response is None:
                logger.error(f"❌ SL-M order returned None (broker circuit breaker or API error)")
                if attempt == 0:
                    continue
                else:
                    return None
            
            if isinstance(response, dict):
                error_msg = response.get("error") or response.get("Error") or response.get("Error Message") or response.get("message")
                if error_msg:
                    logger.error(f"❌ SL-M order API error response: {error_msg}")
                    logger.error(f"   Full response: {response}")
                    if attempt == 0:
                        continue
                    else:
                        return None
            
            order_id = _parse_order_response(response)
            if order_id:
                return order_id
            else:
                logger.error(f"❌ SL-M response parsing failed — no order_id found in: {response}")
                if attempt == 0:
                    continue
                else:
                    return None
        except (ConnectionError, OSError, TimeoutError) as e:
            if attempt == 0:
                logger.warning("SL-M network error (attempt 1): %s", e)
            else:
                logger.error("SL-M failed after retry (network): %s", e)
                return None
        except Exception as e:
            if attempt == 0:
                logger.warning("SL-M attempt 1 failed: %s", e)
            else:
                logger.error("SL-M order failed: %s", e, exc_info=True)
                return None
    return None


def _modify_sl_order(
    order_id:      str,
    new_trigger:   float,
    quantity:      int,
    transaction_type: str = "S",
) -> bool:
    """Modifies existing SL-M order when trailing SL moves up."""
    new_trigger = safe_float(new_trigger, 0.0)
    quantity = safe_int(quantity, 0)
    if quantity <= 0 or new_trigger <= 0:
        logger.error("Invalid SL modify payload | order_id=%s qty=%r trigger=%r", order_id, quantity, new_trigger)
        return False
    try:
        # Calculate Disaster Backup SL
        from core.trading_settings import get as cfg
        trigger_buffer_pct = safe_float(cfg("risk", "broker_sl_trigger_buffer_pct", 0.01), 0.01)
        limit_offset_pct = safe_float(cfg("risk", "broker_sl_limit_offset_pct", 0.0005), 0.0005)
        
        if transaction_type == "S":
            broker_trigger_price = round_to_tick(new_trigger * (1.0 - trigger_buffer_pct))
            broker_limit_price = round_to_tick(broker_trigger_price * (1.0 - limit_offset_pct))
        else:
            broker_trigger_price = round_to_tick(new_trigger * (1.0 + trigger_buffer_pct))
            broker_limit_price = round_to_tick(broker_trigger_price * (1.0 + limit_offset_pct))

        # 🔐 CRITICAL: Ensure Trade token is set before modifying order
        client = ensure_trade_token_on_client()
        
        response = call_broker_api(
            client.modify_order,
            order_id           = order_id,
            price              = str(broker_limit_price),
            quantity           = str(quantity),
            disclosed_quantity = "0",
            trigger_price      = str(broker_trigger_price),
            validity           = "DAY",
            order_type         = "SL",
        )
        if response is None or (isinstance(response, dict) and response.get("error")):
            logger.error("SL modify failed: %s", response)
            return False
        logger.info(
            f"SL modified | OrderID: {order_id} | "
            f"Software SL: ₹{new_trigger} | Broker Trigger: ₹{broker_trigger_price} | "
            f"Broker Limit: ₹{broker_limit_price}"
        )
        return True
    except RuntimeError as e:
        logger.error(f"SL modify: Token guarantee failed: {e}")
        return False
    except Exception as e:
        logger.error("SL modify failed: %s", e, exc_info=True)
        return False


def _cancel_kotak_order(order_id: str) -> bool:
    """Cancels a pending order on Kotak (e.g., SL when exiting via target).
    Returns True if successfully cancelled, False otherwise."""
    try:
        from core.kotak_client import get_client
        client = get_client()
        response = call_broker_api(client.cancel_order, order_id=order_id)
        if response is None:
            logger.warning("Order cancel returned no response: %s", order_id)
            return False
        
        if isinstance(response, dict):
            error_msg = response.get("error") or response.get("Error") or response.get("Error Message")
            if error_msg:
                logger.error("Order cancel failed for %s: %s", order_id, error_msg)
                return False
                
        logger.info(f"Order cancelled: {order_id}")
        return True
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.warning("Order cancel network error (%s): %s", order_id, e)
        return False
    except Exception as e:
        logger.warning("Order cancel failed (%s): %s", order_id, e)
        return False
