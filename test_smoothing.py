import unittest

from reflection.reflection_engine import (
    _apply_daily_adjustment_limit,
    _apply_ema_smoothing,
    _calculate_confidence_strength,
    _calculate_trade_time_decay,
)


class AdaptiveSmoothingTests(unittest.TestCase):
    def test_time_decay_decreases_for_older_trades(self):
        recent = _calculate_trade_time_decay("2026-05-29T10:00:00")
        older = _calculate_trade_time_decay("2026-04-29T10:00:00")

        self.assertGreater(recent, older)

    def test_confidence_strength_is_bounded(self):
        self.assertEqual(_calculate_confidence_strength(0), 0.0)
        self.assertLess(_calculate_confidence_strength(10), 1.0)
        self.assertEqual(_calculate_confidence_strength(100), 1.0)

    def test_ema_smoothing_respects_confidence(self):
        low_conf = _apply_ema_smoothing(1.0, 0.8, 0.2, 0.1)
        high_conf = _apply_ema_smoothing(1.0, 0.8, 0.2, 1.0)

        self.assertGreater(low_conf, high_conf)

    def test_daily_limit_caps_large_moves(self):
        self.assertEqual(_apply_daily_adjustment_limit(1.0, 0.7, 5.0), 0.95)
        self.assertEqual(_apply_daily_adjustment_limit(1.0, 1.1, 5.0), 1.05)


if __name__ == "__main__":
    unittest.main()
