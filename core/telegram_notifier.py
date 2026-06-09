import os
import requests
import logging

logger = logging.getLogger(__name__)

def push_telegram_message(text: str, silent: bool = False) -> bool:
    """
    Synchronous, lightweight push to Telegram API.
    Designed to never block the main trading engine.
    Fails gracefully if Telegram is unreachable.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not bot_token or not chat_id:
        logger.debug("Telegram credentials not configured. Skipping alert.")
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent
    }
    
    try:
        # Timeout is strictly set to 3 seconds to prevent engine stalls
        r = requests.post(url, json=payload, timeout=3.0)
        if r.status_code >= 400:
            logger.error(f"Telegram API Error [{r.status_code}]: {r.text}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram network error: {e}")
        return False
