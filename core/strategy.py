# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/strategy.py — The Fast Math Loop
#
#   SIGNAL STRATEGY SETS:
#     Set definitions live in config/strategy_sets.json.
#     Conditions inside a set are ANDed together; sets are ORed.
#     Buy and sell sets are evaluated separately.
#
#   All strategies support multi-candle lookback window.
#   A signal is valid if it fired in ANY of the last N candles.
#
#   EXIT PRIORITY ORDER:
#     1. Stop Loss (hard floor)
#     2. Trailing SL update
#     3. Profit Target (2:1 RR)
#     4. Sell Signals (technical reversal)
#     5. 3:15 PM Squareoff (intraday only)
# ============================================================

import asyncio
import logging
import math
import os
import pandas as pd
import ta
import time
import yfinance as yf
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from typing import Callable

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
    update_trailing_stop_losses,
    squareoff_all_intraday,
    check_max_daily_loss,
)
from core.state_manager import load_briefing, get_open_positions
from core.trading_settings import get as cfg, get_section
from core.safe_io import atomic_write_json, safe_float, safe_int
from core.strategy_sets import (
    StrategySetDefinition,
    load_strategy_sets,
    normalize_set_key,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ── Runtime config (from config/trading_settings.json, hot-reload) ──
STRATEGY_TYPE         = "INTRADAY"
LOOP_INTERVAL         = 5
MAX_POSITIONS         = 2
MIN_CONFIDENCE        = 70
MATH_RISK_PER_TRADE   = 0.05
LOOKBACK              = 3
MIN_WS_CANDLES_FOR_PATTERNS = 2
SCAN_LOG_INTERVAL     = 90

_failed_order_cooldown: dict[str, float] = {}
_last_scan_log: float = 0.0
FAILED_ORDER_COOLDOWN_SEC = 300
_yfinance_cache: dict[str, list] = {}
_briefing_cache: dict = None
_briefing_cache_time: float = 0.0
BRIEFING_CACHE_SECONDS = 60  # Reload from disk every 60 seconds


def _get_briefing_cached():
    """Get briefing from cache, reload from disk every 60 seconds max."""
    global _briefing_cache, _briefing_cache_time
    now = time.time()
    if _briefing_cache is not None and (now - _briefing_cache_time) < BRIEFING_CACHE_SECONDS:
        return _briefing_cache
    _briefing_cache = load_briefing()  # This logs once every 60 seconds, not every 5
    _briefing_cache_time = now
    return _briefing_cache

def _apply_trading_settings():
    """Reload tunables from config/trading_settings.json (dashboard-editable)."""
    global STRATEGY_TYPE, LOOP_INTERVAL, MAX_POSITIONS, MIN_CONFIDENCE
    global MATH_RISK_PER_TRADE, LOOKBACK, MIN_WS_CANDLES_FOR_PATTERNS
    global SCAN_LOG_INTERVAL

    s = get_section("strategy")
    md = get_section("market_data")
    risk = get_section("risk")

    STRATEGY_TYPE         = s.get("strategy_type", "INTRADAY")
    LOOP_INTERVAL         = max(1, safe_int(s.get("loop_interval_sec"), 5))
    MAX_POSITIONS         = max(1, safe_int(s.get("max_open_positions"), 2))
    MIN_CONFIDENCE        = int(max(0, min(100, safe_float(s.get("min_confidence"), 70))))
    LOOKBACK              = max(1, safe_int(s.get("signal_lookback_candles"), 3))
    MIN_WS_CANDLES_FOR_PATTERNS = max(1, safe_int(s.get("min_ws_candles_for_patterns"), 2))
    MATH_RISK_PER_TRADE   = max(0.0, min(1.0, safe_float(risk.get("math_risk_per_trade"), 0.05)))
    SCAN_LOG_INTERVAL     = max(30, safe_int(md.get("scan_log_interval_sec"), 90))


_apply_trading_settings()

# ── Adaptive Config (loaded from trading_settings.json adaptive section) ──
_adaptive_config: dict = {}
_adaptive_signal_multipliers: dict[str, float] = {}
_adaptive_time_multipliers: dict[str, float] = {}
_adaptive_sl_values: dict[str, float] = {}
_adaptive_market_multiplier: float = 1.0

ADAPTIVE_MULTIPLIER_MIN = 0.4
ADAPTIVE_MULTIPLIER_MAX = 1.2
ADVISORY_MULTIPLIER_MIN = 0.95
ADVISORY_MULTIPLIER_MAX = 1.05


def _clamp(value: float, minimum: float, maximum: float) -> float:
    number = safe_float(value, minimum)
    return max(minimum, min(maximum, number))


def _clamp_confidence(value: float) -> float:
    return _clamp(value, 0.0, 100.0)


def _coerce_adaptive_multiplier(raw, label: str, default: float = 1.0) -> float:
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        logger.warning("Invalid adaptive multiplier for %s: %r; using %.2f", label, raw, default)
        return default

    clipped = _clamp(value, ADAPTIVE_MULTIPLIER_MIN, ADAPTIVE_MULTIPLIER_MAX)
    if clipped != value:
        logger.warning(
            "Adaptive multiplier for %s clipped from %.4f to %.4f",
            label,
            value,
            clipped,
        )
    return clipped


def _coerce_advisory_multiplier(raw, label: str, default: float = 1.0) -> float:
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        logger.warning("Invalid advisory multiplier for %s: %r; using %.2f", label, raw, default)
        return default

    clipped = _clamp(value, ADVISORY_MULTIPLIER_MIN, ADVISORY_MULTIPLIER_MAX)
    if clipped != value:
        logger.warning(
            "Advisory multiplier for %s clipped from %.4f to %.4f",
            label,
            value,
            clipped,
        )
    return clipped


def _coerce_multiplier_map(raw, label: str, normalize_keys: bool = True) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}

    out: dict[str, float] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        map_key = normalize_set_key(key_text) if normalize_keys else key_text
        out[map_key] = _coerce_adaptive_multiplier(value, f"{label}.{key_text}")
    return out


