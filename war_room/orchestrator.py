# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   war_room/orchestrator.py — The Debate Engine
#   Changes: time.sleep → await asyncio.sleep,
#   agent calls → asyncio.to_thread,
#   get_event_loop → get_running_loop
# ============================================================

import asyncio
import logging
import os
import pandas as pd
import ta
import yfinance as yf
from datetime import datetime, time as dt_time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from war_room.agents import technical, fundamental, risk, mediator
from core.data_fetcher import get_candle_history, has_enough_history, get_latest_tick
from core.order_executor import calculate_stop_loss, calculate_quantity, calculate_target
from core.state_manager import save_briefing, load_briefing, get_open_positions
from core.strategy import detect_hammer, detect_bullish_engulfing

load_dotenv()
logger = logging.getLogger(__name__)

WAR_ROOM_INTERVAL = int(os.getenv("WAR_ROOM_INTERVAL_MINUTES", 30))
CAPITAL           = float(os.getenv("CAPITAL", 10000))
AGENT_SLEEP       = 10

WAR_ROOM_START = dt_time(9, 0)
WAR_ROOM_END   = dt_time(15, 0)

nifty_chg 
# ════════════════════════════════════════════════════════════
#   MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

async def run_war_room():
    now = datetime.now().time()
    if not (WAR_ROOM_START <= now <= WAR_ROOM_END):
        logger.info("War room outside market hours. Skipping.")
        return

    logger.info("=" * 55)
    logger.info("⚔️  WAR ROOM SESSION STARTING")
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
        logger.info(f"\n🔍 Debating: {symbol}")

        try:
            result = await _run_debate(symbol, stock, market_bias)
            updated_stocks.append(result if result else stock)
        except Exception as e:
            logger.error(f"Debate failed for {symbol}: {e}", exc_info=True)
            updated_stocks.append(stock)

        # ← FIXED: was time.sleep — blocked the event loop
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
    logger.info("✅ WAR ROOM COMPLETE — Briefing updated.")
    logger.info("=" * 55)


# ════════════════════════════════════════════════════════════
#   SINGLE STOCK DEBATE
# ════════════════════════════════════════════════════════════

