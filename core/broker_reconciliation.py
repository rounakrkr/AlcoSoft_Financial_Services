# ============================================================
#   ALCOSOFT — Broker ↔ local DB reconciliation (LIVE)
#   Run at startup after Kotak login to catch orphan positions.
# ============================================================

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_symbol(raw: str) -> str:
    """RELIANCE-EQ → RELIANCE for comparison."""
    if not raw:
        return ""
    s = str(raw).strip().upper()
    if s.endswith("-EQ"):
        return s[:-3]
    return s.split("-")[0] if "-" in s else s


def _parse_broker_positions(response: Any) -> dict[str, int]:
    """
    Parse Kotak positions() JSON into {symbol: net_qty}.
    Handles several response shapes defensively.
    """
    out: dict[str, int] = {}

    rows: list = []
    if isinstance(response, list):
        rows = response
    elif isinstance(response, dict):
        if response.get("error") or response.get("Error") or response.get("Error Message"):
            logger.warning("Broker positions error: %s", response)
            return out
        rows = response.get("data") or response.get("Data") or response.get("positions") or []

    for row in rows:
        if not isinstance(row, dict):
            continue

        sym = (
            row.get("trdSym")
            or row.get("pTrdSymbol")
            or row.get("sym")
            or row.get("symbol")
            or ""
        )
        sym = _normalize_symbol(sym)
        if not sym:
            continue

        qty = 0
        for key in ("netQty", "net_qty", "flNetQty", "qty", "buyQty", "cfBuyQty"):
            if key in row and row[key] not in (None, ""):
                try:
                    qty = int(float(row[key]))
                    break
                except (TypeError, ValueError):
                    continue

        if qty == 0:
            buy_q = float(row.get("buyQty") or row.get("cfBuyQty") or 0)
            sell_q = float(row.get("sellQty") or row.get("cfSellQty") or 0)
            qty = int(buy_q - sell_q)

        if qty != 0:
            out[sym] = out.get(sym, 0) + qty

    return out


def reconcile_broker_vs_local() -> dict:
    """
    Compare SQLite OPEN positions with Kotak positions (LIVE only).
    Returns summary dict; logs warnings for mismatches.
    """
    from core.state_manager import get_open_positions

    summary = {
        "mode": os.getenv("TRADING_MODE", "PAPER"),
        "local_open": [],
        "broker_open": [],
        "matched": [],
        "local_only": [],
        "broker_only": [],
        "ok": True,
    }

    local = get_open_positions()
    summary["local_open"] = [p["symbol"] for p in local]

    if summary["mode"] != "LIVE":
        logger.info(
            "Reconciliation skipped (PAPER): %s local open position(s)",
            len(local),
        )
        return summary

    try:
        from core.kotak_client import get_client
        from core.api_resilience import call_broker_api

        client = get_client()
        raw = call_broker_api(client.positions)
        broker = _parse_broker_positions(raw)
    except Exception as e:
        logger.error("Broker reconciliation failed: %s", e)
        summary["ok"] = False
        summary["error"] = str(e)
        return summary

    summary["broker_open"] = [
        f"{sym}({qty})" for sym, qty in broker.items() if qty > 0
    ]

    local_syms = {_normalize_symbol(p["symbol"]) for p in local}
    broker_long = {s for s, q in broker.items() if q > 0}

    for p in local:
        sym = _normalize_symbol(p["symbol"])
        if sym in broker_long:
            summary["matched"].append(sym)
        else:
            summary["local_only"].append(sym)

    for sym in broker_long:
        if sym not in local_syms:
            summary["broker_only"].append(sym)

    if summary["local_only"] or summary["broker_only"]:
        summary["ok"] = False
        logger.warning(
            "⚠️ POSITION MISMATCH | local_only=%s | broker_only=%s | matched=%s",
            summary["local_only"],
            summary["broker_only"],
            summary["matched"],
        )
        try:
            from core.alerts import alert_critical
            alert_critical(
                f"Position mismatch!\n"
                f"Local only: {summary['local_only'] or 'none'}\n"
                f"Broker only: {summary['broker_only'] or 'none'}"
            )
        except Exception:
            pass
    else:
        logger.info(
            "✅ Broker reconciliation OK | %s position(s) matched",
            len(summary["matched"]),
        )

    return summary
