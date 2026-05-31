import unittest
from datetime import datetime

from reflection.cognition_llm_client import get_available_providers, get_llm_status
from reflection.cognitive_agents import (
    AGENT_ROTATION,
    FIRST_CYCLE_TIME,
    get_agent_context_prompt,
    get_agent_system_prompt,
)
from reflection.cognition_scheduler import is_market_hours


class OllamaIntegrationSmokeTests(unittest.TestCase):
    def test_llm_status_shape_is_stable(self):
        status = get_llm_status()

        self.assertIn("preferred_provider", status)
        self.assertIn("ollama_url", status)
        self.assertIn("ollama_model", status)
        self.assertIsInstance(get_available_providers(), list)

    def test_first_cycle_agent_contexts_build_without_llm_call(self):
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "total_trades_today": 0,
            "winning_trades": 0,
            "win_rate": 0.0,
            "active_positions": 0,
        }

        self.assertEqual(FIRST_CYCLE_TIME.hour, 9)
        self.assertGreaterEqual(len(AGENT_ROTATION), 1)

        for agent_name in AGENT_ROTATION:
            system_prompt = get_agent_system_prompt(agent_name)
            context = get_agent_context_prompt(agent_name, snapshot, [], [], [])

            self.assertIsInstance(system_prompt, str)
            self.assertIsInstance(context, str)
            self.assertGreater(len(context), 0)

    def test_market_hours_check_is_boolean(self):
        self.assertIsInstance(is_market_hours(), bool)


if __name__ == "__main__":
    unittest.main()
