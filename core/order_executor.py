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
    get_today_gross_pnl,
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
)
from reflection.reflection_engine import record_trade

load_dotenv()
logger = logging.getLogger(__name__)


class OrderExecutionError(Exception):
    """Raised when a LIVE broker order fails — trips order circuit breaker."""

# ── Secrets / mode stay in .env ───────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")
INTRADAY_SQUAREOFF = dt_time(15, 15)
_capital_cache: float = 10000.0
_capital_last_update: float = 0.0
CAPITAL_CACHE_TTL = 300
_order_lock = threading.RLock()

# ── Squareoff flag — prevents repeated calls after 3:15 ──────
_squareoff_done = False

# ── Configuration validation ──────────────────────────────────
_config_validated = False
_config_warnings = []


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
    
    deployed_capital = safe_float(margin_status.get('deployed_in_positions'), 0.0)
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

def calculate_quantity(price: float, stop_loss: float, risk_pct: float = None) -> int:
    """
    🚀 REDESIGNED (2026-06-01 REVISED): Multi-constraint quantity model
    
    BINDING CONSTRAINTS (Three-constraint model):
    1. Risk constraint (max_loss / stop_dist)
    2. Allocation constraint (per_position_budget / price)
    3. Leverage constraint (total_buying_power limit)
    
    Capital constraint REMOVED to honor margin configuration intent.
    Over-leverage validation performed separately as safety net.
    
    Whichever constraint is tightest determines final quantity.
    All constraints are logged explicitly.
    
    Returns: int quantity (MIN of 3 binding constraints)
    """
    price = safe_float(price, 0.0)
    stop_loss = safe_float(stop_loss, 0.0)
    
    if price <= 0:
        logger.error("Quantity rejected: invalid price %r", price)
        return 0
    
    # ─────────────────────────────────────────────────────────
    # STEP 1: Get risk setting
    # ─────────────────────────────────────────────────────────
    if risk_pct is None:
        risk_pct = safe_float(cfg("risk", "max_risk_per_trade", 0.02), 0.02)
    risk_pct = max(0.0, min(1.0, safe_float(risk_pct, 0.02)))
    
    # ─────────────────────────────────────────────────────────
    # STEP 2: Calculate total buying power and per-position budget
    # ─────────────────────────────────────────────────────────
    from core.strategy import MAX_POSITIONS
    
    bp_info = calculate_total_buying_power()
    real_capital = bp_info['real_capital']
    total_buying_power = bp_info['total_buying_power']
    margin_leverage = bp_info['margin_leverage']
    allow_margin = bp_info['allow_margin']
    
    budget_info = calculate_per_position_budget(bp_info, MAX_POSITIONS)
    per_position_budget = budget_info['per_position_budget']
    portfolio_allocation_pct = budget_info['portfolio_allocation_pct']
    
    # ─────────────────────────────────────────────────────────
    # STEP 3: Get current deployment
    # ─────────────────────────────────────────────────────────
    margin_status = get_margin_status()
    deployed_now = safe_float(margin_status.get('deployed_in_positions'), 0.0)
    
    # ─────────────────────────────────────────────────────────
    # STEP 4: Analyze all constraints
    # ─────────────────────────────────────────────────────────
    constraint_analysis = analyze_quantity_constraints(
        price=price,
        stop_loss=stop_loss,
        risk_pct=risk_pct,
        real_capital=real_capital,
        per_position_budget=per_position_budget,
        deployed_now=deployed_now,
        total_buying_power=total_buying_power,
    )
    
    qty = max(0, safe_int(constraint_analysis['final_qty'], 0))
    limiting_constraint = constraint_analysis['limiting_constraint']
    
    # ─────────────────────────────────────────────────────────
    # STEP 5: Handle forced_buy (if configured)
    # ─────────────────────────────────────────────────────────
    forced_buy = cfg("risk", "forced_buy_margin", False)
    if qty <= 0 and forced_buy and allow_margin:
        # Try to buy at least something if allocation allows
        allocation_qty = int(constraint_analysis['allocation_qty'])
        if allocation_qty > 0:
            qty = allocation_qty
            limiting_constraint = "forced_buy_override"
            logger.warning(
                f"⚠️ FORCED BUY OVERRIDE | Risk says {int(constraint_analysis['risk_qty'])} shares, "
                f"but buying {qty} using allocation budget (margin-enabled override)"
            )
    
    # ─────────────────────────────────────────────────────────
    # STEP 6: Validation and logging
    # ─────────────────────────────────────────────────────────
    if qty <= 0:
        logger.error(
            f"❌ INSUFFICIENT BUYING POWER | Price: ₹{price} | SL: ₹{stop_loss} | "
            f"Real Capital: ₹{real_capital:.0f} | Total Buying Power: ₹{total_buying_power:.0f} | "
            f"Per-Position Budget: ₹{per_position_budget:.0f} | Already Deployed: ₹{deployed_now:.0f} | "
            f"Risk-based qty: {int(constraint_analysis['risk_qty'])} | "
            f"Allocation-based qty: {int(constraint_analysis['allocation_qty'])} | "
            f"Leverage limit qty: {int(constraint_analysis['leverage_qty'])}"
        )
        return 0
    
    capital_deployed_if_buy = qty * price
    
    # ─────────────────────────────────────────────────────────
    # STEP 7: Log comprehensive constraint analysis
    # ─────────────────────────────────────────────────────────
    margin_used = max(0, capital_deployed_if_buy - real_capital)
    
    constraint_labels = {
        'risk': 'Risk-limited',
        'allocation': 'Allocation-limited',
        'leverage': 'Leverage-limited',
        'capital': 'Capital-limited',
        'forced_buy_override': 'Forced-buy override',
        'insufficient_all': 'Insufficient (all constraints)',
        'unknown': 'Unknown constraint',
    }
    
    limiting_label = constraint_labels.get(limiting_constraint, 'Unknown')
    
    logger.info(
        f"✅ Quantity calculated | "
        f"Price: ₹{price} | Qty: {qty} | Deployed: ₹{capital_deployed_if_buy:.0f} | "
        f"Limiting: {limiting_label} | "
        f"Risk: {int(constraint_analysis['risk_qty'])}sh | "
        f"Alloc: {int(constraint_analysis['allocation_qty'])}sh | "
        f"Lever: {int(constraint_analysis['leverage_qty'])}sh | "
        f"Buying Power: ₹{total_buying_power:.0f} | "
        f"Margin Used: ₹{margin_used:.0f}"
    )
    
    return qty


