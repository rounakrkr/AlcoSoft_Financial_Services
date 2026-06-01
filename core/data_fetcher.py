# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/data_fetcher.py — Live Market Data Engine
#   Subscribes to Kotak WebSocket, builds OHLCV candles,
#   stores rolling price history for strategy.py
# ============================================================

import time
import logging
import threading
from datetime import datetime, time as dt_time
from collections import defaultdict, deque
import os

from core.trading_settings import get as cfg
from core.safe_io import atomic_write_json, safe_read_json

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
_active_client          = None
MAX_CANDLE_HISTORY      = 100          # Keep last 100 candles per stock


def _candle_interval_seconds() -> int:
    return int(cfg("market_data", "candle_interval_seconds", 300))


def _candle_bucket_minutes(now: datetime) -> int:
    mins = max(1, _candle_interval_seconds() // 60)
    return (now.minute // mins) * mins
BRIEFING_PATH           = "data/session_briefing.json"
TOKENS_CACHE_PATH       = "data/instrument_tokens.json"
FEED_STATS_PATH         = "data/feed_stats.json"
_subscribed_symbols = []           # currently subscribed stock list
_reconnect_attempts = 0            # retry counter
_max_reconnect = 10                # max retries
_reconnect_delay = 5               # initial delay seconds
_reconnect_lock = threading.RLock() # avoid simultaneous reconnect/deadlock
_reconnect_timer = None


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


# Tick stats — surfaced in logs / health checks
_tick_counts: dict[str, int] = defaultdict(int)
_last_tick_log: float = 0.0
_last_stats_publish: float = 0.0


def _feed_stats_snapshot_locked() -> dict:
    tick_counts = dict(_tick_counts)
    return {
        "updated_at": datetime.now().isoformat(),
        "subscribed": list(_subscribed_symbols),
        "symbols_with_ticks": list(_latest_tick.keys()),
        "tick_counts": tick_counts,
        "tick_total": sum(tick_counts.values()),
        "candle_counts": {
            s: len(_candle_history[s]) + (1 if s in _current_candle else 0)
            for s in _subscribed_symbols
        },
    }


def _publish_feed_stats(force: bool = False):
    global _last_stats_publish
    now = time.time()
    if not force and now - _last_stats_publish < 5:
        return
    with _lock:
        snapshot = _feed_stats_snapshot_locked()
    if atomic_write_json(FEED_STATS_PATH, snapshot, label="feed stats", log=logger):
        _last_stats_publish = now


def get_feed_stats() -> dict:
    """Returns live-feed health: symbols, tick counts, candle counts."""
    with _lock:
        return _feed_stats_snapshot_locked()


# ── WebSocket Callbacks ──────────────────────────────────────
def _on_message(message):
    """
    Called every time a price tick arrives from Kotak WebSocket.
    NeoAPI wraps live ticks as {"type": "stock_feed", "data": [ {...}, ... ]}.
    """
    global _reconnect_attempts, _last_tick_log
    _reconnect_attempts = 0
    _reset_keepalive()
    try:
        ticks = _extract_ticks(message)
        for tick in ticks:
            _process_tick(tick)

        # Periodic summary so logs show ALL symbols getting data
        now = time.time()
        if ticks and now - _last_tick_log >= 120:
            _last_tick_log = now
            with _lock:
                active = {s: _tick_counts[s] for s in _subscribed_symbols if _tick_counts.get(s, 0) > 0}
                silent = [s for s in _subscribed_symbols if _tick_counts.get(s, 0) == 0]
            logger.info(
                f"📡 Live feed | ticks received: {len(active)}/{len(_subscribed_symbols)} symbols | "
                f"active={list(active.keys())[:8]}{'...' if len(active) > 8 else ''}"
            )
            if silent:
                logger.warning(f"📡 No ticks yet for: {silent[:10]}{'...' if len(silent) > 10 else ''}")

    except (TypeError, KeyError, ValueError) as e:
        logger.error(
            "Tick parse error (%s): %s",
            type(e).__name__, e,
            exc_info=True,
        )
    except Exception as e:
        logger.critical(
            "Unexpected tick handler error (%s): %s",
            type(e).__name__, e,
            exc_info=True,
        )


def _extract_ticks(message) -> list[dict]:
    """Normalise NeoAPI callback payloads into a list of tick dicts."""
    if message is None:
        return []

    if isinstance(message, str):
        return []

    if isinstance(message, list):
        return [t for t in message if isinstance(t, dict)]

    if isinstance(message, dict):
        msg_type = message.get("type")
        if msg_type == "stock_feed":
            data = message.get("data", [])
            return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        if msg_type == "quotes":
            data = message.get("data")
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict)]
            if isinstance(data, dict):
                return [data]
            return []
        if "tk" in message:
            return [message]

    return []


