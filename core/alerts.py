# ============================================================
#   ALCOSOFT — Slack / webhook alerts (optional via .env)
#   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
# ============================================================

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def send_alert(message: str, severity: str = "INFO") -> bool:
    """
    Post a short message to Slack incoming webhook.
    Returns True if sent, False if webhook not configured or request failed.
    """
    webhook = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    if not webhook:
        return False

    icons = {
        "INFO": "ℹ️",
        "BUY": "🟢",
        "SELL": "🔴",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "CRITICAL": "🚨",
    }
    icon = icons.get(severity.upper(), "📢")
    mode = os.getenv("TRADING_MODE", "PAPER")

    try:
        import requests
        r = requests.post(
            webhook,
            json={"text": f"{icon} *AlcoSoft [{mode}]*\n{message}"},
            timeout=8,
        )
        if r.status_code >= 400:
            logger.warning("Slack alert failed: HTTP %s", r.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("Slack alert error: %s", e)
        return False


def alert_buy(symbol: str, qty: int, price: float, strategy: str, order_id: str = ""):
    send_alert(
        f"*BUY* `{symbol}` | Qty {qty} @ ₹{price:.2f}\n"
        f"Strategy: {strategy}\n"
        f"Order: {order_id or 'PAPER'}",
        "BUY",
    )


def alert_sell(symbol: str, qty: int, price: float, reason: str, pnl: Optional[float] = None):
    pnl_line = f"\nP&L: ₹{pnl:+.2f}" if pnl is not None else ""
    send_alert(
        f"*SELL* `{symbol}` | Qty {qty} @ ₹{price:.2f}\n"
        f"Reason: {reason}{pnl_line}",
        "SELL",
    )


def alert_critical(message: str):
    send_alert(message, "CRITICAL")
