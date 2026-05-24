# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   screener/morning_screener.py — 8:45 AM Stock Picker
#   Scans NIFTY 50, picks top 4 stocks for today's session.
#   Writes the FIRST session_briefing.json of the day.
#   War room then updates this every 30 minutes.
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

load_dotenv()
logger = logging.getLogger(__name__)

# ── NIFTY 50 Stock List ───────────────────────────────────────
# Add/remove based on your preference
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "TECHM", "POWERGRID", "NTPC", "ONGC", "BAJFINANCE",
    "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK",
    "JSWSTEEL", "M&M", "SBILIFE", "TATACONSUM", "TATAMOTORS",
    "TATASTEEL", "BRITANNIA", "CIPLA", "COALINDIA", "HDFCLIFE",
    "LTIM", "BPCL", "UPL", "APOLLOHOSP", "BAJAJ-AUTO",
]

# How many stocks to pick for the day
STOCKS_TO_PICK = 4


# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

def run_morning_screener():
    """
    Called at 8:45 AM before market opens.
    Scans all NIFTY 50 stocks, picks best 3 setups,
    writes initial session_briefing.json.
    """
    logger.info("🌅 Morning screener starting...")

    # Step 1 — Fetch data for all stocks
    stock_summaries = _fetch_all_summaries()

    if not stock_summaries:
        logger.error("No stock data fetched. Screener failed.")
        return

    logger.info(f"Fetched data for {len(stock_summaries)} stocks.")

    # Step 2 — Get market bias
    market_bias = _get_market_bias()
    logger.info(f"Market bias: {market_bias}")

    # Step 3 — Ask Gemini to pick top stocks
    picks = _gemini_pick_stocks(stock_summaries, market_bias)

    if not picks:
        logger.error("Gemini failed to pick stocks. Using fallback.")
        picks = _fallback_picks(stock_summaries)

    # Step 4 — Write initial briefing
    briefing = {
        "generated_at":    datetime.now().isoformat(),
        "session_type":    "MORNING_SCREENER",
        "market_bias":     market_bias,
        "approved_stocks": picks,
        "avoid_list":      [],
    }

    save_briefing(briefing)
    logger.info(
        f"✅ Morning screener done. "
        f"Watching: {[p['ticker'] for p in picks]}"
    )


# ════════════════════════════════════════════════════════════
#   DATA FETCHING
# ════════════════════════════════════════════════════════════

def _fetch_all_summaries() -> list[dict]:
    """
    Fetches previous day OHLCV + quick indicators for all NIFTY 50.
    Returns compact summary per stock — Gemini reads these.
    """
    summaries = []

    for symbol in NIFTY_50:
        try:
            ticker = yf.Ticker(f"{symbol}.NS")

            # Last 30 days for indicators
            hist = ticker.history(period="30d", interval="1d")

            if hist.empty or len(hist) < 15:
                continue

            # Calculate quick indicators
            close  = hist["Close"]
            volume = hist["Volume"]

            rsi      = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            avg_vol  = volume.rolling(20).mean().iloc[-1]
            vol_ratio = round(volume.iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            prev_close   = close.iloc[-2]
            latest_close = close.iloc[-1]
            pct_change   = round(((latest_close - prev_close) / prev_close) * 100, 2)

            # EMA trend
            ema20    = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
            above_ema = close.iloc[-1] > ema20

            # One news headline
            news  = ticker.news
            headline = news[0].get("title", "No news") if news else "No news"

            summaries.append({
                "symbol":       symbol,
                "close":        round(latest_close, 2),
                "pct_change":   pct_change,
                "rsi":          round(rsi, 1) if not pd.isna(rsi) else 50.0,
                "vol_ratio":    vol_ratio,
                "above_ema20":  above_ema,
                "headline":     headline[:80],    # Trim long headlines
            })

        except Exception as e:
            logger.debug(f"Failed to fetch {symbol}: {e}")
            continue

    return summaries


def _get_market_bias() -> str:
    """
    Checks Nifty 50 index previous day trend.
    Returns BULLISH, BEARISH, or NEUTRAL.
    """
    try:
        nifty = yf.Ticker("^NSEI")
        hist  = nifty.history(period="5d", interval="1d")

        if hist.empty:
            return "NEUTRAL"

        # Last 3 days trend
        closes    = hist["Close"].tail(3).tolist()
        up_days   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down_days = len(closes) - 1 - up_days

        if up_days >= 2:   return "BULLISH"
        if down_days >= 2: return "BEARISH"
        return "NEUTRAL"

    except Exception as e:
        logger.warning(f"Market bias check failed: {e}")
        return "NEUTRAL"


# ════════════════════════════════════════════════════════════
#   GEMINI STOCK PICKER
# ════════════════════════════════════════════════════════════

def _gemini_pick_stocks(summaries: list[dict], market_bias: str) -> list[dict]:
    """
    Sends all stock summaries to Gemini.
    Asks it to pick top 4 intraday BUY candidates.
    Returns list of stock dicts for session_briefing.json.
    """
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(
            model_name         = "gemini-flash-latest",
            system_instruction = _screener_system_prompt(),
        )

        user_message = _build_screener_message(summaries, market_bias)
        response     = model.generate_content(user_message)
        raw          = response.text.strip()

        # Parse JSON
        raw = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]

        data   = json.loads(raw)
        picks  = data.get("picks", [])

        # Format for briefing
        formatted = []
        for pick in picks[:STOCKS_TO_PICK]:
            formatted.append({
                "ticker":             pick.get("symbol"),
                "trading_symbol":     pick.get("symbol"),
                "direction":          "BUY_ONLY",
                "confidence":         0,      # War room fills this
                "entry_price":        0.0,    # War room fills this
                "stop_loss":          0.0,    # War room fills this
                "execution_strategy": "TBD",  # War room fills this
                "reason":             pick.get("reason", ""),
            })

        return formatted

    except Exception as e:
        logger.error(f"Gemini screener failed: {e}")
        return []