def _parse_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _process_tick(tick: dict):
    """Processes a single price tick into candle data."""
    token = str(tick.get("tk", "") or tick.get("instrument_token", ""))
    ltp = _parse_float(
        tick.get("ltp")
        or tick.get("last_traded_price")
        or tick.get("last_price")
        or tick.get("iv")  # index feed
    )
    volume = _parse_float(tick.get("v") or tick.get("volume"))

    if not token or ltp <= 0:
        return

    symbol = _token_to_symbol.get(token)
    if not symbol:
        return

    _tick_counts[symbol] += 1

    now = datetime.now()

    with _lock:
        # Update latest tick
        _latest_tick[symbol] = {
            "symbol":    symbol,
            "ltp":       ltp,
            "volume":    volume,
            "timestamp": now.isoformat(),
        }

        # Build OHLCV candle (use tick OHLC when Kotak sends them)
        tick_high = _parse_float(tick.get("h") or tick.get("high"))
        tick_low  = _parse_float(tick.get("lo") or tick.get("low") or tick.get("l"))
        tick_open = _parse_float(tick.get("op") or tick.get("open"))
        _build_candle(symbol, ltp, volume, now, tick_open, tick_high, tick_low)

    _publish_feed_stats()


def _build_candle(
    symbol: str,
    ltp: float,
    volume: float,
    now: datetime,
    tick_open: float = 0,
    tick_high: float = 0,
    tick_low: float = 0,
):
    """
    Aggregates ticks into 5-minute OHLCV candles.
    When a candle period ends, pushes it to history and starts fresh.
    """
    bucket_minute = _candle_bucket_minutes(now)
    bucket_key    = now.strftime(f"%Y-%m-%d %H:{bucket_minute:02d}")


    if symbol not in _current_candle:
        # First tick ever for this symbol
        _current_candle[symbol] = _new_candle(bucket_key, ltp, volume)
        return

    candle = _current_candle[symbol]

    if candle["bucket"] != bucket_key:
        # New 5-min period started — save completed candle to history
        _candle_history[symbol].append(candle)
        # ➕ ye line daalo
        logger.info(f"🕯️ Candle closed: {symbol} | {candle['bucket']} | O:{candle['open']:.2f} H:{candle['high']:.2f} L:{candle['low']:.2f} C:{candle['close']:.2f} V:{candle['volume']}")

        # Start fresh candle
        _current_candle[symbol] = _new_candle(bucket_key, ltp, volume)
    else:
        # Update current candle
        highs = [candle["high"], ltp]
        lows  = [candle["low"], ltp]
        if tick_high > 0:
            highs.append(tick_high)
        if tick_low > 0:
            lows.append(tick_low)
        candle["high"]   = max(highs)
        candle["low"]    = min(lows)
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



# ── WebSocket Keepalive — prevents Kotak 4-min idle disconnect ────────────────
# Kotak closes WebSocket after ~4 minutes of silence.
# We track last tick timestamp; if >3 min silent, we DO NOT send anything (avoid double-subscribe bug)
# Instead, we let Kotak's natural keepalive handle it. If it still dies, reconnect.

