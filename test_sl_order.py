import unittest
from unittest.mock import patch

from core.order_executor import calculate_stop_loss, calculate_target, place_buy_order


class StopLossOrderMathTests(unittest.TestCase):
    def test_stop_loss_and_target_math_is_deterministic(self):
        entry = 500.0
        stop_loss = calculate_stop_loss(entry, "BUY")
        target = calculate_target(entry, stop_loss)

        self.assertLess(stop_loss, entry)
        self.assertGreater(target, entry)

    def test_paper_buy_does_not_require_broker_token_validation(self):
        with patch("core.order_executor.TRADING_MODE", "PAPER"):
            with patch(
                "core.order_executor.validate_and_fix_session_before_order",
                side_effect=AssertionError("paper mode should not validate broker tokens"),
            ):
                with patch("core.order_executor._place_buy_order_impl", return_value={"order_id": "PAPER-1"}):
                    trade = place_buy_order(
                        symbol="TEST",
                        trading_symbol="TEST-EQ",
                        entry_price=100.0,
                        stop_loss=99.0,
                        strategy="TEST",
                    )

        self.assertEqual(trade["order_id"], "PAPER-1")

    def test_live_buy_blocks_when_broker_token_validation_fails(self):
        with patch("core.order_executor.TRADING_MODE", "LIVE"):
            with patch("core.order_executor.validate_and_fix_session_before_order", return_value=False):
                with patch("core.order_executor._place_buy_order_impl") as impl:
                    trade = place_buy_order(
                        symbol="TEST",
                        trading_symbol="TEST-EQ",
                        entry_price=100.0,
                        stop_loss=99.0,
                        strategy="TEST",
                    )

        self.assertEqual(trade, {})
        impl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
