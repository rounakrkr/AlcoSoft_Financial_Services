# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/strategy.py
#
#   KEY CHANGE: run_strategy_loop() now accepts shutdown_event
#   (asyncio.Event) for cooperative clean shutdown.
#   This fixes: RuntimeError: Event loop stopped before Future completed.
# ============================================================

import asyncio
import logging
import os
import pandas as pd
import ta
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from core.data_fetcher import get_latest_tick, get_candle_history, has_enough_history
from core.order_executor import (
    place_buy_order, check_stop_losses,
    squareoff_all_intraday, calculate_stop_loss
)
from core.state_manager import load_briefing, get_open_positions

load_dotenv()
logger = logging.getLogger(__name__)

STRATEGY_TYPE        = os.getenv("STRATEGY_TYPE", "INTRADAY")
LOOP_INTERVAL        = 5
MAX_POSITIONS        = int(os.getenv("MAX_OPEN_POSITIONS", 2))
MIN_CONFIDENCE       = 70
MIN_STRATEGIES_AGREE = int(os.getenv("MIN_STRATEGIES_AGREE", 2))

MARKET_OPEN    = dt_time(9, 15)
MARKET_CLOSE   = dt_time(15, 30)
NO_NEW_TRADES  = dt_time(14, 30)


# ════════════════════════════════════════════════════════════
#   CANDLESTICK PATTERNS
# ════════════════════════════════════════════════════════════

def detect_hammer(df: pd.DataFrame) -> bool:
    row        = df.iloc[-1]
    body       = abs(row["close"] - row["open"])
    lower_wick = min(row["open"], row["close"]) - row["low"]
    upper_wick = row["high"] - max(row["open"], row["close"])
    if body == 0:
        return False
    return lower_wick >= (2 * body) and upper_wick <= (0.5 * body)


def detect_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (prev["close"] < prev["open"] and
            curr["close"] > curr["open"] and
            curr["open"] <= prev["close"] and
            curr["close"] >= prev["open"])


def detect_doji(df: pd.DataFrame) -> bool:
    row = df.iloc[-1]
    body = abs(row["close"] - row["open"])
    rng  = row["high"] - row["low"]
    if rng == 0:
        return False
    return (body / rng) < 0.1


# ════════════════════════════════════════════════════════════
#   STRATEGY LIBRARY
# ════════════════════════════════════════════════════════════

def strategy_rsi_macd(df: pd.DataFrame) -> dict:
    rsi_series  = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd_obj    = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    macd_line   = macd_obj.macd()
    macd_signal = macd_obj.macd_signal()
    rsi         = rsi_series.iloc[-1]
    macd_cross  = (macd_line.iloc[-2] < macd_signal.iloc[-2] and
                   macd_line.iloc[-1] > macd_signal.iloc[-1])
    return {"fired": rsi < 35 and macd_cross, "name": "RSI + MACD Momentum",
            "reason": f"RSI={rsi:.1f}, MACD_cross={macd_cross}"}


def strategy_hammer(df: pd.DataFrame) -> dict:
    rsi   = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    avg_v = df["volume"].rolling(20).mean()
    fired = (detect_hammer(df) and 25 < rsi.iloc[-1] < 50 and
             df["volume"].iloc[-1] > avg_v.iloc[-1])
    return {"fired": fired, "name": "Hammer Reversal",
            "reason": f"Hammer=True, RSI={rsi.iloc[-1]:.1f}"}


def strategy_bullish_engulfing(df: pd.DataFrame) -> dict:
    rsi    = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    engulf = detect_bullish_engulfing(df)
    return {"fired": engulf and rsi.iloc[-1] < 60, "name": "Bullish Engulfing",
            "reason": f"Engulfing={engulf}, RSI={rsi.iloc[-1]:.1f}"}


def strategy_ema_crossover(df: pd.DataFrame) -> dict:
    ema9  = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    ema50 = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    cross_up    = (ema9.iloc[-2] < ema21.iloc[-2] and ema9.iloc[-1] > ema21.iloc[-1])
    above_ema50 = df["close"].iloc[-1] > ema50.iloc[-1]
    return {"fired": cross_up and above_ema50, "name": "EMA 9/21 Crossover",
            "reason": f"EMA_cross={cross_up}, Above_EMA50={above_ema50}"}


def strategy_bollinger_bounce(df: pd.DataFrame) -> dict:
    bb      = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    bb_low  = bb.bollinger_lband()
    rsi     = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    touched = df["close"].iloc[-2] <= bb_low.iloc[-2]
    bounced = df["close"].iloc[-1] > bb_low.iloc[-1]
    return {"fired": touched and bounced and rsi.iloc[-1] < 45,
            "name": "Bollinger Band Bounce",
            "reason": f"Touched={touched}, Bounced={bounced}, RSI={rsi.iloc[-1]:.1f}"}


