import unittest
from unittest.mock import patch

from dashboard.app import app
from reflection.adaptive_config_updater import (
    _calculate_market_regime_multiplier,
    _calculate_signal_multipliers,
)
from reflection.reflection_engine import (
    get_all_signal_stats,
    get_all_symbol_stats,
    get_all_time_window_stats,
)


class EmptyAdaptiveStateTests(unittest.TestCase):
    def test_dashboard_adaptive_api_handles_empty_or_sparse_data(self):
        client = app.test_client()

        response = client.get("/api/adaptive")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertIn("signals", data)
        self.assertIn("time_windows", data)
        self.assertIn("symbols", data)
        self.assertIn("overall_win_rate", data)

    def test_reflection_queries_return_lists(self):
        self.assertIsInstance(get_all_signal_stats(), list)
        self.assertIsInstance(get_all_time_window_stats(), list)
        self.assertIsInstance(get_all_symbol_stats(), list)

    def test_adaptive_empty_defaults_are_safe(self):
        with patch("reflection.adaptive_config_updater.get_all_signal_stats", return_value=[]):
            self.assertEqual(_calculate_signal_multipliers(), {})
            self.assertEqual(_calculate_market_regime_multiplier(), 1.0)


if __name__ == "__main__":
    unittest.main()
