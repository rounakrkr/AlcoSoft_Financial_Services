# ============================================================
#   Technical Analyst — DeepSeek V4 Flash via OpenRouter
# ============================================================

import logging
from war_room.agents.base_agent import call_agent, load_prompt

logger        = logging.getLogger(__name__)
SYSTEM_PROMPT = load_prompt("technical")


def analyze(
    symbol:             str,
    market_data:        dict,
    round_number:       int  = 1,
    previous_responses: list = None,
) -> dict:

    user_message = _build_message(symbol, market_data, previous_responses)
    logger.info(f"[Technical Analyst] {symbol} | Round {round_number}")

    return call_agent(
        agent_name    = "Technical Analyst",
        agent_role    = "technical",
        system_prompt = SYSTEM_PROMPT,
        user_message  = user_message,
        symbol        = symbol,
        round_number  = round_number,
    )


def _build_message(symbol: str, market_data: dict,
                   previous_responses: list = None) -> str:
    msg = f"""
STOCK: {symbol}
CURRENT PRICE: {market_data.get('current_price')}
RSI (14): {market_data.get('rsi')}
MACD: {market_data.get('macd')}
MACD Signal: {market_data.get('macd_signal')}
EMA 9: {market_data.get('ema9')}
EMA 21: {market_data.get('ema21')}
EMA 50: {market_data.get('ema50')}
Candlestick Pattern: {market_data.get('pattern', 'None detected')}
Volume vs Avg: {market_data.get('volume_ratio')}x
Recent candles (OHLCV): {market_data.get('recent_candles')}
""".strip()

    if previous_responses:
        msg += "\n\n--- PREVIOUS RESPONSES (defend or revise your position) ---"
        for r in previous_responses:
            msg += (
                f"\n{r.get('agent')}: {r.get('verdict')} | "
                f"Confidence: {r.get('confidence')}% | "
                f"Reasons: {r.get('reasons')} | "
                f"Concern: {r.get('concern')}"
            )

    return msg