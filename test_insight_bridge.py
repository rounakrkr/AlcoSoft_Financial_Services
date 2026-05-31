import unittest
from unittest.mock import patch

from reflection.insight_bridge import get_execution_advisory


class InsightBridgeTests(unittest.TestCase):
    def test_advisory_is_neutral_without_inputs(self):
        with patch("reflection.insight_bridge._market_observation_hint", return_value=(0.0, 0.0, [])):
            with patch("reflection.insight_bridge._latest_cognition_hint", return_value=(0.0, [])):
                advisory = get_execution_advisory("TEST")

        self.assertEqual(advisory["confidence_multiplier"], 1.0)
        self.assertEqual(advisory["market_multiplier"], 1.0)

    def test_advisory_multiplier_is_capped(self):
        with patch("reflection.insight_bridge._market_observation_hint", return_value=(0.08, 0.08, ["market"])):
            with patch("reflection.insight_bridge._latest_cognition_hint", return_value=(0.04, ["cognition"])):
                advisory = get_execution_advisory("TEST")

        self.assertEqual(advisory["confidence_multiplier"], 1.05)
        self.assertEqual(advisory["market_multiplier"], 1.05)
        self.assertIn("market", advisory["reason"])


if __name__ == "__main__":
    unittest.main()
