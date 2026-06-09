"""
F007 Validation Test: EOD Squareoff Reliability
"""

import os, sys, logging, time
from datetime import datetime
logging.basicConfig(level=logging.CRITICAL)

os.environ.setdefault("TRADING_MODE", "PAPER")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest.mock as mock
from core.order_executor import squareoff_all_intraday

def run_tests():
    print("Running F007 squareoff tests...")

    position = {
        "symbol": "RELIANCE",
        "quantity": 10,
        "entry_price": 2500.0
    }

    # We need to mock:
    # 1. get_open_positions to return our position
    # 2. _squareoff_done to be False
    # 3. datetime.now().time() to return 15:16
    # 4. _resolve_liquidation_price to return 0.0 (simulating dead feed)
    # 5. place_sell_order to capture the arguments

    captured_args = []
    def mock_place_sell_order(symbol, current, reason, exit_price_source=None):
        captured_args.append((symbol, current, reason, exit_price_source))
        return True
    
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 6, 15, 16, 0)

    import core.order_executor
    core.order_executor._squareoff_done = False
    
    with mock.patch("core.order_executor.datetime", MockDatetime):
        with mock.patch("core.order_executor.get_open_positions", return_value=[position]):
            with mock.patch("core.order_executor._resolve_liquidation_price", return_value=(0.0, "unknown")):
                with mock.patch("core.order_executor.place_sell_order", side_effect=mock_place_sell_order):
                    
                    # Test 1: Signature doesn't crash
                    try:
                        squareoff_all_intraday(reason="SCHEDULED_MARKET_CLOSE")
                        print("TEST 1 PASS: Signature fixed (kwargs accepted)")
                    except TypeError as e:
                        print("TEST 1 FAIL: Signature still crashing:", e)
                        return

                    # Test 2: Fallback logic used
                    if not captured_args:
                        print("TEST 2 FAIL: place_sell_order was not called")
                        return
                    
                    sym, price, reason, source = captured_args[0]
                    assert sym == "RELIANCE"
                    assert price == 2500.0 * 0.95, f"Expected {2500.0 * 0.95}, got {price}"
                    assert source == "fallback_entry_price"
                    print("TEST 2 PASS: Used 95% of entry_price as fallback market order limit")

    print("\\nALL F007 TESTS PASSED")

if __name__ == "__main__":
    run_tests()
