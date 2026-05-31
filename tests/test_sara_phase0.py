import unittest

from sara.core.brain import CoreBrain
from sara.core.pipeline import InteractionPipeline
from sara.memory.session import SessionMemory


class SaraPhase0Tests(unittest.TestCase):
    def test_pipeline_records_messages_and_returns_response(self) -> None:
        memory = SessionMemory()
        pipeline = InteractionPipeline(
            brain=CoreBrain(companion_name="SARA"),
            memory=memory,
        )

        response = pipeline.handle("hello")

        self.assertIn("hello", response.text)
        self.assertEqual(memory.turn_count, 1)
        self.assertEqual(memory.history[0]["role"], "user")
        self.assertEqual(memory.history[1]["role"], "assistant")

    def test_brain_exit_command_sets_shutdown_flag(self) -> None:
        response = CoreBrain(companion_name="SARA").respond("exit", turn_count=1)

        self.assertTrue(response.should_exit)


if __name__ == "__main__":
    unittest.main()
