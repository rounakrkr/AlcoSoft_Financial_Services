# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   screener/morning_screener.py — 8:45 AM Stock Picker
#
#   UPGRADE: Now picks 25 stocks for math-only watchlist
#   AND top cognition picks for AI-assisted screening.
#
#   BRIEFING STRUCTURE:
#   {
#     "market_bias":     "BULLISH/BEARISH/NEUTRAL",
#     "approved_stocks": [cognition picks, full risk],
#     "watchlist":       [25 stocks → math only, half 1% risk]
#   }
# ============================================================

import json
import logging
import os
import signal
import threading
import asyncio
import time
from datetime import datetime
from urllib.parse import quote
import pandas as pd
import requests
import ta
import google.generativeai as genai
from dotenv import load_dotenv

from core.state_manager import save_briefing
from core.safe_io import safe_read_json
from core.trading_settings import get as cfg

load_dotenv()
logger = logging.getLogger(__name__)

UPSTOX_TOKENS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "upstox_tokens.json")
UPSTOX_CACHE_TTL_SEC    = 300
UPSTOX_FAILURE_COOLDOWN_SEC = 30
_UPSTOX_HISTORY_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_UPSTOX_FAILED_UNTIL:  dict[str, float] = {}
_screener_instrument_cache: dict[str, str] = {}
_screener_instrument_cache_loaded: bool = False

import niftystocks.ns as ns

# ── Stock Universe (Configurable) ────────────────────────────
try:
    MIDCAP_50 = ns.get_nifty_midcap50()
    
    # Translation mapping for companies that have changed ticker symbols on Upstox
    _symbol_translation = {
        "AMARAJABAT": "ARE&M",
        "SRTRANSFIN": "SHRIRAMFIN",
        "L&TFH": "LTF",
        "GMRINFRA": "GMRAIRPORT"
    }
    MIDCAP_50 = [_symbol_translation.get(sym, sym) for sym in MIDCAP_50 if sym != "IBULHSGFIN"]
    
except Exception as e:
    logger.error(f"Failed to fetch Midcap 50 from niftystocks: {e}")
    MIDCAP_50 = [
        "VODAFONE IDEA", "YESBANK", "IDFCFIRSTB", "PNB", "BANKBARODA", "BHEL", "SAIL", "ZOMATO", "UNIONBANK", "INDIANB", "GMRINFRA", "NHPC", "SUZLON", "TVSMOTOR", "ASHOKLEY", "BANDHANBNK", "FEDERALBNK", "L&TFH", "ABCAPITAL", "M&MFIN", "CHOLAFIN", "RECLTD", "PFC", "LICHSGFIN", "SRF", "AUBANK", "VOLTAS", "CUMMINSIND", "BHARATFORG", "ASTRAL", "BALKRISIND", "GODREJPROP", "OBEROIRLTY", "PERSISTENT", "COFORGE", "MPHASIS", "LTTS", "TATACOMM", "JUBLFOOD", "ESCORTS", "POLYCAB", "APOLLOTYRE", "MRF", "PAGEIND", "PIIND", "TRENT", "COROMANDEL", "MAXHEALTH", "SYNGENE", "LAURUSLABS"
    ]

def _screener_counts():
    total = int(cfg("screener", "screener_total_stocks", 25))
    picks = _cognition_pick_count()
    return total, picks, max(0, total - picks)


def _cognition_pick_count() -> int:
    picks = cfg("screener", "cognition_picks", 4)
    return max(1, int(picks))


def _score_to_confidence(score) -> int:
    try:
        return int(max(0, min(100, round(float(score) * 10))))
    except (TypeError, ValueError):
        return 0


def _coerce_pick_confidence(raw, fallback_score) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = _score_to_confidence(fallback_score)
    if 0 < value <= 1:
        value *= 100
    return int(max(0, min(100, round(value))))


# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