def strategy_volume_breakout(df: pd.DataFrame) -> dict:
    avg_v  = df["volume"].rolling(20).mean()
    sma20  = ta.trend.SMAIndicator(df["close"], window=20).sma_indicator()
    fired  = (df["volume"].iloc[-1] > avg_v.iloc[-1] * 2 and
              df["close"].iloc[-1] > df["high"].iloc[-2] and
              df["close"].iloc[-1] > sma20.iloc[-1])
    return {"fired": fired, "name": "Volume Breakout",
            "reason": "VolSpike=True, PriceBreak=True, AboveSMA=True"}


# ════════════════════════════════════════════════════════════
#   MASTER SIGNAL EVALUATOR
# ════════════════════════════════════════════════════════════

def _evaluate_signal(stock: dict, briefing: dict) -> dict:
    symbol = stock["ticker"]

    if not has_enough_history(symbol, min_candles=50):
        return {"action": "WAIT", "reason": "Building candle history..."}
    if stock.get("confidence", 0) < MIN_CONFIDENCE:
        return {"action": "WAIT", "reason": "War room confidence too low"}
    if (briefing.get("market_bias") != "BULLISH" or
            stock.get("direction") != "BUY_ONLY"):
        return {"action": "WAIT", "reason": "Market bias not bullish"}

    candles = get_candle_history(symbol)
    df = pd.DataFrame(candles).astype({
        "open": float, "high": float, "low": float,
        "close": float, "volume": float
    })

    all_strategies = [
        strategy_rsi_macd(df.copy()),
        strategy_hammer(df.copy()),
        strategy_bullish_engulfing(df.copy()),
        strategy_ema_crossover(df.copy()),
        strategy_bollinger_bounce(df.copy()),
        strategy_volume_breakout(df.copy()),
    ]

    fired       = [s for s in all_strategies if s["fired"]]
    fired_count = len(fired)
    fired_names = [s["name"] for s in fired]

    logger.info(f"{symbol} | Strategies: {fired_count}/6 | {fired_names}")

    if fired_count >= MIN_STRATEGIES_AGREE:
        price     = df.iloc[-1]["close"]
        stop_loss = calculate_stop_loss(price, "BUY")
        return {
            "action": "BUY", "symbol": symbol,
            "price": round(price, 2), "stop_loss": stop_loss,
            "reason": f"{fired_count} strategies: {', '.join(fired_names)}",
            "signals": fired_count,
        }

    return {"action": "WAIT",
            "reason": f"Only {fired_count}/6 fired. Need {MIN_STRATEGIES_AGREE}."}


# ════════════════════════════════════════════════════════════
#   MAIN LOOP — cooperative shutdown via asyncio.Event
# ════════════════════════════════════════════════════════════

async def run_strategy_loop(shutdown_event: asyncio.Event = None):
    """
    Main strategy loop.

    shutdown_event: set by main.py's signal handler.
    Loop exits cleanly after current iteration — no RuntimeError.
    Pass None for standalone testing (runs until KeyboardInterrupt).
    """
    logger.info(f"Strategy loop started | Mode: {STRATEGY_TYPE}")

    async def _sleep(seconds: float) -> bool:
        """Sleep for `seconds`. Returns True if shutdown was triggered."""
        if shutdown_event is None:
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
            return True   # Shutdown triggered during sleep
        except asyncio.TimeoutError:
            return False  # Normal timeout

    while True:
        if shutdown_event and shutdown_event.is_set():
            logger.info("Strategy loop: shutdown received. Exiting cleanly.")
            break

        try:
            now = datetime.now().time()

            if not (MARKET_OPEN <= now <= MARKET_CLOSE):
                if await _sleep(30):
                    break
                continue

            briefing = load_briefing()
            if not briefing:
                logger.warning("No briefing. Waiting for war room...")
                if await _sleep(LOOP_INTERVAL):
                    break
                continue

            live_prices = {
                s["ticker"]: t["ltp"]
                for s in briefing.get("approved_stocks", [])
                if (t := get_latest_tick(s["ticker"]))
            }

            check_stop_losses(live_prices)

            if STRATEGY_TYPE == "INTRADAY":
                squareoff_all_intraday(live_prices)

            if now >= NO_NEW_TRADES:
                if await _sleep(LOOP_INTERVAL):
                    break
                continue

            open_count = len(get_open_positions())
            if open_count >= MAX_POSITIONS:
                if await _sleep(LOOP_INTERVAL):
                    break
                continue

            for stock in briefing.get("approved_stocks", []):
                if stock.get("direction") == "AVOID":
                    continue
                if stock["ticker"] in [p["symbol"] for p in get_open_positions()]:
                    continue

                signal = _evaluate_signal(stock, briefing)

                if signal["action"] == "BUY":
                    product = "MIS" if STRATEGY_TYPE == "INTRADAY" else "CNC"
                    logger.info(
                        f"BUY SIGNAL | {signal['symbol']} | "
                        f"Rs.{signal['price']} | {signal['reason']}"
                    )
                    place_buy_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["reason"],
                        confidence     = stock.get("confidence", 0),
                        product        = product,
                    )
                    open_count += 1
                    if open_count >= MAX_POSITIONS:
                        break

        except Exception as e:
            logger.error(f"Strategy loop error: {e}", exc_info=True)

        if await _sleep(LOOP_INTERVAL):
            break