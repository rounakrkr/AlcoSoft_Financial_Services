# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   reflection/cognition_llm_client.py — LLM Provider Abstraction
#
#   Unified interface for cognitive agent LLM calls.
#   Supports OpenRouter (cloud) and Ollama (local) with fallback.
#   
#   Agents never interact with providers directly.
#   They call: generate_cognition_response(system, user)
# ============================================================

import logging
import os
import json
import requests
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
#   CONFIGURATION
# ════════════════════════════════════════════════════════════

# OpenRouter Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_KEY_2", "")  # Cognition agent key
OPENROUTER_BASE_URL = "https://openrouter.io/api/v1"
OPENROUTER_MODEL = "mistralai/mistral-7b-instruct"  # Fallback model

# Ollama Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral-small")  # Default local model

# Provider Selection
PREFERRED_PROVIDER = os.getenv("COGNITION_LLM_PROVIDER", "openrouter").lower()
"""Options: 'openrouter', 'ollama', 'auto' (try both)"""

# Retry Configuration
MAX_RETRIES = 2
TIMEOUT_SECONDS = 120  # Ollama can take 60+ seconds for large prompts

# ════════════════════════════════════════════════════════════
#   PROVIDER HEALTH CHECK
# ════════════════════════════════════════════════════════════

def _check_ollama_available() -> bool:
    """Check if Ollama is running and accessible."""
    try:
        response = requests.get(
            f"{OLLAMA_BASE_URL}/api/tags",
            timeout=2
        )
        return response.status_code == 200
    except Exception:
        return False


def _check_openrouter_available() -> bool:
    """Check if OpenRouter API key is configured."""
    return bool(OPENROUTER_API_KEY) and len(OPENROUTER_API_KEY) > 10


def get_available_providers() -> list[str]:
    """Return list of available providers in priority order."""
    available = []
    
    if _check_openrouter_available():
        available.append("openrouter")
    
    if _check_ollama_available():
        available.append("ollama")
    
    return available


# ════════════════════════════════════════════════════════════
#   OPENROUTER PROVIDER
# ════════════════════════════════════════════════════════════