_last_tick_timestamp = 0.0  # Track last tick time


def _reset_keepalive():
    """Update last tick timestamp. Don't send explicit keepalive pings."""
    global _last_tick_timestamp
    _last_tick_timestamp = time.time()
    # Removed the Timer-based ping — let Kotak's native keepalive work


def _send_keepalive():
    """DEPRECATED — no longer used. Kept for backward compatibility."""
    pass


def _on_open(message):
    logger.info("✅ WebSocket connection opened.")


def _on_close(message):
    global _subscribed_symbols, _reconnect_attempts, _reconnect_timer
    logger.warning(f"⚠️ WebSocket closed: {message}")

    if not _subscribed_symbols:
        logger.info("No symbols to re-subscribe. Skipping reconnect.")
        return

    # Market open nahi hai? To market open hone tak wait karo
    if not _is_market_open():
        now = datetime.now()
        market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now > market_open_time:
            delay = 30
        else:
            delay = (market_open_time - now).total_seconds() + 5
        logger.info(f"Market not open. Scheduling reconnect at 9:15 AM (in {delay:.0f}s)")
        with _reconnect_lock:
            if _reconnect_timer:                    # ← cancel existing
                _reconnect_timer.cancel()
            _reconnect_timer = threading.Timer(delay, _do_reconnect)
            _reconnect_timer.daemon = True
            _reconnect_timer.start()
        return

    # Market open hai → immediate reconnect attempt
    _schedule_reconnect()


def _on_error(error):
    logger.error(
        "❌ WebSocket error | type=%s | msg=%s | symbols=%s | attempt=%s",
        type(error).__name__,
        error,
        len(_subscribed_symbols),
        _reconnect_attempts + 1,
        exc_info=not isinstance(error, str),
    )
    if _subscribed_symbols:
        _schedule_reconnect()


def _is_market_open():
    """Trading day + regular session (9:15–15:30)."""
    from core.market_calendar import is_market_session_open
    return is_market_session_open()

def _schedule_reconnect():
    global _reconnect_timer, _reconnect_attempts
    with _reconnect_lock:
        if _reconnect_timer:
            _reconnect_timer.cancel()               # ← yeh add karo
            _reconnect_timer = None
        if _reconnect_attempts >= _max_reconnect:
            logger.error("Max reconnect attempts reached. Manual restart needed.")
            return
        delay = min(_reconnect_delay * (2 ** _reconnect_attempts), 300)
        logger.info(f"Scheduling reconnect in {delay}s (attempt {_reconnect_attempts+1}/{_max_reconnect})")
        _reconnect_timer = threading.Timer(delay, _do_reconnect)
        _reconnect_timer.daemon = True
        _reconnect_timer.start()

def _do_reconnect():
    global _reconnect_attempts, _subscribed_symbols
    with _reconnect_lock:
        _reconnect_attempts += 1
        try:
            logger.info("Reconnecting WebSocket...")
            start_live_feed(_subscribed_symbols)  # will re-subscribe
            _reconnect_attempts = 0  # reset on success
            logger.info("Reconnection successful.")
        except (ConnectionError, OSError, TimeoutError) as e:
            logger.error("WebSocket reconnect network error: %s", e)
            _schedule_reconnect()
        except Exception as e:
            logger.critical("WebSocket reconnect failed: %s", e, exc_info=True)
            _schedule_reconnect()


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
def fix_briefing_trading_symbols(briefing: dict):
    """Set trading_symbol from instrument token cache (e.g. INFY → INFY-EQ)."""
    if not os.path.exists(TOKENS_CACHE_PATH):
        symbols = []
        for key in ("approved_stocks", "watchlist"):
            symbols.extend(s.get("ticker") for s in briefing.get(key, []) if s.get("ticker"))
        if symbols:
            resolve_instrument_tokens(list(dict.fromkeys(symbols)))

    if not os.path.exists(TOKENS_CACHE_PATH):
        return

    token_cache = safe_read_json(
        TOKENS_CACHE_PATH,
        {},
        expected_type=dict,
        label="instrument token cache",
        log=logger,
    )

    for stock_list_key in ("approved_stocks", "watchlist"):
        for stock in briefing.get(stock_list_key, []):
            sym = stock.get("ticker", "")
            if sym in token_cache:
                ts = token_cache[sym].get("trading_symbol")
                if ts:
                    stock["trading_symbol"] = ts


