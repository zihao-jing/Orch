"""A turn with several result records (background Monitor timers waking the session) keeps every reply."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orch.backends import ClaudeBackend, TurnRequest, claude_stream_report


def result(text, **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": "s1", **extra}


def transcript(*records):
    return "\n".join(json.dumps(r) for r in records)


class TestMultiResult(unittest.TestCase):
    def run_turn(self, raw):
        req = TurnRequest("worker", "worker", "go", "", "s1", ".", 10, {})
        with patch("orch.backends.run_process", return_value=(0, raw, "", "ok")):
            return ClaudeBackend().run(req, lambda: False)

    def test_single_result_is_unchanged(self):
        res = self.run_turn(transcript(result("Final report.", num_turns=3, duration_ms=500, total_cost_usd=0.2,
                                              usage={"input_tokens": 10, "output_tokens": 5})))
        self.assertEqual(res.output, "Final report.")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.meta["num_turns"], 3)
        self.assertEqual(res.meta["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(claude_stream_report(transcript(result("Final report."))), "Final report.")

    def test_report_then_timer_replies_are_joined_and_labelled(self):
        raw = transcript(
            result("Real report: all tests pass."),
            result("Another old monitor has ended, nothing changed.", total_cost_usd=0.3),
            result("Monitor ended again, nothing changed.", total_cost_usd=0.4),
        )
        res = self.run_turn(raw)
        self.assertEqual(res.output,
                         "[Earlier reply 1]\nReal report: all tests pass.\n\n"
                         "[Earlier reply 2]\nAnother old monitor has ended, nothing changed.\n\n"
                         "[Latest reply]\nMonitor ended again, nothing changed.")
        self.assertEqual(res.meta["total_cost_usd"], 0.4)  # cumulative: the last record's value
        self.assertEqual(claude_stream_report(raw), res.output)

    def test_one_earlier_reply_has_no_number(self):
        res = self.run_turn(transcript(result("Report."), result("Woke up, nothing changed.")))
        self.assertEqual(res.output, "[Earlier reply]\nReport.\n\n[Latest reply]\nWoke up, nothing changed.")

    def test_more_than_four_keep_first_and_last_three_with_note(self):
        raw = transcript(*[result(f"reply {i}") for i in range(1, 7)])
        res = self.run_turn(raw)
        self.assertEqual(res.output,
                         "[Earlier reply 1]\nreply 1\n\n(2 short replies omitted)\n\n"
                         "[Earlier reply 2]\nreply 4\n\n[Earlier reply 3]\nreply 5\n\n[Latest reply]\nreply 6")
        self.assertNotIn("reply 2\n", res.output)  # the omitted ones (the label "[Earlier reply 2]" is not one)
        self.assertNotIn("reply 3\n", res.output)

    def test_five_results_omit_one(self):
        res = self.run_turn(transcript(*[result(f"reply {i}") for i in range(1, 6)]))
        self.assertIn("(1 short reply omitted)", res.output)
        self.assertNotIn("reply 2\n", res.output)
        self.assertTrue(res.output.endswith("[Latest reply]\nreply 5"))

    def test_turn_statistics_are_summed(self):
        raw = transcript(
            result("Report.", num_turns=4, duration_ms=1000, is_error=False,
                   usage={"input_tokens": 100, "output_tokens": 20}, total_cost_usd=0.5),
            result("Woke up.", num_turns=1, duration_ms=200, is_error=False,
                   usage={"input_tokens": 7, "output_tokens": 3}, total_cost_usd=0.6),
        )
        res = self.run_turn(raw)
        self.assertEqual(res.meta["num_turns"], 5)
        self.assertEqual(res.meta["duration_ms"], 1200)
        self.assertEqual(res.meta["usage"], {"input_tokens": 107, "output_tokens": 23})
        self.assertEqual(res.meta["total_cost_usd"], 0.6)
        self.assertEqual(res.status, "ok")

    def test_nested_results_are_ignored(self):
        raw = transcript(result("Report."),
                         result("SUBAGENT_RESULT", parent_tool_use_id="tool1", num_turns=9))
        res = self.run_turn(raw)
        self.assertEqual(res.output, "Report.")
        self.assertEqual(res.meta.get("num_turns"), None)


if __name__ == "__main__":
    unittest.main()
