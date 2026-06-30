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
import requests
import ta
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from typing import Callable
from urllib.parse import quote

from core.data_fetcher import (
    get_latest_tick,
    get_candle_history,
    has_enough_history,
)
from core.order_executor import (
    place_entry_order,
    place_sell_order,
    check_stop_losses,
    check_profit_targets,
    update_trailing_stop_losses,
    squareoff_all_intraday,
    check_max_daily_loss,
    attempt_broker_sl_recovery,  # F002: retry missing broker SL orders each loop
)
from core.regime_filter import is_bull_day as _regime_is_bull_day, is_bear_day as _regime_is_bear_day
from core.state_manager import (
    load_briefing,
    get_open_positions,
    entries_are_enabled,
    get_trading_session_state,
    has_completed_trade_today,
)
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
LOOKBACK              = 3  # Legacy fallback
LOOKBACK_BUY          = 3  # Lookback for BUY pattern detection
LOOKBACK_SELL         = 3  # Lookback for SELL pattern detection
MIN_WS_CANDLES_FOR_PATTERNS = 2
SCAN_LOG_INTERVAL     = 90

_failed_order_cooldown: dict[str, float] = {}
_last_scan_log: float = 0.0
FAILED_ORDER_COOLDOWN_SEC = 300
_yfinance_cache: dict[str, list] = {}
_yfinance_failed_until: dict[str, float] = {}
_yfinance_failure_reason: dict[str, str] = {}
YFINANCE_FAILURE_COOLDOWN_SEC = 300
_yahoo_failure_alerted = False  # F021: one-shot alert flag for Yahoo Finance outage
_briefing_cache: dict = None
_briefing_cache_time: float = 0.0
BRIEFING_CACHE_SECONDS = 60  # Reload from disk every 60 seconds
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}


def _get_briefing_cached():
    """Get briefing from cache, reload from disk every 60 seconds max.

    Also validates cached briefing for TEST_* sessions and resets
    cache if a test briefing is detected.
    """
    global _briefing_cache, _briefing_cache_time
    now = time.time()

    # Check if cache is still fresh
    if _briefing_cache is not None and (now - _briefing_cache_time) < BRIEFING_CACHE_SECONDS:
        # Validate cached content - reject TEST briefings
        session_type = _briefing_cache.get("session_type", "")
        if isinstance(session_type, str) and session_type.startswith("TEST"):
            logger.warning(f"[BRIEFING] Cache contains TEST briefing ({session_type}). Clearing cache.")
            _briefing_cache = None
            _briefing_cache_time = 0.0
        else:
            return _briefing_cache

    # Load or reload from disk
    _briefing_cache = load_briefing()
    _briefing_cache_time = now

    # F018 FIX: STALE / MISSING BRIEFING PROTECTION
    # Ensure the strategy loop never trades on yesterday's screener picks,
    # and never drops exit monitoring if the file is temporarily missing (e.g. during regeneration)
    is_fresh = False
    if _briefing_cache is not None:
        generated_at = _briefing_cache.get("generated_at")
        if generated_at:
            from datetime import datetime
            text = str(generated_at).strip()
            try:
                gen_date = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
                if gen_date == datetime.now().date():
                    is_fresh = True
            except ValueError:
                try:
                    gen_date = datetime.strptime(text[:10], "%Y-%m-%d").date()
                    if gen_date == datetime.now().date():
                        is_fresh = True
                except ValueError:
                    pass
        
    if not is_fresh:
        if _briefing_cache is None:
            logger.warning("[BRIEFING] Briefing missing/unreadable at runtime. Blocking new entries.")
        else:
            logger.warning("[BRIEFING] Stale briefing detected at runtime. Blocking new entries.")
            
        return {
            "session_type": "SAFE_FALLBACK",
            "market_bias": "NEUTRAL",
            "approved_stocks": [],
            "watchlist": [],
            "avoid_list": []
        }

    # Freshly loaded briefing bhi validate karo
    if _briefing_cache:
        session_type = _briefing_cache.get("session_type", "")
        if isinstance(session_type, str) and session_type.startswith("TEST"):
            logger.warning(f"[BRIEFING] Disk briefing is TEST ({session_type}). Ignoring.")
            _briefing_cache = None

    return _briefing_cache

def _apply_trading_settings():
    """Reload tunables from config/trading_settings.json (dashboard-editable)."""
    global STRATEGY_TYPE, LOOP_INTERVAL, MAX_POSITIONS, MIN_CONFIDENCE
    global MATH_RISK_PER_TRADE, LOOKBACK, LOOKBACK_BUY, LOOKBACK_SELL, MIN_WS_CANDLES_FOR_PATTERNS
    global SCAN_LOG_INTERVAL

    s = get_section("strategy")
    md = get_section("market_data")
    risk = get_section("risk")

    STRATEGY_TYPE         = s.get("strategy_type", "INTRADAY")
    LOOP_INTERVAL         = max(1, safe_int(s.get("loop_interval_sec"), 5))
    MAX_POSITIONS         = max(1, safe_int(s.get("max_open_positions"), 2))
    MIN_CONFIDENCE        = int(max(0, min(100, safe_float(s.get("min_confidence"), 70))))
    # Load new separate configs, fallback to legacy signal_lookback_candles for backward compat
    LOOKBACK_BUY          = max(1, safe_int(s.get("buy_signal_lookback_candles", s.get("signal_lookback_candles", 3)), 3))
    LOOKBACK_SELL         = max(1, safe_int(s.get("sell_signal_lookback_candles", s.get("signal_lookback_candles", 3)), 3))
    LOOKBACK              = max(LOOKBACK_BUY, LOOKBACK_SELL)  # Legacy support - use max of both
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


def _is_yfinance_on_cooldown(symbol: str) -> bool:
    retry_at = _yfinance_failed_until.get(symbol, 0.0)
    if retry_at > time.time():
        return True
    _yfinance_failed_until.pop(symbol, None)
    _yfinance_failure_reason.pop(symbol, None)
    return False


def _mark_yfinance_failed(symbol: str, reason: str) -> None:
    _yfinance_failed_until[symbol] = time.time() + YFINANCE_FAILURE_COOLDOWN_SEC
    _yfinance_failure_reason[symbol] = reason


# ════════════════════════════════════════════════════════════
#   UPSTOX CANDLE SOURCE  (replaces Yahoo Finance)
#
#   Uses Upstox v2 Historical Candle API:
#   GET https://api.upstox.com/v2/historical-candle/{key}/5minute/{to}/{from}
#
#   Instrument keys loaded from data/upstox_tokens.json.
#   UPSTOX_ACCESS_TOKEN from .env.
#
#   Drop-in replacement for _fetch_yahoo_history + _fetch_yfinance_with_retry.
#   Return format: pd.DataFrame with columns Open/High/Low/Close/Volume (TZ-naive IST).
# ════════════════════════════════════════════════════════════

_UPSTOX_TOKEN: str = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip("'\" ")
_UPSTOX_HEADERS: dict = {
    "Accept": "application/json",
    "Authorization": f"Bearer {_UPSTOX_TOKEN}",
}

# Instrument key cache: {"RELIANCE": "NSE_EQ|INE002A01018", ...}
_instrument_key_cache: dict[str, str] = {}
_instrument_key_cache_loaded: bool = False


def _load_instrument_keys() -> dict[str, str]:
    """Load instrument key map from data/upstox_tokens.json.
    Falls back to downloading from Upstox if file is missing or empty."""
    global _instrument_key_cache, _instrument_key_cache_loaded
    if _instrument_key_cache_loaded:
        return _instrument_key_cache

    tokens_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "upstox_tokens.json"
    )
    try:
        with open(tokens_path, "r") as f:
            import json as _json
            data = _json.load(f)
        if isinstance(data, dict) and data:
            _instrument_key_cache = {k.upper(): v for k, v in data.items()}
            _instrument_key_cache_loaded = True
            logger.info("[Upstox] Loaded %d instrument keys from tokens file.", len(_instrument_key_cache))
            return _instrument_key_cache
    except Exception as e:
        logger.warning("[Upstox] Could not load upstox_tokens.json: %s — will attempt live download.", e)

    # Fallback: download from Upstox instruments CSV
    try:
        import gzip
        from io import BytesIO
        resp = requests.get(
            "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        resp.raise_for_status()
        with gzip.open(BytesIO(resp.content), "rt") as f:
            import csv as _csv
            reader = _csv.DictReader(f)
            mapping: dict[str, str] = {}
            sym_col = None
            for row in reader:
                if sym_col is None:
                    sym_col = "tradingsymbol" if "tradingsymbol" in row else "trading_symbol"
                sym = str(row.get(sym_col, "")).replace("-EQ", "").upper()
                key = str(row.get("instrument_key", ""))
                if sym and key and "NSE" in key:
                    mapping[sym] = key
        _instrument_key_cache = mapping
        _instrument_key_cache_loaded = True
        logger.info("[Upstox] Downloaded %d instrument keys.", len(mapping))
    except Exception as e:
        logger.error("[Upstox] Instrument key download failed: %s", e)

    return _instrument_key_cache


def _get_upstox_instrument_key(symbol: str) -> str | None:
    """Resolve NSE symbol to Upstox instrument_key. e.g. 'RELIANCE' → 'NSE_EQ|INE002A01018'."""
    clean = symbol.upper().replace(".NS", "")
    keys  = _load_instrument_keys()
    return keys.get(clean)


def _fetch_upstox_history(symbol: str, days: int = 5, interval: str = "5minute") -> pd.DataFrame:
    """
    Fetch historical candles from Upstox API for indicator seeding.
    Returns pd.DataFrame with columns: Open, High, Low, Close, Volume (TZ-naive IST index).
    Raises RuntimeError on failure so callers can mark the symbol WAIT.
    """
    instrument_key = _get_upstox_instrument_key(symbol)
    if not instrument_key:
        raise RuntimeError(f"[Upstox] No instrument_key found for {symbol}. Check upstox_tokens.json.")

    from datetime import timedelta
    to_date   = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Upstox API v2 no longer natively supports "5minute", so we fetch 1minute and resample.
    api_interval = "1minute" if interval == "5minute" else interval

    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{api_interval}/{to_date}/{from_date}"
    try:
        response = requests.get(url, headers=_UPSTOX_HEADERS, timeout=10)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"[Upstox] Network error fetching {symbol}: {e}")

    if response.status_code == 401:
        raise RuntimeError(f"[Upstox] Auth error for {symbol} — check UPSTOX_ACCESS_TOKEN in .env")
    if response.status_code == 429:
        raise RuntimeError(f"[Upstox] Rate limited for {symbol}")
    if response.status_code != 200:
        raise RuntimeError(f"[Upstox] HTTP {response.status_code} for {symbol}")

    data = response.json()
    candles = (data.get("data") or {}).get("candles") or []
    if not candles:
        raise RuntimeError(f"[Upstox] Empty candle response for {symbol}")

    # Candle format: [timestamp, open, high, low, close, volume, oi]
    rows = []
    for c in candles:
        try:
            ts  = pd.to_datetime(c[0]).tz_convert("Asia/Kolkata").tz_localize(None)
            rows.append({
                "Open":   float(c[1]),
                "High":   float(c[2]),
                "Low":    float(c[3]),
                "Close":  float(c[4]),
                "Volume": float(c[5]),
                "_ts":    ts,
            })
        except Exception:
            continue

    if not rows:
        raise RuntimeError(f"[Upstox] Could not parse any candles for {symbol}")

    df = pd.DataFrame(rows).set_index("_ts").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df.dropna(subset=["Close"])
    
    # Resample to 5-minute if requested
    if interval == "5minute":
        df = df.resample("5min").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum"
        }).dropna(subset=["Close"])
        
    logger.info("[Upstox] %s — fetched %d %s candles", symbol, len(df), interval)
    return df