async def _run_debate(symbol: str, stock: dict, market_bias: str) -> dict | None:

    market_data      = _build_market_data(symbol, market_bias)
    fundamental_data = await asyncio.to_thread(_build_fundamental_data, symbol)

    # ── Determine current price ──────────────────────────────
    # Priority: live tick > market_data (candle close) > screener entry_price > 0
    tick = get_latest_tick(symbol)
    if tick and tick.get("ltp", 0) > 0:
        current_price = tick["ltp"]
    elif market_data and market_data.get("current_price", 0) > 0:
        current_price = market_data["current_price"]
    else:
        current_price = stock.get("entry_price", 0)

    # ── FALLBACK: yFinance se live price lo ──────────────
    if current_price <= 0:
        logger.warning(f"No tick yet for {symbol}. Fetching via yFinance...")
        try:
            import yfinance as yf
            hist = yf.Ticker(f"{symbol}.NS").history(period="1d", interval="1m")
            if not hist.empty:
                current_price = round(float(hist["Close"].iloc[-1]), 2)
                logger.info(f"yFinance fallback price for {symbol}: Rs.{current_price}")
        except Exception as e:
            logger.error(f"yFinance fallback also failed for {symbol}: {e}")

    if current_price <= 0:
        logger.warning(f"No valid price for {symbol}. Skipping debate.")
        return None

    proposed_sl   = calculate_stop_loss(current_price, "BUY")
    proposed_qty  = calculate_quantity(current_price, proposed_sl)

    if not market_data:
        logger.warning(f"Not enough market data for {symbol}. Skipping.")
        return None

    # ── ROUND 1 — Blind Independent Analysis ─────────────────
    logger.info(f"  📊 Round 1 — Blind analysis...")

    # ← FIXED: asyncio.to_thread prevents blocking the event loop
    tech_r1 = await asyncio.to_thread(
        technical.analyze, symbol, market_data, 1
    )
    await asyncio.sleep(AGENT_SLEEP)

    fund_r1 = await asyncio.to_thread(
        fundamental.analyze, symbol, fundamental_data, 1
    )
    await asyncio.sleep(AGENT_SLEEP)

    risk_data_r1 = _build_risk_data(
        symbol, current_price, proposed_sl,
        proposed_qty, tech_r1, fund_r1
    )
    risk_r1 = await asyncio.to_thread(
        risk.analyze, symbol, risk_data_r1, 1
    )
    await asyncio.sleep(AGENT_SLEEP)

    round1_responses = [tech_r1, fund_r1, risk_r1]

    logger.info(
        f"  Round 1 → Tech: {tech_r1.get('verdict')} | "
        f"Fund: {fund_r1.get('verdict')} | "
        f"Risk: {risk_r1.get('verdict')}"
    )

    debate_transcript = {"rounds": [round1_responses]}

    # ── Consensus Check ───────────────────────────────────────
    if _is_consensus(round1_responses):
        logger.info(f"  ✅ Consensus in Round 1. Skipping Round 2.")
    else:
        logger.info(f"  ⚔️  Disagreement. Starting Round 2...")

        tech_r2 = await asyncio.to_thread(
            technical.analyze, symbol, market_data, 2, round1_responses
        )
        await asyncio.sleep(AGENT_SLEEP)

        fund_r2 = await asyncio.to_thread(
            fundamental.analyze, symbol, fundamental_data, 2,
            round1_responses + [tech_r2]
        )
        await asyncio.sleep(AGENT_SLEEP)

        risk_data_r2 = _build_risk_data(
            symbol, current_price, proposed_sl,
            proposed_qty, tech_r2, fund_r2
        )
        risk_r2 = await asyncio.to_thread(
            risk.analyze, symbol, risk_data_r2, 2,
            round1_responses + [tech_r2, fund_r2]
        )
        await asyncio.sleep(AGENT_SLEEP)

        round2_responses = [tech_r2, fund_r2, risk_r2]
        debate_transcript["rounds"].append(round2_responses)

        logger.info(
            f"  Round 2 → Tech: {tech_r2.get('verdict')} | "
            f"Fund: {fund_r2.get('verdict')} | "
            f"Risk: {risk_r2.get('verdict')}"
        )

    # ── MEDIATOR ─────────────────────────────────────────────
    logger.info(f"  ⚖️  Mediator deciding...")

    final = await asyncio.to_thread(
        mediator.consolidate,
        symbol,
        debate_transcript,
        {"current_price": current_price, "market_bias": market_bias},
    )

    logger.info(
        f"  MEDIATOR → {final.get('action')} | "
        f"Confidence: {final.get('confidence_score')}%"
    )

    if final.get("action") == "BUY":
        entry  = final.get("entry_price", current_price)
        sl     = final.get("stop_loss", proposed_sl)
        return {
            "ticker":             symbol,
            "trading_symbol":     stock.get("trading_symbol", symbol),
            "direction":          "BUY_ONLY",
            "confidence":         final.get("confidence_score", 0),
            "entry_price":        entry,
            "stop_loss":          sl,
            "target_price":       calculate_target(entry, sl),
            "execution_strategy": final.get("execution_strategy", "War_Room"),
            "reason":             final.get("reasoning", ""),
            "debate_rounds":      len(debate_transcript["rounds"]),
        }
    else:
        return {
            "ticker":    symbol,
            "direction": "AVOID",
            "confidence": final.get("confidence_score", 0),
            "reason":    final.get("reasoning", "War room rejected"),
        }


# ════════════════════════════════════════════════════════════
#   DATA BUILDERS
# ════════════════════════════════════════════════════════════

def _build_market_data(symbol: str, market_bias: str) -> dict | None:
    if not has_enough_history(symbol, min_candles=26):
        logger.info(f"WebSocket history low for {symbol}. Fetching from yFinance...")
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period="1d", interval="1m")   # 1 day, 1-minute candles
            if hist.empty or len(hist) < 26:
                logger.warning(f"Insufficient data for {symbol}. Skipping.")
                return None
            hist = hist.rename(columns={
                "Open": "open", "High": "high",
                "Low": "low", "Close": "close", "Volume": "volume"
            })
            df = hist[["open","high","low","close","volume"]].copy()
            logger.info(f"yFinance fallback: {len(df)} candles loaded for {symbol}.")
        except Exception as e:
            logger.error(f"yFinance fallback failed for {symbol}: {e}")
            return None
    else:
        candles = get_candle_history(symbol)
        df = pd.DataFrame(candles).astype({
            "open": float, "high": float,
            "low":  float, "close": float, "volume": float
        })

    rsi_s    = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd_obj = ta.trend.MACD(df["close"], window_slow=26,
                              window_fast=12, window_sign=9)
    ema9     = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    ema21    = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    ema50    = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    avg_vol  = df["volume"].rolling(20).mean()

    pattern = "None"
    if detect_hammer(df):            pattern = "Hammer"
    if detect_bullish_engulfing(df): pattern = "Bullish Engulfing"

    latest = df.iloc[-1]
    avg_v  = avg_vol.iloc[-1]

    return {
        "current_price": round(latest["close"], 2),
        "rsi":           round(rsi_s.iloc[-1], 2),
        "macd":          round(macd_obj.macd().iloc[-1], 4),
        "macd_signal":   round(macd_obj.macd_signal().iloc[-1], 4),
        "ema9":          round(ema9.iloc[-1], 2),
        "ema21":         round(ema21.iloc[-1], 2),
        "ema50":         round(ema50.iloc[-1], 2),
        "pattern":       pattern,
        "volume_ratio":  round(latest["volume"] / avg_v, 2) if avg_v > 0 else 1.0,
        "market_bias":   market_bias,
        "recent_candles": df.tail(5)[
            ["open","high","low","close","volume"]
        ].to_dict("records"),
    }