def calculate_quantity_with_tranches(
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
        qty = calculate_quantity(price, stop_loss, risk_pct)
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


def calculate_stop_loss(price: float, direction: str = "BUY") -> float:
    price = safe_float(price, 0.0)
    pct = max(0.0001, min(0.20, safe_float(cfg("risk", "stop_loss_percent", 0.01), 0.01)))
    if direction == "BUY":
        return round(price * (1 - pct), 2)
    return round(price * (1 + pct), 2)


def calculate_target(entry: float, stop_loss: float) -> float:
    """Target = entry + (risk × RR ratio). Default 2:1."""
    entry = safe_float(entry, 0.0)
    stop_loss = safe_float(stop_loss, entry)
    risk   = abs(entry - stop_loss)
    rr     = max(0.1, min(10.0, safe_float(cfg("risk", "target_rr_ratio", 2.0), 2.0)))
    return round(entry + (risk * rr), 2)


def _market_protection_pct(price: float) -> float:
    if price < 100:
        return 0.02
    if price <= 500:
        return 0.01
    return 0.005


def _broker_safe_limit_price(ltp: float, transaction: str) -> float:
    """
    Convert a desired market-style order into a limit order inside Kotak's
    protection band. Retail algo market orders are not allowed.
    
    NSE requires prices to be multiples of 0.05. This function ensures compliance.
    """
    ltp = max(safe_float(ltp, 0.0), 0.05)
    pct = _market_protection_pct(ltp)
    
    if str(transaction).upper() == "B":
        adjusted = ltp * (1 + pct)
    else:
        adjusted = ltp * (1 - pct)
    
    # Round to nearest 0.05 multiple (NSE requirement)
    # e.g., 1198.97 → 1199.00, 1198.94 → 1198.95
    rounded = round(adjusted * 20) / 20
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

def place_buy_order(
    symbol:         str,
    trading_symbol: str,
    entry_price:    float,
    stop_loss:      float = None,
    strategy:       str   = "",
    confidence:     int   = 0,
    product:        str   = "MIS",
    risk_pct:       float = None,
) -> dict:
    """
    🔧 FIXED VERSION: Validates session before attempting order.
    Prevents cascading failures from bad tokens.
    """
    symbol = str(symbol or "").strip().upper()
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
                _place_buy_order_impl,
                symbol=symbol,
                trading_symbol=trading_symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                strategy=strategy,
                confidence=confidence_value,
                product=product,
                risk_pct=risk_pct,
                default={},
            )
    except OrderExecutionError as e:
        logger.error("❌ BUY blocked after failure: %s", e)
        return {}


