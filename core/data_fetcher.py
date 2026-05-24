# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/data_fetcher.py — Live Market Data Engine
#   Subscribes to Kotak WebSocket, builds OHLCV candles,
#   stores rolling price history for strategy.py
# ============================================================

import json
import logging
import threading
from datetime import datetime, time as dt_time
from collections import defaultdict, deque
import os

from core.kotak_client import get_client

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
CANDLE_INTERVAL_SECONDS = 300          # 5-minute candles
MAX_CANDLE_HISTORY      = 100          # Keep last 100 candles per stock
BRIEFING_PATH           = "data/session_briefing.json"
TOKENS_CACHE_PATH       = "data/instrument_tokens.json"


# ── In-Memory Storage ────────────────────────────────────────
# Latest raw tick per symbol
_latest_tick: dict[str, dict] = {}

# Rolling candle history per symbol (deque auto-drops oldest)
_candle_history: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=MAX_CANDLE_HISTORY)
)

# Current building candle per symbol
_current_candle: dict[str, dict] = {}

# Instrument token → symbol mapping (for decoding websocket messages)
_token_to_symbol: dict[str, str] = {}

# Thread lock for safe concurrent access
_lock = threading.Lock()


# ── WebSocket Callbacks ──────────────────────────────────────
def _on_message(message):
    """
    Called every time a price tick arrives from Kotak WebSocket.
    Decodes the tick, updates latest price, builds OHLCV candle.
    """
    try:
        # Kotak sends a list of tick dicts
        if isinstance(message, list):
            for tick in message:
                _process_tick(tick)
        elif isinstance(message, dict):
            _process_tick(message)
    except Exception as e:
        logger.error(f"Error processing tick: {e}")


def _process_tick(tick: dict):
    """Processes a single price tick into candle data."""
    token  = str(tick.get("tk", ""))
    ltp    = float(tick.get("ltp", 0) or tick.get("last_price", 0))
    volume = float(tick.get("v", 0) or tick.get("volume", 0))

    if not token or ltp == 0:
        return

    symbol = _token_to_symbol.get(token)
    if not symbol:
        return

    now = datetime.now()

    with _lock:
        # Update latest tick
        _latest_tick[symbol] = {
            "symbol":    symbol,
            "ltp":       ltp,
            "volume":    volume,
            "timestamp": now.isoformat(),
        }

        # Build OHLCV candle
        _build_candle(symbol, ltp, volume, now)


