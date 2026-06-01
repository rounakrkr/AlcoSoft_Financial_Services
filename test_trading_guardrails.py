import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core import order_executor, strategy


class TradingGuardrailTests(unittest.TestCase):
    def test_risk_reducing_sell_bypasses_open_order_circuit(self):
        breaker = Mock()
        breaker.is_open.return_value = True

        with patch("core.order_executor.get_breaker", return_value=breaker):
            with patch("core.order_executor._place_sell_order_impl", return_value=True) as impl:
                ok = order_executor.place_sell_order("RELIANCE", 1320.0, "SQUAREOFF")

        self.assertTrue(ok)
        impl.assert_called_once()
        breaker.call.assert_not_called()

    def test_normal_sell_still_blocked_by_open_order_circuit(self):
        breaker = Mock()
        breaker.is_open.return_value = True

        with patch("core.order_executor.get_breaker", return_value=breaker):
            with patch("core.order_executor._place_sell_order_impl") as impl:
                ok = order_executor.place_sell_order("RELIANCE", 1320.0, "SIGNAL")

        self.assertFalse(ok)
        impl.assert_not_called()

    def test_buy_waits_for_completed_live_candles_before_strategy_eval(self):
        df = pd.DataFrame({
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.5] * 30,
            "volume": [1000.0] * 30,
        })
        stock = {"ticker": "RELIANCE", "direction": "BUY_ONLY", "market_bias": "BULLISH"}

        with patch("core.strategy.MIN_WS_CANDLES_FOR_PATTERNS", 3):
            with patch("core.strategy.get_candle_history", return_value=[{}, {}]):
                with patch("core.strategy._get_indicator_df", return_value=df) as indicator:
                    signal = strategy._evaluate_buy_signal(stock, {})

        self.assertEqual(signal["action"], "WAIT")
        self.assertIn("completed live WS candles", signal["reason"])
        indicator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
