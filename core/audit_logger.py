# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/audit_logger.py — Compliance & Trade Audit Trail
#   Immutable record of every trade decision and execution
# ============================================================

import logging
import json
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIT_DIR = "data/audit"
os.makedirs(AUDIT_DIR, exist_ok=True)


class AuditEntry:
    """Single audit trail entry."""
    
    def __init__(self, event_type: str, data: dict, severity: str = "INFO"):
        self.timestamp = datetime.now().isoformat()
        self.event_type = event_type
        self.data = data
        self.severity = severity  # INFO, WARNING, ERROR, CRITICAL
    
    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "data": self.data,
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict())


class AuditLogger:
    """Structured audit trail for compliance."""
    
    def __init__(self):
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.audit_file = os.path.join(AUDIT_DIR, f"{self.today}.jsonl")
    
    def log_event(self, event_type: str, data: dict, severity: str = "INFO"):
        """Log an event to audit trail."""
        entry = AuditEntry(event_type, data, severity)
        
        with open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")
        
        # Also log to main logger
        level = getattr(logging, severity, logging.INFO)
        logger.log(level, f"[AUDIT] {event_type}: {data}")
    
    def log_trade_signal(
        self,
        symbol: str,
        strategy: str,
        verdict: str,  # BUY/SELL/WAIT
        confidence: int,
        reasons: list,
    ):
        """Log trade signal from strategy."""
        self.log_event(
            "TRADE_SIGNAL",
            {
                "symbol": symbol,
                "strategy": strategy,
                "verdict": verdict,
                "confidence": confidence,
                "reasons": reasons,
            },
        )
    
    def log_order_placed(
        self,
        symbol: str,
        side: str,  # BUY/SELL
        quantity: int,
        price: float,
        order_id: str,
        stop_loss: float = None,
        target: float = None,
    ):
        """Log order placement."""
        self.log_event(
            "ORDER_PLACED",
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "order_id": order_id,
                "stop_loss": stop_loss,
                "target": target,
            },
        )
    
    def log_order_filled(
        self,
        symbol: str,
        order_id: str,
        filled_qty: int,
        filled_price: float,
    ):
        """Log order fill confirmation."""
        self.log_event(
            "ORDER_FILLED",
            {
                "symbol": symbol,
                "order_id": order_id,
                "filled_qty": filled_qty,
                "filled_price": filled_price,
            },
        )
    
    def log_position_closed(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        pnl: float,
        exit_reason: str,
    ):
        """Log position closure and P&L."""
        severity = "INFO" if pnl >= 0 else "WARNING"
        self.log_event(
            "POSITION_CLOSED",
            {
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": pnl,
                "pnl_pct": (pnl / (entry_price * quantity) * 100) if entry_price > 0 else 0,
                "exit_reason": exit_reason,
            },
            severity=severity,
        )
    
    def log_war_room_decision(
        self,
        symbol: str,
        round_number: int,
        agents_verdict: dict,  # agent -> verdict
        mediator_action: str,
    ):
        """Log war room debate outcome."""
        self.log_event(
            "WAR_ROOM_DECISION",
            {
                "symbol": symbol,
                "round": round_number,
                "agents": agents_verdict,
                "final_action": mediator_action,
            },
        )
    
    def log_risk_check_failed(self, symbol: str, reason: str):
        """Log when a trade is rejected by risk manager."""
        self.log_event(
            "RISK_CHECK_FAILED",
            {
                "symbol": symbol,
                "reason": reason,
            },
            severity="WARNING",
        )
    
    def log_system_error(self, error_type: str, message: str, context: dict = None):
        """Log system error."""
        self.log_event(
            "SYSTEM_ERROR",
            {
                "error_type": error_type,
                "message": message,
                "context": context or {},
            },
            severity="ERROR",
        )
    
    def log_circuit_breaker_trip(self, breaker_name: str, reason: str):
        """Log when a circuit breaker trips."""
        self.log_event(
            "CIRCUIT_BREAKER_TRIP",
            {
                "breaker": breaker_name,
                "reason": reason,
            },
            severity="CRITICAL",
        )
    
    def get_today_events(self, event_type: str = None) -> list:
        """Get audit events from today."""
        if not os.path.exists(self.audit_file):
            return []
        
        events = []
        with open(self.audit_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                    if event_type is None or event.get("event_type") == event_type:
                        events.append(event)
                except json.JSONDecodeError:
                    pass
        
        return events
    
    def export_compliance_report(self, filepath: str = None) -> str:
        """Generate compliance report for the day."""
        if filepath is None:
            filepath = os.path.join(AUDIT_DIR, f"{self.today}_report.txt")
        
        events = self.get_today_events()
        trades = [e for e in events if e["event_type"] == "POSITION_CLOSED"]
        signals = [e for e in events if e["event_type"] == "TRADE_SIGNAL"]
        errors = [e for e in events if e["severity"] in ["ERROR", "CRITICAL"]]
        
        report_lines = [
            f"ALCOSOFT COMPLIANCE REPORT — {self.today}",
            "=" * 70,
            f"Total events: {len(events)}",
            f"Trades executed: {len(trades)}",
            f"Signals generated: {len(signals)}",
            f"Errors: {len(errors)}",
            "",
            "TRADES:",
        ]
        
        total_pnl = 0
        for trade in trades:
            data = trade["data"]
            pnl = data["pnl"]
            total_pnl += pnl
            report_lines.append(
                f"  {data['symbol']}: {data['quantity']} @ "
                f"₹{data['entry_price']} → ₹{data['exit_price']} | "
                f"P&L: ₹{pnl:.2f} ({data.get('pnl_pct', 0):.1f}%) | "
                f"{data['exit_reason']}"
            )
        
        if trades:
            report_lines.append(f"\nTotal P&L: ₹{total_pnl:.2f}")
        
        if errors:
            report_lines.append(f"\nERRORS ({len(errors)}):")
            for error in errors[-5:]:  # Last 5 errors
                report_lines.append(f"  {error['data']['error_type']}: {error['data']['message']}")
        
        report_lines.extend(["", "=" * 70])
        report = "\n".join(report_lines)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)
        
        logger.info(f"Compliance report saved: {filepath}")
        return report


# Global audit logger
_audit_logger: AuditLogger = None


def get_audit_logger() -> AuditLogger:
    """Get or create global audit logger."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger


# Convenience functions
def audit_trade_signal(*args, **kwargs):
    get_audit_logger().log_trade_signal(*args, **kwargs)


def audit_order_placed(*args, **kwargs):
    get_audit_logger().log_order_placed(*args, **kwargs)


def audit_position_closed(*args, **kwargs):
    get_audit_logger().log_position_closed(*args, **kwargs)


def audit_war_room_decision(*args, **kwargs):
    get_audit_logger().log_war_room_decision(*args, **kwargs)


def audit_system_error(*args, **kwargs):
    get_audit_logger().log_system_error(*args, **kwargs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    logger = get_audit_logger()
    logger.log_trade_signal("RELIANCE", "RSI_MACD", "BUY", 85, ["RSI oversold", "MACD crossover"])
    logger.log_order_placed("RELIANCE", "BUY", 1, 2500.0, "ORD123", stop_loss=2400.0, target=2600.0)
    logger.log_position_closed("RELIANCE", 2500.0, 2550.0, 1, 50.0, "PROFIT_TARGET")
