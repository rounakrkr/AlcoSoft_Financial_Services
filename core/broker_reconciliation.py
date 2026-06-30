import logging
import os
from typing import Any

from core.safe_io import safe_float, safe_int


logger = logging.getLogger(__name__)


def _normalize_symbol(raw: str) -> str:
    if not raw:
        return ""
    text = str(raw).strip().upper()
    if text.endswith("-EQ"):
        return text[:-3]
    return text.split("-")[0] if "-" in text else text


def _trading_symbol(raw: str, symbol: str) -> str:
    text = str(raw or "").strip().upper()
    if text:
        return text
    return f"{symbol}-EQ"


def _rows_from_response(response: Any) -> list[dict]:
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if not isinstance(response, dict):
        return []
    if response.get("error") or response.get("Error") or response.get("Error Message"):
        logger.warning("Broker response error: %s", response)
        return []
    data = response.get("data") or response.get("Data") or response.get("positions") or []
    if isinstance(data, dict):
        data = data.get("data") or data.get("positions") or [data]
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _extract_qty(row: dict) -> int:
    # Try exact field matches first
    for key in ("netQty", "net_qty", "flNetQty", "qty", "netQuantity", "cfBuyQty"):
        if key in row and row[key] not in (None, ""):
            qty = safe_int(row[key], 0)
            if qty:
                return qty
    
    # Fallback: Calculate net from buy/sell quantities (Kotak format)
    # Try standard field names first
    buy_q = safe_float(row.get("buyQty") or row.get("cfBuyQty"), 0.0)
    sell_q = safe_float(row.get("sellQty") or row.get("cfSellQty"), 0.0)
    
    # If no data, try Kotak's specific format (fl = futures/live)
    if buy_q == 0 and sell_q == 0:
        buy_q = safe_float(row.get("flBuyQty"), 0.0)
        sell_q = safe_float(row.get("flSellQty"), 0.0)
    
    return int(buy_q - sell_q)


def _extract_entry_price(row: dict) -> float:
    for key in (
        "avgPrc",
        "avgPrice",
        "buyAvg",
        "buyAvgPrc",
        "cfBuyAvgPrc",
        "netRate",
        "entry_price",
    ):
        price = safe_float(row.get(key), 0.0)
        if price > 0:
            return price
    
    # Kotak format: calculate from amount / quantity
    # buyAmt / flBuyQty = average price
    buy_amt = safe_float(row.get("buyAmt"), 0.0)
    buy_qty = safe_float(row.get("flBuyQty"), 0.0)
    if buy_amt > 0 and buy_qty > 0:
        return buy_amt / buy_qty
    
    return 0.0


