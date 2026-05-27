# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/strategy.py — The Fast Math Loop
#
#   BUY STRATEGIES:
#     Indicators: RSI+MACD, EMA Crossover, Bollinger Bounce
#     Candle patterns: Hammer, Engulfing, Inverted Hammer, Morning Star,
#     Piercing Line, Bullish Harami, Three White Soldiers, Dragonfly Doji,
#     Bullish Marubozu, Volume Breakout
#
#   BUY GATE: At least one candle pattern (pattern_hit) is ALWAYS required.
#     min=1 → candle pattern only (indicators alone cannot buy)
#     min=2 → candle pattern + total fired >= 2 (2nd can be candle or indicator)
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
import time
import yfinance as yf
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
from core.trading_settings import get as cfg, get_section

load_dotenv()
logger = logging.getLogger(__name__)

# ── Runtime config (from config/trading_settings.json, hot-reload) ──
STRATEGY_TYPE         = "INTRADAY"
LOOP_INTERVAL         = 5
MAX_POSITIONS         = 2
MIN_CONFIDENCE        = 70
MIN_STRATEGIES_AGREE  = 2
MATH_STRATEGIES_AGREE = 2
MATH_RISK_PER_TRADE   = 0.05
MIN_SELL_SIGNALS      = 1
LOOKBACK              = 3
MIN_WS_CANDLES_FOR_PATTERNS = 2
SCAN_LOG_INTERVAL     = 90
WAR_ROOM_GATING       = True

_failed_order_cooldown: dict[str, float] = {}
_last_scan_log: float = 0.0
FAILED_ORDER_COOLDOWN_SEC = 300
_yfinance_cache: dict[str, list] = {}


def _apply_trading_settings():
    """Reload tunables from config/trading_settings.json (dashboard-editable)."""
    global STRATEGY_TYPE, LOOP_INTERVAL, MAX_POSITIONS, MIN_CONFIDENCE
    global MIN_STRATEGIES_AGREE, MATH_STRATEGIES_AGREE, MATH_RISK_PER_TRADE
    global MIN_SELL_SIGNALS, LOOKBACK, MIN_WS_CANDLES_FOR_PATTERNS
    global SCAN_LOG_INTERVAL, WAR_ROOM_GATING

    s = get_section("strategy")
    md = get_section("market_data")
    risk = get_section("risk")

    STRATEGY_TYPE         = s.get("strategy_type", "INTRADAY")
    LOOP_INTERVAL         = int(s.get("loop_interval_sec", 5))
    MAX_POSITIONS         = int(s.get("max_open_positions", 2))
    MIN_CONFIDENCE        = int(s.get("min_confidence", 70))
    MIN_STRATEGIES_AGREE  = int(s.get("min_strategies_agree", 2))
    MATH_STRATEGIES_AGREE = int(s.get("math_strategies_agree", 2))
    MIN_SELL_SIGNALS      = int(s.get("min_sell_signals", 1))
    LOOKBACK              = int(s.get("signal_lookback_candles", 3))
    MIN_WS_CANDLES_FOR_PATTERNS = int(s.get("min_ws_candles_for_patterns", 2))
    WAR_ROOM_GATING       = bool(s.get("war_room_gating", True))
    MATH_RISK_PER_TRADE   = float(risk.get("math_risk_per_trade", 0.05))
    SCAN_LOG_INTERVAL     = int(md.get("scan_log_interval_sec", 90))


_apply_trading_settings()

# ── Market Hours ──────────────────────────────────────────────
MARKET_OPEN   = dt_time(9, 15)
MARKET_CLOSE  = dt_time(15, 30)
NO_NEW_TRADES = dt_time(15, 00)  #########


# ════════════════════════════════════════════════════════════
#   YFINANCE CANDLE FALLBACK
#   Used when WebSocket hasn't built enough candle history yet.
#   Fetches last 5 days of 5-min candles from Yahoo Finance.
# ════════════════════════════════════════════════════════════