def _fetch_yfinance_with_retry(symbol: str, max_attempts: int = 2) -> pd.DataFrame:
    """
    MIGRATED: Now fetches from Upstox instead of Yahoo Finance.
    Kept same name for backward compatibility with all callers.
    Tries 5-day 5-minute data first; falls back to 7-day if empty.
    """
    for days in [5, 7]:
        try:
            df = _fetch_upstox_history(symbol, days=days, interval="5minute")
            if not df.empty:
                logger.info("✅ %s — Upstox chart fetched %d 5min candles (%dd window)", symbol, len(df), days)
                return df
        except RuntimeError as e:
            logger.debug("  %s Upstox attempt (days=%d) failed: %s", symbol, days, e)
            continue

    raise RuntimeError(
        f"{symbol}: Cannot bootstrap indicators — Upstox fetch failed for all window sizes."
    )


def _get_candles_with_yfinance_seed(symbol: str) -> list[dict]:
    """
    Get candles for indicator calculations.

    Priority:
    1. Use completed live WebSocket candles if enough are already available.
    2. Use cached Upstox seed data.
    3. Fetch fresh Upstox seed data with retries.

    No secondary market-data provider is used. If Upstox cannot provide data,
    the caller marks the symbol WAIT instead of placing a trade.
    """
    candles = get_candle_history(symbol)

    # If we have enough candles from WebSocket, we're good
    if len(candles) >= 26:
        _yfinance_cache.pop(symbol, None)  # Clear cache, we're self-sufficient
        return candles

    # If we have cached Upstox seed data, use it (deduplicate at stitch boundary)
    if symbol in _yfinance_cache:
        cached_seed = _yfinance_cache[symbol]
        ws_buckets  = {c["bucket"] for c in candles if c.get("bucket")}
        seed_unique = [c for c in cached_seed if c.get("bucket") not in ws_buckets]
        merged = seed_unique + candles
        if len(merged) >= 26:
            return merged

    # If Upstox fetch failed recently, avoid hammering it every strategy loop.
    if _is_yfinance_on_cooldown(symbol):
        return candles

    # Fetch fresh Upstox data
    hist = _fetch_yfinance_with_retry(symbol, max_attempts=2)

    if not hist.empty:
        hist = _drop_incomplete_candle_if_present(hist)

        # Convert to our candle format with bucket key for deduplication
        seed_candles = [
            {
                "bucket": ts.strftime("%Y-%m-%d %H:%M"),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row["Volume"]),
            }
            for ts, row in hist.iterrows()
        ]

        _yfinance_cache[symbol] = seed_candles

        ws_buckets  = {c["bucket"] for c in candles if c.get("bucket")}
        seed_unique = [c for c in seed_candles if c.get("bucket") not in ws_buckets]
        merged  = seed_unique + candles
        dropped = len(seed_candles) - len(seed_unique)
        logger.info(
            "📦 %s — Combined: %d Upstox + %d WebSocket = %d total candles "
            "(dropped %d duplicate candles at stitch boundary)",
            symbol, len(seed_unique), len(candles), len(merged), dropped,
        )
        return merged
    else:
        logger.critical(
            "❌ %s — Upstox returned empty DataFrame. Marking symbol WAIT.", symbol
        )
        raise RuntimeError(
            f"{symbol}: Upstox returned empty result. Cannot proceed without historical data for indicators."
        )



def _get_indicator_df(symbol: str) -> pd.DataFrame | None:
    """RSI / MACD / EMA / Bollinger — seeded from yfinance, updated by WebSocket."""
    global _yahoo_failure_alerted
    try:
        candles = _get_candles_with_yfinance_seed(symbol)
    except RuntimeError as exc:
        reason = str(exc)
        _mark_yfinance_failed(symbol, reason)
        logger.error("%s - yfinance unavailable; symbol marked WAIT. %s", symbol, reason)
        # F021: Fire a one-shot CRITICAL alert when Yahoo Finance is unavailable
        if not _yahoo_failure_alerted:
            _yahoo_failure_alerted = True
            try:
                from core.alerts import alert_critical
                alert_critical(
                    'Yahoo Finance data unavailable — no indicators can be computed. '
                    'New buy signals blocked. Software sell signals degraded.'
                )
            except Exception:
                pass
        return None

    if len(candles) < 26:
        return None
    df = pd.DataFrame(candles).astype({
        "open": float, "high": float,
        "low":  float, "close": float, "volume": float,
    })
    return _build_indicators(df)


def _get_pattern_df(symbol: str) -> pd.DataFrame | None:
    """Candlestick patterns — WebSocket-built candles only (real-time)."""
    ws_candles = get_candle_history(symbol, include_current=False)
    if len(ws_candles) < MIN_WS_CANDLES_FOR_PATTERNS:
        return None
    return pd.DataFrame(ws_candles).astype({
        "open": float, "high": float,
        "low":  float, "close": float, "volume": float,
    })