def run_morning_screener():
    """
    Called at 8:45 AM.

    Step 1: Fetch indicators for all available stocks (MIDCAP_50 list).
    Step 2: Score every stock mathematically.
    Step 3: Gemini picks N best from ALL stocks (N = cognition_picks setting, configurable).
    Step 4: From remaining stocks, pick best (screener_total - N) by math score.
    Step 5: Total = N (AI) + (screener_total - N) (Math) = screener_total stocks for trading.
    Step 6: Write briefing JSON.
    """
    logger.info("🔄 SCREENER STARTED")

    # STEP 1: Fetch stock data from Yahoo Finance
    logger.info("[1/6] Fetching stock data from Yahoo Finance...")
    summaries = _fetch_all_summaries()
    if not summaries:
        logger.error("❌ SCREENER FAILED: No stock data fetched from Yahoo Finance. Aborting.")
        return False
    logger.info(f"✅ Step 1 complete: {len(summaries)} stocks fetched")

    # STEP 2: Score stocks mathematically
    logger.info("[2/6] Scoring stocks mathematically...")
    market_bias = _get_market_bias()
    scored = _score_all_stocks(summaries)
    total_stocks = len(scored)
    logger.info(f"✅ Step 2 complete: {total_stocks} stocks scored")

    screener_total, cognition_count, _ = _screener_counts()
    logger.info(f"   Configuration: cognition_count={cognition_count}, screener_total={screener_total}")

    # STEP 3: Gemini AI picks stocks
    logger.info("[3/6] Running Gemini AI analysis...")
    all_candidates = scored  # ALL scored stocks
    allowed_symbols = {s["symbol"] for s in all_candidates}

    try:
        ai_cognition_picks = _gemini_pick_stocks(all_candidates)
        ai_cognition_picks = _validate_screener_picks(
            ai_cognition_picks, allowed_symbols, all_candidates
        )
        if not ai_cognition_picks:
            logger.warning(f"⚠️  Gemini returned no valid picks. Using math fallback for top {cognition_count}.")
            ai_cognition_picks = _fallback_picks(scored[:cognition_count])
        logger.info(f"✅ Step 3 complete: Gemini picked {len(ai_cognition_picks)} stocks")
    except Exception as e:
        logger.error(f"❌ GEMINI API ERROR: {str(e)[:100]}. Using math fallback.")
        ai_cognition_picks = _fallback_picks(scored[:cognition_count])
        logger.info(f"✅ Step 3 fallback: Math-based picks {len(ai_cognition_picks)} stocks")

    logger.info(f"   AI cognition picks: {[p['ticker'] for p in ai_cognition_picks]}")

    # STEP 4: Build math watchlist from remaining stocks
    logger.info("[4/6] Building math watchlist from remaining stocks...")
    ai_picked_symbols = {s.get("symbol", s.get("ticker")) for s in ai_cognition_picks}
    remaining_stocks = [s for s in scored if s["symbol"] not in ai_picked_symbols]
    
    math_watchlist_count = screener_total - len(ai_cognition_picks)
    watchlist_candidates = remaining_stocks[:math_watchlist_count]

    # Add per-stock market bias to watchlist
    watchlist = [{
        "ticker":         s["symbol"],
        "trading_symbol": s["symbol"],
        "direction":      "WATCH",
        "confidence":     _score_to_confidence(s["score"]),
        "math_score":     _score_to_confidence(s["score"]),
        "math_score_raw": s["score"],
        "market_bias":    _get_stock_market_bias(s["symbol"]),
        "entry_price":    0.0,
        "stop_loss":      0.0,
        "reason":         f"Math score: {s['score']} | RSI: {s['rsi']} | Vol: {s['vol_ratio']}x",
    } for s in watchlist_candidates]

    logger.info(f"✅ Step 4 complete: {len(watchlist)} math watchlist stocks from {len(remaining_stocks)} remaining")

    # STEP 5: Add market bias to cognition stocks
    logger.info("[5/6] Adding market bias to cognition stocks...")
    for stock in ai_cognition_picks:
        stock["market_bias"] = _get_stock_market_bias(stock.get("ticker", stock.get("symbol", "")))
    logger.info(f"✅ Step 5 complete: Market bias added")

    # STEP 6: Create briefing dict and save
    logger.info("[6/6] Creating and saving briefing...")
    briefing = {
        "generated_at":    datetime.now().isoformat(),
        "session_type":    "MORNING_SCREENER",
        "market_bias":     market_bias,
        "approved_stocks": ai_cognition_picks,
        "watchlist":       watchlist,           # (screener_total-N) — best remaining by math
        "avoid_list":      [],
    }

    # Validate briefing before saving
    if not isinstance(briefing.get("approved_stocks"), list):
        logger.error("❌ SCREENER FAILED: Invalid briefing structure (approved_stocks not a list)")
        return False
    if not isinstance(briefing.get("watchlist"), list):
        logger.error("❌ SCREENER FAILED: Invalid briefing structure (watchlist not a list)")
        return False
    
    total_stocks_in_briefing = len(ai_cognition_picks) + len(watchlist)
    if total_stocks_in_briefing == 0:
        logger.error("❌ SCREENER FAILED: Briefing contains no stocks (both cognition and watchlist empty)")
        return False

    save_result = save_briefing(briefing)
    if not save_result:
        logger.error("❌ SCREENER FAILED: Could not save briefing to disk")
        return False
    
    logger.info(
        f"✅ SCREENER COMPLETED SUCCESSFULLY\n"
        f"  Cognition ({len(ai_cognition_picks)}): {[p['ticker'] for p in ai_cognition_picks]}\n"
        f"  Watchlist ({len(watchlist)}): {[s['ticker'] for s in watchlist]}\n"
        f"  Total trading stocks: {total_stocks_in_briefing}"
    )
    return True