def _get_candles_with_fallback(symbol: str) -> list[dict]:
    candles = get_candle_history(symbol)

    if len(candles) >= 26:
        _yfinance_cache.pop(symbol, None)  # WebSocket ready — cache clear
        return candles

    # Cache mein hai toh wahi use karo — yfinance dobara call nahi hoga
    if symbol in _yfinance_cache:
        merged = _yfinance_cache[symbol] + candles
        return merged

    # Pehli baar fetch karo
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period="5d", interval="5m")
        hist = _drop_incomplete_candle_if_present(hist)
        if not hist.empty:
            yf_candles = [
                {
                    "open":   float(row["Open"]),
                    "high":   float(row["High"]),
                    "low":    float(row["Low"]),
                    "close":  float(row["Close"]),
                    "volume": float(row["Volume"]),
                }
                for _, row in hist.iterrows()
            ]
            _yfinance_cache[symbol] = yf_candles
            logger.info(
                f"📦 {symbol} — yfinance fallback: "
                f"{len(yf_candles)} candles + "
                f"{len(candles)} WebSocket = {len(yf_candles) + len(candles)} total"
            )
            return yf_candles + candles
    except Exception as e:
        logger.warning(f"yfinance fallback failed for {symbol}: {e}")

    return candles


def _get_indicator_df(symbol: str) -> pd.DataFrame | None:
    """RSI / MACD / EMA / Bollinger — seeded from yfinance, updated by WebSocket."""
    candles = _get_candles_with_fallback(symbol)
    if len(candles) < 26:
        return None
    df = pd.DataFrame(candles).astype({
        "open": float, "high": float,
        "low":  float, "close": float, "volume": float,
    })
    return _build_indicators(df)


def _get_pattern_df(symbol: str) -> pd.DataFrame | None:
    """Candlestick patterns — WebSocket-built candles only (real-time)."""
    ws_candles = get_candle_history(symbol)
    if len(ws_candles) < MIN_WS_CANDLES_FOR_PATTERNS:
        return None
    return pd.DataFrame(ws_candles).astype({
        "open": float, "high": float,
        "low":  float, "close": float, "volume": float,
    })


def _get_entry_price(symbol: str) -> tuple[float | None, str]:
    """
    Entry/exit price for broker orders — WebSocket LTP only.
    No yfinance fallback: wrong price → wrong order to Kotak.
    """
    tick = get_latest_tick(symbol)
    if tick and tick.get("ltp", 0) > 0:
        return float(tick["ltp"]), "websocket"
    return None, "none"


def _waiting_pattern_result(name: str, ws_count: int) -> dict:
    return {
        "fired":  False,
        "name":   name,
        "reason": f"Need {MIN_WS_CANDLES_FOR_PATTERNS} live WS candles (have {ws_count})",
    }


def _log_full_scan(briefing: dict):
    """Log every watchlist + war room symbol so logs don't look like only 3-4 stocks exist."""
    from core.data_fetcher import get_feed_stats

    stats = get_feed_stats()
    tick_counts = stats.get("tick_counts", {})
    lines = [f"─── Scan summary ({len(stats.get('subscribed', []))} subscribed) ───"]

    for label, stocks in (
        ("War Room", briefing.get("approved_stocks", [])),
        ("Math", briefing.get("watchlist", [])),
    ):
        for stock in stocks:
            sym = stock["ticker"]
            ticks = tick_counts.get(sym, 0)
            ws_n  = len(get_candle_history(sym))
            lines.append(f"  [{label}] {sym}: ticks={ticks} | live_candles={ws_n}")

    logger.info("\n".join(lines))


# ════════════════════════════════════════════════════════════
#   CANDLESTICK PATTERN HELPERS
# ════════════════════════════════════════════════════════════

def detect_hammer(df: pd.DataFrame) -> bool:
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
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev["close"] < prev["open"] and
        curr["close"] > curr["open"] and
        curr["open"]  <= prev["close"] and
        curr["close"] >= prev["open"]
    )


def detect_bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev["close"] > prev["open"] and
        curr["close"] < curr["open"] and
        curr["open"]  >= prev["close"] and
        curr["close"] <= prev["open"]
    )