def _get_entry_price(symbol: str) -> tuple[float | None, str]:
    """
    Entry/exit price for broker orders — WebSocket LTP only.
    No alternate price source: wrong price means wrong order to Kotak.
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
    """Long upper shadow, small lower shadow AFTER bearish candle — bullish reversal."""
    if len(df) < 2:
        return False
    r = df.iloc[-1]
    body       = abs(r["close"] - r["open"])
    upper_wick = r["high"] - max(r["open"], r["close"])
    lower_wick = min(r["open"], r["close"]) - r["low"]
    if body == 0:
        return False
    shape_ok      = upper_wick >= (2 * body) and lower_wick <= (0.5 * body)
    prior_bearish = df.iloc[-2]["close"] < df.iloc[-2]["open"]
    return shape_ok and prior_bearish


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
    df["median_price"] = (df["high"] + df["low"]) / 2
    df["median_sma14"] = df["median_price"].rolling(14).mean()
    typical_price    = (df["high"] + df["low"] + df["close"]) / 3
    # Intraday VWAP must reset daily. Group by date.
    if "bucket" in df.columns:
        dt_series = pd.to_datetime(df["bucket"])
        df["vwap"] = (typical_price * df["volume"]).groupby(dt_series.dt.date).cumsum() / df["volume"].groupby(dt_series.dt.date).cumsum()
    elif isinstance(df.index, pd.DatetimeIndex):
        df["vwap"] = (typical_price * df["volume"]).groupby(df.index.date).cumsum() / df["volume"].groupby(df.index.date).cumsum()
    else:
        cumulative_vol = df["volume"].replace(0, pd.NA).cumsum()
        df["vwap"] = (typical_price * df["volume"]).cumsum() / cumulative_vol

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

    df["obv"]        = ta.volume.OnBalanceVolumeIndicator(close=df["close"], volume=df["volume"]).on_balance_volume()

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


def strategy_hammer(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    """Pattern on live WS candles; RSI/volume filter from yfinance indicator_df."""
    lookback = lookback if lookback is not None else LOOKBACK
    ind = indicator_df if indicator_df is not None else pattern_df
    hammer_found = False
    for i in _lookback_range(pattern_df, lookback):
        slice_df = pattern_df.iloc[: len(pattern_df) + i + 1]
        if detect_hammer(slice_df):
            hammer_found = True
            break

    if "rsi" in ind.columns:
        rsi_zone = ((ind["rsi"].iloc[-lookback:] > 20) &
                    (ind["rsi"].iloc[-lookback:] < 40)).any()
        latest_rsi = round(ind["rsi"].iloc[-1], 1)
    else:
        rsi_zone = True
        latest_rsi = 0

    if "avg_vol" in ind.columns:
        vol_ok = (ind["volume"].iloc[-lookback:] > ind["avg_vol"].iloc[-lookback:]).any()
    else:
        vol_ok = True

    fired = hammer_found and rsi_zone and vol_ok
    return _candle_strategy_result(
        "Hammer Reversal",
        pattern_hit=hammer_found,
        fired=fired,
        reason=f"Hammer={hammer_found}, RSI={latest_rsi}, Vol={vol_ok}",
    )


def strategy_bullish_engulfing(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    lookback = lookback if lookback is not None else LOOKBACK
    ind = indicator_df if indicator_df is not None else pattern_df
    engulf_found = False
    for i in _lookback_range(pattern_df, lookback):
        slice_df = pattern_df.iloc[: len(pattern_df) + i + 1]
        if detect_bullish_engulfing(slice_df):
            engulf_found = True
            break

    rsi_ok = ind["rsi"].iloc[-1] < 45 if "rsi" in ind.columns else True
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
    rsi_ok     = df["rsi"].iloc[-1] < 35
    latest_rsi = round(df["rsi"].iloc[-1], 1)
    return _indicator_strategy_result(
        "Bollinger Band Bounce",
        touched and bounced and rsi_ok,
        f"Touched={touched}, Bounced={bounced}, RSI={latest_rsi}",
    )


def strategy_volume_breakout(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    """Breakout on live candles; volume average from yfinance indicators."""
    lookback = lookback if lookback is not None else LOOKBACK
    ind = indicator_df if indicator_df is not None else pattern_df
    if "avg_vol" in ind.columns:
        avg_vol   = ind["avg_vol"].iloc[-lookback:]
        vol_spike = (ind["volume"].iloc[-lookback:] > avg_vol * 2.0).any()
    else:
        vol_spike = False

    price_brk = False
    for i in _lookback_range(pattern_df, lookback):
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


def strategy_inverted_hammer(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_inverted_hammer, lookback)
    return _candle_strategy_result("Inverted Hammer", pattern_hit=hit)


def strategy_dragonfly_doji(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_dragonfly_doji, lookback)
    return _candle_strategy_result("Dragonfly Doji", pattern_hit=hit)


def strategy_bullish_marubozu(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_bullish_marubozu, lookback)
    return _candle_strategy_result("Bullish Marubozu", pattern_hit=hit)


def strategy_piercing_line(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_piercing_line, lookback)
    return _candle_strategy_result("Piercing Line", pattern_hit=hit)


def strategy_bullish_harami(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_bullish_harami, lookback)
    return _candle_strategy_result("Bullish Harami", pattern_hit=hit)


def strategy_morning_star(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = _scan_pattern_in_lookback(pattern_df, detect_morning_star, lookback)
    return _candle_strategy_result("Morning Star", pattern_hit=hit)


def strategy_three_white_soldiers(pattern_df: pd.DataFrame, indicator_df: pd.DataFrame = None, lookback: int = None) -> dict:
    hit = detect_three_white_soldiers(pattern_df)
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


def _get_lookback_for_side(side: str) -> int:
    """Get the appropriate lookback window based on trade side (buy/sell)."""
    side_lower = str(side).lower().strip()
    if side_lower == "buy":
        return LOOKBACK_BUY
    elif side_lower == "sell":
        return LOOKBACK_SELL
    else:
        return LOOKBACK  # Fallback to max of both


# ════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class StrategyEvaluationContext:
    side: str
    indicator_df: pd.DataFrame
    pattern_df: pd.DataFrame | None = None
    ws_count: int = 0

    def get_lookback(self) -> int:
        """Get the appropriate lookback window based on this context's trade side."""
        return _get_lookback_for_side(self.side)


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
    fn: Callable[[pd.DataFrame, pd.DataFrame | None, int], dict],
) -> dict:
    if ctx.pattern_df is None:
        return _waiting_candle_strategy(name, ctx.ws_count)
    lookback = ctx.get_lookback()
    return fn(ctx.pattern_df, ctx.indicator_df, lookback)


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


