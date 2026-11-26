"""Provider retry stalls are bounded; healthy silence, running tools and cancellation are not errors."""
import dataclasses
import json
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orch.backends import OpenCodeBackend, TurnRequest, _OpenCodeLive
from orch.opencode_recovery import ProviderRecovery
from test_orch import FAKE_OPENCODE, make_cfg, pipeline, turns

ERROR = {"type": "provider.internal", "message": "api_error: upstream JSONDecodeError", "status": 200}


class TestProviderRecovery(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.events = []
        self.r = ProviderRecovery(10, self.events.append, lambda: self.now)

    def retry(self):
        self.r.line(json.dumps({"type": "retry", "error": ERROR, "attempt": 2}))

    def test_retries_and_step_starts_do_not_extend_window(self):
        self.retry()
        self.now = 9
        self.retry()
        self.r.line('{"type":"step_start"}')
        self.r.line('not JSON')
        self.r.line('[]')
        self.assertIsNone(self.r.abort_reason())
        self.now = 10
        self.assertIn('upstream JSONDecodeError', self.r.abort_reason())
        self.assertEqual(len(self.events), 1)

    def test_real_progress_clears_window_and_a_new_retry_starts_another(self):
        for kind in ('text', 'reasoning', 'tool_use'):
            with self.subTest(kind=kind):
                self.setUp()
                self.retry()
                self.now = 9
                self.r.line(json.dumps({'type': kind, 'part': {'text': 'working', 'tool': 'read',
                    'state': {'status': 'completed', 'input': {'path': 'done.md'}}}}))
                self.now = 50
                self.assertIsNone(self.r.abort_reason())
                self.retry()
                self.now = 59
                self.assertIsNone(self.r.abort_reason())
                self.now = 60
                self.assertIsNotNone(self.r.abort_reason())

    def test_running_tool_blocks_stall_and_finishing_clears_it(self):
        self.r.server_event({'type': 'session.tool.called', 'data': {'id': 'call1', 'assistantMessageID': 'm1'}})
        self.r.server_event({'type': 'session.tool.called', 'data': {'id': 'call2', 'assistantMessageID': 'm1'}})
        self.retry()
        self.now = 20
        self.assertIsNone(self.r.abort_reason())
        self.r.server_event({'type': 'session.tool.success', 'data': {'id': 'call1', 'assistantMessageID': 'm1'}})
        self.retry()
        self.now = 40
        self.assertIsNone(self.r.abort_reason())
        self.r.server_event({'type': 'session.tool.failed', 'data': {'id': 'call2', 'assistantMessageID': 'm1'}})
        self.assertIsNone(self.r.abort_reason())
        self.retry()
        self.now = 50
        self.assertIsNotNone(self.r.abort_reason())

    def test_live_progress_is_session_scoped(self):
        req = TurnRequest('a', 'manager', 'task', '', 'ses_own', '.', 60, {})
        live = _OpenCodeLive(req, 'http://127.0.0.1:1234', 'test', self.r)
        self.retry()
        self.now = 9
        live._server_event({'type': 'session.text.delta', 'data': {'sessionID': 'ses_other', 'delta': 'hi'}})
        self.now = 10
        self.assertIsNotNone(self.r.abort_reason())
        self.setUp()
        live.recovery = self.r
        self.retry()
        self.now = 9
        live._server_event({'type': 'session.reasoning.delta', 'data': {'sessionID': 'ses_own', 'delta': 'thinking'}})
        self.now = 30
        self.assertIsNone(self.r.abort_reason())

    def test_disabled_and_invalid_limits(self):
        self.r = ProviderRecovery(0, clock=lambda: self.now)
        self.retry()
        self.now = 1e6
        self.assertIsNone(self.r.abort_reason())
        for x in (-1, float('nan'), float('inf'), 'oops'):
            with self.assertRaises(ValueError):
                ProviderRecovery(x)


INJECT = r'''
if "--model" in args and args[args.index("--model") + 1] == "recovered":
    mode = "ok"
if mode.startswith("recovery_"):
    out(ev("text", "m0", type="text", text="Already inspected done.md."))
    out(ev("tool_use", "m0", type="tool", tool="read",
           state={"status": "completed", "input": {"path": "done.md"}}))
    if mode != "recovery_silent":
        out({"type": "retry", "sessionID": sid, "attempt": 2,
             "error": {"type": "provider.internal", "message": "api_error: upstream JSONDecodeError", "status": 200}})
    if mode == "recovery_progress":
        live("session.reasoning.delta", assistantMessageID="m0", delta="working")
        out(ev("text", "m0", type="text", text="Provider recovered."))
        time.sleep(0.8)
    elif mode == "recovery_silent":
        time.sleep(0.8)
    else:
        while True:
            time.sleep(0.1)
            out(ev("step_start", "m0", type="step-start"))
            out({"type": "retry", "sessionID": sid, "attempt": 3,
                 "error": {"type": "provider.internal", "message": "api_error: upstream JSONDecodeError"}})
'''


class TestOpenCodeRecovery(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.exe = self.d / 'opencode'
        self.exe.write_text(FAKE_OPENCODE.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1)
                            .replace('if mode == "error":', INJECT + '\nif mode == "error":', 1))
        self.exe.chmod(0o755)

    def req(self, mode='recovery_stall', served=False, **options):
        return TurnRequest('a', 'manager', 'inspect the files', '', None, str(self.d), 5,
            {'executable': str(self.exe), 'provider_retry_timeout': 0.15, **options},
            env={'FAKE_OC_MODE': mode, 'FAKE_OC_LOG': str(self.d / 'calls.jsonl')},
            on_event=(lambda e: None) if served else None)

    def test_stall_stops_both_transports_and_retains_error_partial_and_session(self):
        for served in (False, True):
            with self.subTest(served=served):
                start = time.monotonic()
                result = OpenCodeBackend().run(self.req(served=served), lambda: False)
                self.assertLess(time.monotonic() - start, 4)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.meta['error_kind'], 'provider_stall')
                self.assertIn('JSONDecodeError', result.stderr)
                self.assertIn('Already inspected', result.output)
                self.assertEqual(result.session_id, 'ses_new1')
                self.assertTrue(result.meta['partial'])
                self.assertIn('done.md', result.meta['recovery_note'])
                self.assertTrue(result.meta['provider_recovery']['exhausted'])

    def test_recovery_and_healthy_silence_finish_normally(self):
        for mode in ('recovery_progress', 'recovery_silent'):
            for served in (False, True):
                with self.subTest(mode=mode, served=served):
                    result = OpenCodeBackend().run(self.req(mode, served), lambda: False)
                    self.assertEqual(result.status, 'ok')
                    self.assertNotIn('error_kind', result.meta)

    def test_cancellation_and_hard_timeout_keep_existing_status(self):
        for served in (False, True):
            with self.subTest(served=served):
                request = dataclasses.replace(self.req(served=served), timeout=0.1)
                result = OpenCodeBackend().run(request, lambda: False)
                self.assertEqual(result.status, 'timeout')
                self.assertNotIn('error_kind', result.meta)
                result = OpenCodeBackend().run(self.req(served=served), lambda: True)
                self.assertEqual(result.status, 'stopped')
                self.assertNotIn('error_kind', result.meta)

    def test_disabled_window_leaves_overall_timeout(self):
        request = dataclasses.replace(self.req(provider_retry_timeout=0), timeout=0.7)
        result = OpenCodeBackend().run(request, lambda: False)
        self.assertEqual(result.status, 'timeout')
        self.assertIn('provider_recovery', result.meta)
        self.assertNotIn('error_kind', result.meta)




