# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/order_executor.py — Order Placement & Management
#   Changes: SL-M order on Kotak after BUY, trading_symbol fix,
#   profit targets, war room flip exit, trailing SL,
#   max daily loss check, squareoff flag
# ============================================================

import logging
import os
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from core.kotak_client import get_client, force_reconnect
from core.token_validator import (
    validate_and_fix_session_before_order,
    ensure_trade_token_on_client,
    JWTTokenValidator,
    diagnose_token_health,
)
from core.state_manager import (
    save_open_position, close_position, get_open_positions,
    update_trailing_sl, update_sl_order_id,
    get_today_gross_pnl, load_briefing,
)
from core.audit_logger import (
    audit_order_placed, audit_position_closed, audit_system_error,
)
from core.trading_settings import get as cfg
from core.circuit_breaker import get_breaker
from core.api_resilience import call_broker_api
from core.order_verifier import (
    record_order_sent,
    wait_for_order_verification,
)

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

# ── Squareoff flag — prevents repeated calls after 3:15 ──────
_squareoff_done = False


# ════════════════════════════════════════════════════════════
#   POSITION SIZING
# ════════════════════════════════════════════════════════════

def calculate_quantity(price: float, stop_loss: float, risk_pct: float = None) -> int:
    capital   = _get_available_capital()
    if risk_pct is None:
        risk_pct = float(cfg("risk", "max_risk_per_trade", 0.02))
    max_loss  = capital * risk_pct
    stop_dist = abs(price - stop_loss)
    if stop_dist == 0:
        stop_dist = price * float(cfg("risk", "stop_loss_percent", 0.01)) if price > 0 else 1.0
        if stop_dist == 0:
            stop_dist = 1.0
        logger.warning(f"stop_dist was 0 for {price=}, {stop_loss=}, using {stop_dist}")
    ideal_qty      = max_loss / stop_dist
    affordable_qty = (capital * 0.20) / price
    return max(1, int(min(ideal_qty, affordable_qty)))


def calculate_stop_loss(price: float, direction: str = "BUY") -> float:
    pct = float(cfg("risk", "stop_loss_percent", 0.01))
    if direction == "BUY":
        return round(price * (1 - pct), 2)
    return round(price * (1 + pct), 2)


