# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/order_executor.py — Order Placement & Management
#   PAPER mode = logs only. LIVE mode = real money.
#   The TRADING_MODE in .env is your safety switch.
# ============================================================

import logging
import os
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from core.kotak_client import get_client, force_reconnect
from core.state_manager import (
    save_open_position,
    close_position,
    get_open_positions,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
TRADING_MODE        = os.getenv("TRADING_MODE", "PAPER")
CAPITAL             = float(os.getenv("CAPITAL", 10000))
MAX_RISK_PER_TRADE  = float(os.getenv("MAX_RISK_PER_TRADE", 0.02))
INTRADAY_SQUAREOFF  = dt_time(15, 15)   # Force-close all MIS at 3:15 PM
STOP_LOSS_BUFFER    = 0.008             # 0.8% default stop loss


# ── Position Sizing ───────────────────────────────────────────
def calculate_quantity(price: float, stop_loss: float) -> int:
    """
    How many shares to buy based on risk management.

    Formula:
      max_loss     = capital × risk_percent (e.g. ₹200 on ₹10,000)
      stop_dist    = entry_price - stop_loss
      ideal_qty    = max_loss / stop_dist
      affordable   = (capital × 20%) / price   (max 20% in one trade)
      final        = min(ideal_qty, affordable), at least 1

    This scales automatically as capital grows.
    """
    max_loss   = CAPITAL * MAX_RISK_PER_TRADE
    stop_dist  = abs(price - stop_loss)

    if stop_dist == 0:
        stop_dist = price * STOP_LOSS_BUFFER

    ideal_qty      = max_loss / stop_dist
    affordable_qty = (CAPITAL * 0.20) / price
    quantity       = int(min(ideal_qty, affordable_qty))

    return max(1, quantity)   # Always at least 1 share


def calculate_stop_loss(entry_price: float, direction: str = "BUY") -> float:
    """Default stop loss: 0.8% below entry for BUY, above for SELL."""
    if direction == "BUY":
        return round(entry_price * (1 - STOP_LOSS_BUFFER), 2)
    return round(entry_price * (1 + STOP_LOSS_BUFFER), 2)


# ── Core Order Functions ──────────────────────────────────────
def place_buy_order(
    symbol:         str,
    trading_symbol: str,        # Kotak's exact trading symbol (from scrip master)
    entry_price:    float,
    stop_loss:      float = None,
    strategy:       str   = "",
    confidence:     int   = 0,
    product:        str   = "MIS",   # MIS=intraday, CNC=swing/delivery
) -> dict:
    """
    Places a BUY order.
    In PAPER mode: logs the trade, no real order sent.
    In LIVE mode: sends real order to Kotak.
    Returns trade dict with order_id.
    """
    if stop_loss is None:
        stop_loss = calculate_stop_loss(entry_price, "BUY")

    quantity = calculate_quantity(entry_price, stop_loss)

    trade = {
        "symbol":         symbol,
        "trading_symbol": trading_symbol,
        "quantity":       quantity,
        "entry_price":    entry_price,
        "stop_loss":      stop_loss,
        "strategy":       strategy,
        "confidence":     confidence,
        "product":        product,
        "order_id":       None,
    }

    if TRADING_MODE == "PAPER":
        trade["order_id"] = f"PAPER-{symbol}-{datetime.now().strftime('%H%M%S')}"
        logger.info(
            f"📋 [PAPER] BUY | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | Strategy: {strategy}"
        )

    elif TRADING_MODE == "LIVE":
        trade["order_id"] = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = "B",
            quantity       = quantity,
            price          = entry_price,
            product        = product,
        )
        if not trade["order_id"]:
            logger.error(f"❌ BUY order FAILED for {symbol}")
            return {}

        logger.info(
            f"✅ [LIVE] BUY | {symbol} | Qty: {quantity} | "
            f"@ ₹{entry_price} | SL: ₹{stop_loss} | OrderID: {trade['order_id']}"
        )

    # Save to DB + positions.json regardless of mode
    save_open_position(trade)
    return trade


def place_sell_order(
    symbol:         str,
    trading_symbol: str,
    exit_price:     float,
    reason:         str = "SIGNAL",
    product:        str = "MIS",
) -> bool:
    """
    Places a SELL order to exit an open position.
    Reason can be: SIGNAL, STOPLOSS, SQUAREOFF, MANUAL
    """
    # Find quantity from open positions
    open_positions = get_open_positions()
    position = next((p for p in open_positions if p["symbol"] == symbol), None)

    if not position:
        logger.warning(f"No open position found for {symbol} — skipping sell.")
        return False

    quantity = position["quantity"]
    success  = True

    if TRADING_MODE == "PAPER":
        logger.info(
            f"📋 [PAPER] SELL | {symbol} | Qty: {quantity} | "
            f"@ ₹{exit_price} | Reason: {reason}"
        )

    elif TRADING_MODE == "LIVE":
        order_id = _send_kotak_order(
            trading_symbol = trading_symbol,
            transaction    = "S",
            quantity       = quantity,
            price          = exit_price,
            product        = product,
        )
        if not order_id:
            logger.error(f"❌ SELL order FAILED for {symbol}")
            success = False

    # Update DB + positions.json
    if success:
        close_position(symbol, exit_price, reason)

    return success


