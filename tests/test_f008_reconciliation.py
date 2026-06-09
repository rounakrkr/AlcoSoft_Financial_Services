"""
F008 Validation Test: Broker Reconciliation CNC Guard
"""

import os, sys, logging
logging.basicConfig(level=logging.CRITICAL)

os.environ.setdefault("TRADING_MODE", "PAPER")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest.mock as mock
from core.broker_reconciliation import reconcile_broker_vs_local

def run_tests():
    print("Running F008 reconciliation tests...")

    # We will mock _fetch_broker_positions to return 3 positions:
    # 1. MIS (intraday) -> Should be recovered
    # 2. CNC (delivery) -> Should be skipped
    # 3. UNKNOWN (missing product) -> Should be skipped
    
    # Actually, _fetch_broker_positions returns the dict produced by _parse_broker_position_rows.
    # The parsing logic itself decides the product string. So we should mock the RAW API call:
    # client.positions. Let's mock call_broker_api instead.
    
    mock_raw_positions = {
        "stat": "Ok",
        "data": [
            {
                "trdSym": "RELIANCE-EQ",
                "prod": "MIS",
                "flBuyQty": "100",
                "flSellQty": "0",
                "buyAmt": "250000.00",
            },
            {
                "trdSym": "HDFCBANK-EQ",
                "prod": "CNC",
                "flBuyQty": "50",
                "flSellQty": "0",
                "buyAmt": "80000.00",
            },
            {
                "trdSym": "INFY-EQ",
                # No prod field
                "flBuyQty": "200",
                "flSellQty": "0",
                "buyAmt": "300000.00",
            }
        ]
    }

    import core.state_manager
    core.state_manager._OPEN_POSITIONS = []
    
    def mock_call_broker_api(func, *args, **kwargs):
        if "positions" in str(func):
            return mock_raw_positions
        if "order_report" in str(func):
            return {"data": []}
        return {}

    with mock.patch("core.api_resilience.call_broker_api", side_effect=mock_call_broker_api):
        with mock.patch("core.kotak_client.get_client"):
            # Run reconciliation
            reconcile_broker_vs_local()
            
            # Check local positions
            open_pos = core.state_manager.get_open_positions()
            recovered_symbols = [p["symbol"] for p in open_pos]
            
            print(f"Recovered symbols: {recovered_symbols}")
            
            assert "RELIANCE" in recovered_symbols, "MIS position was not recovered"
            assert "HDFCBANK" not in recovered_symbols, "CNC position was incorrectly recovered!"
            assert "INFY" not in recovered_symbols, "UNKNOWN product position was incorrectly recovered!"
            
            print("TEST PASS: Only MIS positions are recovered. CNC and UNKNOWN are skipped.")

    print("\\nALL F008 TESTS PASSED")

if __name__ == "__main__":
    run_tests()