def _build_fundamental_data(symbol: str) -> dict:
    """Runs in thread via asyncio.to_thread — safe for yfinance blocking calls."""
    try:
        ticker     = yf.Ticker(f"{symbol}.NS")
        news_items = ticker.news[:5] if ticker.news else []
        headlines  = "\n".join([
            f"- {n.get('title', '')}" for n in news_items
        ])
        info   = ticker.info or {}
        sector = info.get("sector", "Unknown")

        nifty    = yf.Ticker("^NSEI")
        nifty_df = nifty.history(period="1d", interval="5m")
        nifty_chg = 0.0
        if not nifty_df.empty:
            nifty_chg = round(
                (nifty_df["Close"].iloc[-1] - nifty_df["Close"].iloc[0])
                / nifty_df["Close"].iloc[0] * 100, 2
            )
        trend = "UP" if nifty_chg > 0 else "DOWN"

    except Exception as e:
        logger.warning(f"Fundamental data failed for {symbol}: {e}")
        headlines = "News unavailable"
        sector    = "Unknown"
        trend     = "Unknown"
        nifty_chg = 0.0

    return {
        "sector":           sector,
        "news":             headlines or "No recent news",
        "nifty_trend":      f"{trend} ({nifty_chg}%)",
        "market_sentiment": (
            "POSITIVE" if nifty_chg > 0.5 else
            "NEGATIVE" if nifty_chg < -0.5 else "NEUTRAL"
        ),
        "upcoming_events": (
            "No known events" if not info.get("earningsTimestamp")
            else f"Earnings on {datetime.fromtimestamp(info['earningsTimestamp']).strftime('%d-%b')}"
        ),
    }


def _build_risk_data(symbol, price, sl, qty, tech_resp, fund_resp) -> dict:
    capital_at_risk = round(abs(price - sl) * qty, 2)
    target          = calculate_target(price, sl)        # ← add
    rr_ratio        = round(abs(target - price) / abs(price - sl), 2)  # ← add

    return {
        "entry_price":          price,
        "stop_loss":            sl,
        "target_price":         target,      # ← add
        "risk_reward":          rr_ratio,    # ← add
        "quantity":             qty,
        "capital_at_risk":      capital_at_risk,
        "open_positions_count": len(get_open_positions()),
        "total_capital":        CAPITAL,
        "tech_verdict":         tech_resp.get("verdict"),
        "tech_reasoning":       tech_resp.get("reasons", []),
        "fund_verdict":         fund_resp.get("verdict"),
        "fund_reasoning":       fund_resp.get("reasons", []),
    }


# ════════════════════════════════════════════════════════════
#   CONSENSUS CHECKER
# ════════════════════════════════════════════════════════════

def _is_consensus(responses: list) -> bool:
    verdicts = [r.get("verdict") or r.get("action", "") for r in responses]
    bullish  = {"BUY", "APPROVE"}
    bearish = {"AVOID", "WAIT", "REJECT", "CONDITIONAL"}
    return (
        all(v in bullish for v in verdicts) or
        all(v in bearish for v in verdicts)
    )


# ════════════════════════════════════════════════════════════
#   SCHEDULER
# ════════════════════════════════════════════════════════════

def start_war_room_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_war_room,
        trigger       = "interval",
        minutes       = WAR_ROOM_INTERVAL,
        id            = "war_room",
        name          = "AlcoSoft War Room",
        max_instances = 1,
    )
    scheduler.start()
    logger.info(f"⏰ War room scheduled every {WAR_ROOM_INTERVAL} minutes.")
    return scheduler


if __name__ == "__main__":
    import asyncio
    print("Running one war room debate cycle...")
    asyncio.run(run_war_room()) 