def _build_candle(symbol: str, ltp: float, volume: float, now: datetime):
    """
    Aggregates ticks into 5-minute OHLCV candles.
    When a candle period ends, pushes it to history and starts fresh.
    """
    # Current candle bucket (floor to nearest 5 min)
    bucket_minute = (now.minute // 5) * 5
    bucket_key    = now.strftime(f"%Y-%m-%d %H:{bucket_minute:02d}")

    if symbol not in _current_candle:
        # First tick ever for this symbol
        _current_candle[symbol] = _new_candle(bucket_key, ltp, volume)
        return

    candle = _current_candle[symbol]

    if candle["bucket"] != bucket_key:
        # New 5-min period started — save completed candle to history
        _candle_history[symbol].append(candle)
        logger.debug(f"Candle closed: {symbol} | {candle}")

        # Start fresh candle
        _current_candle[symbol] = _new_candle(bucket_key, ltp, volume)
    else:
        # Update current candle
        candle["high"]   = max(candle["high"], ltp)
        candle["low"]    = min(candle["low"], ltp)
        candle["close"]  = ltp
        candle["volume"] += volume


def _new_candle(bucket_key: str, ltp: float, volume: float) -> dict:
    return {
        "bucket": bucket_key,
        "open":   ltp,
        "high":   ltp,
        "low":    ltp,
        "close":  ltp,
        "volume": volume,
    }


def _on_open(message):
    logger.info("✅ WebSocket connection opened.")


def _on_close(message):
    logger.warning(f"⚠️ WebSocket closed: {message}")


def _on_error(error):
    logger.error(f"❌ WebSocket error: {error}")


# ── Public Interface ─────────────────────────────────────────
def get_latest_tick(symbol: str) -> dict | None:
    """Returns the most recent price tick for a symbol."""
    with _lock:
        return _latest_tick.get(symbol)


def get_candle_history(symbol: str) -> list[dict]:
    """
    Returns list of completed OHLCV candles (oldest → newest).
    strategy.py uses this for RSI/MACD calculations.
    Minimum 26 candles needed for MACD, 14 for RSI.
    """
    with _lock:
        history = list(_candle_history[symbol])
        # Append current (incomplete) candle as the latest data point
        if symbol in _current_candle:
            history.append(_current_candle[symbol])
        return history


def get_all_symbols() -> list[str]:
    """Returns all symbols currently being tracked."""
    with _lock:
        return list(_latest_tick.keys())


def has_enough_history(symbol: str, min_candles: int = 26) -> bool:
    """
    Returns True if we have enough candle history for indicators.
    MACD needs 26 candles minimum. RSI needs 14.
    Don't trade until this returns True.
    """
    return len(get_candle_history(symbol)) >= min_candles


# ── Instrument Token Resolver ─────────────────────────────────
def resolve_instrument_tokens(symbols: list[str]) -> list[dict]:
    """
    Converts stock symbols (e.g. 'RELIANCE') to Kotak instrument tokens.
    Caches results to avoid repeated API calls.
    Returns list of {"instrument_token": "...", "exchange_segment": "nse_cm"}
    """
    client = get_client()

    # Load cache if exists
    cached = {}
    if os.path.exists(TOKENS_CACHE_PATH):
        with open(TOKENS_CACHE_PATH, "r") as f:
            cached = json.load(f)

    tokens_list = []
    updated     = False

    for symbol in symbols:
        if symbol in cached:
            token_info = cached[symbol]
        else:
            logger.info(f"Resolving instrument token for {symbol}...")
            try:
                result = client.search_scrip(
                    exchange_segment="nse_cm",
                    symbol=symbol,
                    expiry="",
                    option_type="",
                    strike_price="",
                )
                # search_scrip returns a list — take the equity match
                scrip = _find_equity_scrip(result, symbol)
                if not scrip:
                    logger.warning(f"Could not resolve token for {symbol}")
                    continue

                token_info = {
                    "instrument_token": scrip.get("pSymbol") or scrip.get("token"),
                    "exchange_segment": "nse_cm",
                    "trading_symbol":   scrip.get("pTrdSymbol") or symbol,
                }
                cached[symbol] = token_info
                updated = True
            except Exception as e:
                logger.error(f"Token resolution failed for {symbol}: {e}")
                continue

        tokens_list.append({
            "instrument_token": token_info["instrument_token"],
            "exchange_segment": token_info["exchange_segment"],
        })
        _token_to_symbol[str(token_info["instrument_token"])] = symbol

    # Save updated cache
    if updated:
        os.makedirs("data", exist_ok=True)
        with open(TOKENS_CACHE_PATH, "w") as f:
            json.dump(cached, f, indent=2)
        logger.info("Instrument token cache updated.")

    return tokens_list


def _find_equity_scrip(results, symbol: str) -> dict | None:
    """Picks the plain equity scrip from search results (not F&O)."""
    if not results:
        return None
    if isinstance(results, dict) and "data" in results:
        results = results["data"]
    for scrip in results:
        name = scrip.get("pSymbol", "") or scrip.get("symbol", "")
        series = scrip.get("pSeries", "") or scrip.get("series", "")
        if name == symbol and series == "EQ":
            return scrip
    # Fallback: return first result
    return results[0] if results else None


# ── Startup: Subscribe to Live Feed ──────────────────────────
def start_live_feed(symbols: list[str]):
    """
    Main entry point. Call this once at system startup.
    Resolves tokens, attaches callbacks, subscribes to WebSocket.
    """
    logger.info(f"Starting live feed for: {symbols}")

    client = get_client()

    # Attach WebSocket callbacks
    client.on_message = _on_message
    client.on_open    = _on_open
    client.on_close   = _on_close
    client.on_error   = _on_error

    # Resolve instrument tokens
    instrument_tokens = resolve_instrument_tokens(symbols)

    if not instrument_tokens:
        logger.error("No instrument tokens resolved. Cannot start feed.")
        return

    # Subscribe to live ticks
    client.subscribe(
        instrument_tokens=instrument_tokens,
        isIndex=False,
        isDepth=False,
    )

    logger.info(f"✅ Subscribed to live feed: {[t['instrument_token'] for t in instrument_tokens]}")


def stop_live_feed(symbols: list[str]):
    """Unsubscribes from live feed for given symbols."""
    client = get_client()
    instrument_tokens = resolve_instrument_tokens(symbols)
    if instrument_tokens:
        client.un_subscribe(
            instrument_tokens=instrument_tokens,
            isIndex=False,
            isDepth=False,
        )
    logger.info("Live feed stopped.")