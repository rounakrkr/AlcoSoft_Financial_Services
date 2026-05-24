# ============================================================
#   Mediator — Nvidia Nemotron 120B via OpenRouter
# ============================================================

import logging
from war_room.agents.base_agent import call_agent, load_prompt

logger        = logging.getLogger(__name__)
SYSTEM_PROMPT = load_prompt("mediator")


def consolidate(
    symbol:            str,
    debate_transcript: dict,
    market_data:       dict,
) -> dict:

    user_message = _build_message(symbol, debate_transcript, market_data)
    logger.info(f"[Mediator] Final call on {symbol}")

    return call_agent(
        agent_name    = "Mediator",
        agent_role    = "mediator",
        system_prompt = SYSTEM_PROMPT,
        user_message  = user_message,
        symbol        = symbol,
        round_number  = 99,
    )


def _build_message(symbol: str, debate_transcript: dict,
                   market_data: dict) -> str:
    rounds      = debate_transcript.get("rounds", [])
    rounds_text = ""

    for i, round_data in enumerate(rounds, 1):
        rounds_text += f"\n=== ROUND {i} ===\n"
        for r in round_data:
            rounds_text += (
                f"{r.get('agent')}: {r.get('verdict')} | "
                f"Confidence: {r.get('confidence')}% | "
                f"Reasons: {r.get('reasons')} | "
                f"Concern: {r.get('concern')}\n"
            )

    return f"""
STOCK: {symbol}
CURRENT PRICE: {market_data.get('current_price')}
MARKET BIAS: {market_data.get('market_bias')}

FULL DEBATE:
{rounds_text}

Make your final binding decision now.
""".strip()