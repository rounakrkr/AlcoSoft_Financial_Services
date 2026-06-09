import os
import sys
import sqlite3
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.state_manager import _update_daily_stats, get_today_stats, _get_conn, initialize_db

def test_fx05_dual_track_reporting():
    print("\n--- Test FX05: Dual-Track Equity & Margin Reporting ---")
    
    # Initialize DB to ensure columns exist
    initialize_db()

    # Clear today's row to start fresh
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        conn.execute("DELETE FROM daily_stats WHERE date = ?", (today,))
        conn.execute("DELETE FROM trades WHERE date = ?", (today,))

    # Mock getting capital
    with patch('core.order_executor._get_available_capital', return_value=15000.0) as mock_cap:
        # Mock margin status
        mock_margin = {
            "unrealized_pnl": 500.0
        }
        with patch('core.order_executor.get_margin_status', return_value=mock_margin):
            # Run update
            _update_daily_stats()

    stats = get_today_stats()
    
    print("\nTest Case 1: Initial creation (capital_start initialized)")
    print(f"capital_start: {stats.get('capital_start')}")
    print(f"broker_buying_power: {stats.get('broker_buying_power')}")
    print(f"realized_equity: {stats.get('realized_equity')}")
    print(f"unrealized_pnl: {stats.get('unrealized_pnl')}")
    print(f"estimated_total_equity: {stats.get('estimated_total_equity')}")

    # Now add a dummy closed trade
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO trades (symbol, action, quantity, entry_price, status, date, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("TEST1", "BUY", 10, 100, "CLOSED", today, 1000.0))
        
    # Run update again, mocking available capital to have changed (e.g., due to margin)
    with patch('core.order_executor._get_available_capital', return_value=12000.0):
        with patch('core.order_executor.get_margin_status', return_value={"unrealized_pnl": 250.0}):
            _update_daily_stats()

    stats2 = get_today_stats()
    
    print("\nTest Case 2: After trade closed (+1000 PnL)")
    print(f"capital_start: {stats2.get('capital_start')} (Should remain same)")
    print(f"gross_pnl: {stats2.get('gross_pnl')} (Should be 1000.0)")
    print(f"broker_buying_power: {stats2.get('broker_buying_power')} (Should track _get_available_capital)")
    print(f"realized_equity: {stats2.get('realized_equity')} (Should be start + 1000)")
    print(f"unrealized_pnl: {stats2.get('unrealized_pnl')} (Should be 250.0)")
    print(f"estimated_total_equity: {stats2.get('estimated_total_equity')} (Should be realized + 250)")

    # Assertions
    assert stats2.get('broker_buying_power') == 12000.0, "broker_buying_power mismatch"
    assert stats2.get('gross_pnl') == 1000.0, "gross_pnl mismatch"
    assert stats2.get('realized_equity') == stats2.get('capital_start') + 1000.0, "realized_equity mismatch"
    assert stats2.get('unrealized_pnl') == 250.0, "unrealized_pnl mismatch"
    assert stats2.get('estimated_total_equity') == stats2.get('realized_equity') + 250.0, "estimated_total_equity mismatch"

    # Also verify backward compatibility
    assert stats2.get('capital_end') == stats2.get('broker_buying_power'), "capital_end backward compatibility failed"

    print("\n✅ FX05 Tests Passed: Dual-track metrics correctly calculate without touching capital_end semantics.\n")

if __name__ == "__main__":
    test_fx05_dual_track_reporting()
