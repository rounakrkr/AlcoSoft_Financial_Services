# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/order_verifier.py — Order Reconciliation Engine
#   Verifies orders actually executed on broker, prevents orphans
# ============================================================

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class OrderVerifier:
    """Reconciles local orders with broker orders."""
    
    def __init__(self, max_verification_age: int = 300):
        """
        Args:
            max_verification_age: Max seconds to wait for order confirmation
        """
        self.max_verification_age = max_verification_age
        self.pending_orders: Dict[str, dict] = {}  # order_id -> {time, ...}
    
    def record_sent_order(self, order_id: str, symbol: str, details: dict):
        """Record that we sent an order to the broker."""
        self.pending_orders[order_id] = {
            "symbol": symbol,
            "sent_at": datetime.now(),
            "details": details,
            "verified": False,
            "error": None,
        }
        logger.info(f"📤 Order recorded: {order_id} ({symbol})")
    
    def verify_order_executed(self, order_id: str) -> bool:
        """Check if order was actually executed by broker."""
        if order_id not in self.pending_orders:
            logger.warning(f"Order {order_id} not in pending list")
            return False
        
        pending = self.pending_orders[order_id]
        elapsed = (datetime.now() - pending["sent_at"]).total_seconds()
        
        if pending["verified"]:
            logger.info(f"✅ Order {order_id} already verified")
            return True
        
        if elapsed > self.max_verification_age:
            pending["error"] = f"Verification timeout after {elapsed:.0f}s"
            logger.error(f"❌ {pending['error']}")
            return False
        
        # Query broker for order status
        try:
            status = self._query_broker_order(order_id, pending["symbol"])
            if status == "COMPLETE":
                pending["verified"] = True
                logger.info(f"✅ Order {order_id} verified as COMPLETE")
                return True
            elif status == "PENDING":
                logger.debug(f"⏳ Order {order_id} still pending ({elapsed:.0f}s)")
                return False
            elif status == "REJECTED":
                pending["error"] = "Order rejected by broker"
                logger.error(f"❌ {pending['error']}")
                return False
        except Exception as e:
            logger.error(f"❌ Verification failed: {e}")
            return False
    
    def cleanup_old_orders(self):
        """Remove old pending orders."""
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
        try:
            from core.kotak_client import get_client
            from core.api_resilience import call_broker_api
            
            client = get_client()
            
            # This depends on Kotak API — implement based on their documentation
            # For now, return a placeholder
            logger.debug(f"🔍 Querying broker for order {order_id}")
            
            # TODO: Replace with actual Kotak API call
            # For now, assume order is pending
            return "PENDING"
            
        except Exception as e:
            logger.error(f"Failed to query broker: {e}")
            return None
    
    def get_unverified_orders(self) -> List[str]:
        """Get list of orders still waiting for verification."""
        return [
            oid for oid, pending in self.pending_orders.items()
            if not pending["verified"]
        ]
    
    def report(self) -> dict:
        """Get verification status report."""
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


# Global verifier instance
_verifier: Optional[OrderVerifier] = None


def get_verifier() -> OrderVerifier:
    """Get or create global order verifier."""
    global _verifier
    if _verifier is None:
        _verifier = OrderVerifier()
    return _verifier


def record_order_sent(order_id: str, symbol: str, details: dict):
    """Record that an order was sent."""
    verifier = get_verifier()
    verifier.record_sent_order(order_id, symbol, details)


def verify_order(order_id: str) -> bool:
    """Verify that an order executed."""
    verifier = get_verifier()
    return verifier.verify_order_executed(order_id)


def cleanup_verification_queue():
    """Clean up old pending orders."""
    verifier = get_verifier()
    verifier.cleanup_old_orders()


def get_verification_report() -> dict:
    """Get verification status."""
    verifier = get_verifier()
    return verifier.report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    verifier = get_verifier()
    
    # Simulate order lifecycle
    verifier.record_sent_order("ORD123", "RELIANCE", {"qty": 1})
    print("Report:", verifier.report())
