# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   screener/morning_screener.py — 8:45 AM Stock Picker
#
#   UPGRADE: Now picks 25 stocks for math-only watchlist
#   AND top 4 for AI War Room debate.
#
#   BRIEFING STRUCTURE:
#   {
#     "market_bias":     "BULLISH/BEARISH/NEUTRAL",
#     "approved_stocks": [4 stocks → War Room debated, full 2% risk],
#     "watchlist":       [25 stocks → math only, half 1% risk]
#   }
# ============================================================

import json
import logging
import os
from datetime import datetime
import yfinance as yf
import pandas as pd
import ta
import google.generativeai as genai
from dotenv import load_dotenv

from core.state_manager import save_briefing
from core.trading_settings import get as cfg

load_dotenv()
logger = logging.getLogger(__name__)

# ── NIFTY 50 Stock Universe ───────────────────────────────────
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "TECHM", "POWERGRID", "NTPC", "ONGC", "BAJFINANCE",
    "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK",
    "JSWSTEEL", "M&M", "SBILIFE", "TATACONSUM", "TATAMOTOR",
    "TATASTEEL", "BRITANNIA", "CIPLA", "COALINDIA", "HDFCLIFE",
    "LTIMINDTREE", "BPCL", "UPL", "APOLLOHOSP", "BAJAJ-AUTO",
]

def _screener_counts():
    total = int(cfg("screener", "screener_total_stocks", 25))
    picks = int(cfg("screener", "war_room_picks", 4))
    return total, picks, max(0, total - picks)


# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

def run_morning_screener():
    """
    Called at 8:45 AM.

    Step 1: Fetch indicators for all NIFTY 50 stocks.
    Step 2: Score every stock mathematically.
    Step 3: Top N (default 25) scored by math.
    Step 4: Top 4 → War Room (AI debate). Remaining 21 → math-only watchlist.
    Step 5: Write briefing JSON.
    """
    logger.info("Morning screener starting...")

    summaries = _fetch_all_summaries()
    if not summaries:
        logger.error("No stock data fetched. Screener failed.")
        return

    logger.info(f"Fetched data for {len(summaries)} stocks.")

    market_bias = _get_market_bias()
    logger.info(f"Market bias: {market_bias}")

    # ── Math score all stocks ─────────────────────────────────
    scored = _score_all_stocks(summaries)

    screener_total, war_room_picks, math_watchlist_size = _screener_counts()

    # ── Top N scored pool ─────────────────────────────────────
    top_pool = scored[:screener_total]
    if len(top_pool) < screener_total:
        logger.warning(
            f"Only {len(top_pool)}/{screener_total} stocks scored "
            f"(yfinance may have failed for some symbols)."
        )

    war_room_candidates = top_pool[:war_room_picks]
    math_only           = top_pool[war_room_picks:war_room_picks + math_watchlist_size]

    # Add per-stock market bias to watchlist
    watchlist = [{
        "ticker":         s["symbol"],
        "trading_symbol": s["symbol"],   # fixed to -EQ after token resolve in main.py
        "direction":      "WATCH",
        "confidence":     0,
        "math_score":     s["score"],
        "market_bias":    _get_stock_market_bias(s["symbol"]),  # Per-stock bias
        "entry_price":    0.0,
        "stop_loss":      0.0,
        "reason":         f"Math score: {s['score']} | RSI: {s['rsi']} | Vol: {s['vol_ratio']}x",
    } for s in math_only]

    logger.info(
        f"Math watchlist ({len(watchlist)} stocks, no overlap with War Room): "
        f"{[s['ticker'] for s in watchlist]}"
    )

    # ── Top 4 → War Room (AI debate) ──────────────────────────
    allowed_symbols = {s["symbol"] for s in war_room_candidates}

    war_room_picks = _gemini_pick_stocks(war_room_candidates, market_bias)
    war_room_picks = _validate_screener_picks(
        war_room_picks, allowed_symbols, war_room_candidates
    )
    if not war_room_picks:
        logger.warning("Gemini screener failed. Using math fallback for top 4.")
        war_room_picks = _fallback_picks(war_room_candidates)

    logger.info(f"War Room picks: {[p['ticker'] for p in war_room_picks]}")

    # Add per-stock market bias to war room stocks
    for stock in war_room_picks:
        stock["market_bias"] = _get_stock_market_bias(stock.get("ticker", stock.get("symbol", "")))

    # ── Write briefing ────────────────────────────────────────
    briefing = {
        "generated_at":    datetime.now().isoformat(),
        "session_type":    "MORNING_SCREENER",
        "market_bias":     market_bias,
        "approved_stocks": war_room_picks,   # 4 — War Room + full risk
        "watchlist":       watchlist,        # 21 — math only (excludes war room 4)
        "avoid_list":      [],
    }

    save_briefing(briefing)
    logger.info(
        f"Morning screener done.\n"
        f"  War Room  ({len(war_room_picks)}): {[p['ticker'] for p in war_room_picks]}\n"
        f"  Watchlist ({len(watchlist)}): {[s['ticker'] for s in watchlist]}"
    )


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

