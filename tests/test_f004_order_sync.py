"""
F004 Validation Tests — Order / Broker Sync Timeout Handling

Tests:
  T1: Order COMPLETES -> no exception, normal flow
  T2: Order explicitly REJECTED -> raises OrderExecutionError
  T3: Order TIMEOUT -> no exception, logs warning, saves position with UNVERIFIED note
"""

import os, sys, logging
logging.basicConfig(level=logging.CRITICAL)

os.environ.setdefault("TRADING_MODE", "PAPER")
os.environ.setdefault("STRATEGY_TYPE", "INTRADAY")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest.mock as mock
from core.order_executor import _place_buy_order_impl, OrderExecutionError

# ── Helpers ────────────────────────────────────────────────────────────────────

def mock_dependencies(mocker, mock_verification_status):
    # Mocking external calls in _place_buy_order_impl
    mocker.patch("core.order_executor.get_open_positions", return_value=[])
    mocker.patch("core.order_executor.calculate_stop_loss", return_value=90.0)
    mocker.patch("core.order_executor.calculate_quantity", return_value=10)
    mocker.patch("core.order_executor.get_margin_status", return_value={
        "deployed_in_positions": 0.0,
        "current_position_value": 0.0,
        "entry_position_value": 0.0,
        "real_capital": 10000.0,
        "margin_leverage": 2.0
    })
    mocker.patch("core.order_executor.calculate_target", return_value=120.0)
    
    # Force LIVE mode logic for Kotak send
    mocker.patch("core.order_executor.TRADING_MODE", "LIVE")
    mocker.patch("core.order_executor._send_kotak_order", return_value="ORD_12345")
    mocker.patch("core.order_executor.record_order_sent", return_value=None)
    
    # Mock the new string return value for verification
    mocker.patch("core.order_executor.wait_for_order_verification", return_value=mock_verification_status)
    
    # Mock the SL-M order placement which happens immediately after verification
    mocker.patch("core.order_executor._send_kotak_sl_order", return_value="SL_12345")
    
    # We also need to mock time.sleep so tests run fast
    mocker.patch("time.sleep", return_value=None)

# ── TESTS ──────────────────────────────────────────────────────────────────────

def run_tests():
    print("Running F004 order sync tests...")

    # Test 1: COMPLETE
    with mock.patch("core.order_executor.logger") as mock_logger:
        with mock.patch("core.order_executor.get_open_positions") as m_gop:
            with mock.patch("core.order_executor.wait_for_order_verification") as m_verify:
                with mock.patch("core.order_executor._send_kotak_order") as m_send:
                    # We will mock the entire _place_buy_order_impl execution environment
                    pass

    # Actually, a simpler way is just to use patch as context managers
    pass


if __name__ == "__main__":
    with mock.patch("core.order_executor.get_open_positions", return_value=[]), \
         mock.patch("core.order_executor.calculate_stop_loss", return_value=90.0), \
         mock.patch("core.order_executor.calculate_quantity", return_value=10), \
         mock.patch("core.order_executor.get_margin_status", return_value={
             "deployed_in_positions": 0.0,
             "current_position_value": 0.0,
             "entry_position_value": 0.0,
             "real_capital": 10000.0,
             "margin_leverage": 2.0
         }), \
         mock.patch("core.order_executor.calculate_target", return_value=120.0), \
         mock.patch("core.order_executor.TRADING_MODE", "LIVE"), \
         mock.patch("core.order_executor._send_kotak_order", return_value="ORD_12345"), \
         mock.patch("core.order_executor.record_order_sent", return_value=None), \
         mock.patch("core.order_executor._send_kotak_sl_order", return_value="SL_12345"), \
         mock.patch("time.sleep", return_value=None):
         
         # T1: COMPLETE
         with mock.patch("core.order_executor.wait_for_order_verification", return_value="COMPLETE"):
             trade1 = _place_buy_order_impl(symbol="RELIANCE", trading_symbol="RELIANCE", entry_price=100.0)
             assert trade1["order_id"] == "ORD_12345", "T1 FAIL: Missing order_id"
             assert trade1.get("notes") is None, "T1 FAIL: Should not have unverified notes"
             print("TEST 1 PASS: COMPLETE returns normal trade dict")

         # T2: REJECTED
         with mock.patch("core.order_executor.wait_for_order_verification", return_value="REJECTED"):
             try:
                 _place_buy_order_impl(symbol="RELIANCE", trading_symbol="RELIANCE", entry_price=100.0)
                 assert False, "T2 FAIL: Expected OrderExecutionError on REJECTED"
             except OrderExecutionError:
                 print("TEST 2 PASS: REJECTED raises OrderExecutionError")

         # T3: TIMEOUT
         with mock.patch("core.order_executor.wait_for_order_verification", return_value="TIMEOUT"):
             trade3 = _place_buy_order_impl(symbol="RELIANCE", trading_symbol="RELIANCE", entry_price=100.0)
             assert trade3["order_id"] == "ORD_12345", "T3 FAIL: Missing order_id"
             assert trade3.get("notes") == "UNVERIFIED: Broker confirmation timed out", "T3 FAIL: Missing timeout note"
             print("TEST 3 PASS: TIMEOUT returns trade dict (no exception) with UNVERIFIED notes")

    print("\\nALL 3 TESTS PASSED")
