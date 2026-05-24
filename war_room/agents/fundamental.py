# ============================================================
#   Fundamental Analyst — Native Gemini 1.5 Flash
# ============================================================

import logging
from war_room.agents.base_agent import _call_gemini, _parse_json, load_prompt
from core.state_manager import log_war_room_response

logger        = logging.getLogger(__name__)
SYSTEM_PROMPT = load_prompt("fundamental")


def analyze(
    symbol:             str,
    fundamental_data:   dict,
    round_number:       int  = 1,
    previous_responses: list = None,
) -> dict:

    user_message = _build_message(symbol, fundamental_data, previous_responses)
    logger.info(f"[Fundamental Analyst] {symbol} | Round {round_number}")

    try:
        raw = _call_gemini(SYSTEM_PROMPT, user_message)
    except Exception as e:
        logger.error(f"Gemini failed: {e}")
        raw = "{}"

    result = _parse_json(raw)

    log_war_room_response(
        agent        = "Fundamental Analyst",
        symbol       = symbol,
        round_number = round_number,
        verdict      = result.get("verdict", "WAIT"),
        confidence   = result.get("confidence", 0),
        reasons      = result.get("reasons", []),
        concern      = result.get("concern", ""),
    )

    return result


def _build_message(symbol: str, fundamental_data: dict,
                   previous_responses: list = None) -> str:
    msg = f"""
STOCK: {symbol}
SECTOR: {fundamental_data.get('sector', 'Unknown')}
RECENT NEWS:
{fundamental_data.get('news', 'No news available')}
NIFTY TREND: {fundamental_data.get('nifty_trend', 'Unknown')}
MARKET SENTIMENT: {fundamental_data.get('market_sentiment', 'NEUTRAL')}
UPCOMING EVENTS: {fundamental_data.get('upcoming_events', 'None known')}
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