def _parse_broker_position_rows(response: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _rows_from_response(response):
        raw_symbol = (
            row.get("trdSym")
            or row.get("pTrdSymbol")
            or row.get("trading_symbol")
            or row.get("sym")
            or row.get("symbol")
            or ""
        )
        symbol = _normalize_symbol(raw_symbol)
        if not symbol:
            continue

        qty = _extract_qty(row)
        if qty == 0:
            continue

        entry_price = _extract_entry_price(row)
        existing = out.get(symbol)
        if existing:
            total_qty = existing["quantity"] + qty
            if total_qty != 0 and entry_price > 0 and existing["entry_price"] > 0:
                existing["entry_price"] = (
                    (existing["entry_price"] * existing["quantity"]) + (entry_price * qty)
                ) / total_qty
            existing["quantity"] = total_qty
            continue

        out[symbol] = {
            "symbol": symbol,
            "quantity": qty,
            "entry_price": entry_price,
            "trading_symbol": _trading_symbol(raw_symbol, symbol),
            "product": str(row.get("prod") or row.get("product") or "UNKNOWN"),
            "raw": row,
        }
    return out


def _parse_broker_positions(response: Any) -> dict[str, int]:
    return {
        symbol: details["quantity"]
        for symbol, details in _parse_broker_position_rows(response).items()
    }


def _latest_or_entry_price(symbol: str, entry_price: float) -> float:
    try:
        from core.data_fetcher import get_latest_tick

        tick = get_latest_tick(symbol)
        if tick:
            price = safe_float(tick.get("ltp"), entry_price)
            if price > 0:
                return price
    except Exception:
        pass
    return entry_price if entry_price > 0 else 0.01


def _strategy_for_symbol(symbol: str) -> str:
    try:
        from core.state_manager import load_briefing

        briefing = load_briefing() or {}
        for bucket in ("approved_stocks", "watchlist"):
            for item in briefing.get(bucket, []):
                if _normalize_symbol(item.get("ticker") or item.get("symbol")) == symbol:
                    return (
                        item.get("strategy")
                        or item.get("set_name")
                        or item.get("execution_strategy")
                        or "BROKER_RECOVERY"
                    )
    except Exception:
        pass
    return "BROKER_RECOVERY"


def _build_recovered_trade(details: dict) -> dict:
    from core.order_executor import calculate_stop_loss, calculate_target

    symbol = details["symbol"]
    entry = _latest_or_entry_price(symbol, safe_float(details.get("entry_price"), 0.0))
    stop_loss = calculate_stop_loss(entry, "BUY")
    return {
        "symbol": symbol,
        "trading_symbol": details.get("trading_symbol") or f"{symbol}-EQ",
        "quantity": max(0, safe_int(details.get("quantity"), 0)),
        "entry_price": entry,
        "stop_loss": stop_loss,
        "trailing_sl": stop_loss,
        "target_price": calculate_target(entry, stop_loss),
        "strategy": _strategy_for_symbol(symbol),
        "confidence": 0,
        "order_id": "BROKER-RECOVERED",
        "sl_order_id": "",
        "notes": "Recovered from broker position",
    }


def _fetch_broker_positions() -> dict[str, dict]:
    from core.api_resilience import call_broker_api
    from core.kotak_client import get_client

    client = get_client()
    raw = call_broker_api(client.positions)
    if raw is None:
        raise RuntimeError("broker positions returned no data")
    
    # Log the RAW response structure
    logger.warning("🔍 BROKER POSITIONS RAW RESPONSE: %s", raw)
    if isinstance(raw, dict) and raw.get("data"):
        logger.warning("   Data structure: %s", type(raw.get("data")))
        if isinstance(raw.get("data"), list) and len(raw.get("data", [])) > 0:
            logger.warning("   First row keys: %s", list(raw["data"][0].keys()))
    
    result = _parse_broker_position_rows(raw)
    logger.warning("   Parsed positions: %d | Details: %s", len(result), {k: v.get("quantity") for k, v in result.items()})
    
    return result


def _fetch_order_report_rows() -> list[dict]:
    from core.api_resilience import call_broker_api
    from core.kotak_client import get_client

    client = get_client()
    raw = call_broker_api(client.order_report)
    return _rows_from_response(raw)


def _order_id(row: dict) -> str:
    return str(row.get("nOrdNo") or row.get("order_id") or row.get("ordNo") or "").strip()


def _order_symbol(row: dict) -> str:
    return _normalize_symbol(
        row.get("trdSym")
        or row.get("trading_symbol")
        or row.get("sym")
        or row.get("symbol")
        or ""
    )


def _order_status(row: dict) -> str:
    return str(row.get("ordSt") or row.get("orderStatus") or row.get("status") or "").strip().lower()


def _order_side(row: dict) -> str:
    return str(row.get("trnsTp") or row.get("transaction_type") or row.get("side") or "").strip().upper()


def _order_type(row: dict) -> str:
    return str(row.get("prcTp") or row.get("ordTyp") or row.get("order_type") or row.get("type") or "").strip().upper()


def _order_trigger(row: dict) -> float:
    return safe_float(row.get("triggerPrice") or row.get("trigger_price") or row.get("trgPrc"), 0.0)


def _is_active_status(status: str) -> bool:
    if not status:
        return True  # Assume active if status unknown
    status_lower = status.lower()
    # F025: Expanded inactive keywords for thorough status detection
    inactive_keywords = (
        'complete', 'traded', 'executed', 'cancel', 'reject',
        'expired', 'filled', 'done', 'closed',
    )
    return not any(word in status_lower for word in inactive_keywords)


def _is_active_sl_sell(row: dict) -> bool:
    typ = _order_type(row)
    side = _order_side(row)
    return side in ("S", "SELL") and typ.startswith("SL") and _is_active_status(_order_status(row))


def _active_sl_orders_by_symbol() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in _fetch_order_report_rows():
        if not _is_active_sl_sell(row):
            continue
        symbol = _order_symbol(row)
        if not symbol:
            continue
        out.setdefault(symbol, []).append(row)
    return out


def _active_stop_price(position: dict) -> float:
    stop_loss = safe_float(position.get("stop_loss"), 0.0)
    trailing = safe_float(position.get("trailing_sl"), 0.0)
    return max(stop_loss, trailing)


def _cancel_duplicate_sl_orders(symbol: str, keep_id: str, rows: list[dict]) -> int:
    cancelled = 0
    try:
        from core.order_executor import _cancel_kotak_order
    except Exception as exc:
        logger.warning("Cannot cancel duplicate SL orders for %s: %s", symbol, exc)
        return cancelled

    for row in rows:
        oid = _order_id(row)
        if not oid or oid == keep_id:
            continue
        _cancel_kotak_order(oid)
        cancelled += 1
        logger.warning("Cancelled duplicate SL order for %s: %s", symbol, oid)
    return cancelled


def _ensure_single_sl_for_position(position: dict, active_rows: list[dict], live: bool) -> dict:
    from core.state_manager import update_sl_order_id

    symbol = str(position.get("symbol", "")).upper()
    qty = safe_int(position.get("quantity"), 0)
    target_trigger = _active_stop_price(position)
    local_id = str(position.get("kotak_sl_order_id") or "").strip()

    result = {"symbol": symbol, "status": "ok", "created": False, "updated": False, "duplicates_cancelled": 0}
    if not symbol or qty <= 0 or target_trigger <= 0:
        result["status"] = "invalid_position"
        return result

    active_rows = [row for row in active_rows if _order_id(row)]
    keep_row = None
    if local_id:
        keep_row = next((row for row in active_rows if _order_id(row) == local_id), None)
    if keep_row is None and active_rows:
        keep_row = active_rows[0]

    if keep_row:
        keep_id = _order_id(keep_row)
        if keep_id != local_id:
            update_sl_order_id(symbol, keep_id)
            result["updated"] = True

        result["duplicates_cancelled"] = _cancel_duplicate_sl_orders(symbol, keep_id, active_rows)

        broker_trigger = _order_trigger(keep_row)
        if live and broker_trigger > 0 and abs(broker_trigger - target_trigger) >= 0.05:
            try:
                from core.order_executor import _modify_sl_order

                if _modify_sl_order(keep_id, target_trigger, qty):
                    result["updated"] = True
                    logger.info("Synchronized SL trigger for %s: %s -> %s", symbol, broker_trigger, target_trigger)
            except Exception as exc:
                result["status"] = "modify_failed"
                logger.warning("SL modify failed for %s: %s", symbol, exc)
        return result

    if live:
        try:
            from core.order_executor import _send_kotak_sl_order

            new_id = _send_kotak_sl_order(
                trading_symbol=position.get("trading_symbol") or f"{symbol}-EQ",
                quantity=qty,
                trigger_price=target_trigger,
                product=position.get("product") or "MIS",
            )
            if new_id:
                update_sl_order_id(symbol, new_id)
                result["created"] = True
                result["status"] = "created"
            else:
                result["status"] = "create_failed"
        except Exception as exc:
            result["status"] = "create_failed"
            logger.warning("SL recreation failed for %s: %s", symbol, exc)
    else:
        update_sl_order_id(symbol, local_id or f"PAPER-SL-{symbol}")
        result["created"] = not local_id
        result["status"] = "paper_synced"

    return result


def reconcile_stop_loss_orders() -> dict:
    from core.state_manager import get_open_positions

    mode = os.getenv("TRADING_MODE", "PAPER")
    positions = get_open_positions()
    summary = {
        "mode": mode,
        "checked": len(positions),
        "created": 0,
        "updated": 0,
        "duplicates_cancelled": 0,
        "errors": [],
    }

    active_by_symbol: dict[str, list[dict]] = {}
    if mode == "LIVE":
        try:
            active_by_symbol = _active_sl_orders_by_symbol()
        except Exception as exc:
            summary["errors"].append(str(exc))
            logger.error("SL reconciliation could not load order report: %s", exc)
            return summary

    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        result = _ensure_single_sl_for_position(
            position,
            active_by_symbol.get(symbol, []),
            live=mode == "LIVE",
        )
        summary["created"] += int(bool(result.get("created")))
        summary["updated"] += int(bool(result.get("updated")))
        summary["duplicates_cancelled"] += safe_int(result.get("duplicates_cancelled"), 0)
        if result.get("status") in ("create_failed", "modify_failed", "invalid_position"):
            summary["errors"].append(f"{symbol}:{result.get('status')}")

    return summary


def reconcile_broker_vs_local() -> dict:
    from core.state_manager import (
        get_open_positions,
        mark_position_reconciled_closed,
        recover_open_position,
        update_open_position_from_broker,
    )

    mode = os.getenv("TRADING_MODE", "PAPER")
    local = get_open_positions()
    summary = {
        "mode": mode,
        "local_open": [str(p.get("symbol", "")).upper() for p in local if p.get("symbol")],
        "broker_open": [],
        "matched": [],
        "local_only": [],
        "broker_only": [],
        "mismatch": [],
        "repaired": [],
        "sl_reconciliation": {},
        "ok": True,
    }

    if mode != "LIVE":
        summary["sl_reconciliation"] = reconcile_stop_loss_orders()
        logger.info("Reconciliation skipped broker position pull in PAPER mode.")
        return summary

    try:
        broker = _fetch_broker_positions()
    except Exception as exc:
        summary["ok"] = False
        summary["error"] = str(exc)
        logger.error("Broker reconciliation failed before repairs: %s", exc)
        return summary

    summary["broker_open"] = [
        f"{sym}({details['quantity']})"
        for sym, details in broker.items()
        if details["quantity"] > 0
    ]

    local_by_symbol = {_normalize_symbol(p.get("symbol")): p for p in local}
    broker_long = {sym: details for sym, details in broker.items() if details["quantity"] > 0}

    # SAFETY CHECK: If broker API returned NO positions but we have local positions open,
    # do NOT auto-close them. The API may have failed or is in an inconsistent state.
    if len(local) > 0 and len(broker_long) == 0:
        logger.error(
            "🚨 SAFETY BLOCK: Broker API returned NO positions but %d local position(s) exist: %s | "
            "Will NOT auto-close. Verify broker connection or manually close positions.",
            len(local),
            [p.get("symbol") for p in local],
        )
        summary["ok"] = False
        summary["error"] = "Broker API returned zero positions while local positions exist"
        summary["local_only"] = list(local_by_symbol.keys())
        return summary

    for symbol, position in local_by_symbol.items():
        broker_details = broker_long.get(symbol)
        if not broker_details:
            summary["local_only"].append(symbol)
            exit_price = _latest_or_entry_price(symbol, safe_float(position.get("entry_price"), 0.0))
            if mark_position_reconciled_closed(symbol, exit_price, "BROKER_RECONCILED_CLOSED"):
                summary["repaired"].append(f"{symbol}:local_closed")
            continue

        local_qty = safe_int(position.get("quantity"), 0)
        broker_qty = safe_int(broker_details.get("quantity"), 0)
        broker_entry = safe_float(broker_details.get("entry_price"), 0.0)
        local_entry = safe_float(position.get("entry_price"), 0.0)
        if local_qty != broker_qty or (broker_entry > 0 and abs(local_entry - broker_entry) >= 0.05):
            summary["mismatch"].append(symbol)
            updates = {
                "quantity": broker_qty,
                "trading_symbol": broker_details.get("trading_symbol"),
                "notes": "Broker reconciliation repaired local quantity/entry",
            }
            if broker_entry > 0:
                from core.order_executor import calculate_stop_loss, calculate_target

                stop_loss = calculate_stop_loss(broker_entry, "BUY")
                updates.update({
                    "entry_price": broker_entry,
                    "stop_loss": stop_loss,
                    "trailing_sl": max(safe_float(position.get("trailing_sl"), stop_loss), stop_loss),
                    "target_price": calculate_target(broker_entry, stop_loss),
                })
            if update_open_position_from_broker(symbol, updates):
                summary["repaired"].append(f"{symbol}:mismatch")
        else:
            summary["matched"].append(symbol)

    for symbol, details in broker_long.items():
        if symbol in local_by_symbol:
            continue
        # F008: Only recover MIS (intraday) positions — skip CNC/delivery holdings
        product = str(details.get('product', 'MIS')).upper()
        if product not in ('MIS', 'INTRADAY', 'CO', 'BO'):
            logger.warning(
                'Skipping broker-only position %s — product=%s is not intraday. '
                'This may be a CNC/delivery holding.',
                symbol, product,
            )
            summary['broker_only'].append(f'{symbol}(SKIPPED:{product})')
            continue
        summary["broker_only"].append(symbol)
        trade = _build_recovered_trade(details)
        if trade["quantity"] > 0:
            recover_open_position(trade)
            summary["repaired"].append(f"{symbol}:broker_recovered")

    if summary["local_only"] or summary["broker_only"] or summary["mismatch"]:
        summary["ok"] = False
        logger.warning(
            "Position reconciliation repaired state | local_only=%s | broker_only=%s | mismatch=%s",
            summary["local_only"],
            summary["broker_only"],
            summary["mismatch"],
        )
    else:
        logger.info("Broker reconciliation OK | %s position(s) matched", len(summary["matched"]))

    summary["sl_reconciliation"] = reconcile_stop_loss_orders()
    return summary


def reconcile_positions():
    """
    FX13: Reconcile broker positions with internal records.
    Broker Truth > Local Cache > Guessing
    """
    try:
        return reconcile_broker_vs_local()
    except Exception as e:
        logger.error(f"Reconciliation failed: {e}")
        return {"reconciled": 0, "mismatches": 0, "error": str(e), "ok": False}