def condition_obv_trending_up(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "obv" not in df.columns:
        return _indicator_strategy_result("OBV Trending Up", False, "OBV not ready")

    lookback = ctx.get_lookback()
    recent_obv = df["obv"].iloc[-lookback:]
    # Check if OBV is higher than it started in the lookback window
    started = recent_obv.iloc[0]
    ended = recent_obv.iloc[-1]
    trending = bool(ended > started)
    return _indicator_strategy_result(
        "OBV Trending Up",
        trending,
        f"OBV started={started:.0f}, ended={ended:.0f}",
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


def condition_close_1_below_ema21(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    # iloc[-1] is current live candle, iloc[-2] is the last completed candle (Close(1))
    if len(df) < 2 or "ema21" not in df.columns or pd.isna(df["ema21"].iloc[-2]):
        return _indicator_strategy_result("Close(1) < EMA21", False, "Indicators not ready")
    return _indicator_strategy_result(
        "Close(1) < EMA21",
        df["close"].iloc[-2] < df["ema21"].iloc[-2],
        f"Close(1): {df['close'].iloc[-2]:.2f}, EMA21: {df['ema21'].iloc[-2]:.2f}"
    )


def condition_price_above_vwap(ctx: StrategyEvaluationContext) -> dict:
    return _price_above_column_condition(ctx.indicator_df, "vwap", "Price above VWAP")


def condition_price_above_vwap_closed(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "close" not in df.columns or "vwap" not in df.columns or pd.isna(df["close"].iloc[-2]) or pd.isna(df["vwap"].iloc[-2]):
        return _indicator_strategy_result("Close(Closed) > VWAP", False, "Indicators not ready")
    close_2 = df["close"].iloc[-2]
    vwap_2 = df["vwap"].iloc[-2]
    return _indicator_strategy_result(
        "Close(Closed) > VWAP",
        bool(close_2 > vwap_2),
        f"Close(Closed)={close_2:.2f}, VWAP={vwap_2:.2f}"
    )


def condition_ema20_above_vwap(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "ema20" not in df.columns or "vwap" not in df.columns or pd.isna(df["ema20"].iloc[-2]) or pd.isna(df["vwap"].iloc[-2]):
        return _indicator_strategy_result("EMA20 > VWAP", False, "Indicators not ready")
    
    ema20 = df["ema20"].iloc[-2]
    vwap = df["vwap"].iloc[-2]
    return _indicator_strategy_result(
        "EMA20 > VWAP",
        bool(ema20 > vwap),
        f"EMA20(Closed)={ema20:.2f}, VWAP={vwap:.2f}"
    )


def condition_price_breakout_10(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 12 or "close" not in df.columns or "high" not in df.columns:
        return _indicator_strategy_result("Close > Highest(High, 10)", False, "Not enough data")
    
    # Evaluate on the closed candle
    closed_close = df["close"].iloc[-2]
    # Highest high of the 10 candles BEFORE the closed candle
    highest_10 = df["high"].iloc[-12:-2].max()
    
    return _indicator_strategy_result(
        "Close > Highest(High, 10)",
        bool(closed_close > highest_10),
        f"Close(Closed)={closed_close:.2f}, Highest_10={highest_10:.2f}"
    )


def condition_rsi_above_60(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns or pd.isna(df["rsi"].iloc[-2]):
        return _indicator_strategy_result("RSI > 60", False, "RSI not ready")
    
    rsi = df["rsi"].iloc[-2]
    return _indicator_strategy_result(
        "RSI > 60",
        bool(rsi > 60),
        f"RSI(Closed)={rsi:.2f}"
    )


def condition_streak_close_1_above_vwap_0(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "close" not in df.columns or "vwap" not in df.columns or pd.isna(df["close"].iloc[-2]) or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("Close(1) > VWAP(0)", False, "Indicators not ready")
    close_1 = df["close"].iloc[-2]
    vwap_0 = df["vwap"].iloc[-1]
    return _indicator_strategy_result(
        "Close(1) > VWAP(0)",
        bool(close_1 > vwap_0),
        f"Close(1)={close_1:.2f}, VWAP(0)={vwap_0:.2f}"
    )


def condition_streak_close_0_above_period_max_10(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 12 or "close" not in df.columns or "high" not in df.columns:
        return _indicator_strategy_result("Close(0) > Max(High(-1), 10)", False, "Not enough data")
    
    close_0 = df["close"].iloc[-1]
    # Highest high of 10 candles ending at the previous candle (-1)
    # The previous candle is at index -2. 10 candles before it is -12 to -2
    highest_10_prev = df["high"].iloc[-12:-2].max()
    
    return _indicator_strategy_result(
        "Close(0) > Max(High(-1), 10)",
        bool(close_0 > highest_10_prev),
        f"Close(0)={close_0:.2f}, Max_High={highest_10_prev:.2f}"
    )


def condition_pullback_to_ema20_rejection(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 4 or "ema20" not in df.columns or "low" not in df.columns or "close" not in df.columns:
        return _indicator_strategy_result("EMA20 Pullback Reject", False, "Not ready")
    
    # Low touched or dipped below EMA20 in the last 3 candles
    recent_lows = df["low"].iloc[-4:-1]
    recent_emas = df["ema20"].iloc[-4:-1]
    touched_ema = bool((recent_lows <= recent_emas).any())
    
    # Current close is above EMA20
    closed_above = df["close"].iloc[-1] > df["ema20"].iloc[-1]
    
    fired = touched_ema and closed_above
    return _indicator_strategy_result("EMA20 Pullback Reject", fired, f"Touched: {touched_ema}, Closed Above: {closed_above}")


def condition_close_above_open(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "open" not in df.columns:
        return _indicator_strategy_result("Close > Open", False, "Not ready")
    
    close_val = df["close"].iloc[-1]
    open_val = df["open"].iloc[-1]
    fired = close_val > open_val
    return _indicator_strategy_result("Close > Open", bool(fired), f"Close={close_val:.2f}, Open={open_val:.2f}")


def condition_rsi_cooled_down(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 4 or "rsi" not in df.columns:
        return _indicator_strategy_result("RSI Cooled Down", False, "Not ready")
    
    # RSI dipped below 55 recently, but is now ticking up
    recent_rsi = df["rsi"].iloc[-4:-1]
    dipped = bool((recent_rsi <= 55).any())
    ticking_up = df["rsi"].iloc[-1] > df["rsi"].iloc[-2]
    
    fired = dipped and ticking_up
    return _indicator_strategy_result("RSI Cooled Down", fired, f"Dipped<55: {dipped}, Ticking Up: {ticking_up}")


def condition_volume_surge_3x(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "volume" not in df.columns or "avg_vol" not in df.columns:
        return _indicator_strategy_result("Volume 3x Surge", False, "Not ready")
    
    vol = df["volume"].iloc[-1]
    avg_vol = df["avg_vol"].iloc[-1]
    if pd.isna(avg_vol) or avg_vol == 0:
        return _indicator_strategy_result("Volume 3x Surge", False, "Avg Vol is 0")
        
    surge = vol > (avg_vol * 3.0)
    return _indicator_strategy_result("Volume 3x Surge", bool(surge), f"Vol: {vol}, Avg: {avg_vol}")


def condition_macd_hist_rejection_bounce(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 4 or "macd" not in df.columns or "macd_signal" not in df.columns:
        return _indicator_strategy_result("MACD Hist Reject", False, "Not ready")
    
    hist = df["macd"] - df["macd_signal"]
    # hist(0) > hist(1), hist(1) < hist(2) -> It was dropping but bounced
    hist_0 = hist.iloc[-1]
    hist_1 = hist.iloc[-2]
    hist_2 = hist.iloc[-3]
    
    bounced = (hist_0 > hist_1) and (hist_1 < hist_2)
    above_zero = hist_0 > 0
    
    fired = bounced and above_zero
    return _indicator_strategy_result("MACD Hist Reject", bool(fired), f"Bounced: {bounced}, >0: {above_zero}")


def condition_streak_ema20_1_above_vwap_0(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "ema20" not in df.columns or "vwap" not in df.columns or pd.isna(df["ema20"].iloc[-2]) or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("EMA20(1) > VWAP(0)", False, "Indicators not ready")
    ema20_1 = df["ema20"].iloc[-2]
    vwap_0 = df["vwap"].iloc[-1]
    return _indicator_strategy_result(
        "EMA20(1) > VWAP(0)",
        bool(ema20_1 > vwap_0),
        f"EMA20(1)={ema20_1:.2f}, VWAP(0)={vwap_0:.2f}"
    )


def condition_streak_rsi_1_above_61(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns or pd.isna(df["rsi"].iloc[-2]):
        return _indicator_strategy_result("RSI(1) > 61", False, "Indicators not ready")
    rsi_1 = df["rsi"].iloc[-2]
    return _indicator_strategy_result(
        "RSI(1) > 61",
        bool(rsi_1 > 61),
        f"RSI(1)={rsi_1:.2f}"
    )


def condition_streak_close_0_above_period_max_10(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 12 or "close" not in df.columns or "high" not in df.columns:
        return _indicator_strategy_result("Close(0) > Max(High(-1), 10)", False, "Not enough data")
    
    close_0 = df["close"].iloc[-1]
    # Highest high of 10 candles ending at the previous candle (-1)
    # The previous candle is at index -2. 10 candles before it is -12 to -2
    highest_10_prev = df["high"].iloc[-12:-2].max()
    
    return _indicator_strategy_result(
        "Close(0) > Max(High(-1), 10)",
        bool(close_0 > highest_10_prev),
        f"Close(0)={close_0:.2f}, Max_High={highest_10_prev:.2f}"
    )



def condition_median_price_above_vwap(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "median_sma14" not in df.columns or "vwap" not in df.columns or pd.isna(df["median_sma14"].iloc[-1]) or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("Median Price > VWAP", False, "Indicators not ready")
    return _indicator_strategy_result(
        "Median Price > VWAP",
        df["median_sma14"].iloc[-1] > df["vwap"].iloc[-1],
        f"Median SMA14: {df['median_sma14'].iloc[-1]:.2f}, VWAP: {df['vwap'].iloc[-1]:.2f}"
    )


def condition_median_price_above_ema20(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "median_sma14" not in df.columns or "ema20" not in df.columns or pd.isna(df["median_sma14"].iloc[-1]) or pd.isna(df["ema20"].iloc[-1]):
        return _indicator_strategy_result("Median Price > EMA20", False, "Indicators not ready")
    return _indicator_strategy_result(
        "Median Price > EMA20",
        df["median_sma14"].iloc[-1] > df["ema20"].iloc[-1],
        f"Median SMA14: {df['median_sma14'].iloc[-1]:.2f}, EMA20: {df['ema20'].iloc[-1]:.2f}"
    )


def condition_prev_close_above_ema20(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "ema20" not in df.columns or pd.isna(df["ema20"].iloc[-1]):
        return _indicator_strategy_result("Prev Close > EMA20", False, "Indicators not ready")
    return _indicator_strategy_result(
        "Prev Close > EMA20",
        df["close"].iloc[-2] > df["ema20"].iloc[-1],
        f"Prev Close: {df['close'].iloc[-2]:.2f}, EMA20: {df['ema20'].iloc[-1]:.2f}"
    )


def condition_test_trigger(ctx: StrategyEvaluationContext) -> dict:
    return _indicator_strategy_result("Test Trigger", True, "Forced true for testing purposes")


def condition_price_below_vwap(ctx: StrategyEvaluationContext) -> dict:
    return _price_below_column_condition(ctx.indicator_df, "vwap", "Price below VWAP")


def condition_rsi_crosses_35_up(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns:
        return _indicator_strategy_result("RSI Crosses 35 Up", False, "RSI not ready")

    crossed = df["rsi"].iloc[-2] <= 35 and df["rsi"].iloc[-1] > 35
    latest = round(float(df["rsi"].iloc[-1]), 1)
    return _indicator_strategy_result("RSI Crosses 35 Up", crossed, f"RSI={latest}, Crossed={crossed}")


def condition_two_green_candles(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2:
        return _candle_strategy_result("Two Green Candles", False, False, "Not enough candles")

    c1 = df.iloc[-2]
    c2 = df.iloc[-1]
    green1 = c1["close"] > c1["open"]
    green2 = c2["close"] > c2["open"]
    fired = green1 and green2
    return _candle_strategy_result("Two Green Candles", fired, fired, f"G1={green1}, G2={green2}")


def condition_rsi_recovering(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns:
        return _indicator_strategy_result("RSI recovering", False, "RSI not ready")

    recent_oversold = bool((df["rsi"].iloc[-LOOKBACK:] < 45).fillna(False).any())
    rising = df["rsi"].iloc[-1] > df["rsi"].iloc[-2]
    latest = round(float(df["rsi"].iloc[-1]), 1)
    fired = recent_oversold and rising and latest < 65
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
    if ctx.pattern_df is None or ctx.pattern_df.empty:
        return _indicator_strategy_result("Bearish Pattern", False, "Not enough pattern data")
    return strategy_sell_bearish_engulfing(ctx.pattern_df)


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


# ──────────────────────────────────────────────────────────────
#  NEW CONDITION 1: rsi_not_overbought
#  Purpose : Buy-side gate. Ensures RSI has headroom before
#            entering a breakout. Filters buying into an already
#            extended / exhausted move.
#  Threshold: RSI < 65  (not the full 70 overbought — we want
#             a safety margin before the sell zone)
#  Used in : BUY_BREAKOUT_CONFIRMATION
# ──────────────────────────────────────────────────────────────
def condition_rsi_not_overbought(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "rsi" not in df.columns:
        return _indicator_strategy_result("RSI not overbought", False, "RSI not ready")
    latest = df["rsi"].iloc[-1]
    if pd.isna(latest):
        return _indicator_strategy_result("RSI not overbought", False, "RSI not ready")
    latest_f = round(float(latest), 1)
    fired = latest_f < 70.0
    return _indicator_strategy_result(
        "RSI not overbought",
        fired,
        f"RSI={latest_f} {'< 70 ✓' if fired else '>= 70 — too extended'}",
    )


# ──────────────────────────────────────────────────────────────
#  NEW CONDITION 2: macd_positive
#  Purpose : Confirms bullish momentum regime. MACD line above
#            zero means the 12-EMA is above the 26-EMA globally —
#            the macro momentum is bullish. Prevents buying
#            into a dead-cat bounce where MACD is still negative.
#  Used in : BUY_EMA_VOLUME_MOMENTUM, BUY_MACD_VWAP_SURGE
# ──────────────────────────────────────────────────────────────
def condition_macd_positive(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "macd" not in df.columns:
        return _indicator_strategy_result("MACD positive", False, "MACD not ready")
    latest = df["macd"].iloc[-1]
    if pd.isna(latest):
        return _indicator_strategy_result("MACD positive", False, "MACD not ready")
    latest_f = float(latest)
    fired = latest_f > 0.0
    return _indicator_strategy_result(
        "MACD positive",
        fired,
        f"MACD={round(latest_f, 4)} {'> 0 ✓' if fired else '<= 0 — bearish regime'}",
    )


# ──────────────────────────────────────────────────────────────
#  NEW CONDITION 3: ema_trending_up
#  Purpose : Confirms active uptrend structure. EMA9 above EMA21
#            means the short-term average is above the medium-term
#            average — price is in a confirmed upward trajectory.
#            Pairs with rsi_recovering to form the "buy the dip
#            in an uptrend" strategy.
#  NOTE    : This is a STATE condition (is trend up right now?),
#            unlike ema_9_21_crossover which detects the EVENT
#            (did it just cross in the last N candles?).
#  Used in : BUY_EMA_TREND_PULLBACK
# ──────────────────────────────────────────────────────────────
def condition_ema_trending_up(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "ema9" not in df.columns or "ema21" not in df.columns:
        return _indicator_strategy_result("EMA trending up", False, "EMA not ready")
    ema9 = df["ema9"].iloc[-1]
    ema21 = df["ema21"].iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return _indicator_strategy_result("EMA trending up", False, "EMA not ready")
    ema9_f = round(float(ema9), 2)
    ema21_f = round(float(ema21), 2)
    fired = ema9_f > ema21_f
    return _indicator_strategy_result(
        "EMA trending up",
        fired,
        f"EMA9={ema9_f}, EMA21={ema21_f} — {'uptrend ✓' if fired else 'downtrend / flat'}",
    )


# ──────────────────────────────────────────────────────────────
#  NEW CONDITION 4: ema9_below_ema21
#  Purpose : Sell-side counterpart of ema_trending_up.
#            EMA9 crossing below EMA21 signals trend structure
#            has flipped bearish. Used for structural exit signals.
#  NOTE    : This is also a STATE condition — fires whenever
#            EMA9 is below EMA21, not just at the moment of cross.
#            Pair with price_below_vwap for SELL_EMA_CROSS_WEAKNESS.
#  Used in : SELL_EMA_CROSS_WEAKNESS
# ──────────────────────────────────────────────────────────────
def condition_ema9_below_ema21(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if "ema9" not in df.columns or "ema21" not in df.columns:
        return _indicator_strategy_result("EMA9 below EMA21", False, "EMA not ready")
    ema9 = df["ema9"].iloc[-1]
    ema21 = df["ema21"].iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return _indicator_strategy_result("EMA9 below EMA21", False, "EMA not ready")
    ema9_f = round(float(ema9), 2)
    ema21_f = round(float(ema21), 2)
    fired = ema9_f < ema21_f
    return _indicator_strategy_result(
        "EMA9 below EMA21",
        fired,
        f"EMA9={ema9_f}, EMA21={ema21_f} — {'bearish structure ✓' if fired else 'still bullish'}",
    )


# ── SHORT SELLING CONDITIONS ─────────────────────────────────────────────────
# Mirror of BUY_STREAK_MOMENTUM_BREAKOUT — same strictness, downside direction.
# Used by SHORT_STREAK_MOMENTUM_BREAKDOWN (entry) and SHORT_STREAK_MOMENTUM_RECOVERY (cover).


def condition_streak_close_1_below_vwap_0(ctx: StrategyEvaluationContext) -> dict:
    """STATE — Close(1) < VWAP(0): Previous closed candle below VWAP (mirror of close_1_above_vwap_0)"""
    df = ctx.indicator_df
    if len(df) < 2 or "close" not in df.columns or "vwap" not in df.columns or pd.isna(df["close"].iloc[-2]) or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("Close(1) < VWAP(0)", False, "Indicators not ready")
    close_1 = df["close"].iloc[-2]
    vwap_0  = df["vwap"].iloc[-1]
    return _indicator_strategy_result(
        "Close(1) < VWAP(0)",
        bool(close_1 < vwap_0),
        f"Close(1)={close_1:.2f}, VWAP(0)={vwap_0:.2f}"
    )


def condition_streak_ema20_1_below_vwap_0(ctx: StrategyEvaluationContext) -> dict:
    """STATE — EMA20(1) < VWAP(0): Previous EMA20 below VWAP (mirror of ema20_1_above_vwap_0)"""
    df = ctx.indicator_df
    if len(df) < 2 or "ema20" not in df.columns or "vwap" not in df.columns or pd.isna(df["ema20"].iloc[-2]) or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("EMA20(1) < VWAP(0)", False, "Indicators not ready")
    ema20_1 = df["ema20"].iloc[-2]
    vwap_0  = df["vwap"].iloc[-1]
    return _indicator_strategy_result(
        "EMA20(1) < VWAP(0)",
        bool(ema20_1 < vwap_0),
        f"EMA20(1)={ema20_1:.2f}, VWAP(0)={vwap_0:.2f}"
    )


def condition_streak_rsi_1_below_39(ctx: StrategyEvaluationContext) -> dict:
    """STATE — RSI(1) < 39: Previous candle RSI below 39 (mirror of rsi_1_above_61)"""
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns or pd.isna(df["rsi"].iloc[-2]):
        return _indicator_strategy_result("RSI(1) < 39", False, "Indicators not ready")
    rsi_1 = df["rsi"].iloc[-2]
    return _indicator_strategy_result(
        "RSI(1) < 39",
        bool(rsi_1 < 39),
        f"RSI(1)={rsi_1:.2f}"
    )


def condition_close_0_above_ema9(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(0) > EMA(9): Short engine dynamic bail-out (Savior Exit)."""
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "ema9" not in df.columns or pd.isna(df["ema9"].iloc[-1]):
        return _indicator_strategy_result("Close(0) > EMA(9)", False, "Indicators not ready")
    close_0 = df["close"].iloc[-1]
    ema9_0 = df["ema9"].iloc[-1]
    return _indicator_strategy_result(
        "Close(0) > EMA(9)",
        bool(close_0 > ema9_0),
        f"Close(0)={close_0:.2f}, EMA9(0)={ema9_0:.2f}"
    )

def condition_streak_close_0_below_period_min_10(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(0) < Min(Low(-1), 10): Current candle broke 10-period low (mirror of close_0_above_period_max_10).
    This is the GATEKEEPER — same strictness as HIGH break for longs."""
    df = ctx.indicator_df
    if len(df) < 12 or "close" not in df.columns or "low" not in df.columns:
        return _indicator_strategy_result("Close(0) < Min(Low(-1), 10)", False, "Not enough data")
    close_0 = df["close"].iloc[-1]
    # Lowest low of 10 candles ending at the previous candle (-1)
    # The previous candle is at index -2. 10 candles before it is -11 to -1
    lowest_10_prev = df["low"].iloc[-11:-1].min()
    return _indicator_strategy_result(
        "Close(0) < Min(Low(-1), 10)",
        bool(close_0 < lowest_10_prev),
        f"Close(0)={close_0:.2f}, Min_Low={lowest_10_prev:.2f}"
    )


def condition_streak_close_0_not_reversing(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(0) <= High(-1): Current candle is not reversing upward against the short trend."""
    df = ctx.indicator_df
    if len(df) < 2 or "close" not in df.columns or "high" not in df.columns:
        return _indicator_strategy_result("Close(0) <= High(-1)", False, "Not enough data")
    close_0 = df["close"].iloc[-1]
    high_1 = df["high"].iloc[-2]
    return _indicator_strategy_result(
        "Close(0) <= High(-1)",
        bool(close_0 <= high_1),
        f"Close(0)={close_0:.2f}, High(-1)={high_1:.2f}"
    )


def condition_streak_close_0_near_vwap(ctx: StrategyEvaluationContext) -> dict:
    """STATE — Close(0) >= VWAP(0) * 0.988: Block late entries when price is too stretched from VWAP."""
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "vwap" not in df.columns or pd.isna(df["vwap"].iloc[-1]):
        return _indicator_strategy_result("Close(0) >= VWAP(0) * 0.988", False, "Indicators not ready")
    close_0 = df["close"].iloc[-1]
    vwap_0 = df["vwap"].iloc[-1]
    limit_price = vwap_0 * 0.988
    return _indicator_strategy_result(
        "Close(0) >= VWAP(0) * 0.988",
        bool(close_0 >= limit_price),
        f"Close(0)={close_0:.2f}, Limit={limit_price:.2f}"
    )


def condition_close_1_above_ema21(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(1) rose above EMA21: Short cover signal (mirror of close_1_below_ema21 for longs).
    Fires when the previous closed candle crossed above EMA21 — momentum recovering upward."""
    df = ctx.indicator_df
    if len(df) < 3 or "close" not in df.columns or "ema21" not in df.columns or pd.isna(df["ema21"].iloc[-1]):
        return _indicator_strategy_result("Close(1) crossed above EMA21", False, "Indicators not ready")
    close_1     = df["close"].iloc[-2]
    close_2     = df["close"].iloc[-3]
    ema21_now   = df["ema21"].iloc[-1]
    # Crossed: prev-prev was below EMA21, prev closed above EMA21
    crossed_up  = (close_2 <= ema21_now) and (close_1 > ema21_now)
    return _indicator_strategy_result(
        "Close(1) crossed above EMA21",
        bool(crossed_up),
        f"Close(1)={close_1:.2f}, Close(2)={close_2:.2f}, EMA21={ema21_now:.2f}"
    )


# ── END SHORT SELLING CONDITIONS ──────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════
#   R7_COMB_486 CONDITIONS — VARIANT_D Long Entry + EMA50 Exit
#
#   These conditions implement the research-validated R7_COMB_486
#   strategy (272.71% net return, Jan 2024 - Jun 2026).
#
#   Long Entry (VARIANT_D) logic:
#     Price < VWAP           → stock is in a dip (buy the dip)
#     EMA9 < EMA21           → short-term bearish structure (buying reversal)
#     Close > Close(-1)      → current candle is green (reversal trigger)
#     Close < EMA9           → entry is still at the bottom (not chasing)
#     RSI rising AND RSI<50  → momentum turning up from oversold zone
#
#   Dynamic Exit (EMA50):
#     Close < EMA50          → support lost, exit remaining position
#     (min_hold_time enforced separately in order_executor.py)
# ══════════════════════════════════════════════════════════════

def condition_r7_price_below_vwap(ctx: StrategyEvaluationContext) -> dict:
    """STATE — Close(0) < VWAP(0): Price is in an intraday dip below VWAP.
    R7 VARIANT_D buys the pullback, not the breakout."""
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "vwap" not in df.columns:
        return _indicator_strategy_result("R7: Price below VWAP", False, "Indicators not ready")
    close_0 = df["close"].iloc[-1]
    vwap_0  = df["vwap"].iloc[-1]
    if pd.isna(close_0) or pd.isna(vwap_0):
        return _indicator_strategy_result("R7: Price below VWAP", False, "Indicators not ready")
    fired = bool(close_0 < vwap_0)
    return _indicator_strategy_result(
        "R7: Price below VWAP",
        fired,
        f"Close={close_0:.2f}, VWAP={vwap_0:.2f} — {'dip ✓' if fired else 'above VWAP — not a dip'}",
    )


def condition_r7_ema9_below_ema21(ctx: StrategyEvaluationContext) -> dict:
    """STATE — EMA9(0) < EMA21(0): Short-term trend structure is bearish.
    R7 VARIANT_D specifically buys reversals in a weak trend, not confirmed uptrends."""
    df = ctx.indicator_df
    if "ema9" not in df.columns or "ema21" not in df.columns:
        return _indicator_strategy_result("R7: EMA9 < EMA21", False, "EMA not ready")
    ema9 = df["ema9"].iloc[-1]
    ema21 = df["ema21"].iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return _indicator_strategy_result("R7: EMA9 < EMA21", False, "EMA not ready")
    fired = bool(float(ema9) < float(ema21))
    return _indicator_strategy_result(
        "R7: EMA9 < EMA21",
        fired,
        f"EMA9={ema9:.2f}, EMA21={ema21:.2f} — {'weak structure (buy dip) ✓' if fired else 'uptrend — not a dip entry'}",
    )


def condition_r7_green_reversal_candle(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(0) > Close(-1): Current candle is green (bullish reversal signal).
    This is the primary EVENT trigger for R7 VARIANT_D — the actual reversal bar."""
    df = ctx.indicator_df
    if len(df) < 2 or "close" not in df.columns:
        return _indicator_strategy_result("R7: Green reversal candle", False, "Not enough data")
    close_0 = df["close"].iloc[-1]
    close_1 = df["close"].iloc[-2]
    if pd.isna(close_0) or pd.isna(close_1):
        return _indicator_strategy_result("R7: Green reversal candle", False, "Not enough data")
    fired = bool(float(close_0) > float(close_1))
    return _indicator_strategy_result(
        "R7: Green reversal candle",
        fired,
        f"Close(0)={close_0:.2f}, Close(-1)={close_1:.2f} — {'green ✓' if fired else 'red — no reversal'}",
    )


def condition_r7_close_below_ema9(ctx: StrategyEvaluationContext) -> dict:
    """STATE — Close(0) < EMA9(0): Entry is still at the bottom, not chasing a fast move.
    Prevents entering after a fast spike that already bounced through EMA9."""
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "ema9" not in df.columns:
        return _indicator_strategy_result("R7: Close below EMA9", False, "Indicators not ready")
    close_0 = df["close"].iloc[-1]
    ema9_0  = df["ema9"].iloc[-1]
    if pd.isna(close_0) or pd.isna(ema9_0):
        return _indicator_strategy_result("R7: Close below EMA9", False, "Indicators not ready")
    fired = bool(float(close_0) < float(ema9_0))
    return _indicator_strategy_result(
        "R7: Close below EMA9",
        fired,
        f"Close={close_0:.2f}, EMA9={ema9_0:.2f} — {'entry at bottom ✓' if fired else 'above EMA9 — chasing'}",
    )


def condition_r7_rsi_recovering_oversold(ctx: StrategyEvaluationContext) -> dict:
    """SEMI — RSI rising (RSI(0) > RSI(-1)) AND RSI(0) < 50: Momentum turning up from oversold.
    RSI must be below 50 (oversold zone) AND currently rising — confirming bottom formation."""
    df = ctx.indicator_df
    if len(df) < 2 or "rsi" not in df.columns:
        return _indicator_strategy_result("R7: RSI recovering oversold", False, "RSI not ready")
    rsi_0 = df["rsi"].iloc[-1]
    rsi_1 = df["rsi"].iloc[-2]
    if pd.isna(rsi_0) or pd.isna(rsi_1):
        return _indicator_strategy_result("R7: RSI recovering oversold", False, "RSI not ready")
    rsi_rising   = bool(float(rsi_0) > float(rsi_1))
    rsi_oversold = bool(float(rsi_0) < 50.0)
    fired = rsi_rising and rsi_oversold
    return _indicator_strategy_result(
        "R7: RSI recovering oversold",
        fired,
        f"RSI={rsi_0:.1f} (prev={rsi_1:.1f}) — rising={rsi_rising}, oversold(<50)={rsi_oversold}",
    )


def condition_r7_close_below_ema50(ctx: StrategyEvaluationContext) -> dict:
    """EVENT — Close(0) < EMA50(0): Price dropped below EMA50 support.
    R7 dynamic exit trigger. min_hold_time (20 candles) is enforced in order_executor.py
    before this condition is acted upon."""
    df = ctx.indicator_df
    if len(df) < 1 or "close" not in df.columns or "ema50" not in df.columns:
        return _indicator_strategy_result("R7: Close below EMA50", False, "Indicators not ready")
    close_0 = df["close"].iloc[-1]
    ema50_0 = df["ema50"].iloc[-1]
    if pd.isna(close_0) or pd.isna(ema50_0):
        return _indicator_strategy_result("R7: Close below EMA50", False, "EMA50 not ready")
    fired = bool(float(close_0) < float(ema50_0))
    return _indicator_strategy_result(
        "R7: Close below EMA50",
        fired,
        f"Close={close_0:.2f}, EMA50={ema50_0:.2f} — {'exit signal ✓' if fired else 'still above EMA50'}",
    )


# ── END R7_COMB_486 CONDITIONS ────────────────────────────────────────────────


CONDITION_REGISTRY: dict[str, StrategyConditionFn] = {}
for _name, _func in list(globals().items()):
    if _name.startswith("condition_") and callable(_func):
        _key = _name.replace("condition_", "")
        CONDITION_REGISTRY[_key] = _func


class StrategySetEvaluator:
    def __init__(self, condition_registry: dict[str, StrategyConditionFn]):
        self.condition_registry = condition_registry

    def evaluate(self, side: str, ctx: StrategyEvaluationContext) -> dict | None:
        config = load_strategy_sets()
        set_defs = config.buy_sets if side == "buy" else config.sell_sets

        for set_def in set_defs:
            if not _strategy_set_enabled(set_def.name):
                continue
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


def _calculate_adaptive_stop_loss(symbol: str, price: float, direction: str = "LONG") -> float:
    price = safe_float(price, 0.0)
    if direction == "LONG":
        pct = _clamp(safe_float(cfg("risk", "long_stop_loss_percent", 0.008), 0.008), 0.0001, 0.20)
    else:
        pct = _clamp(safe_float(cfg("risk", "short_stop_loss_percent", 0.005), 0.005), 0.0001, 0.20)
        
    pct *= _adaptive_stop_loss_multiplier(symbol)
    
    if direction == "LONG":
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


def _get_strategy_set_execution_policy(set_name: str) -> dict:
    def _attach_blocking_config(policy: dict) -> dict:
        policy = dict(policy or {})
        blocking_enabled = _adaptive_safety_block_enabled()
        policy["adaptive_safety_blocking_enabled"] = blocking_enabled
        policy["execution_blocked_by_adaptive_safety"] = bool(
            policy.get("suppressed") and blocking_enabled
        )
        return policy

    try:
        from reflection.reflection_engine import get_signal_execution_policy
        return _attach_blocking_config(get_signal_execution_policy(set_name))
    except Exception as exc:
        logger.error("Signal execution policy unavailable for %s: %s", set_name, exc)
        return _attach_blocking_config({
            "signal_name": set_name,
            "suppressed": False,
            "reason": f"Execution policy unavailable: {exc}",
            "scope": "error",
        })


_gap_cache: dict[str, tuple[datetime.date, float]] = {}

def _get_daily_gap(symbol: str, df: pd.DataFrame) -> float:
    today = datetime.now().date()
    if symbol in _gap_cache and _gap_cache[symbol][0] == today:
        return _gap_cache[symbol][1]
    
    try:
        dates = pd.to_datetime(df["bucket"]).dt.date
        unique_dates = pd.Series(dates).unique()
        if len(unique_dates) < 2:
            return 0.0
        
        today_mask = (dates == unique_dates[-1])
        today_open = float(df[today_mask]["open"].iloc[0])
        
        prev_mask = (dates == unique_dates[-2])
        prev_close = float(df[prev_mask]["close"].iloc[-1])
        
        if prev_close <= 0:
            return 0.0
            
        gap = (today_open - prev_close) / prev_close
        _gap_cache[symbol] = (today, gap)
        return gap
    except Exception as e:
        logger.warning(f"Failed to calculate daily gap for {symbol}: {e}")
        return 0.0


def _adaptive_safety_block_enabled() -> bool:
    return bool(cfg("risk", "adaptive_safety_blocks_execution", False))


def _strategy_set_enabled(set_name: str) -> bool:
    settings = get_section("strategy_sets")
    if not isinstance(settings, dict):
        return True
    if set_name in settings:
        return bool(settings.get(set_name))
    normalized = normalize_set_key(set_name)
    if normalized in settings:
        return bool(settings.get(normalized))
    return True


def _suppression_rejection(triggered_set: dict, symbol: str, action: str = "BUY") -> dict | None:
    policy = _get_strategy_set_execution_policy(triggered_set["set_name"])
    if not policy.get("suppressed"):
        return None

    if not policy.get("adaptive_safety_blocking_enabled"):
        logger.info(
            "%s adaptive safety alert-only | %s | %s",
            action,
            symbol,
            policy.get("reason") or f"{triggered_set['set_name']} suppressed",
        )
        return None

    reason = policy.get("reason") or f"{triggered_set['set_name']} execution-suppressed"
    logger.warning("%s blocked | %s | %s", action, symbol, reason)
    return {
        "action": "WAIT",
        "reason": reason,
        "execution_policy": policy,
    }


def condition_candle_2_breaks_candle_1_high(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2:
        return _indicator_strategy_result("Candle 2 breaks Candle 1 High", False, "Not enough data")
    
    # Must be exactly the 9:20 candle
    if df.index[-1].time() != dt_time(9, 20):
        return _indicator_strategy_result("Candle 2 breaks Candle 1 High", False, f"Time is {df.index[-1].time()}, not 09:20")
    
    c1_high = df["high"].iloc[-2]
    c2_high = df["high"].iloc[-1]
    
    fired = c2_high > c1_high
    return _indicator_strategy_result(
        "Candle 2 breaks Candle 1 High",
        bool(fired),
        f"C2 High={c2_high:.2f}, C1 High={c1_high:.2f}"
    )

def condition_candle_2_breaks_candle_1_low(ctx: StrategyEvaluationContext) -> dict:
    df = ctx.indicator_df
    if len(df) < 2:
        return _indicator_strategy_result("Candle 2 breaks Candle 1 Low", False, "Not enough data")
    
    # Must be exactly the 9:20 candle
    if df.index[-1].time() != dt_time(9, 20):
        return _indicator_strategy_result("Candle 2 breaks Candle 1 Low", False, f"Time is {df.index[-1].time()}, not 09:20")
    
    c1_low = df["low"].iloc[-2]
    c2_low = df["low"].iloc[-1]
    
    fired = c2_low < c1_low
    return _indicator_strategy_result(
        "Candle 2 breaks Candle 1 Low",
        bool(fired),
        f"C2 Low={c2_low:.2f}, C1 Low={c1_low:.2f}"
    )


#   BUY SIGNAL EVALUATOR
# ════════════════════════════════════════════════════════════

def _evaluate_buy_signal(stock: dict, briefing: dict) -> dict:
    symbol = stock["ticker"]

    if stock.get("direction") == "AVOID":
        return {"action": "WAIT", "reason": "Stock explicitly marked AVOID"}

    # Rule #1: Max 1 completed trade per stock per day
    _disable_rule_1 = bool(cfg("risk", "disable_once_per_day_rule", False))
    if not _disable_rule_1 and has_completed_trade_today(symbol):
        reason = "Stock has already completed a full trade cycle today (Rule #1)"
        logger.debug("BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    ws_count = len(get_candle_history(symbol, include_current=False))
    if ws_count < MIN_WS_CANDLES_FOR_PATTERNS:
        return {
            "action": "WAIT",
            "reason": f"Need {MIN_WS_CANDLES_FOR_PATTERNS} completed live WS candles (have {ws_count})",
        }

    df = _get_indicator_df(symbol)
    if df is None:
        reason = _yfinance_failure_reason.get(symbol)
        if reason:
            return {"action": "WAIT", "reason": f"yfinance unavailable: {reason}"}
        return {"action": "WAIT", "reason": "Insufficient indicator history"}

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

    direction = "SHORT" if "SHORT_" in triggered_set["set_name"].upper() else "LONG"
    
    # ─────────────────────────────────────────────────────────
    # NO-CLASH RULE (Gap Filter)
    # ─────────────────────────────────────────────────────────
    gap_pct = _get_daily_gap(symbol, df)
    if direction == "LONG":
        long_gap_thr = safe_float(cfg("risk", "long_exclude_gap_threshold", -0.008), -0.008)
        if gap_pct <= long_gap_thr:
            reason = f"No-Clash Rule: Long blocked because stock gap {gap_pct*100:.2f}% <= {long_gap_thr*100:.2f}%"
            logger.debug("BUY blocked | %s | %s", symbol, reason)
            return {"action": "WAIT", "reason": reason}
    else:
        short_gap_thr = safe_float(cfg("risk", "short_target_gap_threshold", -0.008), -0.008)
        if gap_pct > short_gap_thr:
            reason = f"No-Clash Rule: Short blocked because stock gap {gap_pct*100:.2f}% > {short_gap_thr*100:.2f}%"
            logger.debug("BUY blocked | %s | %s", symbol, reason)
            return {"action": "WAIT", "reason": reason}


    # Rule #2: Prevent BUY if any SELL strategy is currently triggering
    sell_ctx = StrategyEvaluationContext(
        side="sell",
        indicator_df=df,
        pattern_df=pattern_df,
        ws_count=ws_count,
    )
    sell_triggered_set = _strategy_set_evaluator.evaluate("sell", sell_ctx)
    if sell_triggered_set:
        reason = f"Conflicting SELL strategy {sell_triggered_set['set_name']} also triggered (Rule #2)"
        logger.debug("BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    suppression = _suppression_rejection(triggered_set, symbol)
    if suppression:
        return suppression

    execution_policy = _get_strategy_set_execution_policy(triggered_set["set_name"])
    base_confidence = _resolve_base_confidence(stock, triggered_set, "confidence")
    confidence_trace = _build_confidence_trace(base_confidence, triggered_set, symbol=symbol)
    adaptive_confidence = confidence_trace["final_confidence"]
    if adaptive_confidence < MIN_CONFIDENCE:
        reason = _confidence_rejection_reason(triggered_set, adaptive_confidence, confidence_trace)
        logger.debug("BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol} (WS tick missing)"}

    stop_loss = _calculate_adaptive_stop_loss(symbol, price, direction)

    return {
        "action":    "BUY",
        "direction": direction,
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
        "execution_policy": execution_policy,
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
    Uses the same execution philosophy as screener-approved BUY candidates.
    """
    symbol = stock["ticker"]

    if stock.get("direction") == "AVOID":
        return {"action": "WAIT", "reason": "Marked AVOID"}

    # Rule #1: Max 1 completed trade per stock per day
    if has_completed_trade_today(symbol):
        reason = "Stock has already completed a full trade cycle today (Rule #1)"
        logger.debug("MATH BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    ws_count = len(get_candle_history(symbol, include_current=False))
    if ws_count < MIN_WS_CANDLES_FOR_PATTERNS:
        return {
            "action": "WAIT",
            "reason": f"Need {MIN_WS_CANDLES_FOR_PATTERNS} completed live WS candles (have {ws_count})",
        }

    df = _get_indicator_df(symbol)
    if df is None:
        reason = _yfinance_failure_reason.get(symbol)
        if reason:
            return {"action": "WAIT", "reason": f"yfinance unavailable: {reason}"}
        return {"action": "WAIT", "reason": "Insufficient indicator history"}

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

    direction = "SHORT" if "SHORT_" in triggered_set["set_name"].upper() else "LONG"
    
    # ─────────────────────────────────────────────────────────
    # NO-CLASH RULE (Gap Filter)
    # ─────────────────────────────────────────────────────────
    gap_pct = _get_daily_gap(symbol, df)
    if direction == "LONG":
        long_gap_thr = safe_float(cfg("risk", "long_exclude_gap_threshold", -0.008), -0.008)
        if gap_pct <= long_gap_thr:
            reason = f"No-Clash Rule: Long blocked because stock gap {gap_pct*100:.2f}% <= {long_gap_thr*100:.2f}%"
            logger.debug("MATH BUY blocked | %s | %s", symbol, reason)
            return {"action": "WAIT", "reason": reason}
    else:
        short_gap_thr = safe_float(cfg("risk", "short_target_gap_threshold", -0.008), -0.008)
        if gap_pct > short_gap_thr:
            reason = f"No-Clash Rule: Short blocked because stock gap {gap_pct*100:.2f}% > {short_gap_thr*100:.2f}%"
            logger.debug("MATH BUY blocked | %s | %s", symbol, reason)
            return {"action": "WAIT", "reason": reason}


    # Rule #2: Prevent BUY if any SELL strategy is currently triggering
    sell_ctx = StrategyEvaluationContext(
        side="sell",
        indicator_df=df,
        pattern_df=pattern_df,
        ws_count=ws_count,
    )
    sell_triggered_set = _strategy_set_evaluator.evaluate("sell", sell_ctx)
    if sell_triggered_set:
        reason = f"Conflicting SELL strategy {sell_triggered_set['set_name']} also triggered (Rule #2)"
        logger.debug("MATH BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    suppression = _suppression_rejection(triggered_set, symbol)
    if suppression:
        return suppression

    execution_policy = _get_strategy_set_execution_policy(triggered_set["set_name"])
    base_confidence = _resolve_base_confidence(stock, triggered_set, "math_score")
    confidence_trace = _build_confidence_trace(base_confidence, triggered_set, symbol=symbol)
    adaptive_confidence = confidence_trace["final_confidence"]
    if adaptive_confidence < MIN_CONFIDENCE:
        reason = _confidence_rejection_reason(triggered_set, adaptive_confidence, confidence_trace)
        logger.debug("MATH BUY blocked | %s | %s", symbol, reason)
        return {"action": "WAIT", "reason": reason}

    price, price_src = _get_entry_price(symbol)
    if not price:
        return {"action": "WAIT", "reason": f"No live price for {symbol}"}
        
    stop_loss = _calculate_adaptive_stop_loss(symbol, price, direction)

    return {
        "action":       "BUY",
        "direction":    direction,
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
        "execution_policy": execution_policy,
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

        # Candle history for sell indicators; yfinance is the only external source.
        try:
            candles = _get_candles_with_yfinance_seed(symbol)
        except RuntimeError as exc:
            reason = str(exc)
            _mark_yfinance_failed(symbol, reason)
            logger.error("%s - yfinance unavailable; sell signal check skipped. %s", symbol, reason)
            continue

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

        # ── R7 min_hold_time guard ────────────────────────────────
        # SELL_R7_EMA50_DYN_EXIT must NOT fire within the first
        # r7_min_hold_candles (default 20) WS candles after entry.
        # This prevents early EMA50 noise exits at the start of a trade.
        if triggered_set["set_name"] == "SELL_R7_EMA50_DYN_EXIT":
            _min_hold = safe_int(cfg("risk", "r7_min_hold_candles", 20), 20)
            _entry_ws  = safe_int(position.get("entry_ws_candles", 0), 0)
            _now_ws    = len(get_candle_history(symbol, include_current=False))
            _elapsed   = _now_ws - _entry_ws
            if _elapsed < _min_hold:
                logger.debug(
                    "[R7 EMA50 Exit] %s — hold guard active: %d/%d candles elapsed. Skipping.",
                    symbol, _elapsed, _min_hold,
                )
                continue
        # ── end R7 min_hold_time guard ────────────────────────────

        suppression = _suppression_rejection(triggered_set, symbol, action="SELL")
        if suppression:
            logger.warning("SELL signal skipped | %s | %s", symbol, suppression["reason"])
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
    # F002: Attempt to place missing broker SL orders BEFORE any exit checks.
    # Silent when all positions are protected; CRITICAL log when recovery attempted.
    attempt_broker_sl_recovery()
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
    if not entries_are_enabled():
        return False
    if stock.get("direction") == "AVOID":
        return False
    open_symbols = [p["symbol"] for p in get_open_positions()]
    if stock["ticker"] in open_symbols:
        return False
    # Cooldown check
    last_fail = _failed_order_cooldown.get(stock["ticker"], 0)
    if time.time() - last_fail < FAILED_ORDER_COOLDOWN_SEC:
        return False

    # 3-Candle Cooldown after trade closure
    symbol = stock["ticker"]
    try:
        import sqlite3
        from datetime import datetime
        from core.trading_settings import get as cfg
        today = datetime.now().strftime('%Y-%m-%d')
        from core.state_manager import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT exit_time FROM trades WHERE symbol = ? AND date = ? AND status != 'OPEN' AND exit_time IS NOT NULL ORDER BY id DESC LIMIT 1", (symbol, today)).fetchone()
        conn.close()
        if row and row[0]:
            exit_time = datetime.fromisoformat(row[0])
            seconds_since_exit = (datetime.now() - exit_time).total_seconds()
            candle_interval_seconds = int(cfg("market_data", "candle_interval_seconds", 300))
            cooldown_seconds = candle_interval_seconds * 3
            if seconds_since_exit < cooldown_seconds:
                return False
    except Exception as e:
        logger.warning(f"Error checking 3-candle cooldown for {symbol}: {e}")

    return True


def _interval_seconds(interval: str | None, hist=None) -> int:
    if interval:
        raw = str(interval).strip().lower()
        try:
            if raw.endswith("m"):
                return max(60, int(raw[:-1]) * 60)
            if raw.endswith("h"):
                return max(60, int(raw[:-1]) * 3600)
            if raw.endswith("d"):
                return 86400
        except ValueError:
            pass

    try:
        if hist is not None and len(hist.index) >= 2:
            seconds = int((hist.index[-1] - hist.index[-2]).total_seconds())
            if seconds > 0:
                return seconds
    except Exception:
        pass

    return max(60, int(cfg("market_data", "candle_interval_seconds", 300)))


def _drop_incomplete_candle_if_present(hist, interval: str | None = None):
    """
    Yahoo can include the currently-building intraday or daily candle.
    Drop that last row so indicators use only completed candles.
    """
    if hist is None or hist.empty:
        return hist

    last_time = hist.index[-1]
    tz = getattr(last_time, "tzinfo", None)
    now = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    interval_sec = _interval_seconds(interval, hist)

    if interval_sec >= 86400:
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if last_time.date() == now.date() and now < market_close:
            logger.debug("Dropped incomplete daily Yahoo candle: %s", last_time)
            return hist.iloc[:-1]
        return hist

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((now - day_start).total_seconds())
    current_period_start = day_start + pd.Timedelta(seconds=(elapsed // interval_sec) * interval_sec)

    if last_time >= current_period_start:
        logger.debug("Dropped incomplete Yahoo candle: %s >= %s", last_time, current_period_start)
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
        f"Buy gate: {MIN_WS_CANDLES_FOR_PATTERNS}+ completed live WS candles | "
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

            # ── Regime pre-warm: run ONCE at market open (9:15 AM) ──
            # Ensures Telegram alert fires at open, not on first buy signal.
            _today = datetime.now().date()
            if not hasattr(run_strategy_loop, "_regime_checked_date") or \
               run_strategy_loop._regime_checked_date != _today:
                if bool(cfg("risk", "regime_filter_enabled", True)):
                    try:
                        _regime_is_bull_day()  # warms cache + fires Telegram alert
                        logger.info("[RegimeFilter] Pre-warmed at market open.")
                    except Exception as _re:
                        logger.warning(f"[RegimeFilter] Pre-warm failed: {_re}")
                run_strategy_loop._regime_checked_date = _today

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

            if not entries_are_enabled():
                state = get_trading_session_state()
                reason = state.get("reason", "")
                
                # Check for dashboard-requested emergency square-off
                if state.get("state") == "LIQUIDATING" and "EMERGENCY_SQUAREOFF_REQUESTED" in reason:
                    logger.critical("🚨 Detecting Dashboard Emergency Square-off Request...")
                    try:
                        from core.emergency_squareoff import trigger_emergency_squareoff
                        trigger_emergency_squareoff()
                        await asyncio.sleep(1) # Allow state transition to flush
                        continue
                    except Exception as e:
                        logger.error(f"Failed to execute dashboard-triggered squareoff: {e}")
                
                # Log only once when state first becomes non-ACTIVE to avoid spam every 2s
                _prev_locked_key = getattr(run_strategy_loop, "_last_logged_locked_state", None)
                _cur_locked_key = f"{state.get('state')}|{reason}"
                if _prev_locked_key != _cur_locked_key:
                    logger.warning(
                        "Entries disabled by session state %s (%s). Exit checks continue; BUY scan skipped.",
                        state.get("state"),
                        reason,
                    )
                    run_strategy_loop._last_logged_locked_state = _cur_locked_key
                else:
                    logger.debug(
                        "Entries still disabled: %s (%s).",
                        state.get("state"),
                        reason,
                    )
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            # Update live capital file for dashboard
            try:
                from core.order_executor import get_capital_snapshot
                capital_snapshot = get_capital_snapshot()
                atomic_write_json(
                    "data/live_capital.json",
                    {
                        **capital_snapshot,
                        "capital": capital_snapshot.get("free_margin"),
                        "timestamp": datetime.now().isoformat(),
                    },
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

            _regime_enabled = bool(cfg("risk", "regime_filter_enabled", True))
            if _regime_enabled and not _regime_is_bull_day() and not _regime_is_bear_day():
                # Choppy day: no new trades allowed at all. Skip all signal evaluations.
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
                    # ── Strategy 13 Master: Regime Filter ──
                    _regime_enabled = bool(cfg("risk", "regime_filter_enabled", True))
                    if _regime_enabled:
                        is_long = signal.get("direction", "LONG") == "LONG"
                        if is_long and not _regime_is_bull_day():
                            logger.debug("[RegimeFilter] Skipping BUY LONG — not a bull day.")
                            continue
                        elif not is_long and not _regime_is_bear_day():
                            logger.debug("[RegimeFilter] Skipping BUY SHORT — not a bear day.")
                            continue

                    _log_triggered_strategy_set("BUY", signal)
                    trade = place_entry_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["strategy"],
                        confidence     = signal.get("confidence", 0),
                        direction      = signal.get("direction", "LONG"),
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
                if not _can_open_new_position(stock):
                    continue

                signal = _evaluate_math_signal(stock, briefing)
                if signal["action"] == "BUY":
                    # ── Strategy 13 Master: Regime Filter ──
                    _regime_enabled = bool(cfg("risk", "regime_filter_enabled", True))
                    if _regime_enabled:
                        is_long = signal.get("direction", "LONG") == "LONG"
                        if is_long and not _regime_is_bull_day():
                            logger.debug("[RegimeFilter] Skipping math BUY LONG — not a bull day.")
                            continue
                        elif not is_long and not _regime_is_bear_day():
                            logger.debug("[RegimeFilter] Skipping math BUY SHORT — not a bear day.")
                            continue
                    _log_triggered_strategy_set("BUY", signal)
                    trade = place_entry_order(
                        symbol         = signal["symbol"],
                        trading_symbol = stock.get("trading_symbol", signal["symbol"]),
                        entry_price    = signal["price"],
                        stop_loss      = signal["stop_loss"],
                        strategy       = signal["strategy"],
                        confidence     = signal.get("confidence", 0),
                        direction      = signal.get("direction", "LONG"),
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


# ─────────────────────────────────────────────────────────────
# HELPER FUNCTIONS (Added for robustness)
# ─────────────────────────────────────────────────────────────

def _calculate_final_confidence(
    base: float,
    signal_mult: float = 1.0,
    time_mult: float = 1.0,
    market_mult: float = 1.0,
    cognition_mult: float = 1.0
) -> float:
    """Calculate final confidence score with all multipliers."""
    final = base * signal_mult * time_mult * market_mult * cognition_mult
    return min(100.0, max(0.0, final))  # Clamp to [0, 100]

def _detect_market_regime() -> str:
    """Detect current market regime."""
    # Simple implementation - returns NEUTRAL for paper trading
    return "NEUTRAL"
