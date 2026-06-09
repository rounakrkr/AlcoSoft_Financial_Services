# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/order_verifier.py — Order Reconciliation Engine
#   Verifies orders actually executed on broker, prevents orphans
# ============================================================

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

def normalize_kotak_status(ord_st: Optional[str]) -> Optional[str]:
    """Map Kotak ordSt into distinct unified states."""
    if not ord_st:
        return None
    s = str(ord_st).strip().lower()
    
    if s in ("cancel rejected", "cancel_rejected", "cancel reject"):
        return "CANCEL_REJECTED"
        
    if s in ("complete", "traded", "fully executed", "executed") or "traded" in s:
        return "COMPLETE"
        
    if s in ("cancelled", "canceled", "cancel"):
        return "CANCELLED"
        
    if s in ("rejected", "reject"):
        return "REJECTED"
        
    if s in ("open", "pending", "trigger pending", "partially executed", "partial", "modified", "validation pending", "put order req received"):
        return "PENDING"
        
    # Unknown — treat as pending until proven otherwise
    return "PENDING"


def extract_broker_fill_qty(row: dict | None) -> int:
    """Extract filled quantity from Kotak order row."""
    if not isinstance(row, dict):
        return 0
    from core.safe_io import safe_int
    for key in ("fldQty", "fillQty", "trdQty", "filledQty", "tradedQty"):
        qty = safe_int(row.get(key), 0)
        if qty > 0:
            return qty
    return 0


def _find_order_in_history(data: Any, order_id: str) -> Optional[dict]:
    """Pick latest history row for order_id."""
    if not data:
        return None
    rows: List[dict] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        rows = [data]
    if not rows:
        return None
    oid = str(order_id).strip()
    matched = [r for r in rows if str(r.get("nOrdNo", r.get("on", ""))).strip() == oid]
    return (matched[-1] if matched else rows[-1])


def fetch_kotak_order_row(order_id: str) -> Optional[dict]:
    """
    Query Kotak for order status via order_history, then order_report fallback.
    Returns raw broker row or None.
    """
    if str(order_id).startswith("PAPER-"):
        return {"nOrdNo": order_id, "ordSt": "complete"}

    from core.kotak_client import get_client
    from core.api_resilience import call_broker_api

    client = get_client()

    history = call_broker_api(client.order_history, str(order_id))
    if history and isinstance(history, dict):
        if history.get("error") or history.get("Error") or history.get("Error Message"):
            logger.debug("order_history error for %s: %s", order_id, history)
        else:
            row = _find_order_in_history(history.get("data"), order_id)
            if row and row.get("ordSt"):
                return row

    book = call_broker_api(client.order_report)
    if book and isinstance(book, dict) and "data" in book:
        oid = str(order_id).strip()
        for item in book["data"]:
            if not isinstance(item, dict):
                continue
            if str(item.get("nOrdNo", "")).strip() == oid:
                return item

    return None


class OrderVerifier:
    """Reconciles local orders with broker orders."""

    def __init__(self, max_verification_age: int = 300):
        self.max_verification_age = max_verification_age
        self.pending_orders: Dict[str, dict] = {}

    def record_sent_order(self, order_id: str, symbol: str, details: dict):
        self.pending_orders[order_id] = {
            "symbol": symbol,
            "sent_at": datetime.now(),
            "details": details,
            "verified": False,
            "error": None,
            "broker_status": None,
        }
        logger.info(f"📤 Order recorded: {order_id} ({symbol})")

    def verify_order_executed(self, order_id: str) -> bool:
        if order_id not in self.pending_orders:
            logger.warning(f"Order {order_id} not in pending list")
            return False

        pending = self.pending_orders[order_id]
        elapsed = (datetime.now() - pending["sent_at"]).total_seconds()

        if pending["verified"]:
            return True

        if elapsed > self.max_verification_age:
            pending["error"] = f"Verification timeout after {elapsed:.0f}s"
            logger.error(f"❌ {pending['error']}")
            return False

        try:
            status = self._query_broker_order(order_id, pending["symbol"])
            pending["broker_status"] = status

            if status == "COMPLETE":
                pending["verified"] = True
                logger.info(f"✅ Order {order_id} verified as COMPLETE")
                return True
            if status == "REJECTED":
                pending["error"] = "Order rejected by broker"
                logger.error(f"❌ Order {order_id} rejected by broker")
                return False
            if status == "PENDING":
                logger.debug(f"⏳ Order {order_id} still pending ({elapsed:.0f}s)")
                return False

            logger.warning(f"⚠️ Order {order_id}: unknown broker status ({status})")
            return False

        except Exception as e:
            logger.error(f"❌ Verification failed: {e}")
            return False

    def cleanup_old_orders(self):
        now = datetime.now()
        expired = [
            oid for oid, pending in self.pending_orders.items()
            if (now - pending["sent_at"]).total_seconds() > self.max_verification_age * 2
        ]
        for oid in expired:
            logger.warning(f"⚠️  Removing old pending order: {oid}")
            del self.pending_orders[oid]

    def _query_broker_order(self, order_id: str, symbol: str) -> Optional[str]:
        """Query Kotak broker for order status."""
        logger.debug(f"🔍 Querying broker for order {order_id} ({symbol})")

        if os.getenv("TRADING_MODE", "PAPER") == "PAPER":
            return "COMPLETE"

        row = fetch_kotak_order_row(order_id)
        if not row:
            return "PENDING"

        ord_st = row.get("ordSt") or row.get("orderStatus") or row.get("status")
        normalized = normalize_kotak_status(ord_st)
        if normalized == "REJECTED" and row.get("rejRsn"):
            logger.error(f"Order {order_id} rejected: {row.get('rejRsn')}")
        return normalized

    def get_unverified_orders(self) -> List[str]:
        return [
            oid for oid, pending in self.pending_orders.items()
            if not pending["verified"]
        ]

    def report(self) -> dict:
        verified = sum(1 for p in self.pending_orders.values() if p["verified"])
        unverified = sum(1 for p in self.pending_orders.values() if not p["verified"])
        errors = sum(1 for p in self.pending_orders.values() if p["error"])
        return {
            "total": len(self.pending_orders),
            "verified": verified,
            "pending": unverified,
            "errors": errors,
            "unverified_orders": self.get_unverified_orders(),
        }


