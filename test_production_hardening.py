import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.safe_io import atomic_write_json, safe_read_json
from core.state_manager import load_briefing
from core.broker_reconciliation import _parse_broker_positions, reconcile_broker_vs_local


class ProductionHardeningTests(unittest.TestCase):
    def test_safe_json_read_falls_back_on_malformed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")

            self.assertEqual(
                safe_read_json(path, {"ok": True}, expected_type=dict),
                {"ok": True},
            )

    def test_atomic_json_write_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"

            self.assertTrue(atomic_write_json(path, {"value": 7}))
            self.assertEqual(
                safe_read_json(path, {}, expected_type=dict),
                {"value": 7},
            )

    def test_malformed_briefing_loads_safe_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_briefing.json"
            path.write_text("[]", encoding="utf-8")

            with patch("core.state_manager.BRIEFING_PATH", str(path)):
                briefing = load_briefing()

        self.assertEqual(briefing["approved_stocks"], [])
        self.assertEqual(briefing["watchlist"], [])
        self.assertEqual(briefing["avoid_list"], [])

    def test_broker_position_parser_handles_common_shapes(self):
        response = {
            "data": [
                {"trdSym": "TEST-EQ", "netQty": "2", "avgPrc": "101.5"},
                {"trdSym": "ZERO-EQ", "netQty": "0"},
                {"bad": "row"},
            ]
        }

        self.assertEqual(_parse_broker_positions(response), {"TEST": 2})

    def test_live_reconciliation_repairs_local_only_position(self):
        with patch.dict("os.environ", {"TRADING_MODE": "LIVE"}):
            with patch("core.state_manager.get_open_positions", return_value=[{
                "symbol": "TEST",
                "quantity": 1,
                "entry_price": 100,
            }]):
                with patch("core.broker_reconciliation._fetch_broker_positions", return_value={}):
                    with patch("core.state_manager.mark_position_reconciled_closed", return_value=True):
                        with patch("core.broker_reconciliation.reconcile_stop_loss_orders", return_value={}):
                            summary = reconcile_broker_vs_local()

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["local_only"], ["TEST"])
        self.assertIn("TEST:local_closed", summary["repaired"])


if __name__ == "__main__":
    unittest.main()