# ════════════════════════════════════════════════════════════
#   MATH SCORER — Pure indicators, no AI
# ════════════════════════════════════════════════════════════

def _score_all_stocks(summaries: list[dict]) -> list[dict]:
    """
    Scores every stock on a 0-10 scale using pure math.
    Higher = better intraday BUY setup.

    Criteria:
      RSI 25-50  → +3  (oversold zone, recovering)
      Vol ≥ 1.2x → +2  (interest building)
      Above EMA20→ +2  (uptrend)
      Chg -1%/+3%→ +1  (not already run up)
      ATR > 0.5% → +1  (enough movement to trade)
      Positive news → +1 (no negative headwinds)
    """
    scored = []
    for s in summaries:
        score = 0

        # RSI sweet spot — oversold but recovering
        if 25 <= s.get("rsi", 50) <= 50:
            score += 3
        elif 50 < s.get("rsi", 50) <= 60:
            score += 1   # Acceptable but less ideal

        # Volume building — institutional interest
        if s.get("vol_ratio", 1.0) >= 1.5:
            score += 2
        elif s.get("vol_ratio", 1.0) >= 1.2:
            score += 1

        # Trend — above EMA20 means uptrend
        if s.get("above_ema20", False):
            score += 2

        # Price change — not already run up too much
        chg = s.get("pct_change", 0)
        if -1 < chg < 3:
            score += 1

        # No negative news (basic check)
        headline = s.get("headline", "").lower()
        negative_words = ["fraud", "loss", "crash", "ban", "penalty", "investigation"]
        if not any(word in headline for word in negative_words):
            score += 1

        scored.append({**s, "score": score})

    # Sort descending — best setups first
    return sorted(scored, key=lambda x: x["score"], reverse=True)


# ════════════════════════════════════════════════════════════
#   DATA FETCHING
# ════════════════════════════════════════════════════════════