def detect_shooting_star(df: pd.DataFrame) -> bool:
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    body       = abs(r["close"] - r["open"])
    upper_wick = r["high"] - max(r["open"], r["close"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    if body == 0:
        return False
    return upper_wick >= (2 * body) and lower_wick <= (0.5 * body)


def detect_inverted_hammer(df: pd.DataFrame) -> bool:
    """Long upper shadow, small lower shadow — bullish reversal signal."""
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    body       = abs(r["close"] - r["open"])
    upper_wick = r["high"] - max(r["open"], r["close"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    if body == 0:
        return False
    return upper_wick >= (2 * body) and lower_wick <= (0.5 * body)


def detect_dragonfly_doji(df: pd.DataFrame) -> bool:
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    rng = r["high"] - r["low"]
    if rng == 0:
        return False
    body = abs(r["close"] - r["open"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    upper_wick = r["high"] - max(r["open"], r["close"])
    return body <= 0.1 * rng and lower_wick >= 0.6 * rng and upper_wick <= 0.1 * rng


def detect_bullish_marubozu(df: pd.DataFrame) -> bool:
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    if r["close"] <= r["open"]:
        return False
    rng = r["high"] - r["low"]
    if rng == 0:
        return False
    body = r["close"] - r["open"]
    upper_wick = r["high"] - r["close"]
    lower_wick = r["open"] - r["low"]
    return body >= 0.85 * rng and upper_wick <= 0.1 * rng and lower_wick <= 0.1 * rng


def detect_piercing_line(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev["close"] >= prev["open"]:
        return False
    if curr["close"] <= curr["open"]:
        return False
    midpoint = (prev["open"] + prev["close"]) / 2
    return curr["open"] < prev["low"] and curr["close"] > midpoint


def detect_bullish_harami(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev["close"] >= prev["open"]:
        return False
    if curr["close"] <= curr["open"]:
        return False
    return (
        curr["open"] > prev["close"]
        and curr["close"] < prev["open"]
        and abs(curr["close"] - curr["open"]) < abs(prev["close"] - prev["open"]) * 0.6
    )


def detect_morning_star(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    if c1["close"] >= c1["open"]:
        return False
    if c3["close"] <= c3["open"]:
        return False
    star_body = abs(c2["close"] - c2["open"])
    c1_body = abs(c1["close"] - c1["open"])
    if c1_body == 0:
        return False
    gap_down = c2["high"] < c1["close"]
    closes_into = c3["close"] > (c1["open"] + c1["close"]) / 2
    return gap_down and star_body < c1_body * 0.4 and closes_into


def detect_three_white_soldiers(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    greens = all(c["close"] > c["open"] for c in (c1, c2, c3))
    rising = c2["close"] > c1["close"] and c3["close"] > c2["close"]
    return greens and rising


def _scan_pattern_in_lookback(
    pattern_df: pd.DataFrame,
    detect_fn,
    lookback: int | None = None,
) -> bool:
    """True if detect_fn matches any candle in the lookback window."""
    n = lookback if lookback is not None else LOOKBACK
    for i in _lookback_range(pattern_df, n):
        slice_df = pattern_df.iloc[: len(pattern_df) + i + 1]
        if detect_fn(slice_df):
            return True
    return False


def _candle_strategy_result(
    name: str,
    pattern_hit: bool,
    fired: bool | None = None,
    reason: str = "",
) -> dict:
    """Standard candle-strategy payload for BUY gating."""
    return {
        "kind":        "candle",
        "pattern_hit": pattern_hit,
        "fired":       pattern_hit if fired is None else fired,
        "name":        name,
        "reason":      reason or f"pattern={pattern_hit}",
    }


def _indicator_strategy_result(name: str, fired: bool, reason: str) -> dict:
    return {"kind": "indicator", "pattern_hit": False, "fired": fired, "name": name, "reason": reason}


# ════════════════════════════════════════════════════════════
#   SHARED INDICATOR BUILDER
# ════════════════════════════════════════════════════════════

def _build_indicators(df: pd.DataFrame) -> pd.DataFrame:
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
    available = min(n, len(df) - 1)
    return range(-available, 0)


# ════════════════════════════════════════════════════════════
#   BUY STRATEGIES
# ════════════════════════════════════════════════════════════

def strategy_rsi_macd(df: pd.DataFrame) -> dict:
    rsi_ok = (df["rsi"].iloc[-LOOKBACK:] < 35).any()

    macd_cross = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["macd"].iloc[i - 1] < df["macd_sig"].iloc[i - 1] and
                df["macd"].iloc[i] > df["macd_sig"].iloc[i]):
            macd_cross = True
            break

    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return _indicator_strategy_result(
        "RSI + MACD Momentum",
        rsi_ok and macd_cross,
        f"RSI_recent={latest_rsi}, MACD_cross={macd_cross}",
    )


def strategy_hammer(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    """Pattern on live WS candles; RSI/volume filter from yfinance indicator_df."""
    ind = indicator_df if indicator_df is not None else pattern_df
    hammer_found = False
    for i in _lookback_range(pattern_df, LOOKBACK):
        slice_df = pattern_df.iloc[: len(pattern_df) + i + 1]
        if detect_hammer(slice_df):
            hammer_found = True
            break

    if "rsi" in ind.columns:
        rsi_zone = ((ind["rsi"].iloc[-LOOKBACK:] > 25) &
                    (ind["rsi"].iloc[-LOOKBACK:] < 50)).any()
        latest_rsi = round(ind["rsi"].iloc[-1], 1)
    else:
        rsi_zone = True
        latest_rsi = 0

    if "avg_vol" in ind.columns:
        vol_ok = (ind["volume"].iloc[-LOOKBACK:] > ind["avg_vol"].iloc[-LOOKBACK:]).any()
    else:
        vol_ok = True

    fired = hammer_found and rsi_zone and vol_ok
    return _candle_strategy_result(
        "Hammer Reversal",
        pattern_hit=hammer_found,
        fired=fired,
        reason=f"Hammer={hammer_found}, RSI={latest_rsi}, Vol={vol_ok}",
    )


def strategy_bullish_engulfing(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    ind = indicator_df if indicator_df is not None else pattern_df
    engulf_found = False
    for i in _lookback_range(pattern_df, LOOKBACK):
        slice_df = pattern_df.iloc[: len(pattern_df) + i + 1]
        if detect_bullish_engulfing(slice_df):
            engulf_found = True
            break

    rsi_ok = ind["rsi"].iloc[-1] < 60 if "rsi" in ind.columns else True
    latest_rsi = round(ind["rsi"].iloc[-1], 1) if "rsi" in ind.columns else 0
    return _candle_strategy_result(
        "Bullish Engulfing",
        pattern_hit=engulf_found,
        fired=engulf_found and rsi_ok,
        reason=f"Engulfing={engulf_found}, RSI={latest_rsi}",
    )


def strategy_ema_crossover(df: pd.DataFrame) -> dict:
    if pd.isna(df["ema50"].iloc[-1]):
        return _indicator_strategy_result("EMA 9/21 Crossover", False, "EMA50 not ready")

    cross_up = False
    for i in _lookback_range(df, LOOKBACK):
        if (df["ema9"].iloc[i - 1] < df["ema21"].iloc[i - 1] and
                df["ema9"].iloc[i] > df["ema21"].iloc[i]):
            cross_up = True
            break

    above_ema50 = df["close"].iloc[-1] > df["ema50"].iloc[-1]
    return _indicator_strategy_result(
        "EMA 9/21 Crossover",
        cross_up and above_ema50,
        f"Cross={cross_up}, Above_EMA50={above_ema50}",
    )


def strategy_bollinger_bounce(df: pd.DataFrame) -> dict:
    touched = (df["close"].iloc[-LOOKBACK:] <=
               df["bb_lower"].iloc[-LOOKBACK:]).any()

    bounced    = df["close"].iloc[-1] > df["bb_lower"].iloc[-1]
    rsi_ok     = df["rsi"].iloc[-1] < 45
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return _indicator_strategy_result(
        "Bollinger Band Bounce",
        touched and bounced and rsi_ok,
        f"Touched={touched}, Bounced={bounced}, RSI={latest_rsi}",
    )


def strategy_volume_breakout(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    """Breakout on live candles; volume average from yfinance indicators."""
    ind = indicator_df if indicator_df is not None else pattern_df
    if "avg_vol" in ind.columns:
        avg_vol   = ind["avg_vol"].iloc[-LOOKBACK:]
        vol_spike = (ind["volume"].iloc[-LOOKBACK:] > avg_vol * 2).any()
    else:
        vol_spike = False

    price_brk = False
    for i in _lookback_range(pattern_df, LOOKBACK):
        if pattern_df["close"].iloc[i] > pattern_df["high"].iloc[i - 1]:
            price_brk = True
            break

    above_sma = (
        ind["close"].iloc[-1] > ind["sma20"].iloc[-1]
        if "sma20" in ind.columns else True
    )
    return _candle_strategy_result(
        "Volume Breakout",
        pattern_hit=price_brk,
        fired=vol_spike and price_brk and above_sma,
        reason=f"VolSpike={vol_spike}, PriceBreak={price_brk}, SMA={above_sma}",
    )


def strategy_inverted_hammer(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_inverted_hammer)
    return _candle_strategy_result("Inverted Hammer", pattern_hit=hit)


def strategy_dragonfly_doji(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_dragonfly_doji)
    return _candle_strategy_result("Dragonfly Doji", pattern_hit=hit)


def strategy_bullish_marubozu(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_bullish_marubozu)
    return _candle_strategy_result("Bullish Marubozu", pattern_hit=hit)


def strategy_piercing_line(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_piercing_line)
    return _candle_strategy_result("Piercing Line", pattern_hit=hit)


def strategy_bullish_harami(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_bullish_harami)
    return _candle_strategy_result("Bullish Harami", pattern_hit=hit)


def strategy_morning_star(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_morning_star)
    return _candle_strategy_result("Morning Star", pattern_hit=hit)


def strategy_three_white_soldiers(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_three_white_soldiers)
    return _candle_strategy_result("Three White Soldiers", pattern_hit=hit)


def _waiting_candle_strategy(name: str, ws_count: int) -> dict:
    r = _waiting_pattern_result(name, ws_count)
    r["kind"] = "candle"
    r["pattern_hit"] = False
    return r


def _build_buy_strategies(
    df: pd.DataFrame,
    pattern_df: pd.DataFrame | None,
    ws_count: int,
) -> list[dict]:
    """All BUY strategies — indicators + candle patterns (WS candles)."""
    candle_fns = [
        ("Hammer Reversal",       strategy_hammer),
        ("Bullish Engulfing",   strategy_bullish_engulfing),
        ("Inverted Hammer",       strategy_inverted_hammer),
        ("Dragonfly Doji",        strategy_dragonfly_doji),
        ("Bullish Marubozu",      strategy_bullish_marubozu),
        ("Piercing Line",         strategy_piercing_line),
        ("Bullish Harami",        strategy_bullish_harami),
        ("Morning Star",          strategy_morning_star),
        ("Three White Soldiers",  strategy_three_white_soldiers),
        ("Volume Breakout",       strategy_volume_breakout),
    ]

    strategies = [
        strategy_rsi_macd(df),
        strategy_ema_crossover(df),
        strategy_bollinger_bounce(df),
    ]

    for name, fn in candle_fns:
        if pattern_df is not None:
            strategies.append(fn(pattern_df, df))
        else:
            strategies.append(_waiting_candle_strategy(name, ws_count))

    return strategies


def _apply_buy_gate(all_strategies: list[dict], min_required: int) -> tuple[bool, str]:
    """
    BUY rules:
      - Always: at least one candle pattern_hit in lookback
      - min_required == 1: candle pattern only (no indicator-only buys)
      - min_required >= 2: pattern_hit + total fired >= min_required
    """
    candle = [s for s in all_strategies if s.get("kind") == "candle"]
    fired  = [s for s in all_strategies if s["fired"]]
    patterns = [s for s in candle if s.get("pattern_hit")]

    if not patterns:
        names = [s["name"] for s in candle]
        return False, f"No candle pattern (need 1+ of: {', '.join(names[:4])}...)"

    if min_required <= 1:
        return True, f"Candle pattern: {', '.join(s['name'] for s in patterns)}"

    if len(fired) < min_required:
        return False, (
            f"Only {len(fired)}/{min_required} strategies fired "
            f"(candle OK: {', '.join(s['name'] for s in patterns)})"
        )
    return True, ""


# ════════════════════════════════════════════════════════════
#   SELL STRATEGIES
# ════════════════════════════════════════════════════════════

def strategy_sell_rsi_overbought(df: pd.DataFrame) -> dict:
    overbought = (df["rsi"].iloc[-LOOKBACK:] > 70).any()
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return {
        "fired":  overbought,
        "name":   "RSI Overbought",
        "reason": f"RSI={latest_rsi} crossed above 70",
    }


def strategy_sell_macd_bearish(df: pd.DataFrame) -> dict:
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
    symbol = stock["ticker"]

    if WAR_ROOM_GATING:
        if stock.get("direction") not in ("BUY_ONLY",):
            return {"action": "WAIT", "reason": f"War room direction: {stock.get('direction', 'unknown')}"}

        if stock.get("confidence", 0) < MIN_CONFIDENCE:
            return {"action": "WAIT", "reason": f"War room confidence too low ({stock.get('confidence')}%)"}

        if stock.get("market_bias") == "BEARISH":
            return {"action": "WAIT", "reason": "Stock market bias: BEARISH"}
    else:
        if stock.get("direction") == "AVOID":
            return {"action": "WAIT", "reason": "Stock explicitly marked AVOID"}

        if stock.get("market_bias") == "BEARISH":
            return {"action": "WAIT", "reason": "Stock market bias: BEARISH"}

    df = _get_indicator_df(symbol)
    if df is None:
        return {"action": "WAIT", "reason": "Insufficient indicator history (yfinance+WS)"}

    ws_count   = len(get_candle_history(symbol))
    pattern_df = _get_pattern_df(symbol)
    all_strategies = _build_buy_strategies(df, pattern_df, ws_count)
    total      = len(all_strategies)

    fired       = [s for s in all_strategies if s["fired"]]
    fired_count = len(fired)
    fired_names = [s["name"] for s in fired]
    pattern_hits = [s["name"] for s in all_strategies if s.get("pattern_hit")]

    ok, gate_msg = _apply_buy_gate(all_strategies, MIN_STRATEGIES_AGREE)
    if not ok:
        return {"action": "WAIT", "reason": gate_msg}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol} (WS tick missing)"}

    stop_loss = calculate_stop_loss(price, "BUY")
    return {
        "action":    "BUY",
        "symbol":    symbol,
        "price":     round(price, 2),
        "stop_loss": stop_loss,
        "reason":    (
            f"{fired_count}/{total}: {', '.join(fired_names)} | "
            f"candle: {', '.join(pattern_hits)} | price={price_src}"
        ),
        "signals":   fired_count,
    }


def _evaluate_math_signal(stock: dict, briefing: dict) -> dict:
    """
    Math-only evaluation for watchlist stocks.
    Math watchlist — same BUY gate (candle pattern required).
    Only in BULLISH market.
    """
    symbol = stock["ticker"]

    if stock.get("market_bias") not in ("BULLISH", "NEUTRAL"):
        return {"action": "WAIT", "reason": f"Stock market bias: {stock.get('market_bias', 'UNKNOWN')}"}

    if stock.get("direction") == "AVOID":
        return {"action": "WAIT", "reason": "Marked AVOID"}

    df = _get_indicator_df(symbol)
    if df is None:
        return {"action": "WAIT", "reason": "Insufficient indicator history"}

    ws_count   = len(get_candle_history(symbol))
    pattern_df = _get_pattern_df(symbol)
    all_strategies = _build_buy_strategies(df, pattern_df, ws_count)
    total      = len(all_strategies)

    fired       = [s for s in all_strategies if s["fired"]]
    fired_count = len(fired)
    fired_names = [s["name"] for s in fired]
    pattern_hits = [s["name"] for s in all_strategies if s.get("pattern_hit")]

    logger.debug(
        f"[MATH] {symbol} | {fired_count}/{total} | candles={ws_count} | "
        f"patterns={pattern_hits or 'none'} | {fired_names or 'none'}"
    )

    ok, gate_msg = _apply_buy_gate(all_strategies, MATH_STRATEGIES_AGREE)
    if not ok:
        return {"action": "WAIT", "reason": f"Math: {gate_msg}"}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol}"}
    stop_loss = calculate_stop_loss(price, "BUY")
    return {
        "action":       "BUY",
        "trade_type":   "MATH",
        "symbol":       symbol,
        "price":        round(price, 2),
        "stop_loss":    stop_loss,
        "reason":       (
            f"MATH {fired_count}/{total}: {', '.join(fired_names)} | "
            f"candle: {', '.join(pattern_hits)} | price={price_src}"
        ),
        "signals":      fired_count,
        "risk_pct":     MATH_RISK_PER_TRADE,
    }


# ════════════════════════════════════════════════════════════
#   SELL SIGNAL CHECKER
# ════════════════════════════════════════════════════════════

def _check_sell_signals(live_prices: dict[str, float]):
    for position in get_open_positions():
        symbol  = position["symbol"]
        current = live_prices.get(symbol)

        if not current:
            continue

        # ── Candle history — WebSocket + yfinance fallback ────
        candles = _get_candles_with_fallback(symbol)

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
#   ALL EXITS
# ════════════════════════════════════════════════════════════

def _check_all_exits(live_prices: dict[str, float]):
    check_stop_losses(live_prices)
    update_trailing_stop_losses(live_prices)
    check_profit_targets(live_prices)
    _check_sell_signals(live_prices)
    check_war_room_flip(live_prices)
    if STRATEGY_TYPE == "INTRADAY":
        squareoff_all_intraday(live_prices)


# ════════════════════════════════════════════════════════════
#   HELPERS
# ════════════════════════════════════════════════════════════

def _is_market_open(now: dt_time) -> bool:
    from core.market_calendar import is_trading_day
    if not is_trading_day(datetime.now().date()):
        return False
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _get_live_prices(briefing: dict) -> dict[str, float]:
    prices = {}
    # War Room + Watchlist dono se symbols lo
    all_stocks = briefing.get("approved_stocks", []) + briefing.get("watchlist", [])
    for stock in all_stocks:
        symbol = stock["ticker"]
        tick   = get_latest_tick(symbol)
        if tick:
            prices[symbol] = tick["ltp"]

    # Also open positions (safety)
    for position in get_open_positions():
        symbol = position["symbol"]
        if symbol not in prices:
            tick = get_latest_tick(symbol)
            if tick:
                prices[symbol] = tick["ltp"]

    return prices


def _can_open_new_position(stock: dict) -> bool:
    if stock.get("direction") == "AVOID":
        return False
    open_symbols = [p["symbol"] for p in get_open_positions()]
    if stock["ticker"] in open_symbols:
        return False
    # Cooldown check
    last_fail = _failed_order_cooldown.get(stock["ticker"], 0)
    if time.time() - last_fail < FAILED_ORDER_COOLDOWN_SEC:
        return False
    return True


def _drop_incomplete_candle_if_present(hist):
    """
    Yfinance kabhi kabhi current incomplete 5-min candle 
    last row mein include kar deta hai during market hours.
    Ye function use drop karta hai agar waise ho.
    """
    if hist is None or hist.empty:
        return hist

    last_time = hist.index[-1]
    # Timezone remove karo (yfinance timezone-aware timestamps deta hai)
    if hasattr(last_time, 'tzinfo') and last_time.tzinfo is not None:
        import pytz
        last_time = last_time.astimezone(pytz.timezone('Asia/Kolkata')).replace(tzinfo=None)

    now = datetime.now()
    current_bucket_min   = (now.minute // 5) * 5
    current_period_start = now.replace(
        minute      = current_bucket_min,
        second      = 0,
        microsecond = 0,
    )

    if last_time >= current_period_start:
        logger.debug(
            f"Dropped incomplete yfinance candle: "
            f"{last_time.strftime('%H:%M')} (current period)"
        )
        return hist.iloc[:-1]

    return hist


# ════════════════════════════════════════════════════════════
#   MAIN ASYNC LOOP
# ════════════════════════════════════════════════════════════

async def run_strategy_loop(shutdown_event: asyncio.Event):
    global _last_scan_log
    logger.info(
        f"⚡ Strategy loop started | "
        f"Mode: {STRATEGY_TYPE} | "
        f"Lookback: {LOOKBACK} candles | "
        f"War Room buy: {MIN_STRATEGIES_AGREE} (+ candle required) | "
        f"Math buy: {MATH_STRATEGIES_AGREE} (+ candle required) | "
        f"Pattern candles: {MIN_WS_CANDLES_FOR_PATTERNS}+ live WS | "
        f"Sell: {MIN_SELL_SIGNALS}/4"
    )

    while not shutdown_event.is_set():
        try:
            _apply_trading_settings()
            now = datetime.now().time()
            if not _is_market_open(now):
                await asyncio.sleep(30)
                continue

            briefing = load_briefing()
            if not briefing:
                logger.warning("No briefing. Waiting for war room...")
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            # Periodic log: ALL 21 math + 4 war room stocks (not just signal fires)
            if time.time() - _last_scan_log >= SCAN_LOG_INTERVAL:
                _last_scan_log = time.time()
                _log_full_scan(briefing)

            live_prices = _get_live_prices(briefing)
            _check_all_exits(live_prices)

            # Update live capital file for dashboard
            try:
                from core.order_executor import _get_available_capital
                import json
                cap = _get_available_capital()
                with open("data/live_capital.json", "w") as f:
                    json.dump({"capital": cap, "timestamp": datetime.now().isoformat()}, f)
            except Exception:
                pass

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
                    logger.info(
                        f"🟢 BUY SIGNAL | {signal['symbol']} | "
                        f"₹{signal['price']} | {signal['reason']}"
                    )
                    trade = place_buy_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["reason"],
                        confidence     = stock.get("confidence", 0),
                    )
                    if trade:
                        open_count += 1
                        _failed_order_cooldown.pop(signal["symbol"], None)  # success pe reset
                    else:
                        _failed_order_cooldown[signal["symbol"]] = time.time()  # fail pe mark

            # Math watchlist — excludes War Room symbols (no overlap)
            war_room_syms = {s["ticker"] for s in briefing.get("approved_stocks", [])}
            open_syms = {p["symbol"] for p in get_open_positions()}
            for stock in briefing.get("watchlist", []):
                if stock["ticker"] in war_room_syms:
                    continue
                if open_count >= MAX_POSITIONS:
                    break
                if stock["ticker"] in open_syms:
                    continue

                signal = _evaluate_math_signal(stock, briefing)
                if signal["action"] == "BUY":
                    logger.info(
                        f"[MATH BUY] {signal['symbol']} | "
                        f"Rs.{signal['price']} | {signal['reason']}"
                    )
                    trade = place_buy_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["reason"],
                        confidence     = stock.get("math_score", 0),
                        risk_pct = signal.get("risk_pct", MATH_RISK_PER_TRADE),   # or directly MATH_RISK_PER_TRADE
                    )
                    if trade:
                        open_count += 1
                    else:
                        _failed_order_cooldown[signal["symbol"]] = time.time()

        except Exception as e:
            logger.error(f"Strategy loop error: {e}", exc_info=True)

        await asyncio.sleep(LOOP_INTERVAL)

    logger.info("Strategy loop stopped.")


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pass
