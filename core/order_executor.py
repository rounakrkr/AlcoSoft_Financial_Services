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
from core.state_manager import (
    save_open_position, close_position, get_open_positions,
    update_trailing_sl, update_sl_order_id,
    get_today_gross_pnl, load_briefing,
)
from core.audit_logger import (
    audit_order_placed, audit_position_closed, audit_system_error,
)
from core.trading_settings import get as cfg

load_dotenv()
logger = logging.getLogger(__name__)

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
    risk_pct:       float = None,    # <-- add
) -> dict:

    if stop_loss is None:
        stop_loss = calculate_stop_loss(entry_price, "BUY")

    quantity     = calculate_quantity(entry_price, stop_loss, risk_pct)   # <-- pass risk_pct
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
            return {}

        # Step 2 — Place SL-M SELL order on Kotak immediately
        # This protects position even if laptop goes offline
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
        
        # Audit logging
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
    return trade


# ════════════════════════════════════════════════════════════
#   SELL ORDER
# ════════════════════════════════════════════════════════════

def place_sell_order(
    symbol:   str,
    exit_price: float,
    reason:   str = "SIGNAL",
    product:  str = "MIS",
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
            success = False

    if success:
        close_position(symbol, exit_price, reason)
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

def _send_kotak_order(
    trading_symbol: str,
    transaction:    str,
    quantity:       int,
    price:          float,
    order_type:     str = "L",
    product:        str = "MIS",
) -> str | None:

    for attempt in range(2):
        try:
            client   = get_client() if attempt == 0 else force_reconnect()
            response = client.place_order(
                exchange_segment   = "nse_cm",
                product            = product,
                price              = str(round(price, 2)),
                order_type         = order_type,
                quantity           = str(quantity),
                validity           = "DAY",
                trading_symbol     = trading_symbol,
                transaction_type   = transaction,
                amo                = "NO",
                disclosed_quantity = "0",
                market_protection  = "0",
                pf                 = "N",
                trigger_price      = "0",
            )
            if response and isinstance(response, dict):
                order_id = (
                    response.get("nOrdNo") or
                    response.get("order_id") or
                    response.get("id")
                )
                if order_id:
                    return str(order_id)
            logger.error(f"Unexpected Kotak response: {response}")
            return None

        except Exception as e:
            if attempt == 0:
                logger.warning(f"Order attempt 1 failed: {e}. Retrying...")
            else:
                logger.error(f"Order failed after retry: {e}")
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
    for attempt in range(2):
        try:
            client   = get_client() if attempt == 0 else force_reconnect()
            response = client.place_order(
                exchange_segment   = "nse_cm",
                product            = product,
                price              = "0",
                order_type         = "SL-M",
                quantity           = str(quantity),
                validity           = "DAY",
                trading_symbol     = trading_symbol,
                transaction_type   = "S",
                amo                = "NO",
                disclosed_quantity = "0",
                market_protection  = "0",
                pf                 = "N",
                trigger_price      = str(round(trigger_price, 2)),
            )
            if response and isinstance(response, dict):
                order_id = (
                    response.get("nOrdNo") or
                    response.get("order_id") or
                    response.get("id")
                )
                if order_id:
                    return str(order_id)
            return None

        except Exception as e:
            if attempt == 0:
                logger.warning(f"SL-M attempt 1 failed: {e}. Retrying...")
            else:
                logger.error(f"SL-M order failed: {e}")
                return None


def _modify_sl_order(
    order_id:      str,
    new_trigger:   float,
    quantity:      int,
) -> bool:
    """Modifies existing SL-M order when trailing SL moves up."""
    try:
        client   = get_client()
        response = client.modify_order(
            order_id          = order_id,
            price             = "0",
            quantity          = str(quantity),
            disclosed_quantity= "0",
            trigger_price     = str(round(new_trigger, 2)),
            validity          = "DAY",
            order_type        = "SL-M",
        )
        logger.info(f"SL-M modified | OrderID: {order_id} | New trigger: ₹{new_trigger}")
        return True
    except Exception as e:
        logger.error(f"SL-M modify failed: {e}")
        return False


def _cancel_kotak_order(order_id: str):
    """Cancels a pending order on Kotak (e.g., SL-M when exiting via target)."""
    try:
        client = get_client()
        client.cancel_order(order_id=order_id)
        logger.info(f"Order cancelled: {order_id}")
    except Exception as e:
        logger.warning(f"Order cancel failed (may already be filled): {e}")