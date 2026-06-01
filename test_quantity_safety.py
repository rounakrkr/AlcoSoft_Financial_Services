import unittest
from unittest.mock import patch

from core.order_executor import _place_buy_order_impl, calculate_quantity


def _cfg(values):
    def side_effect(section, key, default=None):
        return values.get((section, key), default)

    return side_effect


class QuantitySafetyTests(unittest.TestCase):
    def test_quantity_never_goes_negative_when_buying_power_is_exhausted(self):
        values = {
            ("risk", "paper_capital"): 100000,
            ("risk", "allow_margin"): True,
            ("risk", "forced_buy_margin"): False,
            ("risk", "margin_leverage"): 5.0,
            ("risk", "position_size_margin"): 1.0,
            ("risk", "max_risk_per_trade"): 0.02,
        }

        with patch("core.order_executor.TRADING_MODE", "PAPER"):
            with patch("core.order_executor.cfg", side_effect=_cfg(values)):
                with patch("core.order_executor.get_today_gross_pnl", return_value=0.0):
                    with patch("core.order_executor.get_margin_status", return_value={"deployed_in_positions": 500000.0}):
                        with patch("core.strategy.MAX_POSITIONS", 4):
                            qty = calculate_quantity(price=2312.2, stop_loss=2306.42)

        self.assertEqual(qty, 0)

    def test_buy_impl_rejects_negative_quantity_before_saving(self):
        with patch("core.order_executor.TRADING_MODE", "PAPER"):
            with patch("core.order_executor.get_open_positions", return_value=[]):
                with patch("core.order_executor.calculate_quantity", return_value=-53):
                    with patch("core.order_executor._get_available_capital", return_value=0.0):
                        with patch("core.order_executor.save_open_position") as save_open:
                            trade = _place_buy_order_impl(
                                symbol="TCS",
                                trading_symbol="TCS-EQ",
                                entry_price=2312.2,
                                stop_loss=2306.42,
                                strategy="TEST",
                                confidence=80,
                            )

        self.assertEqual(trade, {})
        save_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