_verifier: Optional[OrderVerifier] = None


def get_verifier() -> OrderVerifier:
    global _verifier
    if _verifier is None:
        _verifier = OrderVerifier()
    return _verifier


def record_order_sent(order_id: str, symbol: str, details: dict):
    get_verifier().record_sent_order(order_id, symbol, details)


def verify_order(order_id: str) -> bool:
    return get_verifier().verify_order_executed(order_id)


def wait_for_order_verification(
    order_id: str,
    timeout_sec: float = 45,
    poll_interval: float = 2.0,
) -> str:
    """Poll broker until order is COMPLETE, REJECTED, or TIMEOUT."""
    if str(order_id).startswith("PAPER-"):
        return "COMPLETE"

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if verify_order(order_id):
            return "COMPLETE"
        pending = get_verifier().pending_orders.get(order_id, {})
        if pending.get("error"):
            return "REJECTED"
        time.sleep(poll_interval)

    logger.error(f"❌ Order {order_id} not verified within {timeout_sec}s")
    return "TIMEOUT"


def wait_for_sl_verification(
    order_id: str,
    timeout_sec: float = 3.0,
) -> str:
    """
    Poll broker until SL order is PENDING, TRIGGER_PENDING, COMPLETE, or REJECTED.
    Returns "ACCEPTED" if it's healthy, "REJECTED" if rejected.
    """
    if str(order_id).startswith("PAPER-"):
        return "ACCEPTED"

    # CRITICAL: Kotak RMS rejections are asynchronous. 
    # An order sits in "put order req received" for a split second before rejection.
    # We MUST wait at least 1-2 seconds before checking, otherwise we get a false positive.
    import time
    time.sleep(1.5)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        status = get_verifier()._query_broker_order(order_id, "SL-ORDER")
        if status == "REJECTED":
            return "REJECTED"
        if status in ("PENDING", "COMPLETE"):
            # Give it one more tiny wait to be absolutely sure
            time.sleep(0.5)
            status_check_2 = get_verifier()._query_broker_order(order_id, "SL-ORDER")
            if status_check_2 == "REJECTED":
                return "REJECTED"
            return "ACCEPTED"
            
        time.sleep(0.5)
        
    return "ACCEPTED"  # If it times out without rejection, assume it's sitting in pending queue



def cleanup_verification_queue():
    get_verifier().cleanup_old_orders()


def reconcile_pending_orders() -> dict:
    """
    Poll broker for all unverified orders (call from health monitor loop).
    """
    verifier = get_verifier()
    verifier.cleanup_old_orders()

    summary = {
        "checked": 0,
        "verified": 0,
        "rejected": 0,
        "still_pending": 0,
        "errors": 0,
    }

    for order_id in list(verifier.get_unverified_orders()):
        summary["checked"] += 1
        ok = verifier.verify_order_executed(order_id)
        pending = verifier.pending_orders.get(order_id, {})

        if pending.get("verified"):
            summary["verified"] += 1
        elif pending.get("error"):
            if "rejected" in (pending.get("error") or "").lower():
                summary["rejected"] += 1
            else:
                summary["errors"] += 1
        elif not ok:
            summary["still_pending"] += 1

    if summary["checked"]:
        logger.debug("Order reconciliation: %s", summary)
    return summary


def get_verification_report() -> dict:
    return get_verifier().report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    verifier = get_verifier()
    verifier.record_sent_order("ORD123", "RELIANCE", {"qty": 1})
    print("Report:", verifier.report())