def _fetch_all_summaries() -> list[dict]:
    """Fetches prev day OHLCV + indicators for all NIFTY 50."""
    summaries = []

    for symbol in NIFTY_50:
        try:
            ticker = yf.Ticker(f"{symbol}.NS")
            hist   = ticker.history(period="30d", interval="1d")

            if hist.empty or len(hist) < 15:
                continue

            close  = hist["Close"]
            volume = hist["Volume"]

            rsi      = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            avg_vol  = volume.rolling(20).mean().iloc[-1]
            vol_ratio = round(volume.iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            prev_close   = close.iloc[-2]
            latest_close = close.iloc[-1]
            pct_change   = round(((latest_close - prev_close) / prev_close) * 100, 2)

            ema20    = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
            above_ema = close.iloc[-1] > ema20

            news     = ticker.news
            headline = news[0].get("title", "No news") if news else "No news"

            summaries.append({
                "symbol":      symbol,
                "close":       round(latest_close, 2),
                "pct_change":  pct_change,
                "rsi":         round(rsi, 1) if not pd.isna(rsi) else 50.0,
                "vol_ratio":   vol_ratio,
                "above_ema20": above_ema,
                "headline":    headline[:80],
            })

        except Exception as e:
            continue

    return summaries


def _get_market_bias() -> str:
    """NIFTY 50 index trend → BULLISH / BEARISH / NEUTRAL."""
    try:
        nifty  = yf.Ticker("^NSEI")
        hist   = nifty.history(period="5d", interval="1d")
        if hist.empty:
            return "NEUTRAL"

        closes    = hist["Close"].tail(3).tolist()
        up_days   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down_days = len(closes) - 1 - up_days

        if up_days >= 2:    return "BULLISH"
        if down_days >= 2:  return "BEARISH"
        return "NEUTRAL"

    except Exception as e:
        logger.warning(f"Market bias check failed: {e}")
        return "NEUTRAL"


def _get_stock_market_bias(symbol: str) -> str:
    """
    Per-stock market bias based on that stock's last 5 days trend.
    Independent of NIFTY — allows trading strong stocks in weak markets.
    """
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty or len(hist) < 3:
            return "NEUTRAL"

        closes = hist["Close"].tail(3).tolist()
        up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down_days = len(closes) - 1 - up_days

        if up_days >= 2:
            return "BULLISH"
        if down_days >= 2:
            return "BEARISH"
        return "NEUTRAL"

    except Exception:
        return "NEUTRAL"


def _gemini_pick_stocks(candidates: list[dict], market_bias: str) -> list[dict]:
    """Gemini ranks math top-N; picks must stay in candidates list only."""
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(
            model_name         = "gemini-flash-latest",
            system_instruction = _screener_system_prompt(),
        )

        user_message = _build_screener_message(candidates, market_bias)
        response     = model.generate_content(user_message)
        raw          = response.text.strip()

        raw = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end <= start:
            raise ValueError("No JSON found in response")

        data  = json.loads(raw[start:end])
        picks = data.get("picks", [])

        n = int(cfg("screener", "war_room_picks", 4))
        formatted = []
        for pick in picks[:n]:
            sym = (pick.get("symbol") or "").strip().upper()
            if not sym:
                continue
            formatted.append({
                "ticker":             sym,
                "trading_symbol":     sym,
                "direction":          "BUY_ONLY",
                "confidence":         0,
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
    n = int(cfg("screener", "war_room_picks", 4))

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
        p["math_score"] = sc.get("score", 0)
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
                "confidence":         0,
                "entry_price":        0.0,
                "stop_loss":          0.0,
                "execution_strategy": "TBD",
                "reason":             f"Math pad | Score: {s.get('score', 0)}",
                "math_score":         s.get("score", 0),
            })
            seen.add(sym)
            if len(valid) >= n:
                break

    return valid[:n]


def _screener_system_prompt() -> str:
    n = int(cfg("screener", "war_room_picks", 4))
    return f"""
INTRADAY STOCK SCREENER - Pick {n} stocks for live AI war room debate.

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
{{"picks":[{{"symbol":"NAME","reason":"Catalyst: [news summary] | Tech: RSI+Vol | Risk: [if any]"}}]}}
""".strip()


def _build_screener_message(candidates: list[dict], market_bias: str) -> str:
    n = int(cfg("screener", "war_room_picks", 4))
    symbols = ", ".join(s["symbol"] for s in candidates)
    lines = [
        f"MARKET BIAS: {market_bias}",
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
        "confidence":         0,
        "entry_price":        0.0,
        "stop_loss":          0.0,
        "execution_strategy": "TBD",
        "reason":             f"Math fallback | Score: {s['score']}",
    } for s in top_scored[: int(cfg("screener", "war_room_picks", 4))]]


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("Running morning screener standalone...")
    run_morning_screener()

    with open("data/session_briefing.json") as f:
        briefing = json.load(f)

    print(f"\nMarket bias: {briefing.get('market_bias')}")
    print(f"\nWar Room picks ({len(briefing.get('approved_stocks', []))}):")
    for s in briefing.get("approved_stocks", []):
        print(f"  {s['ticker']} — {s['reason']}")

    print(f"\nWatchlist ({len(briefing.get('watchlist', []))}):")
    for s in briefing.get("watchlist", [])[:5]:
        print(f"  {s['ticker']} — Score: {s['math_score']} — {s['reason']}")
    print("  ... (showing top 5 only)")