def _place_buy_order_impl(
    symbol:         str,
    trading_symbol: str,
    entry_price:    float,
    stop_loss:      float = None,
    strategy:       str   = "",
    confidence:     int   = 0,
    product:        str   = "MIS",
    risk_pct:       float = None,
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
        stop_loss = calculate_stop_loss(entry_price, "BUY")
    else:
        stop_loss = safe_float(stop_loss, 0.0)
    if stop_loss <= 0 or stop_loss >= entry_price:
        logger.error("BUY rejected for %s: invalid stop loss %r", symbol, stop_loss)
        return {}

    quantity     = calculate_quantity(entry_price, stop_loss, risk_pct)
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
    margin_status = get_margin_status()
    capital_deployed_if_buy = quantity * entry_price
    deployed_now = safe_float(margin_status.get('deployed_in_positions'), 0.0)
    real_capital = safe_float(margin_status.get('real_capital'), 0.0)
    leverage = safe_float(margin_status.get('margin_leverage'), 1.0)
    total_would_deploy = deployed_now + capital_deployed_if_buy
    would_over_leverage = total_would_deploy > (real_capital * leverage)
    
    if would_over_leverage:
        logger.error(
            f"⚠️ OVER-LEVERAGE WARNING for {symbol} | "
            f"Current: ₹{margin_status['deployed_in_positions']:.0f} + "
            f"This order: ₹{capital_deployed_if_buy:.0f} = "
            f"₹{total_would_deploy:.0f} > "
            f"Max available: ₹{margin_status['real_capital'] * margin_status['margin_leverage']:.0f}"
        )
        return {}
    
    target_price = calculate_target(entry_price, stop_loss)

    trade = {
        "symbol":         symbol,
        "trading_symbol": trading_symbol,
        "quantity":       quantity,
        "entry_price":    entry_price,
        "stop_loss":      stop_loss,
        "target_price":   target_price,
        "strategy":       strategy,
        "confidence":     confidence,
        "product":        product,
        "order_id":       None,
        "sl_order_id":    None,
    }

    if TRADING_MODE == "PAPER":
        trade["order_id"]    = f"PAPER-{symbol}-{datetime.now().strftime('%H%M%S')}"
        trade["sl_order_id"] = f"PAPER-SL-{symbol}-{datetime.now().strftime('%H%M%S')}"
        logger.info(
            f"📋 [PAPER] BUY | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | Target: ₹{target_price}"
        )

    elif TRADING_MODE == "LIVE":
        # Step 1 — Place BUY order
        trade["order_id"] = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = "B",
            quantity       = quantity,
            price          = entry_price,
            order_type     = "L",
            product        = product,
        )
        if not trade["order_id"]:
            logger.error(f"❌ BUY order FAILED for {symbol}")
            raise OrderExecutionError(f"BUY order rejected for {symbol}")

        logger.info(f"✅ BUY order placed: {trade['order_id']} | Qty: {quantity}")
        
        record_order_sent(
            trade["order_id"],
            symbol,
            {"side": "BUY", "qty": quantity, "price": entry_price, "product": product},
        )
        if not wait_for_order_verification(trade["order_id"], timeout_sec=45):
            logger.error(
                f"❌ BUY not confirmed on broker for {symbol} — "
                f"not saving local position (order_id={trade['order_id']})"
            )
            raise OrderExecutionError(f"BUY not verified for {symbol}")

        logger.info(f"✅ BUY verified on broker | Symbol: {symbol} | Qty: {quantity} | Product: {product} | SL: ₹{stop_loss}")

        # ⚠️ DIAGNOSTIC: Check if BUY order is visible in positions (some exchanges need time)
        import time
        time.sleep(1)  # Small buffer for order settlement
        
        # Step 2 — Place SL-M SELL order on Kotak immediately
        # 🔧 NO PRE-VALIDATION: ensure_trade_token_on_client() already handles all token checks
        # Calling validate_and_fix_session_before_order() here causes DOUBLE token refresh!
        
        logger.info(f"🔄 About to call _send_kotak_sl_order for {symbol} | Qty: {quantity} | Trigger: ₹{stop_loss} | Symbol: {trading_symbol} | Product: {product}")
        try:
            trade["sl_order_id"] = _send_kotak_sl_order(
                trading_symbol = trading_symbol,
                quantity       = quantity,
                trigger_price  = stop_loss,
                product        = product,
            )
            logger.info(f"✅ _send_kotak_sl_order returned: {trade['sl_order_id']}")
        except Exception as e:
            logger.error(f"❌ _send_kotak_sl_order THREW EXCEPTION: {e}", exc_info=True)
            trade["sl_order_id"] = None
        
        if trade["sl_order_id"]:
            logger.info(
                f"🛡️ Kotak SL-M placed | {symbol} | "
                f"Qty: {quantity} | Trigger: ₹{stop_loss} | OrderID: {trade['sl_order_id']}"
            )
        else:
            logger.warning(
                f"⚠️ Kotak SL-M FAILED for {symbol}. "
                f"Software SL active but no broker-side protection!"
            )

        logger.info(
            f"✅ [LIVE] BUY | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | Target: ₹{target_price}"
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
) -> bool:
    """Place SELL — protected by order circuit breaker."""
    breaker = get_breaker("order")
    symbol = str(symbol or "").strip().upper()
    exit_price = safe_float(exit_price, 0.0)
    if breaker.is_open():
        logger.error("🔴 Order circuit OPEN — blocking SELL for %s", symbol)
        return False

    try:
        with _order_lock:
            return breaker.call(
                _place_sell_order_impl,
                symbol=symbol,
                exit_price=exit_price,
                reason=reason,
                product=product,
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

    quantity       = safe_int(position.get("quantity"), 0)
    if quantity <= 0:
        logger.error("SELL rejected for %s: invalid quantity %r", symbol, position.get("quantity"))
        return False
    # ← FIXED: use stored trading_symbol, not raw ticker
    trading_symbol = position.get("trading_symbol") or symbol
    success        = True

    if TRADING_MODE == "PAPER":
        logger.info(
            f"📋 [PAPER] SELL | {symbol} | Qty: {quantity} | "
            f"@ ₹{exit_price} | Reason: {reason}"
        )

    elif TRADING_MODE == "LIVE":
        # Cancel existing SL-M order first (if it exists)
        sl_order_id = position.get("kotak_sl_order_id")
        if sl_order_id and reason != "STOPLOSS":
            _cancel_kotak_order(sl_order_id)

        order_id = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = "S",
            quantity       = quantity,
            price          = _broker_safe_limit_price(exit_price, "S"),
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
        if not wait_for_order_verification(order_id, timeout_sec=45):
            logger.error(
                f"❌ SELL not confirmed on broker for {symbol} — "
                f"keeping local position open (order_id={order_id})"
            )
            raise OrderExecutionError(f"SELL not verified for {symbol}")

    if success:
        entry = safe_float(position.get("entry_price"), 0.0)
        pnl = (exit_price - entry) * quantity if entry else None
        close_position(symbol, exit_price, reason)
        
        # ── Record trade outcome for reflection statistics ────
        try:
            from datetime import time as dt_time
            
            strategy_context = position.get("strategy", "UNKNOWN")
            confidence = position.get("confidence", 0)  # Now contains adaptive-adjusted confidence from signal
            
            # Calculate drawdown if available (estimate from position tracking)
            max_price = safe_float(position.get("max_price_during_hold"), exit_price)
            drawdown = max(0, (max_price - exit_price) / exit_price * 100) if exit_price > 0 else 0
            
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
                exit_price=exit_price,
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
            alert_sell(symbol, quantity, exit_price, reason, pnl)
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

        if not current:
            continue

        # Use trailing SL if it's higher than original SL
        active_sl = max(
            trailing_sl or 0,
            stop_loss   or 0
        )

        if active_sl and current <= active_sl:
            sl_type = "TRAILING_SL" if (trailing_sl and trailing_sl > stop_loss) \
                      else "STOPLOSS"
            logger.warning(
                f"🔴 {sl_type} HIT | {symbol} | "
                f"₹{current} ≤ ₹{active_sl}"
            )
            place_sell_order(symbol, current, sl_type)


def check_profit_targets(live_prices: dict[str, float]):
    """Exits when price hits 2:1 target."""
    for position in get_open_positions():
        symbol  = position["symbol"]
        target  = safe_float(position.get("target_price"), 0.0)
        current = safe_float(live_prices.get(symbol), 0.0)

        if not current or not target:
            continue

        if current >= target:
            logger.info(
                f"🎯 TARGET HIT | {symbol} | "
                f"₹{current} ≥ Target ₹{target}"
            )
            place_sell_order(symbol, current, "TARGET")


def _get_available_capital(force_refresh: bool = False) -> float:
    global _capital_cache, _capital_last_update
    import time

    if TRADING_MODE == "PAPER":
        return _calculate_paper_capital_available()

    now = time.time()
    if not force_refresh and (now - _capital_last_update) < CAPITAL_CACHE_TTL:
        return _capital_cache   # cache se do

    try:
        from core.kotak_client import get_client
        client = get_client()
        limits = client.limits(segment="ALL", exchange="ALL", product="ALL")

        if isinstance(limits, dict) and limits.get("stCode") == 300015:
            return _capital_cache   # market closed — purana cache rakho

        available = (
            limits.get("Net")
            or limits.get("availablecash")
            or limits.get("data", {}).get("Net")
        )
        available_value = safe_float(available, 0.0)
        if available_value > 0:
            _capital_cache = available_value
            _capital_last_update = now
            return _capital_cache

    except Exception as e:
        logger.warning(f"Capital fetch failed: {e}. Using cache.")

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

    Returns: {
        'real_capital': float,                # ₹ in real account
        'margin_leverage': float,             # 2.0 = 2x, 3.0 = 3x, etc
        'total_available_with_margin': float, # Real capital × leverage
        'current_position_value': float,      # Current market value (not entry value)
        'unrealized_pnl': float,              # Open position gains/losses
        'effective_capital': float,           # Capital + unrealized + closed PnL
        'margin_used': float,                 # ₹ margin actually being used
        'margin_pct': float,                  # % of real capital (0-100+)
        'remaining_margin': float,            # ₹ margin left to deploy
        'is_over_leveraged': bool,            # margin_pct > 100%
    }
    """
    # For PAPER mode, use calculated available capital
    if TRADING_MODE == "PAPER":
        real_capital = safe_float(cfg("risk", "paper_capital", 10000), 10000.0)
    else:
        real_capital = safe_float(_get_available_capital(force_refresh=True), 0.0)

    allow_margin = cfg("risk", "allow_margin", False)
    margin_leverage = safe_float(cfg("risk", "margin_leverage", 2.0), 2.0)
    valuation = _current_position_valuation()
    current_position_value = valuation["current_position_value"]
    entry_position_value = valuation["entry_position_value"]
    unrealized_pnl = valuation["unrealized_pnl"]
    closed_pnl = get_today_gross_pnl()
    total_pnl = closed_pnl + unrealized_pnl
    effective_capital = real_capital + total_pnl

    if margin_leverage < 1.0:
        margin_leverage = 1.0
    elif margin_leverage > 5.0:
        margin_leverage = 5.0

    if not allow_margin:
        remaining_cash = max(0.0, real_capital - current_position_value)
        return {
            'real_capital': real_capital,
            'margin_leverage': 1.0,
            'total_available_with_margin': real_capital,
            'current_position_value': current_position_value,
            'deployed_in_positions': entry_position_value,
            'entry_position_value': entry_position_value,
            'unrealized_pnl': unrealized_pnl,
            'effective_capital': effective_capital,
            'margin_used': 0.0,
            'margin_pct': 0.0,
            'remaining_margin': remaining_cash,
            'is_over_leveraged': current_position_value > real_capital,
        }

    # Margin available = extra capital we can use beyond real capital
    margin_available = real_capital * (margin_leverage - 1.0)

    # Margin used = how much of the DEPLOYMENT exceeds effective capital
    # If deployment > effective capital, we're using margin
    margin_used = max(0, current_position_value - effective_capital)

    # Margin percentage = current leverage ratio as percentage
    # Example: if deployed ₹20,000 and real capital ₹10,000, leverage = 2.0x = 200%
    current_leverage = (current_position_value / real_capital) if real_capital > 0 else 0.0
    current_leverage = max(0.0, current_leverage)
    margin_pct = max(0.0, (current_leverage - 1.0) * 100)  # extra leverage beyond 1x

    remaining_margin = max(0, margin_available - margin_used)
    is_over_leveraged = margin_used > margin_available

    return {
        'real_capital': real_capital,
        'margin_leverage': margin_leverage,
        'total_available_with_margin': real_capital * margin_leverage,
        'current_position_value': current_position_value,
        'deployed_in_positions': entry_position_value,
        'entry_position_value': entry_position_value,
        'unrealized_pnl': unrealized_pnl,
        'effective_capital': effective_capital,
        'margin_used': margin_used,
        'margin_pct': margin_pct,
        'remaining_margin': remaining_margin,
        'is_over_leveraged': is_over_leveraged,
    }
def update_trailing_stop_losses(live_prices: dict[str, float]):
    """
    Moves SL up as price rises. Never moves SL down.
    In LIVE mode: modifies Kotak SL order too.
    """
    for position in get_open_positions():
        symbol      = position["symbol"]
        current     = safe_float(live_prices.get(symbol), 0.0)
        current_tsl = safe_float(position.get("trailing_sl") or position.get("stop_loss", 0), 0.0)

        if not current:
            continue

        tsl_pct = max(0.0001, min(0.20, safe_float(cfg("risk", "trailing_sl_percent", 0.008), 0.008)))
        new_tsl = round(current * (1 - tsl_pct), 2)

        if new_tsl > current_tsl:
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
                        position["quantity"]
                    )


def check_max_daily_loss() -> bool:
    """Daily loss check based on initial trading capital (not available after position deployment)."""
    gross_pnl = get_today_gross_pnl()
    
    # Use INITIAL capital for loss limit calculation, not available capital
    # (available capital gets reduced when positions are deployed, which incorrectly constrains the loss limit)
    if TRADING_MODE == "PAPER":
        initial_capital = safe_float(cfg("risk", "paper_capital", 100000), 100000.0)
    else:
        # In LIVE mode, use the real capital
        initial_capital = max(1.0, safe_float(_get_available_capital(force_refresh=True), 100000.0))
    
    max_daily_loss_pct = max(0.0, min(1.0, safe_float(cfg("risk", "max_daily_loss_percent", 0.05), 0.05)))
    max_daily_loss = -(initial_capital * max_daily_loss_pct)

    if gross_pnl <= max_daily_loss:
        logger.warning(
            f"🚨 MAX DAILY LOSS HIT | "
            f"P&L: ₹{gross_pnl:.2f} | Limit: ₹{max_daily_loss:.2f} | "
            f"No new trades today."
        )
        try:
            from core.circuit_breaker import halt_all_trading
            from core.alerts import alert_critical
            halt_all_trading(f"Max daily loss ₹{gross_pnl:.2f}")
            alert_critical(
                f"Max daily loss hit: ₹{gross_pnl:.2f} (limit ₹{max_daily_loss:.2f}). "
                f"New trades halted."
            )
        except Exception:
            pass
        return True
    return False
 

def squareoff_all_intraday(live_prices: dict[str, float]):
    """Force-closes all MIS positions at 3:15 PM. Runs only once."""
    global _squareoff_done

    if _squareoff_done:
        return

    if datetime.now().time() < INTRADAY_SQUAREOFF:
        return

    open_positions = get_open_positions()
    if not open_positions:
        _squareoff_done = True
        return

    logger.warning(
        f"⏰ 3:15 PM — Squaring off {len(open_positions)} position(s)."
    )

    for position in open_positions:
        symbol  = position["symbol"]
        current = live_prices.get(symbol, position["entry_price"])
        place_sell_order(symbol, current, "SQUAREOFF")

    _squareoff_done = True


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
    order_price = round(safe_float(price, 0.0), 2)
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
            logger.info(f"📋 RAW BROKER RESPONSE: {response}")
            
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
                logger.error(f"Network error on attempt 2: {e}. Aborting.")
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
) -> str | None:
    """
    Places a SL-M SELL order on Kotak.
    This is the broker-side protection —
    fires even if AlcoSoft is offline.
    """
    # STEP 0: Handle trading symbol
    # 🔥 FIX (2026-05-27): Kotak order API REQUIRES the -EQ suffix!
    order_symbol = trading_symbol  # ← KEEP -EQ suffix!
    trigger_price = safe_float(trigger_price, 0.0)
    quantity = safe_int(quantity, 0)
    if quantity <= 0 or trigger_price <= 0:
        logger.error(
            "Invalid SL order payload | symbol=%s qty=%r trigger=%r",
            order_symbol,
            quantity,
            trigger_price,
        )
        return None
    logger.debug(f"📋 SL-M order symbol: {order_symbol}")
    logger.info(f"🔄 [SL-M] Starting SL order placement | Symbol: {order_symbol} | Qty: {quantity} | Trigger: ₹{trigger_price}")
    
    stop_limit_price = _broker_safe_limit_price(trigger_price, "S")
    sl_kwargs = dict(
        exchange_segment   = "nse_cm",
        product            = product,
        price              = str(stop_limit_price),
        order_type         = "SL",
        quantity           = str(quantity),
        validity           = "DAY",
        trading_symbol     = order_symbol,  # 🔥 USE SYMBOL WITH -EQ SUFFIX
        transaction_type   = "S",
        amo                = "NO",
        disclosed_quantity = "0",
        market_protection  = "0",
        pf                 = "N",
        trigger_price      = str(round(trigger_price, 2)),
    )

    logger.info(f"📋 SL-M ORDER KWARGS (WILL SEND TO KOTAK):")
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
            logger.info(f"📋 RAW BROKER RESPONSE (SL-M): {response}")
            
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
) -> bool:
    """Modifies existing SL-M order when trailing SL moves up."""
    new_trigger = safe_float(new_trigger, 0.0)
    quantity = safe_int(quantity, 0)
    if quantity <= 0 or new_trigger <= 0:
        logger.error("Invalid SL modify payload | order_id=%s qty=%r trigger=%r", order_id, quantity, new_trigger)
        return False
    try:
        # 🔐 CRITICAL: Ensure Trade token is set before modifying order
        client = ensure_trade_token_on_client()
        
        stop_limit_price = _broker_safe_limit_price(new_trigger, "S")
        response = call_broker_api(
            client.modify_order,
            order_id           = order_id,
            price              = str(stop_limit_price),
            quantity           = str(quantity),
            disclosed_quantity = "0",
            trigger_price      = str(round(new_trigger, 2)),
            validity           = "DAY",
            order_type         = "SL",
        )
        if response is None or (isinstance(response, dict) and response.get("error")):
            logger.error("SL-M modify failed: %s", response)
            return False
        logger.info(f"SL-M modified | OrderID: {order_id} | New trigger: ₹{new_trigger}")
        return True
    except RuntimeError as e:
        logger.error(f"SL-M modify: Token guarantee failed: {e}")
        return False
    except Exception as e:
        logger.error("SL-M modify failed: %s", e, exc_info=True)
        return False


def _cancel_kotak_order(order_id: str):
    """Cancels a pending order on Kotak (e.g., SL-M when exiting via target)."""
    try:
        from core.kotak_client import get_client
        client = get_client()
        response = call_broker_api(client.cancel_order, order_id=order_id)
        if response is None:
            logger.warning("Order cancel returned no response: %s", order_id)
        else:
            logger.info(f"Order cancelled: {order_id}")
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.warning("Order cancel network error (%s): %s", order_id, e)
    except Exception as e:
        logger.warning("Order cancel failed (%s): %s", order_id, e)
