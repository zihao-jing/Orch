"""Interrupted Claude streams must never become raw JSON reports for the next agent."""

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orch.backends import ClaudeBackend, TurnRequest, claude_stream_report
from orch.config import ControllerConfig, MonitorConfig


def transcript(*events):
    return "\n".join(json.dumps({"session_id": "session", **e}) for e in events)


def assistant(text, mid="m1", **extra):
    return {"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}, **extra}


def stream(kind, **extra):
    return {"type": "stream_event", "event": {"type": kind, **extra}}


class TestClaudeStream(unittest.TestCase):
    def report(self, output, status="timeout"):
        mon = MonitorConfig(name="test", workspace=".", working_dir=".", goal="test", max_rounds=1,
                            agents=[], output_dir=".", controller=ControllerConfig())
        return MonitorRunner(mon).report_text({"status": status, "output": output})

    def request(self):
        return TurnRequest("worker", "worker", "go", "", "session", ".", 10, {}, on_event=lambda e: None)


    def test_stream_and_complete_message_are_not_duplicated_and_partial_tail_survives(self):
        raw = transcript(
            stream("message_start", message={"id": "m1"}),
            stream("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            stream("content_block_delta", index=0, delta={"type": "text_delta", "text": "First update."}),
            assistant("First update."),
            stream("content_block_start", index=1, content_block={"type": "text", "text": ""}),
            stream("content_block_delta", index=1, delta={"type": "text_delta", "text": "Still checking"}),
        ) + '\n{"type":"stream_event","event":'
        out = claude_stream_report(raw)
        self.assertEqual(out.count("First update."), 1)
        self.assertIn("Still checking", out)
        self.assertNotIn("stream_event", out)

    def test_multiple_messages_and_complete_snapshot_without_deltas(self):
        raw = transcript(
            stream("message_start", message={"id": "m1"}),
            assistant("First update."),
            stream("message_start", message={"id": "m2"}),
            stream("content_block_delta", index=0, delta={"type": "text_delta", "text": "Sec"}),
            assistant("Second update.", "m2"),
        )
        out = claude_stream_report(raw)
        self.assertEqual(out, "Second update.")  # keep upstream's last-message-only policy
        self.assertEqual(out.count("Second"), 1)


    def test_normal_final_result_and_usage_are_unchanged(self):
        raw = transcript(assistant("Preliminary update."),
                         {"type": "result", "result": "Final report.", "is_error": False,
                          "usage": {"input_tokens": 20, "output_tokens": 5}})
        with patch("orch.backends.run_process", return_value=(0, raw, "", "ok")):
            result = ClaudeBackend().run(self.request(), lambda: False)
        self.assertEqual((result.status, result.output), ("ok", "Final report."))
        self.assertEqual(result.meta["usage"]["output_tokens"], 5)
        self.assertNotIn("partial", result.meta)
        self.assertEqual(result.raw, "")

    def test_success_without_final_result_is_still_an_error(self):
        with patch("orch.backends.run_process", return_value=(0, transcript(assistant("Partial.")), "", "ok")):
            result = ClaudeBackend().run(self.request(), lambda: False)
        self.assertEqual(result.status, "error")
        self.assertIn("parse_error", result.meta)
        self.assertIn("Partial.", result.output)

    def test_ordinary_text_and_json_reports_are_not_reinterpreted(self):
        for text in ('A normal report.', '{"type":"result","result":"application data"}',
                     'Example:\n{"type":"assistant","message":{"content":[]}}'):
            self.assertIsNone(claude_stream_report(text))

    def test_real_process_timeout_retains_partial_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake_claude.py"
            cli.write_text("import time\nprint(" + repr(transcript(assistant("Work saved.")))
                           + ", flush=True)\ntime.sleep(30)\n")
            req = dataclasses.replace(self.request(), cwd=tmp, timeout=0.5)
            with patch.object(ClaudeBackend, "build_command", return_value=([sys.executable, str(cli)], "session")):
                result = ClaudeBackend().run(req, lambda: False)
        self.assertEqual(result.status, "timeout")
        self.assertIn("Work saved.", result.output)
        self.assertNotIn('"type"', result.output)


    def test_snapshot_seeds_text_before_a_later_streamed_block(self):
        raw = transcript(
            stream("message_start", message={"id": "m1"}),
            assistant("First update."),
            stream("content_block_delta", index=1, delta={"type": "text_delta", "text": "Still checking"}),
        )
        self.assertEqual(claude_stream_report(raw), "First update.\n\nStill checking")

    def test_saved_json_array_is_recovered_and_nested_session_is_ignored(self):
        from orch.backends import _claude_stream_text
        raw = json.dumps([{"session_id": "main", **assistant("Main report.")},
                          {"session_id": "child", **assistant("Nested report.", parent_tool_use_id="tool1")}])
        self.assertEqual(_claude_stream_text(raw), ("Main report.", "main"))
        self.assertEqual(claude_stream_report(raw), "Main report.")


    def test_plain_cli_error_after_stream_initialization_is_kept(self):
        from orch.backends import _claude_stream_text
        raw = transcript({'type': 'system', 'subtype': 'init'}) + '\nError: connection lost\n'
        self.assertEqual(_claude_stream_text(raw), ('Error: connection lost', 'session'))


if __name__ == "__main__":
    unittest.main()