def _coerce_float_map(raw, label: str, normalize_keys: bool = False) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}

    out: dict[str, float] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        try:
            out_key = normalize_set_key(key_text) if normalize_keys else key_text.upper()
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("not finite")
            out[out_key] = numeric
        except (TypeError, ValueError):
            logger.warning("Invalid adaptive value for %s.%s: %r", label, key_text, value)
    return out


def _load_adaptive_config():
    """Load adaptive configuration from trading_settings.json."""
    global _adaptive_config, _adaptive_signal_multipliers, _adaptive_time_multipliers
    global _adaptive_sl_values, _adaptive_market_multiplier
    
    try:
        s = get_section("adaptive")
        if not s:
            _adaptive_config = {}
            _adaptive_signal_multipliers = {}
            _adaptive_time_multipliers = {}
            _adaptive_sl_values = {}
            _adaptive_market_multiplier = 1.0
            return
        
        # Strategy adaptive values
        strategy_adaptive = s.get("strategy", {})
        if not isinstance(strategy_adaptive, dict):
            strategy_adaptive = {}
        _adaptive_signal_multipliers = _coerce_multiplier_map(
            strategy_adaptive.get("signal_confidence_multipliers", {}),
            "adaptive.strategy.signal_confidence_multipliers",
        )
        _adaptive_market_multiplier = _coerce_adaptive_multiplier(
            strategy_adaptive.get("market_regime_multiplier", 1.0),
            "adaptive.strategy.market_regime_multiplier",
        )
        
        # Time window adaptive values
        _adaptive_time_multipliers = _coerce_multiplier_map(
            s.get("time_windows", {}),
            "adaptive.time_windows",
            normalize_keys=False,
        )
        
        # Symbol-specific SL values
        _adaptive_sl_values = _coerce_float_map(
            s.get("symbol_stops", {}),
            "adaptive.symbol_stops",
        )
        
        _adaptive_config = s
        logger.debug(f"📊 Adaptive config loaded | Signals: {len(_adaptive_signal_multipliers)} | Times: {len(_adaptive_time_multipliers)} | SLs: {len(_adaptive_sl_values)}")
    except Exception as e:
        _adaptive_config = {}
        _adaptive_signal_multipliers = {}
        _adaptive_time_multipliers = {}
        _adaptive_sl_values = {}
        _adaptive_market_multiplier = 1.0
        logger.warning("Adaptive config load failed; using neutral multipliers: %s", e)


_load_adaptive_config()

# ── Market Hours ──────────────────────────────────────────────
MARKET_OPEN   = dt_time(9, 15)
MARKET_CLOSE  = dt_time(15, 30)
NO_NEW_TRADES = dt_time(15, 00)  #########


# ════════════════════════════════════════════════════════════
#   HELPER: GET TIME WINDOW FOR CURRENT TIME
# ════════════════════════════════════════════════════════════

