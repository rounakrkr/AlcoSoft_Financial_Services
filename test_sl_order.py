import unittest

from core.order_executor import calculate_stop_loss, calculate_target


class StopLossOrderMathTests(unittest.TestCase):
    def test_stop_loss_and_target_math_is_deterministic(self):
        entry = 500.0
        stop_loss = calculate_stop_loss(entry, "BUY")
        target = calculate_target(entry, stop_loss)

        self.assertLess(stop_loss, entry)
        self.assertGreater(target, entry)


if __name__ == "__main__":
    unittest.main()