def purge_invalid_token_cache():
    """Remove wrong series (BL/NC/etc.) from instrument_tokens.json."""
    if not os.path.exists(TOKENS_CACHE_PATH):
        return
    cached = safe_read_json(
        TOKENS_CACHE_PATH,
        {},
        expected_type=dict,
        label="instrument token cache",
        log=logger,
    )
    cleaned = {k: v for k, v in cached.items() if _is_valid_equity_entry(k, v)}
    if len(cleaned) != len(cached):
        os.makedirs("data", exist_ok=True)
        atomic_write_json(TOKENS_CACHE_PATH, cleaned, label="instrument token cache", log=logger)
        logger.info(
            f"Purged {len(cached) - len(cleaned)} invalid token(s) from cache."
        )


def resolve_instrument_tokens(symbols: list[str]) -> list[dict]:
    """
    Converts stock symbols (e.g. 'RELIANCE') to Kotak instrument tokens.
    Caches results to avoid repeated API calls.
    Returns list of {"instrument_token": "...", "exchange_segment": "nse_cm"}
    """
    from core.kotak_client import get_client
    client = get_client()

    # Load cache if exists
    cached = {}
    if os.path.exists(TOKENS_CACHE_PATH):
        cached = safe_read_json(
            TOKENS_CACHE_PATH,
            {},
            expected_type=dict,
            label="instrument token cache",
            log=logger,
        )

    tokens_list = []
    updated     = False

    for symbol in symbols:
        if symbol in cached and _is_valid_equity_entry(symbol, cached[symbol]):
            token_info = cached[symbol]
        else:
            if symbol in cached:
                logger.warning(
                    f"Invalid cached token for {symbol} "
                    f"({cached[symbol].get('trading_symbol')}). Re-resolving..."
                )
                del cached[symbol]
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

                token_raw = scrip.get("pSymbol") or scrip.get("token") or scrip.get("instrument_token")
                trading_sym = _safe_str(scrip.get("pTrdSymbol")) or f"{symbol}-EQ"
                if not trading_sym.upper().endswith("-EQ"):
                    logger.warning(
                        f"{symbol}: non-EQ trading symbol {trading_sym} — skipping"
                    )
                    continue

                token_info = {
                    "instrument_token": str(token_raw),
                    "exchange_segment": "nse_cm",
                    "trading_symbol":   trading_sym,
                }
                logger.info(
                    f"  {symbol} → token={token_info['instrument_token']} "
                    f"| {trading_sym}"
                )
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
        atomic_write_json(TOKENS_CACHE_PATH, cached, label="instrument token cache", log=logger)
        logger.info("Instrument token cache updated.")

    return tokens_list


def _safe_str(value) -> str:
    """Kotak CSV fields are often int/float — never call .upper() on raw values."""
    if value is None:
        return ""
    return str(value).strip()


