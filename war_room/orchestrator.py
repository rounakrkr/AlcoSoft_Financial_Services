# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   war_room/orchestrator.py — The Debate Engine
#
#   FIXES FROM ORIGINAL:
#   1. time.sleep() → await asyncio.sleep()
#      The original froze the ENTIRE event loop during debates.
#      With 3 stocks x 6 agents x 10s = 3 min freeze every 30 min.
#      During that freeze: stop losses not checked, ticks not processed.
#
#   2. tech_resp.get("reasoning") → tech_resp.get("reasons", [])
#      Agent output format uses "reasons" (list), not "reasoning".
#      Risk Manager was always receiving None for both fields.
# ============================================================

import asyncio
import logging
import os
import time
import pandas as pd
import ta
import yfinance as yf
from datetime import datetime, time as dt_time
from dotenv import load_dotenv

from war_room.agents import technical, fundamental, risk, mediator
from core.data_fetcher import get_candle_history, has_enough_history, get_latest_tick
from core.order_executor import calculate_stop_loss, calculate_quantity
from core.state_manager import save_briefing, load_briefing, get_open_positions

load_dotenv()
logger = logging.getLogger(__name__)

CAPITAL           = float(os.getenv("CAPITAL", 10000))
AGENT_SLEEP       = 10    # Seconds between agent calls (rate limit safety)

WAR_ROOM_START = dt_time(9, 0)
WAR_ROOM_END   = dt_time(15, 0)


# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

async def run_war_room():
    """
    Full debate cycle. Called every 30 minutes by scheduler.
    FIXED: All time.sleep() replaced with await asyncio.sleep()
    so the event loop is never blocked during agent calls.
    """
    now = datetime.now().time()
    if not (WAR_ROOM_START <= now <= WAR_ROOM_END):
        logger.info("War room outside market hours. Skipping.")
        return

    logger.info("=" * 55)
    logger.info("WAR ROOM SESSION STARTING")
    logger.info("=" * 55)

    current_briefing = load_briefing()
    if not current_briefing:
        logger.warning("No briefing found. Morning screener hasn't run yet.")
        return

    stocks_to_debate = current_briefing.get("approved_stocks", [])
    market_bias      = current_briefing.get("market_bias", "NEUTRAL")

    if not stocks_to_debate:
        logger.warning("No stocks in briefing to debate.")
        return

    updated_stocks = []

    for stock in stocks_to_debate:
        symbol = stock["ticker"]
        logger.info(f"Debating: {symbol}")

        try:
            result = await _run_debate(symbol, stock, market_bias)
            if result:
                updated_stocks.append(result)
        except Exception as e:
            logger.error(f"Debate failed for {symbol}: {e}", exc_info=True)
            updated_stocks.append(stock)

        # Rate limit buffer between stocks — ASYNC, does not block loop
        await asyncio.sleep(AGENT_SLEEP)

    new_briefing = {
        "generated_at":    datetime.now().isoformat(),
        "market_bias":     market_bias,
        "approved_stocks": updated_stocks,
        "avoid_list": [
            s["ticker"] for s in updated_stocks
            if s.get("direction") == "AVOID"
        ],
    }

    save_briefing(new_briefing)
    logger.info("=" * 55)
    logger.info("WAR ROOM SESSION COMPLETE — Briefing updated.")
    logger.info("=" * 55)


# ════════════════════════════════════════════════════════════
#   SINGLE STOCK DEBATE
# ════════════════════════════════════════════════════════════

