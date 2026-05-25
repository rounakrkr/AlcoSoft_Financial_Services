# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/strategy.py — The Fast Math Loop
#
#   BUY STRATEGIES (6):
#     RSI+MACD, Hammer, Bullish Engulfing,
#     EMA Crossover, Bollinger Bounce, Volume Breakout
#
#   SELL STRATEGIES (4):
#     RSI Overbought, MACD Bearish, Bearish Engulfing,
#     EMA Breakdown
#
#   All strategies support multi-candle lookback window.
#   A signal is valid if it fired in ANY of the last N candles.
#
#   EXIT PRIORITY ORDER:
#     1. Stop Loss (hard floor)
#     2. Trailing SL update
#     3. Profit Target (2:1 RR)
#     4. Sell Signals (technical reversal)
#     5. War Room Flip (AI says exit)
#     6. 3:15 PM Squareoff (intraday only)
# ============================================================

import asyncio
import logging
import os
import pandas as pd
import ta
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from core.data_fetcher import (
    get_latest_tick,
    get_candle_history,
    has_enough_history,
)
from core.order_executor import (
    place_buy_order,
    place_sell_order,
    check_stop_losses,
    check_profit_targets,
    check_war_room_flip,
    update_trailing_stop_losses,
    squareoff_all_intraday,
    check_max_daily_loss,
    calculate_stop_loss,
)
from core.state_manager import load_briefing, get_open_positions

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
STRATEGY_TYPE        = os.getenv("STRATEGY_TYPE", "INTRADAY")
LOOP_INTERVAL        = 5
MAX_POSITIONS        = int(os.getenv("MAX_OPEN_POSITIONS", 2))
MIN_CONFIDENCE       = 70
MIN_STRATEGIES_AGREE = int(os.getenv("MIN_STRATEGIES_AGREE", 2))
MIN_SELL_SIGNALS     = int(os.getenv("MIN_SELL_SIGNALS", 1))
LOOKBACK             = int(os.getenv("SIGNAL_LOOKBACK_CANDLES", 3))

# WAR_ROOM_GATING=true  → strategy only trades war-room-approved stocks (production)
# WAR_ROOM_GATING=false → strategy scans all stocks, ignores war room direction (testing)
WAR_ROOM_GATING = os.getenv("WAR_ROOM_GATING", "true").lower() == "true"

# ── Market Hours ──────────────────────────────────────────────
MARKET_OPEN   = dt_time(9, 15)
MARKET_CLOSE  = dt_time(15, 30)
NO_NEW_TRADES = dt_time(14, 30)


# ════════════════════════════════════════════════════════════
#   CANDLESTICK PATTERN HELPERS
#   Called per-candle, not per-series.
#   Pass a DataFrame slice ending at the candle you want.
# ════════════════════════════════════════════════════════════

def detect_hammer(df: pd.DataFrame) -> bool:
    """
    Small body at top + long lower wick.
    Lower wick >= 2× body. Upper wick tiny.
    Bullish reversal — sellers tried, buyers won.
    """
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    body       = abs(r["close"] - r["open"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    upper_wick = r["high"] - max(r["open"], r["close"])
    if body == 0:
        return False
    return lower_wick >= (2 * body) and upper_wick <= (0.5 * body)


def detect_bullish_engulfing(df: pd.DataFrame) -> bool:
    """
    Current green candle fully engulfs previous red candle.
    One of the strongest reversal signals.
    """
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev["close"] < prev["open"] and   # prev bearish
        curr["close"] > curr["open"] and   # curr bullish
        curr["open"]  <= prev["close"] and  # opens inside prev body
        curr["close"] >= prev["open"]       # closes above prev body
    )


def detect_bearish_engulfing(df: pd.DataFrame) -> bool:
    """
    Current red candle fully engulfs previous green candle.
    Strong reversal sell signal.
    """
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev["close"] > prev["open"] and   # prev bullish
        curr["close"] < curr["open"] and   # curr bearish
        curr["open"]  >= prev["close"] and  # opens above prev close
        curr["close"] <= prev["open"]       # closes below prev open
    )