def _find_equity_scrip(results, symbol: str) -> dict | None:
    """
    Pick NSE cash-market EQ scrip only.
    Kotak master CSV: pSymbolName = RELIANCE, pSymbol = token (int), pTrdSymbol = RELIANCE-EQ.
    """
    if not results:
        return None
    if isinstance(results, dict):
        if "error" in results or "Error" in results:
            logger.warning(f"Scrip search error for {symbol}: {results}")
            return None
        if "data" in results:
            results = results["data"]
        elif "message" in results:
            logger.warning(f"Scrip search: {results.get('message')}")
            return None
    if not isinstance(results, list):
        return None

    sym_upper = symbol.upper()
    candidates = []

    for scrip in results:
        # pSymbol is numeric token — NOT the company name
        name = _safe_str(scrip.get("pSymbolName") or scrip.get("symbol")).upper()
        if name != sym_upper:
            continue

        trading = _safe_str(scrip.get("pTrdSymbol") or scrip.get("trading_symbol"))
        series  = _safe_str(scrip.get("pSeries") or scrip.get("series")).upper()

        if trading.upper().endswith("-EQ") or series == "EQ":
            return scrip
        candidates.append(scrip)

    for scrip in candidates:
        trading = _safe_str(scrip.get("pTrdSymbol")).upper()
        if trading and not any(
            trading.endswith(suffix)
            for suffix in ("-BL", "-BE", "-NC", "-N1", "-N2", "-IL")
        ):
            return scrip

    logger.warning(f"No EQ scrip found for {symbol} in {len(results)} results")
    return None


def _is_valid_equity_entry(symbol: str, entry: dict) -> bool:
    """Reject cached tokens that point to block/bond series."""
    trading = _safe_str(entry.get("trading_symbol")).upper()
    if not trading:
        return False
    if trading.endswith("-EQ"):
        return True
    bad_suffixes = ("-BL", "-BE", "-NC", "-N1", "-N2", "-IL")
    return not any(trading.endswith(s) for s in bad_suffixes)


# ── Startup: Subscribe to Live Feed ──────────────────────────
def start_live_feed(symbols: list[str]):
    """Starts WebSocket subscription for price ticks."""
    global _active_client, _subscribed_symbols, _reconnect_attempts, _tick_counts
    symbols = list(dict.fromkeys(symbols))  # dedupe, preserve order
    logger.info(f"Starting live feed for {len(symbols)} symbols: {symbols}")
    _tick_counts.clear()

    from core.kotak_client import get_client
    client = get_client()
    _active_client = client

    # Register WebSocket callbacks
    client.on_message = _on_message
    client.on_open    = _on_open
    client.on_close   = _on_close
    client.on_error   = _on_error

    _subscribed_symbols = symbols.copy()
    _reconnect_attempts = 0
    _publish_feed_stats(force=True)

    instrument_tokens = resolve_instrument_tokens(symbols)
    if not instrument_tokens:
        logger.error("No instrument tokens resolved. Cannot start feed.")
        _publish_feed_stats(force=True)
        return

    logger.info(f"Subscribing to {len(instrument_tokens)} instruments...")
    try:
        client.subscribe(
            instrument_tokens=instrument_tokens,
            isIndex=False,
            isDepth=False,
        )
        logger.info(f"✅ Subscribed to live feed: {[t['instrument_token'] for t in instrument_tokens]}")
        
        _publish_feed_stats(force=True)

        # Keep the connection alive with keepalive ping
        _reset_keepalive()
        
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.error(
            "❌ WebSocket subscribe failed (network) | symbols=%s | %s",
            len(instrument_tokens), e,
        )
        _schedule_reconnect()
    except Exception as e:
        logger.error(
            "❌ WebSocket subscribe failed | symbols=%s | %s",
            len(instrument_tokens), e,
            exc_info=True,
        )
        _schedule_reconnect()


def stop_live_feed(symbols: list[str]):
    global _active_client, _subscribed_symbols, _reconnect_timer
    if _reconnect_timer:
        _reconnect_timer.cancel()
        _reconnect_timer = None
    _subscribed_symbols = []
    _publish_feed_stats(force=True)

    if _active_client is None:        
        logger.warning("No active client to unsubscribe from.")
        logger.info("Live feed stopped.")
        return

    instrument_tokens = resolve_instrument_tokens(symbols)
    if instrument_tokens:
        _active_client.un_subscribe(
            instrument_tokens=instrument_tokens,
            isIndex=False,
            isDepth=False,
        )
    _active_client = None              
    logger.info("Live feed stopped.")