async def _run_debate(symbol: str, stock: dict, market_bias: str) -> dict | None:
    """
    Runs the full multi-round debate for one stock.
    All agent calls are run in an executor (threadpool) so they
    don't block the event loop while waiting for API responses.
    """
    market_data      = _build_market_data(symbol, market_bias)
    fundamental_data = _build_fundamental_data(symbol)
    tick             = get_latest_tick(symbol)
    current_price    = tick["ltp"] if tick else stock.get("entry_price", 0)
    proposed_sl      = calculate_stop_loss(current_price, "BUY")
    proposed_qty     = calculate_quantity(current_price, proposed_sl)

    if not market_data:
        logger.warning(f"Not enough market data for {symbol}. Skipping debate.")
        return None

    loop = asyncio.get_event_loop()

    # ── ROUND 1 — Blind Independent Analysis ─────────────────
    logger.info(f"  Round 1 — Blind analysis for {symbol}...")

    # Run each agent in a thread (they are blocking HTTP calls)
    tech_r1 = await loop.run_in_executor(
        None, lambda: technical.analyze(symbol, market_data, round_number=1)
    )
    await asyncio.sleep(AGENT_SLEEP)   # Rate limit — non-blocking

    fund_r1 = await loop.run_in_executor(
        None, lambda: fundamental.analyze(symbol, fundamental_data, round_number=1)
    )
    await asyncio.sleep(AGENT_SLEEP)

    risk_data_r1 = _build_risk_data(symbol, current_price, proposed_sl, proposed_qty, tech_r1, fund_r1)
    risk_r1 = await loop.run_in_executor(
        None, lambda: risk.analyze(symbol, risk_data_r1, round_number=1)
    )
    await asyncio.sleep(AGENT_SLEEP)

    round1_responses = [tech_r1, fund_r1, risk_r1]

    logger.info(
        f"  Round 1 -> Tech: {tech_r1.get('verdict')} | "
        f"Fund: {fund_r1.get('verdict')} | "
        f"Risk: {risk_r1.get('verdict')}"
    )

    debate_transcript = {"rounds": [round1_responses]}

    if not _is_consensus(round1_responses):
        # ── ROUND 2 — Adversarial Rebuttal ───────────────────
        logger.info(f"  Disagreement. Starting Round 2 for {symbol}...")

        tech_r2 = await loop.run_in_executor(
            None, lambda: technical.analyze(
                symbol, market_data, round_number=2,
                previous_responses=round1_responses
            )
        )
        await asyncio.sleep(AGENT_SLEEP)

        fund_r2 = await loop.run_in_executor(
            None, lambda: fundamental.analyze(
                symbol, fundamental_data, round_number=2,
                previous_responses=round1_responses + [tech_r2]
            )
        )
        await asyncio.sleep(AGENT_SLEEP)

        risk_data_r2 = _build_risk_data(symbol, current_price, proposed_sl, proposed_qty, tech_r2, fund_r2)
        risk_r2 = await loop.run_in_executor(
            None, lambda: risk.analyze(
                symbol, risk_data_r2, round_number=2,
                previous_responses=round1_responses + [tech_r2, fund_r2]
            )
        )
        await asyncio.sleep(AGENT_SLEEP)

        round2_responses = [tech_r2, fund_r2, risk_r2]
        debate_transcript["rounds"].append(round2_responses)

        logger.info(
            f"  Round 2 -> Tech: {tech_r2.get('verdict')} | "
            f"Fund: {fund_r2.get('verdict')} | "
            f"Risk: {risk_r2.get('verdict')}"
        )
    else:
        logger.info(f"  Consensus in Round 1. Skipping Round 2.")

    # ── MEDIATOR — Final Binding Call ─────────────────────────
    logger.info(f"  Mediator making final call for {symbol}...")

    final = await loop.run_in_executor(
        None, lambda: mediator.consolidate(
            symbol=symbol,
            debate_transcript=debate_transcript,
            market_data={"current_price": current_price, "market_bias": market_bias},
        )
    )

    logger.info(
        f"  VERDICT -> {final.get('action')} | "
        f"Confidence: {final.get('confidence_score')}%"
    )

    if final.get("action") == "BUY":
        return {
            "ticker":             symbol,
            "trading_symbol":     stock.get("trading_symbol", symbol),
            "direction":          "BUY_ONLY",
            "confidence":         final.get("confidence_score", 0),
            "entry_price":        final.get("entry_price", current_price),
            "stop_loss":          final.get("stop_loss", proposed_sl),
            "execution_strategy": final.get("execution_strategy", "War_Room"),
            "reason":             final.get("reasoning", ""),
            "debate_rounds":      len(debate_transcript["rounds"]),
        }
    else:
        return {
            "ticker":    symbol,
            "direction": "AVOID",
            "confidence": final.get("confidence_score", 0),
            "reason":    final.get("reasoning", "War room rejected trade"),
        }


# ════════════════════════════════════════════════════════════
#   DATA BUILDERS
# ════════════════════════════════════════════════════════════

