import unittest

from orch.backends import QoderBackend, TurnRequest


class TestQoderBackend(unittest.TestCase):
    def test_stream_command_uses_current_qoder_flags(self):
        request = TurnRequest(
            "critic", "verifier", "review", "be concise", None, ".", 20,
            {"model": "Qwen4-Max", "permission_mode": "auto"},
            on_event=lambda _event: None,
        )

        command, _session_id = QoderBackend().build_command(request)

        self.assertNotIn("--verbose", command)
        self.assertIn("--include-partial-messages", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")


if __name__ == "__main__":
    unittest.main()
