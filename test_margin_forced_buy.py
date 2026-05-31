import unittest
from unittest.mock import patch

from core.order_executor import calculate_quantity


def _risk_cfg(allow_margin: bool, forced_buy: bool):
    values = {
        ("risk", "allow_margin"): allow_margin,
        ("risk", "forced_buy_margin"): forced_buy,
        ("risk", "margin_leverage"): 2.0,
        ("risk", "position_size_margin"): 0.75,
        ("risk", "max_risk_per_trade"): 0.02,
        ("risk", "stop_loss_percent"): 0.01,
    }

    def side_effect(section, key, default=None):
        return values.get((section, key), default)

    return side_effect


class MarginForcedBuyTests(unittest.TestCase):
    def test_no_margin_rejects_unaffordable_quantity(self):
        with patch("core.order_executor._get_available_capital", return_value=800.0):
            with patch("core.order_executor.cfg", side_effect=_risk_cfg(False, False)):
                self.assertEqual(calculate_quantity(price=1000, stop_loss=980), 0)

    def test_forced_margin_can_buy_affordable_share(self):
        with patch("core.order_executor._get_available_capital", return_value=800.0):
            with patch("core.order_executor.cfg", side_effect=_risk_cfg(True, True)):
                self.assertEqual(calculate_quantity(price=1000, stop_loss=980), 1)


if __name__ == "__main__":
    unittest.main()