def detect_shooting_star(df: pd.DataFrame) -> bool:
    """
    Small body at bottom + long upper wick.
    Opposite of hammer. Bearish reversal signal.
    """
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    body       = abs(r["close"] - r["open"])
    upper_wick = r["high"] - max(r["open"], r["close"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    if body == 0:
        return False
    return upper_wick >= (2 * body) and lower_wick <= (0.5 * body)


# ════════════════════════════════════════════════════════════
#   SHARED INDICATOR BUILDER
#   Build once per symbol per loop. All strategies read from it.
# ════════════════════════════════════════════════════════════

def _build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds all indicator columns to DataFrame.
    Called once — all strategies share same df.
    """
    df = df.copy()
    df["rsi"]        = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    df["avg_vol"]    = df["volume"].rolling(20).mean()

    macd_obj         = ta.trend.MACD(df["close"], window_slow=26,
                                      window_fast=12, window_sign=9)
    df["macd"]       = macd_obj.macd()
    df["macd_sig"]   = macd_obj.macd_signal()

    df["ema9"]       = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema21"]      = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema50"]      = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema20"]      = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["sma20"]      = ta.trend.SMAIndicator(df["close"], window=20).sma_indicator()

    bb               = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_lower"]   = bb.bollinger_lband()
    df["bb_upper"]   = bb.bollinger_hband()

    return df


def _lookback_range(df: pd.DataFrame, n: int) -> range:
    """
    Returns range of negative indices for lookback.
    e.g. n=3 → range(-3, 0) → checks iloc[-3], iloc[-2], iloc[-1]
    Guards against asking for more candles than we have.
    """
    available = min(n, len(df) - 1)
    return range(-available, 0)


# ════════════════════════════════════════════════════════════
#   BUY STRATEGIES
#   Each returns {"fired": bool, "name": str, "reason": str}
#   "fired" = True if signal was present in any of last LOOKBACK candles
# ════════════════════════════════════════════════════════════

def strategy_rsi_macd(df: pd.DataFrame) -> dict:
    """
    RSI oversold (< 35) in last N candles
    AND MACD bullish crossover in last N candles.
    Classic momentum reversal.
    """
    # RSI: was it oversold in any recent candle?
    rsi_ok = (df["rsi"].iloc[-LOOKBACK:] < 35).any()

    # MACD cross: did bullish cross happen in last N candles?
    macd_cross = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["macd"].iloc[i - 1] < df["macd_sig"].iloc[i - 1] and
                df["macd"].iloc[i] > df["macd_sig"].iloc[i]):
            macd_cross = True
            break

    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  rsi_ok and macd_cross,
        "name":   "RSI + MACD Momentum",
        "reason": f"RSI_recent={latest_rsi}, MACD_cross={macd_cross}",
    }


def strategy_hammer(df: pd.DataFrame) -> dict:
    """
    Hammer candle in last N candles
    + RSI between 25-50 (oversold zone, not extreme)
    + Volume above average (confirms conviction).
    """
    # Hammer in any recent candle?
    hammer_found = False
    for i in _lookback_range(df, LOOKBACK):
        slice_df = df.iloc[: len(df) + i + 1]
        if detect_hammer(slice_df):
            hammer_found = True
            break

    # RSI in the right zone in any recent candle?
    rsi_zone = ((df["rsi"].iloc[-LOOKBACK:] > 25) &
                (df["rsi"].iloc[-LOOKBACK:] < 50)).any()

    # Volume spike in any recent candle?
    vol_ok = (df["volume"].iloc[-LOOKBACK:] >
              df["avg_vol"].iloc[-LOOKBACK:]).any()

    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  hammer_found and rsi_zone and vol_ok,
        "name":   "Hammer Reversal",
        "reason": f"Hammer={hammer_found}, RSI={latest_rsi}, Vol={vol_ok}",
    }


def strategy_bullish_engulfing(df: pd.DataFrame) -> dict:
    """
    Bullish engulfing candle in last N candles
    + RSI not overbought (< 60).
    """
    engulf_found = False
    for i in _lookback_range(df, LOOKBACK):
        slice_df = df.iloc[: len(df) + i + 1]
        if detect_bullish_engulfing(slice_df):
            engulf_found = True
            break

    rsi_ok     = df["rsi"].iloc[-1] < 60
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  engulf_found and rsi_ok,
        "name":   "Bullish Engulfing",
        "reason": f"Engulfing={engulf_found}, RSI={latest_rsi}",
    }


def strategy_ema_crossover(df: pd.DataFrame) -> dict:
    """
    EMA 9 crossed above EMA 21 in last N candles
    AND price currently above EMA 50 (overall uptrend).
    """
    if pd.isna(df["ema50"].iloc[-1]):
        return {"fired": False, "name": "EMA 9/21 Crossover", "reason": "EMA50 not ready"}
    
    cross_up = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["ema9"].iloc[i - 1] < df["ema21"].iloc[i - 1] and
                df["ema9"].iloc[i] > df["ema21"].iloc[i]):
            cross_up = True
            break

    above_ema50 = df["close"].iloc[-1] > df["ema50"].iloc[-1]
    return {
        "fired":  cross_up and above_ema50,
        "name":   "EMA 9/21 Crossover",
        "reason": f"Cross={cross_up}, Above_EMA50={above_ema50}",
    }


def strategy_bollinger_bounce(df: pd.DataFrame) -> dict:
    """
    Price touched lower Bollinger Band in last N candles
    AND currently closed back above it (bounce confirmed)
    AND RSI < 45.
    """
    # Did price touch/break lower band in recent candles?
    touched = (df["close"].iloc[-LOOKBACK:] <=
               df["bb_lower"].iloc[-LOOKBACK:]).any()

    # Currently bounced above lower band?
    bounced = df["close"].iloc[-1] > df["bb_lower"].iloc[-1]

    rsi_ok     = df["rsi"].iloc[-1] < 45
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  touched and bounced and rsi_ok,
        "name":   "Bollinger Band Bounce",
        "reason": f"Touched={touched}, Bounced={bounced}, RSI={latest_rsi}",
    }


def strategy_volume_breakout(df: pd.DataFrame) -> dict:
    """
    Volume was 2× average in last N candles (institutional activity)
    AND price broke above previous candle's high
    AND above SMA20 (not in downtrend).
    """
    # Volume spike in recent candles?
    avg_vol   = df["avg_vol"].iloc[-LOOKBACK:]
    vol_spike = (df["volume"].iloc[-LOOKBACK:] > avg_vol * 2).any()

    # Price breakout in recent candles?
    price_brk = False
    for i in _lookback_range(df, LOOKBACK):
        if df["close"].iloc[i] > df["high"].iloc[i - 1]:
            price_brk = True
            break

    above_sma = df["close"].iloc[-1] > df["sma20"].iloc[-1]
    return {
        "fired":  vol_spike and price_brk and above_sma,
        "name":   "Volume Breakout",
        "reason": f"VolSpike={vol_spike}, PriceBreak={price_brk}, SMA={above_sma}",
    }


# ════════════════════════════════════════════════════════════
#   SELL STRATEGIES
#   Same lookback logic, but detecting REVERSAL signals.
#   Called only for OPEN POSITIONS — not for fresh entries.
# ════════════════════════════════════════════════════════════

def strategy_sell_rsi_overbought(df: pd.DataFrame) -> dict:
    """
    RSI crossed above 70 (overbought) in last N candles.
    Price has likely run too far — expect pullback.
    """
    overbought = (df["rsi"].iloc[-LOOKBACK:] > 70).any()
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  overbought,
        "name":   "RSI Overbought",
        "reason": f"RSI={latest_rsi} crossed above 70",
    }


def strategy_sell_macd_bearish(df: pd.DataFrame) -> dict:
    """
    MACD line crossed below signal line in last N candles.
    Momentum has shifted bearish.
    """
    bearish_cross = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["macd"].iloc[i - 1] > df["macd_sig"].iloc[i - 1] and
                df["macd"].iloc[i] < df["macd_sig"].iloc[i]):
            bearish_cross = True
            break

    return {
        "fired":  bearish_cross,
        "name":   "MACD Bearish Cross",
        "reason": f"MACD crossed below signal in last {LOOKBACK} candles",
    }


def strategy_sell_bearish_engulfing(df: pd.DataFrame) -> dict:
    """
    Bearish engulfing OR shooting star in last N candles.
    Strong reversal — sellers took control.
    """
    pattern_found = False
    pattern_name  = ""

    for i in _lookback_range(df, LOOKBACK):
        slice_df = df.iloc[: len(df) + i + 1]
        if detect_bearish_engulfing(slice_df):
            pattern_found = True
            pattern_name  = "BearishEngulfing"
            break
        if detect_shooting_star(slice_df):
            pattern_found = True
            pattern_name  = "ShootingStar"
            break

    return {
        "fired":  pattern_found,
        "name":   "Bearish Pattern",
        "reason": f"Pattern={pattern_name}",
    }


def strategy_sell_ema_breakdown(df: pd.DataFrame) -> dict:
    """
    Price crossed BELOW EMA 20 in last N candles.
    Short-term trend has turned bearish.
    """
    breakdown = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["close"].iloc[i - 1] > df["ema20"].iloc[i - 1] and
                df["close"].iloc[i] < df["ema20"].iloc[i]):
            breakdown = True
            break

    return {
        "fired":  breakdown,
        "name":   "EMA20 Breakdown",
        "reason": f"Price broke below EMA20 in last {LOOKBACK} candles",
    }


# ════════════════════════════════════════════════════════════
#   BUY SIGNAL EVALUATOR
# ════════════════════════════════════════════════════════════

def _evaluate_buy_signal(stock: dict, briefing: dict) -> dict:
    """
    Runs all 6 buy strategies on the stock.
    BUY fires only when MIN_STRATEGIES_AGREE or more say fired=True.

    Gates (all must pass before strategies even run):
      - Enough candle history (50 candles)
      - War room confidence >= MIN_CONFIDENCE
      - Market bias is BULLISH
      - Stock direction is BUY_ONLY
    """
    symbol = stock["ticker"]

    if not has_enough_history(symbol, min_candles=26):
        return {"action": "WAIT", "reason": "Building history..."}

    if WAR_ROOM_GATING:
        # Production: war room must approve the stock
        if stock.get("direction") not in ("BUY_ONLY",):
            return {"action": "WAIT", "reason": f"War room direction: {stock.get('direction', 'unknown')}"}

        if stock.get("confidence", 0) < MIN_CONFIDENCE:
            return {"action": "WAIT", "reason": f"War room confidence too low ({stock.get('confidence')}%)"}

        if briefing.get("market_bias") not in ("BULLISH", "NEUTRAL"):
            return {"action": "WAIT", "reason": "Market bias not BULLISH/NEUTRAL"}
    else:
        # Testing: only hard-block explicit AVOID, let everything else through
        if stock.get("direction") == "AVOID":
            return {"action": "WAIT", "reason": "Stock explicitly marked AVOID"}

        if briefing.get("market_bias") == "BEARISH":
            return {"action": "WAIT", "reason": "Market bias BEARISH — skipping even in test mode"}

    # Build DataFrame with all indicators
    candles = get_candle_history(symbol)
    if len(candles) < 26:
        return {"action": "WAIT", "reason": "Insufficient candles"}

    df = pd.DataFrame(candles).astype({
        "open": float, "high": float,
        "low":  float, "close": float, "volume": float
    })
    df = _build_indicators(df)

    # Run all buy strategies
    all_strategies = [
        strategy_rsi_macd(df),
        strategy_hammer(df),
        strategy_bullish_engulfing(df),
        strategy_ema_crossover(df),
        strategy_bollinger_bounce(df),
        strategy_volume_breakout(df),
    ]

    fired       = [s for s in all_strategies if s["fired"]]
    fired_count = len(fired)
    fired_names = [s["name"] for s in fired]

    logger.info(
        f"📊 {symbol} | Buy signals: {fired_count}/6 | "
        f"{fired_names if fired_names else 'None'}"
    )

    if fired_count >= MIN_STRATEGIES_AGREE:
        price     = df["close"].iloc[-1]
        stop_loss = calculate_stop_loss(price, "BUY")
        return {
            "action":    "BUY",
            "symbol":    symbol,
            "price":     round(price, 2),
            "stop_loss": stop_loss,
            "reason":    f"{fired_count}/6 strategies: {', '.join(fired_names)}",
            "signals":   fired_count,
        }

    return {
        "action": "WAIT",
        "reason": f"Only {fired_count}/6 strategies fired. Need {MIN_STRATEGIES_AGREE}.",
    }


# ════════════════════════════════════════════════════════════
#   SELL SIGNAL CHECKER — For Open Positions
# ════════════════════════════════════════════════════════════

def _check_sell_signals(live_prices: dict[str, float]):
    """
    For each open position, runs all 4 sell strategies.
    Exits if MIN_SELL_SIGNALS or more agree.

    Called on every loop tick — fast because strategies
    share one pre-built indicator DataFrame.
    """
    for position in get_open_positions():
        symbol  = position["symbol"]
        current = live_prices.get(symbol)

        if not current:
            continue

        if not has_enough_history(symbol, min_candles=26):
            continue

        candles = get_candle_history(symbol)
        if len(candles) < 26:
            continue

        df = pd.DataFrame(candles).astype({
            "open": float, "high": float,
            "low":  float, "close": float, "volume": float
        })
        df = _build_indicators(df)

        sell_strategies = [
            strategy_sell_rsi_overbought(df),
            strategy_sell_macd_bearish(df),
            strategy_sell_bearish_engulfing(df),
            strategy_sell_ema_breakdown(df),
        ]

        fired       = [s for s in sell_strategies if s["fired"]]
        fired_count = len(fired)
        fired_names = [s["name"] for s in fired]

        logger.debug(
            f"📉 {symbol} | Sell signals: {fired_count}/4 | "
            f"{fired_names if fired_names else 'None'}"
        )

        if fired_count >= MIN_SELL_SIGNALS:
            logger.info(
                f"🔴 SELL SIGNAL | {symbol} | ₹{current} | "
                f"{fired_count}/4: {', '.join(fired_names)}"
            )
            place_sell_order(symbol, current, "SELL_SIGNAL")


# ════════════════════════════════════════════════════════════
#   ALL EXITS — Coordinated, Priority-Ordered
# ════════════════════════════════════════════════════════════

def _check_all_exits(live_prices: dict[str, float]):
    """
    Runs all exit checks in priority order on every tick.

    Priority:
    1. Hard SL (software, includes current trailing SL value)
    2. Trailing SL — update value upward as price rises
    3. Profit target hit
    4. Technical sell signals
    5. War room flip (AVOID or BEARISH bias)
    6. 3:15 PM squareoff (intraday only)

    Each check re-reads open_positions from DB,
    so if a position was closed by step 1,
    steps 2-6 will naturally skip it.
    """

    # 1. Hard SL — highest priority
    check_stop_losses(live_prices)

    # 2. Update trailing SL value (not an exit — just moves SL up)
    update_trailing_stop_losses(live_prices)

    # 3. Profit target
    check_profit_targets(live_prices)

    # 4. Technical sell signals
    _check_sell_signals(live_prices)

    # 5. War room flip
    check_war_room_flip(live_prices)

    # 6. Intraday squareoff
    if STRATEGY_TYPE == "INTRADAY":
        squareoff_all_intraday(live_prices)


# ════════════════════════════════════════════════════════════
#   HELPERS
# ════════════════════════════════════════════════════════════

def _is_market_open(now: dt_time) -> bool:
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _get_live_prices(briefing: dict) -> dict[str, float]:
    """Builds {symbol: ltp} dict from latest WebSocket ticks."""
    prices = {}
    for stock in briefing.get("approved_stocks", []):
        symbol = stock["ticker"]
        tick   = get_latest_tick(symbol)
        if tick:
            prices[symbol] = tick["ltp"]

    # Also include open positions not in briefing
    # (e.g., stocks removed from watchlist mid-session)
    for position in get_open_positions():
        symbol = position["symbol"]
        if symbol not in prices:
            tick = get_latest_tick(symbol)
            if tick:
                prices[symbol] = tick["ltp"]

    return prices


def _can_open_new_position(stock: dict) -> bool:
    """Returns True if we can enter a new trade for this stock."""
    if stock.get("direction") == "AVOID":
        return False

    open_symbols = [p["symbol"] for p in get_open_positions()]
    if stock["ticker"] in open_symbols:
        return False  # Already holding this stock

    return True


# ════════════════════════════════════════════════════════════
#   MAIN ASYNC LOOP
# ════════════════════════════════════════════════════════════

async def run_strategy_loop(shutdown_event: asyncio.Event):
    """
    Runs every LOOP_INTERVAL seconds during market hours.

    Every tick:
      1. Load briefing (war room updates it every 30 min)
      2. Build live prices from WebSocket ticks
      3. Run ALL exits (priority-ordered)
      4. Check daily loss limit
      5. Look for new BUY signals on approved stocks
    """
    logger.info(
        f"⚡ Strategy loop started | "
        f"Mode: {STRATEGY_TYPE} | "
        f"Lookback: {LOOKBACK} candles | "
        f"Buy: {MIN_STRATEGIES_AGREE}/6 | "
        f"Sell: {MIN_SELL_SIGNALS}/4"
    )

    while not shutdown_event.is_set():   # ← while True ki jagah yeh
        try:
            now = datetime.now().time()
            if not _is_market_open(now):
                await asyncio.sleep(30)
                continue

            briefing = load_briefing()
            if not briefing:
                logger.warning("No briefing. Waiting for war room...")
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            live_prices = _get_live_prices(briefing)
            _check_all_exits(live_prices)

            if now >= NO_NEW_TRADES:
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            if check_max_daily_loss():
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            open_count = len(get_open_positions())
            if open_count >= MAX_POSITIONS:
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            for stock in briefing.get("approved_stocks", []):
                if not _can_open_new_position(stock):
                    continue
                signal = _evaluate_buy_signal(stock, briefing)
                if signal["action"] == "BUY":
                    # ... place_buy_order ...
                    open_count += 1
                    if open_count >= MAX_POSITIONS:
                        break

        except Exception as e:
            logger.error(f"Strategy loop error: {e}", exc_info=True)

        await asyncio.sleep(LOOP_INTERVAL)

    logger.info("Strategy loop stopped.")   # optional


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("AlcoSoft Strategy Module — Config Check")
    print(f"  Lookback window:     {LOOKBACK} candles")
    print(f"  Buy threshold:       {MIN_STRATEGIES_AGREE}/6 strategies")
    print(f"  Sell threshold:      {MIN_SELL_SIGNALS}/4 strategies")
    print(f"  Max positions:       {MAX_POSITIONS}")
    print(f"  Min war room conf:   {MIN_CONFIDENCE}%")
    print(f"  Strategy type:       {STRATEGY_TYPE}")
    print()
    print("Buy strategies:")
    buys = [
        "RSI + MACD Momentum",
        "Hammer Reversal",
        "Bullish Engulfing",
        "EMA 9/21 Crossover",
        "Bollinger Band Bounce",
        "Volume Breakout",
    ]
    for i, s in enumerate(buys, 1):
        print(f"  {i}. {s}")
    print()
    print("Sell strategies:")
    sells = [
        "RSI Overbought (>70)",
        "MACD Bearish Cross",
        "Bearish Engulfing / Shooting Star",
        "EMA20 Breakdown",
    ]
    for i, s in enumerate(sells, 1):
        print(f"  {i}. {s}")
    print()
    print("Exit priority order:")
    exits = [
        "Hard Stop Loss (+ Trailing SL)",
        "Trailing SL value update",
        "Profit Target (2:1 RR)",
        "Technical Sell Signals",
        "War Room AVOID / BEARISH bias",
        "3:15 PM Intraday Squareoff",
    ]
    for i, e in enumerate(exits, 1):
        print(f"  {i}. {e}")
    print()
    print("✅ Strategy module loaded successfully.")