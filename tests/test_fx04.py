import os
import sys
from datetime import datetime
from unittest.mock import patch, MagicMock

# Ensure we can import from core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.order_executor import squareoff_all_intraday
from core.state_manager import get_trading_session_state, set_trading_session_state, get_open_positions

def test_fx04_squareoff_state():
    print("\n--- Test FX04: EOD Squareoff State Transition ---")
    
    # Force system into ACTIVE state to simulate intraday trading
    set_trading_session_state("ACTIVE", "TEST_START")
    print(f"Initial state: {get_trading_session_state()['state']} | entries_enabled: {get_trading_session_state()['entries_enabled']}")

    # Mock time so squareoff_all_intraday thinks it's past 3:15 PM
    import core.order_executor
    from datetime import time as dt_time
    core.order_executor.INTRADAY_SQUAREOFF = dt_time(0, 0) # Mock time to always trigger
    core.order_executor._squareoff_done = False # Reset flag

    # Mock get_open_positions to return empty so it hits the early exit
    print("\nTest Case 1: No Open Positions")
    with patch('core.order_executor.get_open_positions', return_value=[]):
        squareoff_all_intraday()
    
    state1 = get_trading_session_state()
    print(f"State after squareoff (No positions): {state1['state']} | entries_enabled: {state1['entries_enabled']} | reason: {state1['reason']}")
    assert state1['state'] == "ACTIVE", "State should be ACTIVE"
    assert state1['entries_enabled'] is True, "Entries should be enabled"

    # Reset for next test
    core.order_executor._squareoff_done = False
    
    # Mock get_open_positions to return one position, and place_sell_order to succeed
    print("\nTest Case 2: With Open Positions (Successful Squareoff)")
    with patch('core.order_executor.get_open_positions', side_effect=[
        [{"symbol": "RELIANCE", "quantity": 10, "entry_price": 100}], # First call before loop
        [] # Second call at end of function
    ]):
        with patch('core.order_executor.place_sell_order', return_value=True):
            squareoff_all_intraday()

    state2 = get_trading_session_state()
    print(f"State after squareoff (With positions): {state2['state']} | entries_enabled: {state2['entries_enabled']} | reason: {state2['reason']}")
    assert state2['state'] == "ACTIVE", "State should be ACTIVE"
    assert state2['entries_enabled'] is True, "Entries should be enabled"

    print("\n✅ FX04 Tests Passed: EOD Squareoff safely restores ACTIVE state.\n")

if __name__ == "__main__":
    test_fx04_squareoff_state()
