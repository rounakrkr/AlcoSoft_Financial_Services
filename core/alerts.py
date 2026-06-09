import logging
import os
import json
from datetime import datetime, time
from typing import Optional
from core.telegram_notifier import push_telegram_message

logger = logging.getLogger(__name__)

# Ensure data directory exists for last_alert.json
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
LAST_ALERT_FILE = os.path.join(DATA_DIR, "last_alert.json")

def is_quiet_window() -> bool:
    """Check if current time falls within the predefined quiet windows."""
    now = datetime.now().time()
    # Lunch chop
    if time(12, 0) <= now <= time(13, 30):
        return True
    # Late session fade
    if time(15, 0) <= now <= time(15, 30):
        return True
    return False

def save_last_alert(message: str, severity: str):
    """Save the last alert to disk for the /health command to read."""
    try:
        # Strip HTML tags for clean storage
        clean_msg = message.replace("<b>", "").replace("</b>", "")
        data = {
            "message": clean_msg,
            "severity": severity,
            "time": datetime.now().strftime("%H:%M:%S")
        }
        with open(LAST_ALERT_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save last alert: {e}")

class SlackTransport:
    def send(self, message: str, severity: str, silent: bool = False) -> bool:
        webhook = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
        if not webhook:
            return False

        # Slack doesn't natively support silent pushes easily via simple webhook,
        # but we preserve the transport for validation period.
        icons = {
            "INFO": "ℹ️",
            "BUY": "🟢",
            "SELL": "🔴",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "CRITICAL": "🚨",
            "HIGH": "🔔"
        }
        icon = icons.get(severity.upper(), "📢")
        mode = os.getenv("TRADING_MODE", "PAPER")

        # Strip HTML for slack
        clean_msg = message.replace("<b>", "*").replace("</b>", "*")

        try:
            import requests
            r = requests.post(
                webhook,
                json={"text": f"{icon} *AlcoSoft [{mode}]*\n{clean_msg}"},
                timeout=5,
            )
            if r.status_code >= 400:
                logger.warning("Slack alert failed: HTTP %s", r.status_code)
                return False
            return True
        except Exception as e:
            logger.warning("Slack alert error: %s", e)
            return False

class TelegramTransport:
    def send(self, message: str, severity: str, silent: bool = False) -> bool:
        mode = os.getenv("TRADING_MODE", "PAPER")
        # Prefix the mode if not already in the message
        if "AlcoSoft" not in message:
            formatted = f"<b>[{mode}]</b>\n\n{message}"
        else:
            formatted = message
            
        return push_telegram_message(formatted, silent=silent)

class NotificationService:
    # Pluggable transports
    transports = [SlackTransport(), TelegramTransport()]

    @classmethod
    def broadcast(cls, message: str, priority: str = "HIGH"):
        """
        Broadcast a message across all configured transports.
        Applies quiet window logic aggressively to minimize noise.
        """
        save_last_alert(message, priority)
        
        silent = False
        if priority.upper() == "HIGH" and is_quiet_window():
            silent = True
            
        if priority.upper() == "LOW":
            silent = True

        for transport in cls.transports:
            try:
                transport.send(message, priority, silent=silent)
            except Exception as e:
                logger.error(f"Transport {transport.__class__.__name__} failed: {e}")

def send_alert(message: str, severity: str = "INFO") -> bool:
    """Legacy wrapper for backward compatibility."""
    priority = "HIGH"
    if severity.upper() in ["CRITICAL", "ERROR"]:
        priority = "CRITICAL"
    NotificationService.broadcast(message, priority)
    return True

def alert_buy(symbol: str, qty: int, price: float, strategy: str, order_id: str = ""):
    message = (
        f"🟢 <b>ENTRY: {symbol} (Long)</b>\n"
        f"Price: ₹{price:.2f} | Qty: {qty}\n"
        f"Reason: {strategy}"
    )
    NotificationService.broadcast(message, "HIGH")

def alert_sell(symbol: str, qty: int, price: float, reason: str, pnl: Optional[float] = None):
    """Tone mapping ensures stops are not presented as failures."""
    if pnl is not None and pnl < 0:
        header = f"🛡️ <b>STOP EXECUTED: {symbol}</b>"
        footer = "Status: Risk managed successfully."
    elif pnl is not None and pnl > 0:
        header = f"🎯 <b>TARGET HIT: {symbol}</b>"
        footer = "Status: Closed."
    else:
        header = f"📉 <b>CLOSED: {symbol}</b>"
        footer = "Status: Flat."
        
    pnl_line = f"\nRealized: ₹{pnl:+.2f}" if pnl is not None else ""
    
    message = (
        f"{header}\n"
        f"Exit: ₹{price:.2f} | Qty: {qty}\n"
        f"Reason: {reason}{pnl_line}\n"
        f"{footer}"
    )
    NotificationService.broadcast(message, "HIGH")

def alert_critical(message: str):
    NotificationService.broadcast(f"🚨 <b>CRITICAL:</b> {message}", "CRITICAL")
    
def alert_drawdown_lock(pnl: float):
    message = (
        "🛑 <b>DAILY LOSS LIMIT REACHED</b>\n"
        f"Daily PnL: ₹{pnl:+.2f}\n"
        "Trading disabled for remainder of session.\n"
        "Capital protection active."
    )
    NotificationService.broadcast(message, "HIGH")