def _call_openrouter(
    system_prompt: str,
    user_message: str,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1000
) -> Optional[str]:
    """
    Call OpenRouter API for cognition response.
    
    Args:
        system_prompt: System role/instructions
        user_message: User query/context
        model: Optional model override
        temperature: Response randomness (0.0-1.0)
        max_tokens: Maximum response length
    
    Returns:
        Response text or None on failure
    """
    if not OPENROUTER_API_KEY:
        logger.warning("OpenRouter API key not configured")
        return None
    
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://alcosoft-trading.local",
        "X-Title": "AlcoSoft Cognitive Agent"
    }
    
    payload = {
        "model": model or OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT_SECONDS
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content")
        else:
            logger.warning(f"OpenRouter returned {response.status_code}: {response.text[:100]}")
            return None
            
    except requests.Timeout:
        logger.warning(f"OpenRouter timeout ({TIMEOUT_SECONDS}s)")
        return None
    except Exception as e:
        logger.warning(f"OpenRouter call failed: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   OLLAMA PROVIDER (Local)
# ════════════════════════════════════════════════════════════

def _call_ollama(
    system_prompt: str,
    user_message: str,
    model: Optional[str] = None,
    temperature: float = 0.7
) -> Optional[str]:
    """
    Call Ollama local LLM for cognition response.
    Uses the /api/chat endpoint (compatible with chat models).

    Args:
        system_prompt: System role/instructions
        user_message: User query/context
        model: Optional model override
        temperature: Response randomness

    Returns:
        Response text or None on failure
    """
    url = f"{OLLAMA_BASE_URL}/api/chat"

    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": temperature,
        "stream": False
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=TIMEOUT_SECONDS
        )

        if response.status_code == 200:
            data = response.json()
            message = data.get("message", {})
            return message.get("content", "").strip()
        else:
            logger.warning(f"Ollama returned {response.status_code}")
            return None

    except requests.Timeout:
        logger.warning(f"Ollama timeout ({TIMEOUT_SECONDS}s)")
        return None
    except Exception as e:
        logger.warning(f"Ollama call failed: {e}")
        return None


# ════════════════════════════════════════════════════════════
#   JSON RESPONSE PARSING
# ════════════════════════════════════════════════════════════

def _extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Extract JSON from LLM response.
    Handles cases where JSON is wrapped in markdown or other text.
    """
    if not text:
        return None
    
    text = text.strip()
    
    # Try parsing directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Try extracting from markdown code block
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    
    # Try extracting from plain code block
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    
    # Try finding JSON object in text
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    
    logger.warning(f"Could not extract JSON from response: {text[:100]}")
    return None


# ════════════════════════════════════════════════════════════
#   MAIN GENERATION FUNCTION
# ════════════════════════════════════════════════════════════

def generate_cognition_response(
    system_prompt: str,
    user_message: str,
    response_format: str = "json"
) -> Optional[Dict[str, Any]]:
    """
    Generate cognitive agent response using available LLM provider.

    LAYER 1 BEHAVIOR (Agents A/B/C/D):
    - If PREFERRED_PROVIDER = "ollama": Use OLLAMA ONLY, NO fallback to OpenRouter
    - If PREFERRED_PROVIDER = "openrouter": Use OpenRouter with Ollama fallback
    - If PREFERRED_PROVIDER = "auto": Try all available providers

    Returns None on failure (caller handles gracefully).

    Args:
        system_prompt: System role (what the agent should do)
        user_message: User query (market data + context)
        response_format: 'json' or 'text'

    Returns:
        Parsed JSON dict or None on failure
    """

    available = get_available_providers()

    if not available:
        logger.error("❌ No LLM provider available (OpenRouter key missing, Ollama not running)")
        return None

    # Determine provider order based on PREFERRED_PROVIDER
    providers = []

    if PREFERRED_PROVIDER == "ollama":
        # LAYER 1: Ollama ONLY, NO fallback to OpenRouter
        if "ollama" in available:
            providers = ["ollama"]
        else:
            logger.error("❌ LAYER 1 (Ollama-only): Ollama not available, skipping cognition")
            return None
    elif PREFERRED_PROVIDER == "openrouter":
        # Reflection/research layer: OpenRouter primary, Ollama fallback
        providers = ["openrouter", "ollama"] if "openrouter" in available else ["ollama"]
    elif PREFERRED_PROVIDER == "auto":
        # Try all available
        providers = available
    else:
        # Default: try all available
        providers = available

    # Try each provider
    for provider_name in providers:
        logger.debug(f"🧠 Trying {provider_name} for cognition response...")

        for attempt in range(MAX_RETRIES):
            try:
                if provider_name == "openrouter":
                    response_text = _call_openrouter(system_prompt, user_message)
                elif provider_name == "ollama":
                    response_text = _call_ollama(system_prompt, user_message)
                else:
                    continue

                if response_text:
                    if response_format == "json":
                        parsed = _extract_json(response_text)
                        if parsed:
                            logger.debug(f"✅ {provider_name} returned valid JSON")
                            return parsed
                        else:
                            logger.warning(f"{provider_name} returned invalid JSON")
                    else:
                        return {"text": response_text}

                # If first attempt failed, try retry
                if attempt < MAX_RETRIES - 1:
                    logger.debug(f"Retrying {provider_name}...")

            except Exception as e:
                logger.warning(f"{provider_name} attempt {attempt+1} failed: {e}")

        logger.warning(f"❌ {provider_name} failed after {MAX_RETRIES} attempts")

    logger.error("❌ Cognition providers exhausted")
    return None


# ════════════════════════════════════════════════════════════
#   PROVIDER STATUS REPORTING
# ════════════════════════════════════════════════════════════

def get_llm_status() -> Dict[str, Any]:
    """Return status of all LLM providers."""
    return {
        "timestamp": datetime.now().isoformat(),
        "preferred_provider": PREFERRED_PROVIDER,
        "openrouter_available": _check_openrouter_available(),
        "ollama_available": _check_ollama_available(),
        "available_providers": get_available_providers(),
        "ollama_url": OLLAMA_BASE_URL,
        "ollama_model": OLLAMA_MODEL,
    }


def log_llm_status():
    """Log current LLM provider status."""
    status = get_llm_status()
    logger.info(f"🧠 LLM Provider Status:")
    logger.info(f"   Preferred: {status['preferred_provider']}")
    logger.info(f"   OpenRouter: {'✅ Available' if status['openrouter_available'] else '❌ Unavailable'}")
    logger.info(f"   Ollama: {'✅ Available' if status['ollama_available'] else '❌ Unavailable'}")
    logger.info(f"   Available: {status['available_providers']}")


# ════════════════════════════════════════════════════════════
#   TESTING
# ════════════════════════════════════════════════════════════

def test_llm_providers():
    """Quick test of available providers."""
    logger.info("Testing LLM providers...")
    
    log_llm_status()
    
    test_system = "You are a helpful assistant. Respond with JSON only."
    test_user = '{"question": "What is 2+2?"}'
    
    result = generate_cognition_response(test_system, test_user)
    
    if result:
        logger.info(f"✅ LLM test passed: {result}")
    else:
        logger.error("❌ LLM test failed - no providers available")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_llm_providers()