def _yahoo_chart_symbol(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper()
    if symbol.startswith("^") or "." in symbol:
        return symbol
    return f"{symbol}.NS"


def _empty_history_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def _interval_seconds(interval: str | None, hist=None) -> int:
    raw = str(interval or "").strip().lower()
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

    return 300


def _drop_incomplete_candle_if_present(hist: pd.DataFrame, interval: str | None = None) -> pd.DataFrame:
    if hist is None or hist.empty:
        return hist

    last_time = hist.index[-1]
    tz = getattr(last_time, "tzinfo", None)
    now = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    interval_sec = _interval_seconds(interval, hist)

    if interval_sec >= 86400:
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if last_time.date() == now.date() and now < market_close:
            logger.debug("Dropped incomplete daily Yahoo candle for screener: %s", last_time)
            return hist.iloc[:-1]
        return hist

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((now - day_start).total_seconds())
    current_period_start = day_start + pd.Timedelta(seconds=(elapsed // interval_sec) * interval_sec)
    if last_time >= current_period_start:
        logger.debug("Dropped incomplete Yahoo candle for screener: %s", last_time)
        return hist.iloc[:-1]

    return hist



def _load_screener_instrument_keys() -> dict[str, str]:
    """Load Upstox instrument keys from data/upstox_tokens.json."""
    global _screener_instrument_cache, _screener_instrument_cache_loaded
    if _screener_instrument_cache_loaded:
        return _screener_instrument_cache
    try:
        with open(UPSTOX_TOKENS_PATH, "r") as f:
            import json as _json
            data = _json.load(f)
        if isinstance(data, dict) and data:
            _screener_instrument_cache = {k.upper(): v for k, v in data.items()}
            _screener_instrument_cache_loaded = True
            return _screener_instrument_cache
    except Exception as e:
        logger.warning("[Screener/Upstox] Could not load upstox_tokens.json: %s", e)
    try:
        import gzip
        from io import BytesIO
        resp = requests.get(
            "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
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
        _screener_instrument_cache = mapping
        _screener_instrument_cache_loaded = True
        logger.info("[Screener/Upstox] Downloaded %d instrument keys.", len(mapping))
    except Exception as e:
        logger.error("[Screener/Upstox] Instrument key download failed: %s", e)
    return _screener_instrument_cache


def _fetch_yahoo_history(symbol: str, period: str = "30d", interval: str = "1d", timeout: float = 8.0) -> pd.DataFrame:
    """
    MIGRATED: Now fetches daily candles from Upstox v2 Historical Candle API.
    'period' is mapped to a day-count window. 'interval' must be '1d' for daily.
    Kept same signature for backward compatibility with all callers.
    """
    # Map Yahoo period strings to day counts
    _period_days = {
        "1d": 2, "5d": 7, "30d": 35, "60d": 65, "90d": 95, "6mo": 185, "1y": 370,
    }
    days = _period_days.get(period.lower(), 35)
    clean_sym = symbol.upper().replace(".NS", "").replace("^", "")

    # Special handling for NIFTY 50 index (^NSEI)
    if symbol.startswith("^") or "NSEI" in symbol.upper():
        clean_sym = "NIFTY"  # map to NIFTY index instrument key

    cache_key = (clean_sym, interval)
    now_ts = time.time()
    cached = _UPSTOX_HISTORY_CACHE.get(cache_key)
    if cached and now_ts - cached[0] < UPSTOX_CACHE_TTL_SEC:
        return cached[1].copy()

    failed_until = _UPSTOX_FAILED_UNTIL.get(clean_sym, 0.0)
    if failed_until > now_ts:
        raise RuntimeError(f"[Screener/Upstox] Cooldown active for {clean_sym}")

    keys = _load_screener_instrument_keys()
    instrument_key = keys.get(clean_sym)
    if not instrument_key:
        raise RuntimeError(f"[Screener/Upstox] No instrument_key for {symbol} (tried '{clean_sym}')")

    from datetime import timedelta
    _UPSTOX_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip("'\" ")
    _headers = {"Accept": "application/json", "Authorization": f"Bearer {_UPSTOX_TOKEN}"}
    to_date   = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    upstox_interval = "day" if interval == "1d" else "5minute"
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{upstox_interval}/{to_date}/{from_date}"

    try:
        resp = requests.get(url, headers=_headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        _UPSTOX_FAILED_UNTIL[clean_sym] = now_ts + UPSTOX_FAILURE_COOLDOWN_SEC
        raise RuntimeError(f"[Screener/Upstox] Network error for {symbol}: {e}")

    if resp.status_code == 401:
        raise RuntimeError(f"[Screener/Upstox] Auth error — check UPSTOX_ACCESS_TOKEN")
    if resp.status_code == 429:
        _UPSTOX_FAILED_UNTIL[clean_sym] = now_ts + 60
        raise RuntimeError(f"[Screener/Upstox] Rate limited for {symbol}")
    if resp.status_code != 200:
        _UPSTOX_FAILED_UNTIL[clean_sym] = now_ts + UPSTOX_FAILURE_COOLDOWN_SEC
        raise RuntimeError(f"[Screener/Upstox] HTTP {resp.status_code} for {symbol}")

    candles = (resp.json().get("data") or {}).get("candles") or []
    if not candles:
        return _empty_history_frame()

    rows = []
    for c in candles:
        try:
            ts = pd.to_datetime(c[0]).tz_convert("Asia/Kolkata").tz_localize(None)
            rows.append({"Open": float(c[1]), "High": float(c[2]), "Low": float(c[3]),
                         "Close": float(c[4]), "Volume": float(c[5]), "_ts": ts})
        except Exception:
            continue

    if not rows:
        return _empty_history_frame()

    frame = pd.DataFrame(rows).set_index("_ts").sort_index()
    frame = frame[~frame.index.duplicated(keep="first")].dropna(subset=["Close"])
    frame = _drop_incomplete_candle_if_present(frame, interval=interval)
    _UPSTOX_HISTORY_CACHE[cache_key] = (time.time(), frame.copy())
    logger.debug("[Screener/Upstox] %s — fetched %d daily candles", symbol, len(frame))
    return frame



def _fetch_all_summaries() -> list[dict]:
    """
    Fetches OHLCV + indicators for all MIDCAP_50 stocks.
    FIX 1: ticker variable properly passed into thread via default arg.
    FIX 2: news fetch separated with its own timeout.
    FIX 3: TATAMOTOR → TATAMOTORS, LTIMINDTREE → LTIM in MIDCAP_50 list.
    """
    summaries = []
    failed_symbols = []
    timeout_symbols = []
    logger.info(f"Fetching Upstox historical data for {len(MIDCAP_50)} stocks (8s timeout per stock)...")

    def _fetch_one(sym, result):
        try:
            hist = _fetch_yahoo_history(sym, period="30d", interval="1d")
            result["hist"] = hist
        except Exception as e:
            result["error"] = str(e)

    for symbol in MIDCAP_50:
        result = {}
        th = threading.Thread(target=_fetch_one, args=(symbol, result), daemon=True)
        th.start()
        th.join(timeout=8.0)

        if th.is_alive():
            timeout_symbols.append(symbol)
            logger.debug(f"  {symbol}: TIMEOUT — skipped")
            continue

        if result.get("error"):
            failed_symbols.append(f"{symbol}({result['error'][:30]})")
            continue

        hist = result.get("hist")
        if hist is None or hist.empty or len(hist) < 22:
            logger.debug(f"  {symbol}: insufficient data ({len(hist) if hist is not None else 0} bars)")
            continue

        try:
            close  = hist["Close"].dropna()
            volume = hist["Volume"].dropna()

            if len(close) < 22:
                continue

            rsi       = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            avg_vol   = volume.rolling(20).mean().iloc[-1]
            vol_ratio = round(float(volume.iloc[-1]) / float(avg_vol), 2) if avg_vol > 0 else 1.0

            prev_close   = float(close.iloc[-2])
            latest_close = float(close.iloc[-1])
            pct_change   = round(((latest_close - prev_close) / prev_close) * 100, 2)

            ema20     = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
            above_ema = latest_close > float(ema20)

            headline = "No news"

            summaries.append({
                "symbol":      symbol,
                "close":       round(latest_close, 2),
                "pct_change":  pct_change,
                "rsi":         round(float(rsi), 1) if not pd.isna(rsi) else 50.0,
                "vol_ratio":   vol_ratio,
                "above_ema20": above_ema,
                "headline":    headline,
            })

        except Exception as e:
            failed_symbols.append(f"{symbol}({str(e)[:40]})")

    logger.info(f"✅ Fetched: {len(summaries)}/{len(MIDCAP_50)} stocks")
    if timeout_symbols:
        logger.warning(f"⚠️  Timeouts ({len(timeout_symbols)}): {timeout_symbols}")
    if failed_symbols:
        logger.warning(f"⚠️  Failures ({len(failed_symbols)}): {failed_symbols[:5]}")

    return summaries


def _get_market_bias() -> str:
    """
    NIFTY 50 index trend → BULLISH / BEARISH / NEUTRAL.
    With 3-second timeout to prevent hangs.
    """
    result_container = {"bias": "NEUTRAL"}
    
    def fetch_nifty():
        try:
            hist = _fetch_yahoo_history("^NSEI", period="5d", interval="1d", timeout=3.0)
            if hist.empty:
                result_container["bias"] = "NEUTRAL"
                return

            closes    = hist["Close"].tail(3).tolist()
            up_days   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            down_days = len(closes) - 1 - up_days

            if up_days >= 2:    
                result_container["bias"] = "BULLISH"
            elif down_days >= 2:  
                result_container["bias"] = "BEARISH"
            else:
                result_container["bias"] = "NEUTRAL"
        except Exception as e:
            logger.debug(f"Market bias check failed: {e}")
            result_container["bias"] = "NEUTRAL"
    
    thread = threading.Thread(target=fetch_nifty, daemon=True)
    thread.start()
    thread.join(timeout=3.0)
    
    return result_container.get("bias", "NEUTRAL")


def _get_stock_market_bias(symbol: str) -> str:
    """
    Per-stock market bias based on that stock's last 5 days trend.
    Independent of NIFTY — allows trading strong stocks in weak markets.
    With 2-second timeout per stock.
    """
    result_container = {"bias": "NEUTRAL"}
    
    def fetch_stock_bias():
        try:
            hist = _fetch_yahoo_history(symbol, period="5d", interval="1d", timeout=2.0)
            if hist.empty or len(hist) < 3:
                result_container["bias"] = "NEUTRAL"
                return

            closes = hist["Close"].tail(3).tolist()
            up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            down_days = len(closes) - 1 - up_days

            if up_days >= 2:
                result_container["bias"] = "BULLISH"
            elif down_days >= 2:
                result_container["bias"] = "BEARISH"
            else:
                result_container["bias"] = "NEUTRAL"
        except Exception as e:
            logger.debug(f"Stock bias {symbol}: {str(e)[:30]}")
            result_container["bias"] = "NEUTRAL"
    
    thread = threading.Thread(target=fetch_stock_bias, daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    
    return result_container.get("bias", "NEUTRAL")


def _gemini_pick_stocks(candidates: list[dict]) -> list[dict]:
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(model_name="gemini-flash-lite-latest")

        system_prompt = _screener_system_prompt()
        user_message  = _build_screener_message(candidates)
        full_prompt   = f"{system_prompt}\n\n{user_message}"

        result_container = {"response": None, "error": None}

        def call_gemini():
            try:
                result_container["response"] = model.generate_content(full_prompt)
            except Exception as e:
                result_container["error"] = str(e)

        thread = threading.Thread(target=call_gemini, daemon=True)
        thread.start()
        thread.join(timeout=120.0)

        if thread.is_alive():
            logger.warning("⚠️  Gemini timeout (>120s). Math fallback.")
            return []

        if result_container.get("error"):
            raise Exception(result_container["error"])

        response = result_container.get("response")
        if not response:
            raise Exception("No response from Gemini")

        raw   = response.text.strip()
        raw   = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end <= start:
            raise ValueError("No JSON in Gemini response")

        data  = json.loads(raw[start:end])
        picks = data.get("picks", [])

        n = _cognition_pick_count()
        formatted = []
        for pick in picks[:n]:
            sym = (pick.get("symbol") or "").strip().upper()
            if not sym:
                continue
            formatted.append({
                "ticker":             sym,
                "trading_symbol":     sym,
                "direction":          "BUY_ONLY",
                "confidence":         _coerce_pick_confidence(pick.get("confidence"), 0),
                "entry_price":        0.0,
                "stop_loss":          0.0,
                "execution_strategy": "TBD",
                "reason":             pick.get("reason", "Gemini screener pick"),
            })

        return formatted

    except Exception as e:
        logger.error(f"Gemini screener failed: {e}")
        return []


def _validate_screener_picks(
    picks: list[dict],
    allowed: set[str],
    scored_candidates: list[dict],
) -> list[dict]:
    """Drop symbols not in math shortlist; pad from math rank if needed."""
    allowed_up = {s.upper() for s in allowed}
    by_sym = {s["symbol"].upper(): s for s in scored_candidates}
    n = _cognition_pick_count()

    valid = []
    seen = set()
    for p in picks:
        sym = (p.get("ticker") or "").strip().upper()
        if sym not in allowed_up:
            logger.warning("Gemini picked %s — not in math list, rejected", sym)
            continue
        if sym in seen:
            continue
        seen.add(sym)
        sc = by_sym.get(sym, {})
        p["math_score"] = _score_to_confidence(sc.get("score", 0))
        p["math_score_raw"] = sc.get("score", 0)
        p["confidence"] = _coerce_pick_confidence(p.get("confidence"), sc.get("score", 0))
        valid.append(p)

    if len(valid) < n:
        for s in scored_candidates:
            sym = s["symbol"].upper()
            if sym in seen:
                continue
            valid.append({
                "ticker":             sym,
                "trading_symbol":     sym,
                "direction":          "BUY_ONLY",
                "confidence":         _score_to_confidence(s.get("score", 0)),
                "entry_price":        0.0,
                "stop_loss":          0.0,
                "execution_strategy": "TBD",
                "reason":             f"Math pad | Score: {s.get('score', 0)}",
                "math_score":         _score_to_confidence(s.get("score", 0)),
                "math_score_raw":     s.get("score", 0),
            })
            seen.add(sym)
            if len(valid) >= n:
                break

    return valid[:n]


def _screener_system_prompt() -> str:
    n = _cognition_pick_count()
    return f"""
INTRADAY STOCK SCREENER - Pick {n} stocks for live cognition screening.

YOU ARE: Market analyst who combines technical setup with NEWS/CATALYSTS/SECTOR MOMENTUM.

IGNORE pure math ranking. Instead, analyze:
1. LATEST NEWS - Is sentiment positive? Any government action? Earnings upcoming?
2. SECTOR MOMENTUM - Is the sector rallying or in trouble?
3. CORPORATE EVENTS - IPO lockout expiry? Dividend? Results? Board meetings?
4. TECHNICAL SETUP - Must have oversold RSI (25-50) + volume building. This is GATING only.
5. RISK FACTORS - Any red flags? Ongoing investigations? Debt concerns?

RANKING CRITERIA (in order of importance):
a) NEWS SENTIMENT - Positive catalysts + sector tailwinds = MUST HAVE
b) TECHNICAL SETUP - RSI 25-50 (oversold recovery zone) + vol ≥ 1.2x (interest building)
c) AVOID - Negative news, upcoming earnings uncertainty, government penalties, sector bearishness

CONSTRAINT: Pick exactly {n} from the allowed list ONLY. Do NOT add other symbols.

OUTPUT JSON ONLY:
{{"picks":[{{"symbol":"NAME","confidence":75,"reason":"Catalyst: [news summary] | Tech: RSI+Vol | Risk: [if any]"}}]}}
""".strip()


def _build_screener_message(candidates: list[dict]) -> str:
    n = _cognition_pick_count()
    symbols = ", ".join(s["symbol"] for s in candidates)
    lines = [
        f"TODAY'S CANDIDATES ({len(candidates)}): {symbols}",
        "Pick based on NEWS/CATALYSTS/SECTOR MOMENTUM, NOT just math scores.",
        "",
        "CANDIDATE DETAILS:",
    ]
    for i, s in enumerate(candidates, 1):
        # Focus on news/sentiment first, technical as gating
        lines.append(
            f"\n#{i} {s['symbol']}")
        lines.append(
            f"   LATEST NEWS: {s['headline']}")
        lines.append(
            f"   TECHNICAL SETUP: Close={s['close']} | Chg={s['pct_change']}% | "
            f"RSI={s['rsi']} (oversold if <50) | Vol={s['vol_ratio']}x | "
            f"AboveEMA20={s['above_ema20']}")
        lines.append(
            f"   MATH SCORE: {s.get('score', 0)}/10 (for reference only)")
    
    lines.append(f"\n\nPICK EXACTLY {n} stocks based on NEWS CATALYSTS + TECHNICAL SETUP.")
    lines.append("Ignore pure math ranking. Focus on: What's the STORY? What's moving it?")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#   FALLBACK — Pure math, no AI
# ════════════════════════════════════════════════════════════

def _fallback_picks(top_scored: list[dict]) -> list[dict]:
    """Used when Gemini screener fails."""
    return [{
        "ticker":             s["symbol"],
        "trading_symbol":     s["symbol"],
        "direction":          "BUY_ONLY",
        "confidence":         _score_to_confidence(s["score"]),
        "entry_price":        0.0,
        "stop_loss":          0.0,
        "execution_strategy": "TBD",
        "reason":             f"Math fallback | Score: {s['score']}",
        "math_score":         _score_to_confidence(s["score"]),
        "math_score_raw":     s["score"],
    } for s in top_scored[: _cognition_pick_count()]]


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("Running morning screener standalone...")
    run_morning_screener()

    briefing = safe_read_json(
        "data/session_briefing.json",
        {},
        expected_type=dict,
        label="session briefing",
        log=logger,
    )

    print(f"\nMarket bias: {briefing.get('market_bias')}")
    print(f"\nCognition picks ({len(briefing.get('approved_stocks', []))}):")
    for s in briefing.get("approved_stocks", []):
        print(f"  {s['ticker']} — {s['reason']}")

    print(f"\nWatchlist ({len(briefing.get('watchlist', []))}):")
    for s in briefing.get("watchlist", [])[:5]:
        print(f"  {s['ticker']} — Score: {s['math_score']} — {s['reason']}")
    print("  ... (showing top 5 only)")
