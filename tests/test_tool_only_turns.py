"""Completed tool work does not require a closing prose reply to avoid model fallback."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orch.backends import MockBackend, OpenCodeBackend, TurnRequest
from orch.controller import classify
from test_orch import make_cfg, managed, run, turns


class TestToolOnlyTurns(unittest.TestCase):
    rules = SimpleNamespace(human_pattern=r"NEEDS_HUMAN", permission_pattern=r"PERMISSION_REQUIRED")

    def result(self, tool_status="completed", status="ok", error=False):
        events = [{"type": "step_start", "sessionID": "ses_tools"}]
        if tool_status is not None:
            events.append({"type": "tool_use", "sessionID": "ses_tools", "part": {
                "tool": "edit", "state": {"status": tool_status, "input": {"filePath": "draft.md"},
                                          "output": "Edit applied."}}})
        events.append({"type": "step_finish", "sessionID": "ses_tools", "part": {
            "reason": "stop", "tokens": {"input": 100, "output": 200}}})
        if error:
            events.append({"type": "error", "error": {"message": "upstream unavailable"}})
        req = TurnRequest("writer", "worker", "Edit the draft", "", None, ".", 30, {})
        return OpenCodeBackend()._result(req, ["opencode"], 0, "\n".join(map(json.dumps, events)), "", status)

    def test_completed_tools_are_valid_without_reply_text(self):
        res = self.result()
        self.assertEqual(res.output, "")
        self.assertEqual(res.meta["tools_completed"], 1)
        self.assertEqual(classify(self.rules, res), ("ok", ""))
        self.assertIn("Edit applied.", res.raw)  # evidence remains available, never fabricated as a reply

    def test_steps_tokens_and_unfinished_tools_do_not_count_as_completed_work(self):
        for tool_status in (None, "pending", "running"):
            with self.subTest(tool_status=tool_status):
                self.assertEqual(classify(self.rules, self.result(tool_status))[0], "error")

    def test_actual_failures_are_preserved_after_tool_work(self):
        for status in ("error", "timeout", "stopped"):
            with self.subTest(status=status):
                self.assertEqual(classify(self.rules, self.result(status=status))[0], status)
        self.assertEqual(classify(self.rules, self.result(error=True))[0], "error")
        res = self.result()
        res.stderr = "PERMISSION_REQUIRED"
        self.assertEqual(classify(self.rules, res)[0], "needs_human")

    def test_worker_goes_to_review_without_retry_or_backup(self):
        original = MockBackend._run
        calls = []

        def scripted(backend, req, stop, sid):
            if req.role == "worker":
                calls.append(req)
                return self.result()
            return original(backend, req, stop, sid)

        with tempfile.TemporaryDirectory() as directory:
            mon = managed(mgr={"milestones": 1}, critic={"pass_after": 1})
            worker = next(a for a in mon["agents"] if a["role"] == "worker")
            worker.update(retries=1, fallback=[{"label": "backup"}])
            with patch.object(MockBackend, "_run", scripted):
                status, rec = run(make_cfg(Path(directory), [mon]))
            self.assertEqual(status, "completed")
            self.assertEqual(len(calls), 1)
            ts = turns(rec)
            work = next(t for t in ts if t["role"] == "worker")
            self.assertEqual((work["status"], work["output"]), ("ok", ""))
            self.assertEqual(len(work["attempts"]), 1)
            self.assertFalse(work.get("fallbacks"))
            self.assertTrue(any(t["purpose"] == "verify" for t in ts))
