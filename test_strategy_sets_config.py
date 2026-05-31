import unittest
from datetime import time as dt_time

from core.strategy_sets import load_strategy_sets, normalize_set_key
from core import strategy


class StrategySetConfigTests(unittest.TestCase):
    def test_config_defines_buy_and_sell_sets(self):
        config = load_strategy_sets()

        self.assertGreater(len(config.buy_sets), 0)
        self.assertGreater(len(config.sell_sets), 0)

    def test_configured_conditions_are_registered(self):
        config = load_strategy_sets()
        configured = {
            condition
            for set_def in config.buy_sets + config.sell_sets
            for condition in set_def.conditions
        }
        missing = configured - set(strategy.CONDITION_REGISTRY)

        self.assertEqual(missing, set())

    def test_strategy_sets_define_confidence_metadata(self):
        config = load_strategy_sets()

        for set_def in config.buy_sets + config.sell_sets:
            self.assertGreaterEqual(set_def.base_confidence, 0)
            self.assertLessEqual(set_def.base_confidence, 100)
            self.assertGreater(set_def.confidence_weight, 0)

    def test_stock_confidence_can_lower_but_not_raise_set_base(self):
        triggered_set = {
            "set_name": "BUY_TEST",
            "base_confidence": 75,
            "confidence_weight": 1.0,
        }

        self.assertEqual(
            strategy._resolve_base_confidence({"confidence": 60}, triggered_set, "confidence"),
            60,
        )
        self.assertEqual(
            strategy._resolve_base_confidence({"confidence": 90}, triggered_set, "confidence"),
            75,
        )
        self.assertEqual(
            strategy._resolve_base_confidence({}, triggered_set, "confidence"),
            75,
        )
        self.assertEqual(
            strategy._resolve_base_confidence({"confidence": 0}, triggered_set, "confidence"),
            75,
        )

    def test_math_score_is_scaled_to_percent_confidence(self):
        triggered_set = {
            "set_name": "BUY_TEST",
            "base_confidence": 75,
            "confidence_weight": 1.0,
        }

        self.assertEqual(
            strategy._resolve_base_confidence({"math_score": 7}, triggered_set, "math_score"),
            70,
        )

    def test_adaptive_multipliers_change_final_confidence(self):
        triggered_set = {
            "set_name": "BUY_TEST",
            "base_confidence": 80,
            "confidence_weight": 1.05,
        }
        old_signal = strategy._adaptive_signal_multipliers.copy()
        old_time = strategy._adaptive_time_multipliers.copy()
        old_market = strategy._adaptive_market_multiplier

        try:
            strategy._adaptive_signal_multipliers = {normalize_set_key("BUY_TEST"): 1.1}
            strategy._adaptive_time_multipliers = {"9:15-10:00": 0.9}
            strategy._adaptive_market_multiplier = 0.95

            confidence = strategy._apply_adaptive_confidence(
                80,
                triggered_set,
                now=dt_time(9, 30),
                reload_config=False,
            )

            self.assertAlmostEqual(confidence, 79.0, places=1)
        finally:
            strategy._adaptive_signal_multipliers = old_signal
            strategy._adaptive_time_multipliers = old_time
            strategy._adaptive_market_multiplier = old_market

    def test_adaptive_symbol_stop_changes_runtime_stop_loss(self):
        old_stops = strategy._adaptive_sl_values.copy()
        try:
            strategy._adaptive_sl_values = {}
            base_stop = strategy._calculate_adaptive_stop_loss("TEST", 100.0, "BUY")

            strategy._adaptive_sl_values = {"TEST": 1.2}
            adapted_stop = strategy._calculate_adaptive_stop_loss("TEST", 100.0, "BUY")

            self.assertLess(adapted_stop, base_stop)
        finally:
            strategy._adaptive_sl_values = old_stops


if __name__ == "__main__":
    unittest.main()