def _get_time_window(now: dt_time) -> str:
    """
    Get current time window for adaptive multiplier lookup.
    Examples: "9:15-10:00", "11:30-1:00", "2:00-3:15"
    """
    hour = now.hour
    minute = now.minute
    total_min = hour * 60 + minute
    
    # Define time windows (adjust as needed)
    if total_min >= 9*60+15 and total_min < 10*60:
        return "9:15-10:00"
    elif total_min >= 10*60 and total_min < 11*60+30:
        return "10:00-11:30"
    elif total_min >= 11*60+30 and total_min < 13*60:
        return "11:30-1:00"
    elif total_min >= 13*60 and total_min < 14*60:
        return "1:00-2:00"
    elif total_min >= 14*60 and total_min < 15*60+30:
        return "2:00-3:30"
    else:
        return "other"


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
    """Log every cognition and watchlist symbol so quiet stocks stay visible."""
    from core.data_fetcher import get_feed_stats

    stats = get_feed_stats()
    tick_counts = stats.get("tick_counts", {})
    lines = [f"─── Scan summary ({len(stats.get('subscribed', []))} subscribed) ───"]

    for label, stocks in (
        ("Cognition", briefing.get("approved_stocks", [])),
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
    typical_price    = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_vol   = df["volume"].replace(0, pd.NA).cumsum()
    df["vwap"]       = (typical_price * df["volume"]).cumsum() / cumulative_vol

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
@dataclass(frozen=True)
class StrategyEvaluationContext:
    side: str
    indicator_df: pd.DataFrame
    pattern_df: pd.DataFrame | None = None
    ws_count: int = 0


StrategyConditionFn = Callable[[StrategyEvaluationContext], dict]


def _price_above_column_condition(df: pd.DataFrame, column: str, label: str) -> dict:
    value = df[column].iloc[-1] if column in df.columns else None
    if value is None or pd.isna(value):
        return _indicator_strategy_result(label, False, f"{column} not ready")

    close = df["close"].iloc[-1]
    return _indicator_strategy_result(
        label,
        close > value,
        f"Close={round(close, 2)} > {column.upper()}={round(value, 2)}",
    )


def _price_below_column_condition(df: pd.DataFrame, column: str, label: str) -> dict:
    value = df[column].iloc[-1] if column in df.columns else None
    if value is None or pd.isna(value):
        return _indicator_strategy_result(label, False, f"{column} not ready")

    close = df["close"].iloc[-1]
    return _indicator_strategy_result(
        label,
        close < value,
        f"Close={round(close, 2)} < {column.upper()}={round(value, 2)}",
    )


def condition_rsi_macd_momentum(ctx: StrategyEvaluationContext) -> dict:
    return strategy_rsi_macd(ctx.indicator_df)


def condition_ema_9_21_crossover(ctx: StrategyEvaluationContext) -> dict:
    return strategy_ema_crossover(ctx.indicator_df)


def condition_bollinger_band_bounce(ctx: StrategyEvaluationContext) -> dict:
    return strategy_bollinger_bounce(ctx.indicator_df)


def _buy_candle_condition(
    ctx: StrategyEvaluationContext,
    name: str,
    fn: Callable[[pd.DataFrame, pd.DataFrame | None], dict],
) -> dict:
    if ctx.pattern_df is None:
        return _waiting_candle_strategy(name, ctx.ws_count)
    return fn(ctx.pattern_df, ctx.indicator_df)


def condition_hammer_reversal(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Hammer Reversal", strategy_hammer)


def condition_bullish_engulfing(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Bullish Engulfing", strategy_bullish_engulfing)


def condition_inverted_hammer(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Inverted Hammer", strategy_inverted_hammer)


def condition_dragonfly_doji(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Dragonfly Doji", strategy_dragonfly_doji)


def condition_bullish_marubozu(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Bullish Marubozu", strategy_bullish_marubozu)


def condition_piercing_line(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Piercing Line", strategy_piercing_line)


def condition_bullish_harami(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Bullish Harami", strategy_bullish_harami)


def condition_morning_star(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Morning Star", strategy_morning_star)


def condition_three_white_soldiers(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Three White Soldiers", strategy_three_white_soldiers)


def condition_volume_breakout(ctx: StrategyEvaluationContext) -> dict:
    return _buy_candle_condition(ctx, "Volume Breakout", strategy_volume_breakout)


def condition_bullish_reversal_candle(ctx: StrategyEvaluationContext) -> dict:
    if ctx.pattern_df is None:
        return _waiting_candle_strategy("Bullish Reversal Candle", ctx.ws_count)

    candle_results = [
        condition_hammer_reversal(ctx),
        condition_bullish_engulfing(ctx),
        condition_inverted_hammer(ctx),
        condition_dragonfly_doji(ctx),
        condition_bullish_marubozu(ctx),
        condition_piercing_line(ctx),
        condition_bullish_harami(ctx),
        condition_morning_star(ctx),
        condition_three_white_soldiers(ctx),
    ]
    fired = [r for r in candle_results if r.get("fired")]
    pattern_hits = [r for r in candle_results if r.get("pattern_hit")]
    names = [r["name"] for r in fired or pattern_hits]

    return _candle_strategy_result(
        "Bullish Reversal Candle",
        pattern_hit=bool(pattern_hits),
        fired=bool(fired),
        reason=", ".join(names) if names else "No bullish reversal candle",
    )


def condition_volume_spike(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "avg_vol" not in df.columns:
        return _indicator_strategy_result("Volume Spike", False, "avg_vol not ready")

    recent_volume = df["volume"].iloc[-LOOKBACK:]
    recent_avg = df["avg_vol"].iloc[-LOOKBACK:]
    spike = bool((recent_volume > (recent_avg * 1.5)).fillna(False).any())
    latest_volume = round(float(df["volume"].iloc[-1]), 2)
    latest_avg = df["avg_vol"].iloc[-1]
    avg_text = "not ready" if pd.isna(latest_avg) else round(float(latest_avg), 2)
    return _indicator_strategy_result(
        "Volume Spike",
        spike,
        f"LatestVol={latest_volume}, AvgVol={avg_text}",
    )


def condition_price_above_ema20(ctx: StrategyEvaluationContext) -> dict:
    return _price_above_column_condition(ctx.indicator_df, "ema20", "Price above EMA20")


def condition_price_below_ema20(ctx: StrategyEvaluationContext) -> dict:
    return _price_below_column_condition(ctx.indicator_df, "ema20", "Price below EMA20")


def condition_price_above_vwap(ctx: StrategyEvaluationContext) -> dict:
    return _price_above_column_condition(ctx.indicator_df, "vwap", "Price above VWAP")


def condition_price_below_vwap(ctx: StrategyEvaluationContext) -> dict:
    return _price_below_column_condition(ctx.indicator_df, "vwap", "Price below VWAP")


def condition_rsi_recovering(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns:
        return _indicator_strategy_result("RSI recovering", False, "RSI not ready")

    recent_oversold = bool((df["rsi"].iloc[-LOOKBACK:] < 35).fillna(False).any())
    rising = df["rsi"].iloc[-1] > df["rsi"].iloc[-2]
    latest = round(float(df["rsi"].iloc[-1]), 1)
    fired = recent_oversold and rising and latest < 60
    return _indicator_strategy_result(
        "RSI recovering",
        fired,
        f"RSI={latest}, recent_oversold={recent_oversold}, rising={rising}",
    )


def condition_rsi_weakening(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns:
        return _indicator_strategy_result("RSI weakening", False, "RSI not ready")

    recent_hot = bool((df["rsi"].iloc[-LOOKBACK:] > 60).fillna(False).any())
    falling = df["rsi"].iloc[-1] < df["rsi"].iloc[-2]
    latest = round(float(df["rsi"].iloc[-1]), 1)
    fired = recent_hot and falling
    return _indicator_strategy_result(
        "RSI weakening",
        fired,
        f"RSI={latest}, recent_hot={recent_hot}, falling={falling}",
    )


def condition_rsi_overbought(ctx: StrategyEvaluationContext) -> dict:
    return strategy_sell_rsi_overbought(ctx.indicator_df)


def condition_macd_bearish_cross(ctx: StrategyEvaluationContext) -> dict:
    return strategy_sell_macd_bearish(ctx.indicator_df)


def condition_bearish_pattern(ctx: StrategyEvaluationContext) -> dict:
    return strategy_sell_bearish_engulfing(ctx.indicator_df)


def condition_ema20_breakdown(ctx: StrategyEvaluationContext) -> dict:
    return strategy_sell_ema_breakdown(ctx.indicator_df)


def condition_price_below_recent_support(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < LOOKBACK + 1:
        return _indicator_strategy_result("Price below recent support", False, "Not enough candles")

    prior_lows = df["low"].iloc[-(LOOKBACK + 1):-1]
    support = prior_lows.min()
    close = df["close"].iloc[-1]
    fired = close < support
    return _indicator_strategy_result(
        "Price below recent support",
        fired,
        f"Close={round(close, 2)}, Support={round(support, 2)}",
    )


CONDITION_REGISTRY: dict[str, StrategyConditionFn] = {
    "rsi_macd_momentum": condition_rsi_macd_momentum,
    "ema_9_21_crossover": condition_ema_9_21_crossover,
    "bollinger_band_bounce": condition_bollinger_band_bounce,
    "hammer_reversal": condition_hammer_reversal,
    "bullish_engulfing": condition_bullish_engulfing,
    "inverted_hammer": condition_inverted_hammer,
    "dragonfly_doji": condition_dragonfly_doji,
    "bullish_marubozu": condition_bullish_marubozu,
    "piercing_line": condition_piercing_line,
    "bullish_harami": condition_bullish_harami,
    "morning_star": condition_morning_star,
    "three_white_soldiers": condition_three_white_soldiers,
    "volume_breakout": condition_volume_breakout,
    "bullish_reversal_candle": condition_bullish_reversal_candle,
    "volume_spike": condition_volume_spike,
    "price_above_ema20": condition_price_above_ema20,
    "price_below_ema20": condition_price_below_ema20,
    "price_above_vwap": condition_price_above_vwap,
    "price_below_vwap": condition_price_below_vwap,
    "rsi_recovering": condition_rsi_recovering,
    "rsi_weakening": condition_rsi_weakening,
    "rsi_overbought": condition_rsi_overbought,
    "macd_bearish_cross": condition_macd_bearish_cross,
    "bearish_pattern": condition_bearish_pattern,
    "ema20_breakdown": condition_ema20_breakdown,
    "price_below_recent_support": condition_price_below_recent_support,
}


class StrategySetEvaluator:
    def __init__(self, condition_registry: dict[str, StrategyConditionFn]):
        self.condition_registry = condition_registry

    def evaluate(self, side: str, ctx: StrategyEvaluationContext) -> dict | None:
        config = load_strategy_sets()
        set_defs = config.buy_sets if side == "buy" else config.sell_sets

        for set_def in set_defs:
            condition_results = self._evaluate_conditions(set_def, ctx)
            if condition_results and all(r.get("fired") for r in condition_results):
                return {
                    "side": set_def.side,
                    "set_name": set_def.name,
                    "conditions": condition_results,
                    "priority": set_def.priority,
                    "base_confidence": set_def.base_confidence,
                    "confidence_weight": set_def.confidence_weight,
                    "notes": set_def.notes,
                }

        return None

    def _evaluate_conditions(
        self,
        set_def: StrategySetDefinition,
        ctx: StrategyEvaluationContext,
    ) -> list[dict]:
        results = []
        for condition_key in set_def.conditions:
            fn = self.condition_registry.get(condition_key)
            if fn is None:
                results.append({
                    "key": condition_key,
                    "name": condition_key,
                    "fired": False,
                    "reason": "Condition not registered",
                })
                continue

            result = dict(fn(ctx))
            result["key"] = condition_key
            results.append(result)

        return results


_strategy_set_evaluator = StrategySetEvaluator(CONDITION_REGISTRY)


def _confidence_to_percent(raw) -> float | None:
    if raw is None:
        return None

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None

    if 0 <= value <= 1:
        value *= 100
    return _clamp_confidence(value)


def _stock_confidence_to_percent(raw, stock_confidence_key: str) -> float | None:
    if raw in (None, ""):
        return None

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None

    if stock_confidence_key == "math_score" and 0 < value <= 10:
        value *= 10
    elif stock_confidence_key == "confidence" and value == 0:
        return None
    elif 0 <= value <= 1:
        value *= 100

    return _clamp_confidence(value)


def _triggered_set_base_confidence(triggered_set: dict) -> float:
    value = _confidence_to_percent(triggered_set.get("base_confidence"))
    return value if value is not None else _clamp_confidence(MIN_CONFIDENCE)


def _resolve_base_confidence(
    stock: dict,
    triggered_set: dict,
    stock_confidence_key: str,
) -> float:
    set_base = _triggered_set_base_confidence(triggered_set)
    stock_confidence = _stock_confidence_to_percent(
        stock.get(stock_confidence_key),
        stock_confidence_key,
    )
    if stock_confidence is None:
        return set_base

    # Conservative fusion: a weak screener/AI score can veto a strong set,
    # but it cannot inflate the configured quality of that set.
    return min(set_base, stock_confidence)


def _strategy_confidence_weight(triggered_set: dict) -> float:
    try:
        value = float(triggered_set.get("confidence_weight", 1.0))
        if not math.isfinite(value):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        return 1.0
    return _clamp(value, 0.1, 2.0)


def _apply_adaptive_confidence(
    base_confidence: float,
    triggered_set: dict,
    symbol: str | None = None,
    now: dt_time | None = None,
    reload_config: bool = True,
) -> float:
    return _build_confidence_trace(
        base_confidence,
        triggered_set,
        symbol=symbol,
        now=now,
        reload_config=reload_config,
    )["final_confidence"]


def _build_confidence_trace(
    base_confidence: float,
    triggered_set: dict,
    symbol: str | None = None,
    now: dt_time | None = None,
    reload_config: bool = True,
) -> dict:
    if reload_config:
        _load_adaptive_config()

    set_name = str(triggered_set.get("set_name") or "UNKNOWN")
    final_confidence = _clamp_confidence(base_confidence)
    set_key = normalize_set_key(set_name)
    signal_multiplier = _coerce_adaptive_multiplier(
        _adaptive_signal_multipliers.get(set_key, 1.0),
        f"runtime.signal.{set_key}",
    )
    confidence_weight = _strategy_confidence_weight(triggered_set)

    final_confidence *= signal_multiplier
    final_confidence *= confidence_weight

    now = now or datetime.now().time()
    time_window = _get_time_window(now)
    time_multiplier = _coerce_adaptive_multiplier(
        _adaptive_time_multipliers.get(time_window, 1.0),
        f"runtime.time_window.{time_window}",
    )
    market_multiplier = _coerce_adaptive_multiplier(
        _adaptive_market_multiplier,
        "runtime.market_regime",
    )
    advisory_multiplier = 1.0
    advisory_reason = "neutral"

    final_confidence *= time_multiplier
    final_confidence *= market_multiplier

    if symbol:
        try:
            from reflection.insight_bridge import get_execution_advisory

            advisory = get_execution_advisory(symbol)
            advisory_multiplier = _coerce_advisory_multiplier(
                advisory.get("confidence_multiplier", 1.0),
                f"cognition_advisory.{symbol}",
                default=1.0,
            )
            advisory_reason = advisory.get("reason", "neutral")
        except Exception as exc:
            logger.debug("Cognition advisory skipped for %s: %s", symbol, exc)

    final_confidence *= advisory_multiplier

    return {
        "base_confidence": round(_clamp_confidence(base_confidence), 4),
        "signal_multiplier": round(signal_multiplier, 4),
        "confidence_weight": round(confidence_weight, 4),
        "time_window": time_window,
        "time_window_multiplier": round(time_multiplier, 4),
        "market_regime_multiplier": round(market_multiplier, 4),
        "advisory_multiplier": round(advisory_multiplier, 4),
        "advisory_reason": advisory_reason,
        "min_confidence": _clamp_confidence(MIN_CONFIDENCE),
        "final_confidence": round(_clamp_confidence(final_confidence), 4),
    }



def _adaptive_stop_loss_multiplier(symbol: str) -> float:
    value = _adaptive_sl_values.get(str(symbol).upper())
    if value is None:
        return 1.0
    return _clamp(value, 0.5, 2.0)


def _calculate_adaptive_stop_loss(symbol: str, price: float, direction: str = "BUY") -> float:
    price = safe_float(price, 0.0)
    pct = _clamp(safe_float(cfg("risk", "stop_loss_percent", 0.01), 0.01), 0.0001, 0.20)
    pct *= _adaptive_stop_loss_multiplier(symbol)
    if direction == "BUY":
        return round(price * (1 - pct), 2)
    return round(price * (1 + pct), 2)


def _confidence_rejection_reason(triggered_set: dict, confidence: float, trace: dict | None = None) -> str:
    trace_text = f" | {_format_confidence_trace(trace)}" if trace else ""
    return (
        f"Final confidence {round(confidence, 1)} below "
        f"min_confidence {MIN_CONFIDENCE} for {triggered_set['set_name']}{trace_text}"
    )


def _triggered_condition_names(triggered_set: dict) -> list[str]:
    return [condition["name"] for condition in triggered_set.get("conditions", [])]


def _format_confidence_trace(trace: dict | None) -> str:
    if not trace:
        return ""
    return (
        f"base={round(trace.get('base_confidence', 0), 1)} "
        f"signal_x={trace.get('signal_multiplier', 1.0)} "
        f"weight_x={trace.get('confidence_weight', 1.0)} "
        f"time={trace.get('time_window')}@{trace.get('time_window_multiplier', 1.0)} "
        f"market_x={trace.get('market_regime_multiplier', 1.0)} "
        f"advisory_x={trace.get('advisory_multiplier', 1.0)} "
        f"final={round(trace.get('final_confidence', 0), 1)} "
        f"min={round(trace.get('min_confidence', MIN_CONFIDENCE), 1)}"
    )


def _format_strategy_set_reason(
    triggered_set: dict,
    price_src: str,
    confidence: float,
    confidence_trace: dict | None = None,
) -> str:
    names = ", ".join(_triggered_condition_names(triggered_set))
    trace = _format_confidence_trace(confidence_trace)
    return (
        f"{triggered_set['set_name']} | "
        f"Satisfied: {names} | price={price_src} | "
        f"adapted_conf={round(confidence, 1)}"
        f"{' | ' + trace if trace else ''}"
    )


def _log_triggered_strategy_set(action: str, signal: dict):
    conditions = signal.get("set_conditions", [])
    satisfied = "\n".join(
        f"- {condition['name']}: {condition.get('reason', '')}"
        for condition in conditions
    )
    logger.info(
        f"{action} SIGNAL TRIGGERED\n"
        f"Symbol: {signal['symbol']}\n"
        f"Price: {signal['price']}\n"
        f"Triggered Set: {signal['set_name']}\n"
        f"Set Side: {signal['set_side'].upper()}\n"
        f"Confidence: {_format_confidence_trace(signal.get('confidence_trace'))}\n"
        f"Satisfied:\n{satisfied}\n"
        f"Why: {signal.get('set_notes') or 'All configured set conditions were satisfied'}"
    )


#   BUY SIGNAL EVALUATOR
# ════════════════════════════════════════════════════════════

def _evaluate_buy_signal(stock: dict, briefing: dict) -> dict:
    symbol = stock["ticker"]

    if stock.get("direction") == "AVOID":
        return {"action": "WAIT", "reason": "Stock explicitly marked AVOID"}

    if stock.get("market_bias") == "BEARISH":
        return {"action": "WAIT", "reason": "Stock market bias: BEARISH"}

    df = _get_indicator_df(symbol)
    if df is None:
        return {"action": "WAIT", "reason": "Insufficient indicator history (yfinance+WS)"}

    ws_count   = len(get_candle_history(symbol))
    pattern_df = _get_pattern_df(symbol)
    ctx = StrategyEvaluationContext(
        side="buy",
        indicator_df=df,
        pattern_df=pattern_df,
        ws_count=ws_count,
    )
    triggered_set = _strategy_set_evaluator.evaluate("buy", ctx)
    if not triggered_set:
        return {"action": "WAIT", "reason": "No complete BUY strategy set triggered"}

    base_confidence = _resolve_base_confidence(stock, triggered_set, "confidence")
    confidence_trace = _build_confidence_trace(base_confidence, triggered_set, symbol=symbol)
    adaptive_confidence = confidence_trace["final_confidence"]
    if adaptive_confidence < MIN_CONFIDENCE:
        reason = _confidence_rejection_reason(triggered_set, adaptive_confidence, confidence_trace)
        logger.info("BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol} (WS tick missing)"}

    stop_loss = _calculate_adaptive_stop_loss(symbol, price, "BUY")
    
    return {
        "action":    "BUY",
        "symbol":    symbol,
        "price":     round(price, 2),
        "stop_loss": stop_loss,
        "base_confidence": round(base_confidence, 1),
        "confidence": round(adaptive_confidence, 1),
        "strategy": triggered_set["set_name"],
        "set_name": triggered_set["set_name"],
        "set_side": triggered_set["side"],
        "set_conditions": triggered_set["conditions"],
        "set_notes": triggered_set.get("notes", ""),
        "confidence_trace": confidence_trace,
        "reason": _format_strategy_set_reason(
            triggered_set,
            price_src,
            adaptive_confidence,
            confidence_trace,
        ),
        "signals": len(triggered_set["conditions"]),
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
    ctx = StrategyEvaluationContext(
        side="buy",
        indicator_df=df,
        pattern_df=pattern_df,
        ws_count=ws_count,
    )
    triggered_set = _strategy_set_evaluator.evaluate("buy", ctx)
    if not triggered_set:
        return {"action": "WAIT", "reason": "Math: no complete BUY strategy set triggered"}

    base_confidence = _resolve_base_confidence(stock, triggered_set, "math_score")
    confidence_trace = _build_confidence_trace(base_confidence, triggered_set, symbol=symbol)
    adaptive_confidence = confidence_trace["final_confidence"]
    if adaptive_confidence < MIN_CONFIDENCE:
        reason = _confidence_rejection_reason(triggered_set, adaptive_confidence, confidence_trace)
        logger.info("MATH BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol}"}
    stop_loss = _calculate_adaptive_stop_loss(symbol, price, "BUY")
    
    return {
        "action":       "BUY",
        "trade_type":   "MATH",
        "symbol":       symbol,
        "price":        round(price, 2),
        "stop_loss":    stop_loss,
        "base_confidence": round(base_confidence, 1),
        "confidence":   round(adaptive_confidence, 1),
        "strategy":     triggered_set["set_name"],
        "set_name":     triggered_set["set_name"],
        "set_side":     triggered_set["side"],
        "set_conditions": triggered_set["conditions"],
        "set_notes":    triggered_set.get("notes", ""),
        "confidence_trace": confidence_trace,
        "reason":       _format_strategy_set_reason(
            triggered_set,
            price_src,
            adaptive_confidence,
            confidence_trace,
        ),
        "signals":      len(triggered_set["conditions"]),
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

        ctx = StrategyEvaluationContext(side="sell", indicator_df=df)
        triggered_set = _strategy_set_evaluator.evaluate("sell", ctx)
        if not triggered_set:
            continue

        signal = {
            "symbol": symbol,
            "price": round(current, 2),
            "set_name": triggered_set["set_name"],
            "set_side": triggered_set["side"],
            "set_conditions": triggered_set["conditions"],
            "set_notes": triggered_set.get("notes", ""),
        }
        _log_triggered_strategy_set("SELL", signal)
        place_sell_order(symbol, current, f"SELL_SET:{triggered_set['set_name']}")


# ════════════════════════════════════════════════════════════
#   ALL EXITS
# ════════════════════════════════════════════════════════════

def _check_all_exits(live_prices: dict[str, float]):
    check_stop_losses(live_prices)
    update_trailing_stop_losses(live_prices)
    check_profit_targets(live_prices)
    _check_sell_signals(live_prices)
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
    # Cognition picks + watchlist symbols.
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
    try:
        strategy_set_config = load_strategy_sets()
        buy_set_count = len(strategy_set_config.buy_sets)
        sell_set_count = len(strategy_set_config.sell_sets)
    except Exception:
        buy_set_count = 0
        sell_set_count = 0

    logger.info(
        f"⚡ Strategy loop started | "
        f"Mode: {STRATEGY_TYPE} | "
        f"Lookback: {LOOKBACK} candles | "
        f"Buy sets: {buy_set_count} | "
        f"Sell sets: {sell_set_count} | "
        f"Pattern candles: {MIN_WS_CANDLES_FOR_PATTERNS}+ live WS | "
        f"Config: strategy_sets.json"
    )

    while not shutdown_event.is_set():
        try:
            _apply_trading_settings()
            _load_adaptive_config()
            now = datetime.now().time()
            if not _is_market_open(now):
                await asyncio.sleep(30)
                continue

            briefing = _get_briefing_cached()
            if not briefing:
                logger.warning("No briefing. Waiting for screener or cognition picks...")
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            # Periodic log: every tracked symbol, not just signal fires.
            if time.time() - _last_scan_log >= SCAN_LOG_INTERVAL:
                _last_scan_log = time.time()
                _log_full_scan(briefing)

            live_prices = _get_live_prices(briefing)
            _check_all_exits(live_prices)

            # Update live capital file for dashboard
            try:
                from core.order_executor import _get_available_capital
                cap = _get_available_capital()
                atomic_write_json(
                    "data/live_capital.json",
                    {"capital": cap, "timestamp": datetime.now().isoformat()},
                    label="live capital",
                    log=logger,
                )
            except Exception:
                logger.debug("Live capital snapshot update skipped", exc_info=True)

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
                if open_count >= MAX_POSITIONS:
                    break
                if not _can_open_new_position(stock):
                    continue
                signal = _evaluate_buy_signal(stock, briefing)
                if signal["action"] == "BUY":
                    _log_triggered_strategy_set("BUY", signal)
                    trade = place_buy_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["strategy"],
                        confidence     = signal.get("confidence", 0),
                    )
                    if trade:
                        open_count += 1
                        _failed_order_cooldown.pop(signal["symbol"], None)  # success pe reset
                    else:
                        _failed_order_cooldown[signal["symbol"]] = time.time()  # fail pe mark

            # Math watchlist excludes cognition picks (no overlap).
            cognition_syms = {s["ticker"] for s in briefing.get("approved_stocks", [])}
            open_syms = {p["symbol"] for p in get_open_positions()}
            for stock in briefing.get("watchlist", []):
                if stock["ticker"] in cognition_syms:
                    continue
                if open_count >= MAX_POSITIONS:
                    break
                if stock["ticker"] in open_syms:
                    continue

                signal = _evaluate_math_signal(stock, briefing)
                if signal["action"] == "BUY":
                    _log_triggered_strategy_set("BUY", signal)
                    trade = place_buy_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["strategy"],
                        confidence     = signal.get("confidence", 0),
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
