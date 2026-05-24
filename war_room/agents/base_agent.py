# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   war_room/agents/base_agent.py
#   All OpenRouter models + Native Gemini + Native Groq
#   GPT-OSS standby borrows same key as failed agent
# ============================================================

import json
import logging
import os
import google.generativeai as genai
from groq import Groq
from openai import OpenAI
from core.state_manager import log_war_room_response

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
PROMPTS_DIR     = "war_room/prompts"

# Each OpenRouter model has its own dedicated key
OPENROUTER_KEYS = {
    "technical":  os.getenv("OPENROUTER_KEY_1"),
    "mediator":   os.getenv("OPENROUTER_KEY_2"),
    "reflection": os.getenv("OPENROUTER_KEY_3"),
}

MODELS = {
    "technical":  os.getenv("MODEL_TECHNICAL",  "deepseek/deepseek-v4-flash:free"),
    "mediator":   os.getenv("MODEL_MEDIATOR",   "nvidia/nemotron-3-super-120b-a12b:free"),
    "reflection": os.getenv("MODEL_REFLECTION", "openrouter/owl-alpha"),
    "standby":    os.getenv("MODEL_STANDBY",    "openai/gpt-oss-120b:free"),
}


def load_prompt(agent_name: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{agent_name}.txt")
    with open(path, "r") as f:
        return f.read().strip()


def call_agent(
    agent_name:    str,
    agent_role:    str,
    system_prompt: str,
    user_message:  str,
    symbol:        str,
    round_number:  int,
) -> dict:
    """
    Calls OpenRouter primary model.
    On failure → GPT-OSS standby on same key.
    Only for: technical, mediator, reflection roles.
    Fundamental → use _call_gemini directly.
    Risk → use _call_groq directly.
    """
    key = OPENROUTER_KEYS.get(agent_role)

    if not key:
        logger.error(f"No OpenRouter key found for role: {agent_role}")
        return _error_response()

    # Primary call
    try:
        raw = _call_openrouter(
            key    = key,
            model  = MODELS[agent_role],
            system = system_prompt,
            user   = user_message,
        )
        logger.info(f"[{agent_name}] Primary model success.")

    except Exception as e:
        logger.warning(
            f"[{agent_name}] Primary failed: {e}. "
            f"GPT-OSS standby activating..."
        )
        # Standby uses SAME key — primary is done, key is free
        try:
            raw = _call_openrouter(
                key    = key,
                model  = MODELS["standby"],
                system = system_prompt,
                user   = user_message,
            )
            logger.info(f"[{agent_name}] Standby success.")
        except Exception as e2:
            logger.error(f"[{agent_name}] Standby also failed: {e2}")
            raw = "{}"

    result = _parse_json(raw)

    log_war_room_response(
        agent        = agent_name,
        symbol       = symbol,
        round_number = round_number,
        verdict      = result.get("verdict") or result.get("action", "UNKNOWN"),
        confidence   = result.get("confidence", 0),
        reasons      = result.get("reasons", []),
        concern      = result.get("concern", ""),
    )

    return result


def _call_openrouter(key: str, model: str,
                     system: str, user: str) -> str:
    """Shared OpenRouter caller — used by primary + standby."""
    client = OpenAI(api_key=key, base_url=OPENROUTER_BASE)
    r = client.chat.completions.create(
        model       = model,
        messages    = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens  = 300,
        temperature = 0.3,
    )
    return r.choices[0].message.content


def _call_gemini(system: str, user: str) -> str:
    """Native Gemini — Fundamental Analyst + Morning Screener."""
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel(
        model_name         = "gemini-1.5-flash",
        system_instruction = system,
    )
    return model.generate_content(user).text


def _call_groq(system: str, user: str) -> str:
    """Native Groq — Risk Manager."""
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    r = client.chat.completions.create(
        model       = "llama-3.3-70b-versatile",
        messages    = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens  = 300,
        temperature = 0.3,
    )
    return r.choices[0].message.content


def _parse_json(raw: str) -> dict:
    try:
        cleaned = raw.strip()
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
        start   = cleaned.find("{")
        end     = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            cleaned = cleaned[start:end]
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"JSON parse failed: {e}")
        return _error_response()


def _error_response() -> dict:
    return {
        "verdict":    "WAIT",
        "confidence": 0,
        "reasons":    ["Agent call failed"],
        "concern":    "Could not get response",
        "action":     "NO_TRADE",
    }