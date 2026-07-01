import json
import logging
import os
import re
import threading
import time

import google.generativeai as genai
from groq import Groq
from openai import OpenAI


logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

OPENROUTER_KEYS = {
    "technical": os.getenv("OPENROUTER_KEY_4"),
    "fundamental": os.getenv("OPENROUTER_KEY_3"),
    "mediator": os.getenv("OPENROUTER_KEY_2"),
    "reflection": os.getenv("OPENROUTER_KEY_1"),
}

MODELS = {
    "technical": os.getenv("MODEL_TECHNICAL", "deepseek/deepseek-v4-flash:free"),
    "fundamental": os.getenv("MODEL_FUNDAMENTAL", "nousresearch/hermes-3-llama-3.1-405b:free"),
    "mediator": os.getenv("MODEL_MEDIATOR", "google/gemma-4-31b-it:free"),
    "reflection": os.getenv("MODEL_REFLECTION", "openrouter/owl-alpha"),
    "standby": os.getenv("MODEL_STANDBY", "openai/gpt-oss-120b:free"),
}


def call_openrouter(key: str, model: str, system: str, user: str, max_tokens: int = 300) -> str:
    if not key:
        raise RuntimeError("OpenRouter key missing")
    client = OpenAI(api_key=key, base_url=OPENROUTER_BASE)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return response.choices[0].message.content


def call_gemini(system: str, user: str) -> str:
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel(
        model_name="gemini-flash-latest",
        system_instruction=system,
    )
    return model.generate_content(user).text


def call_groq(system: str, user: str) -> str:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=300,
        temperature=0.3,
    )
    return response.choices[0].message.content


def parse_json(raw: str) -> dict:
    try:
        cleaned = (raw or "").strip()
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            cleaned = cleaned[start:end]
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else error_response()
    except Exception as exc:
        logger.error("LLM JSON parse failed: %s | raw=%s", exc, (raw or "")[:200])
        return error_response()


def error_response() -> dict:
    return {
        "verdict": "WAIT",
        "confidence": 0,
        "reasons": ["Agent call failed"],
        "concern": "Could not get response",
        "action": "NO_TRADE",
    }


def gateway_online() -> bool:
    """P3-7/P3-8: True if at least one LLM provider credential is configured."""
    if any(OPENROUTER_KEYS.values()):
        return True
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY"))


_gateway_alert_lock = threading.Lock()
_last_gateway_alert_ts = 0.0
_GATEWAY_ALERT_COOLDOWN_SEC = 1800  # 30 min — avoid alert spam on repeated failures


def alert_gateway_offline(context: str) -> None:
    """
    P3-7/P3-8: surface a SILENT cognition/LLM outage as a loud, rate-limited alert.

    Cognition/reflection is research-only and must NEVER block trading, so this
    only logs loudly and fires an operator alert — it never raises.
    """
    global _last_gateway_alert_ts
    logger.error("🧠❌ LLM cognition gateway OFFLINE: %s", context)
    now = time.time()
    with _gateway_alert_lock:
        if now - _last_gateway_alert_ts < _GATEWAY_ALERT_COOLDOWN_SEC:
            return
        _last_gateway_alert_ts = now
    try:
        from core.alerts import alert_critical
        alert_critical(
            f"Cognition/LLM gateway OFFLINE — {context}. "
            f"Reflection & cognitive agents are not producing insights (trading unaffected)."
        )
    except Exception as exc:
        logger.warning("Failed to send gateway-offline alert: %s", exc)


_call_openrouter = call_openrouter
_call_gemini = call_gemini
_call_groq = call_groq
_parse_json = parse_json