def calculate_target(entry: float, stop_loss: float) -> float:
    """Target = entry + (risk × RR ratio). Default 2:1."""
    risk   = abs(entry - stop_loss)
    rr     = float(cfg("risk", "target_rr_ratio", 2.0))
    return round(entry + (risk * rr), 2)


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
    breaker = get_breaker("order")
    if breaker.is_open():
        logger.error("🔴 Order circuit OPEN — blocking BUY for %s", symbol)
        return {}

    if not validate_and_fix_session_before_order():
        logger.error(
            f"❌ BUY BLOCKED: Session not ready for {symbol} "
            f"(token invalid or couldn't refresh)"
        )
        breaker._on_failure()
        return {}

    try:
        return breaker.call(
            _place_buy_order_impl,
            symbol=symbol,
            trading_symbol=trading_symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            strategy=strategy,
            confidence=confidence,
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

    if stop_loss is None:
        stop_loss = calculate_stop_loss(entry_price, "BUY")

    quantity     = calculate_quantity(entry_price, stop_loss, risk_pct)
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

        # Step 2 — Place SL-M SELL order on Kotak immediately
        # 🔧 NEW: Re-validate session before SL placement (extra safety)
        if not validate_and_fix_session_before_order():
            logger.warning(
                f"⚠️ Session degraded after BUY. SL-M may fail. "
                f"Will still attempt."
            )
        
        trade["sl_order_id"] = _send_kotak_sl_order(
            trading_symbol = trading_symbol,
            quantity       = quantity,
            trigger_price  = stop_loss,
            product        = product,
        )
        if trade["sl_order_id"]:
            logger.info(
                f"🛡️ Kotak SL-M placed | {symbol} | "
                f"Trigger: ₹{stop_loss} | OrderID: {trade['sl_order_id']}"
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
    if breaker.is_open():
        logger.error("🔴 Order circuit OPEN — blocking SELL for %s", symbol)
        return False

    try:
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

    open_positions = get_open_positions()
    position = next((p for p in open_positions if p["symbol"] == symbol), None)

    if not position:
        logger.warning(f"No open position for {symbol}")
        return False

    quantity       = position["quantity"]
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
            price          = exit_price,
            order_type     = "MKT",   # Market sell for speed
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
        entry = float(position.get("entry_price") or 0)
        pnl = (exit_price - entry) * quantity if entry else None
        close_position(symbol, exit_price, reason)
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
        trailing_sl = position.get("trailing_sl")
        stop_loss   = position.get("stop_loss")
        current     = live_prices.get(symbol)

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
        target  = position.get("target_price")
        current = live_prices.get(symbol)

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
        return float(cfg("risk", "paper_capital", 10000))

    now = time.time()
    if not force_refresh and (now - _capital_last_update) < CAPITAL_CACHE_TTL:
        return _capital_cache   # cache se do

    try:
        client = get_client()
        limits = client.limits(segment="ALL", exchange="ALL", product="ALL")

        if isinstance(limits, dict) and limits.get("stCode") == 300015:
            return _capital_cache   # market closed — purana cache rakho

        available = (
            limits.get("Net")
            or limits.get("availablecash")
            or limits.get("data", {}).get("Net")
        )
        if available and float(available) > 0:
            _capital_cache = float(available)
            _capital_last_update = now
            return _capital_cache

    except Exception as e:
        logger.warning(f"Capital fetch failed: {e}. Using cache.")

    return _capital_cache


def check_war_room_flip(live_prices: dict[str, float]):
    """
    Exits positions when war room changes its mind.
    Triggered if:
      - Stock direction → AVOID in latest briefing
      - Market bias → BEARISH
    """
    briefing = load_briefing()
    if not briefing:
        return

    avoid_list  = briefing.get("avoid_list", [])
    market_bias = briefing.get("market_bias", "NEUTRAL")

    for position in get_open_positions():
        symbol  = position["symbol"]
        current = live_prices.get(symbol, position["entry_price"])

        if symbol in avoid_list:
            logger.warning(f"⚔️ WAR ROOM FLIP | {symbol} → AVOID → Exiting")
            place_sell_order(symbol, current, "WAR_ROOM_FLIP")

        elif market_bias == "BEARISH":
            logger.warning(f"🐻 BEARISH BIAS | {symbol} → Exiting")
            place_sell_order(symbol, current, "BEARISH_BIAS")


def update_trailing_stop_losses(live_prices: dict[str, float]):
    """
    Moves SL up as price rises. Never moves SL down.
    In LIVE mode: modifies Kotak SL order too.
    """
    for position in get_open_positions():
        symbol      = position["symbol"]
        current     = live_prices.get(symbol)
        current_tsl = position.get("trailing_sl") or position.get("stop_loss", 0)

        if not current:
            continue

        tsl_pct = float(cfg("risk", "trailing_sl_percent", 0.008))
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
    """Daily loss check based on actual available capital."""
    gross_pnl      = get_today_gross_pnl()
    # Use dynamic capital (live balance) for limit calculation
    live_capital = _get_available_capital()
    max_daily_loss = -(live_capital * float(cfg("risk", "max_daily_loss_percent", 0.05)))

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
    if response and isinstance(response, dict):
        if response.get("error") or response.get("Error") or response.get("Error Message"):
            logger.error("Kotak order error response: %s", response)
            return None
        order_id = (
            response.get("nOrdNo")
            or response.get("order_id")
            or response.get("id")
        )
        if order_id:
            return str(order_id)
    logger.error("Unexpected Kotak response: %s", response)
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

    # STEP 2: Build Order Payload
    order_kwargs = dict(
        exchange_segment   = "nse_cm",  # ✅ Valid: "nse_cm" is accepted by NeoAPI 2.x
        product            = product,
        price              = str(round(price, 2)),
        order_type         = order_type,
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
    logger.debug(f"📋 SL-M order symbol: {order_symbol}")
    
    sl_kwargs = dict(
        exchange_segment   = "nse_cm",
        product            = product,
        price              = "0",
        order_type         = "SL-M",
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

    # ✅ FIXED: Use sl_kwargs (not order_kwargs!)
    for attempt in range(2):
        try:
            # 🔐 CRITICAL: Ensure Trade token is set on client before order
            try:
                if attempt == 0:
                    client = ensure_trade_token_on_client()
                else:
                    logger.warning(f"[SL-M Attempt 2/2] Auth error detected — forcing fresh session with Trade token...")
                    from core.kotak_client import force_reconnect as _force_reconnect
                    _force_reconnect()
                    client = ensure_trade_token_on_client()
            except RuntimeError as e:
                if attempt == 0:
                    logger.warning(f"🔐 SL-M: Token guarantee failed on attempt 1: {e} — will retry...")
                    continue
                else:
                    logger.error(f"🔐 SL-M: Token guarantee failed on attempt 2: {e} — aborting")
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
            order_id = _parse_order_response(response)
            if order_id:
                return order_id
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
    try:
        # 🔐 CRITICAL: Ensure Trade token is set before modifying order
        client = ensure_trade_token_on_client()
        
        response = call_broker_api(
            client.modify_order,
            order_id           = order_id,
            price              = "0",
            quantity           = str(quantity),
            disclosed_quantity = "0",
            trigger_price      = str(round(new_trigger, 2)),
            validity           = "DAY",
            order_type         = "SL-M",
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