# ── Stop Loss Monitor ─────────────────────────────────────────
def check_stop_losses(live_prices: dict[str, float]):
    """
    Called by strategy.py on every tick.
    live_prices = {"RELIANCE": 2445.0, "TCS": 3890.0, ...}
    If any position hits its stop loss → sell immediately.
    """
    open_positions = get_open_positions()

    for position in open_positions:
        symbol     = position["symbol"]
        stop_loss  = position.get("stop_loss")
        current    = live_prices.get(symbol)

        if not current or not stop_loss:
            continue

        if current <= stop_loss:
            logger.warning(
                f"🔴 STOP LOSS HIT | {symbol} | "
                f"Price: ₹{current} ≤ SL: ₹{stop_loss}"
            )
            place_sell_order(
                symbol         = symbol,
                trading_symbol = position.get("symbol"),  # fallback
                exit_price     = current,
                reason         = "STOPLOSS",
                product        = "MIS",
            )


# ── Intraday Square-Off ───────────────────────────────────────
def squareoff_all_intraday(live_prices: dict[str, float]):
    """
    Force-closes all MIS (intraday) positions at 3:15 PM.
    Called by strategy.py's time check every cycle.
    Never hold intraday positions overnight — that's a rule.
    """
    now = datetime.now().time()

    if now < INTRADAY_SQUAREOFF:
        return   # Not time yet

    open_positions = get_open_positions()
    intraday_open  = [p for p in open_positions if p.get("trading_mode") == "MIS"
                      or p.get("status") == "OPEN"]

    if not intraday_open:
        return

    logger.warning(f"⏰ 3:15 PM — Force squaring off {len(intraday_open)} position(s).")

    for position in intraday_open:
        symbol  = position["symbol"]
        price   = live_prices.get(symbol, position["entry_price"])

        place_sell_order(
            symbol         = symbol,
            trading_symbol = symbol,
            exit_price     = price,
            reason         = "SQUAREOFF",
            product        = "MIS",
        )


# ── Kotak API Order Sender (Internal) ────────────────────────
def _send_kotak_order(
    trading_symbol: str,
    transaction:    str,     # "B" or "S"
    quantity:       int,
    price:          float,
    product:        str = "MIS",
) -> str | None:
    """
    Sends the actual order to Kotak Neo.
    Returns order_id string on success, None on failure.
    Retries once with a fresh session on auth errors.
    """
    for attempt in range(2):   # Max 2 attempts
        try:
            client   = get_client() if attempt == 0 else force_reconnect()
            response = client.place_order(
                exchange_segment  = "nse_cm",
                product           = product,
                price             = str(round(price, 2)),
                order_type        = "L",          # Limit order
                quantity          = str(quantity),
                validity          = "DAY",
                trading_symbol    = trading_symbol,
                transaction_type  = transaction,
                amo               = "NO",
                disclosed_quantity= "0",
                market_protection = "0",
                pf                = "N",
                trigger_price     = "0",
            )

            # Extract order_id from response
            if response and isinstance(response, dict):
                order_id = (
                    response.get("nOrdNo")
                    or response.get("order_id")
                    or response.get("id")
                )
                if order_id:
                    return str(order_id)

            logger.error(f"Unexpected Kotak response: {response}")
            return None

        except Exception as e:
            if attempt == 0:
                logger.warning(f"Order attempt 1 failed ({e}). Retrying with fresh session...")
            else:
                logger.error(f"Order failed after retry: {e}")
                return None

    return None


# ── Portfolio Snapshot ────────────────────────────────────────
def get_portfolio_snapshot() -> dict:
    """
    Fetches live holdings + limits from Kotak.
    Used by dashboard and reflection loop.
    """
    if TRADING_MODE == "PAPER":
        open_pos = get_open_positions()
        return {
            "mode":          "PAPER",
            "open_positions": open_pos,
            "count":          len(open_pos),
        }

    try:
        client   = get_client()
        limits   = client.limits(segment="ALL", exchange="ALL", product="ALL")
        holdings = client.holdings()
        return {
            "mode":     "LIVE",
            "limits":   limits,
            "holdings": holdings,
        }
    except Exception as e:
        logger.error(f"Portfolio snapshot failed: {e}")
        return {}