def _screener_system_prompt() -> str:
    return """
You are a pre-market stock screener for an Indian intraday trading desk.

Your job: From the NIFTY 50 data provided, pick exactly 3 stocks
that have the best BUY setup for today's intraday session.

Look for:
- RSI between 30-50 (oversold but recovering)
- Volume ratio above 1.0 (interest building)
- Price above EMA20 (not in downtrend)
- Positive or neutral news
- Reasonable % change (not already run up too much)

OUTPUT FORMAT (strict JSON, nothing else):
{
  "picks": [
    {
      "symbol": "RELIANCE",
      "reason": "one line max explaining why"
    },
    {
      "symbol": "TCS",
      "reason": "one line max explaining why"
    },
    {
      "symbol": "INFY",
      "reason": "one line max explaining why"
    }
  ]
}

RULES:
- Exactly 4 picks always
- reason: one line, max 10 words
- Only pick BUY candidates, never short setups
- If market bias is BEARISH, be very conservative
""".strip()


def _build_screener_message(summaries: list[dict], market_bias: str) -> str:
    """Builds compact stock data string for Gemini."""

    lines = [f"MARKET BIAS TODAY: {market_bias}\n"]
    lines.append("NIFTY 50 SNAPSHOT:\n")

    for s in summaries:
        lines.append(
            f"{s['symbol']}: Close={s['close']} | "
            f"Chg={s['pct_change']}% | RSI={s['rsi']} | "
            f"Vol={s['vol_ratio']}x | AboveEMA={s['above_ema20']} | "
            f"News: {s['headline']}"
        )

    lines.append("\nPick the top 4 intraday BUY candidates from above.")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#   FALLBACK — If Gemini fails
# ════════════════════════════════════════════════════════════

def _fallback_picks(summaries: list[dict]) -> list[dict]:
    """
    Pure math fallback if Gemini API fails.
    Scores each stock, picks top 4.
    """
    scored = []

    for s in summaries:
        score = 0
        if 25 <= s["rsi"] <= 50:    score += 3   # RSI in sweet spot
        if s["vol_ratio"] >= 1.2:   score += 2   # Volume building
        if s["above_ema20"]:        score += 2   # In uptrend
        if -1 < s["pct_change"] < 2: score += 1  # Not already run up

        scored.append((score, s["symbol"]))

    scored.sort(reverse=True)
    top4 = scored[:STOCKS_TO_PICK]

    return [{
        "ticker":             sym,
        "trading_symbol":     sym,
        "direction":          "BUY_ONLY",
        "confidence":         0,
        "entry_price":        0.0,
        "stop_loss":          0.0,
        "execution_strategy": "TBD",
        "reason":             "Math fallback pick",
    } for _, sym in top4]


if __name__ == "__main__":
    print("Running morning screener standalone...")
    run_morning_screener()
    
    # Check what it picked
    import json
    with open("data/session_briefing.json") as f:
        briefing = json.load(f)
    
    print("\nPicked stocks:")
    for s in briefing.get("approved_stocks", []):
        print(f"  {s['ticker']} — {s['reason']}")
    print(f"\nMarket bias: {briefing.get('market_bias')}")