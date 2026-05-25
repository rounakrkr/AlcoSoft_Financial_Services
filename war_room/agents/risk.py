# ============================================================
#   Risk Manager — Native Groq (Llama 3.3 70B)
# ============================================================

import logging
from war_room.agents.base_agent import _call_groq, _parse_json, load_prompt
from core.state_manager import log_war_room_response

logger        = logging.getLogger(__name__)
SYSTEM_PROMPT = load_prompt("risk")


def analyze(
    symbol:             str,
    risk_data:          dict,
    round_number:       int  = 1,
    previous_responses: list = None,
) -> dict:

    user_message = _build_message(symbol, risk_data, previous_responses)
    logger.info(f"[Risk Manager] {symbol} | Round {round_number}")

    try:
        raw = _call_groq(SYSTEM_PROMPT, user_message)
    except Exception as e:
        logger.error(f"Groq failed: {e}")
        raw = "{}"

    result = _parse_json(raw)

    log_war_room_response(
        agent        = "Risk Manager",
        symbol       = symbol,
        round_number = round_number,
        verdict      = result.get("verdict", "WAIT"),
        confidence   = result.get("confidence", 0),
        reasons      = result.get("reasons", []),
        concern      = result.get("concern", ""),
    )

    return result


def _build_message(symbol: str, risk_data: dict,
                   previous_responses: list = None) -> str:
    msg = f"""
STOCK: {symbol}
ENTRY PRICE: {risk_data.get('entry_price')}
STOP LOSS: {risk_data.get('stop_loss')}
TARGET PRICE: {risk_data.get('target_price')}
RISK/REWARD RATIO: {risk_data.get('risk_reward')}:1
QUANTITY: {risk_data.get('quantity')}
CAPITAL AT RISK: ₹{risk_data.get('capital_at_risk')}
OPEN POSITIONS: {risk_data.get('open_positions_count')}
TOTAL CAPITAL: ₹{risk_data.get('total_capital')}

TECHNICAL ANALYST:
  Verdict: {risk_data.get('tech_verdict')}
  Reasons: {risk_data.get('tech_reasoning')}

FUNDAMENTAL ANALYST:
  Verdict: {risk_data.get('fund_verdict')}
  Reasons: {risk_data.get('fund_reasoning')}
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