def _build_market_data(symbol: str, market_bias: str) -> dict | None:
    if not has_enough_history(symbol, min_candles=50):
        return None

    candles = get_candle_history(symbol)
    df = pd.DataFrame(candles).astype({
        "open": float, "high": float,
        "low": float, "close": float, "volume": float
    })

    rsi_s    = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd_obj = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    ema9     = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    ema21    = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    ema50    = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    avg_vol  = df["volume"].rolling(20).mean()

    from core.strategy import detect_hammer, detect_bullish_engulfing
    pattern = "None"
    if detect_hammer(df):            pattern = "Hammer"
    if detect_bullish_engulfing(df): pattern = "Bullish Engulfing"

    latest = df.iloc[-1]
    return {
        "current_price": round(latest["close"], 2),
        "rsi":           round(rsi_s.iloc[-1], 2),
        "macd":          round(macd_obj.macd().iloc[-1], 4),
        "macd_signal":   round(macd_obj.macd_signal().iloc[-1], 4),
        "ema9":          round(ema9.iloc[-1], 2),
        "ema21":         round(ema21.iloc[-1], 2),
        "ema50":         round(ema50.iloc[-1], 2),
        "pattern":       pattern,
        "volume_ratio":  round(latest["volume"] / avg_vol.iloc[-1], 2),
        "market_bias":   market_bias,
        "recent_candles": df.tail(5)[["open","high","low","close","volume"]].to_dict("records"),
    }


def _build_fundamental_data(symbol: str) -> dict:
    try:
        ticker     = yf.Ticker(f"{symbol}.NS")
        news_items = ticker.news[:5] if ticker.news else []
        headlines  = "\n".join([f"- {n.get('title', '')}" for n in news_items])
        info       = ticker.info or {}
        sector     = info.get("sector", "Unknown")

        nifty     = yf.Ticker("^NSEI")
        nifty_df  = nifty.history(period="1d", interval="5m")
        nifty_chg = 0.0
        if not nifty_df.empty:
            nifty_chg = round(
                (nifty_df["Close"].iloc[-1] - nifty_df["Close"].iloc[0])
                / nifty_df["Close"].iloc[0] * 100, 2
            )
        nifty_trend = "UP" if nifty_chg > 0 else "DOWN"

    except Exception as e:
        logger.warning(f"Fundamental data fetch failed for {symbol}: {e}")
        headlines = "News unavailable"
        sector    = "Unknown"
        nifty_trend = "Unknown"
        nifty_chg   = 0.0

    return {
        "sector":           sector,
        "news":             headlines or "No recent news",
        "nifty_trend":      f"{nifty_trend} ({nifty_chg}%)",
        "market_sentiment": "POSITIVE" if nifty_chg > 0.2 else
                            "NEGATIVE" if nifty_chg < -0.2 else "NEUTRAL",
        "upcoming_events":  "Check manually for earnings dates",
    }


def _build_risk_data(symbol, price, sl, qty, tech_resp, fund_resp) -> dict:
    """
    FIX: Changed .get("reasoning") to .get("reasons", [])
    Agent output format uses "reasons" (list), not "reasoning" (string).
    The original bug meant Risk Manager always received None for both fields.
    """
    capital_at_risk = round(abs(price - sl) * qty, 2)
    open_positions  = get_open_positions()

    return {
        "entry_price":          price,
        "stop_loss":            sl,
        "quantity":             qty,
        "capital_at_risk":      capital_at_risk,
        "open_positions_count": len(open_positions),
        "total_capital":        CAPITAL,
        "tech_verdict":         tech_resp.get("verdict"),
        "tech_reasoning":       tech_resp.get("reasons", []),    # FIXED
        "fund_verdict":         fund_resp.get("verdict"),
        "fund_reasoning":       fund_resp.get("reasons", []),    # FIXED
    }


# ════════════════════════════════════════════════════════════
#   CONSENSUS CHECKER
# ════════════════════════════════════════════════════════════

def _is_consensus(responses: list) -> bool:
    verdicts = [r.get("verdict") or r.get("action", "") for r in responses]
    bullish  = {"BUY", "APPROVE"}
    bearish  = {"AVOID", "WAIT", "REJECT"}
    return all(v in bullish for v in verdicts) or all(v in bearish for v in verdicts)


# ════════════════════════════════════════════════════════════
#   STANDALONE TEST
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # FIX for ModuleNotFoundError when running directly:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("Running one war room debate cycle...")
    print("Make sure session_briefing.json exists first!")
    print("(Run: python -m screener.morning_screener first)\n")

    asyncio.run(run_war_room())

    import json
    try:
        with open("data/session_briefing.json") as f:
            briefing = json.load(f)
        print("\nUpdated briefing:")
        for s in briefing.get("approved_stocks", []):
            print(f"  {s['ticker']} | {s['direction']} | {s.get('confidence', 0)}%")
    except FileNotFoundError:
        print("No briefing file found.")