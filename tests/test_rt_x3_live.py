"""X3 live test: a real `opencode serve` of its own, a temp folder, one cheap model. Only with ORCH_LIVE=1.

Never touches the user's own opencode processes: it starts its own server (own port and password), creates its session
in a temp folder, and deletes only that session.
"""
from rt_helpers import *  # noqa: F403
import os
import shutil
import tempfile
import time
import unittest

from orch.runtime.adapters.base import SystemClock
from orch.runtime.adapters.opencode import OpenCodeAdapter


@unittest.skipUnless(os.environ.get("ORCH_LIVE") == "1", "live: set ORCH_LIVE=1")
class TestLive(unittest.TestCase):
    def test_turn_job_wake_and_restart_with_a_real_server(self):
        tmp = tempfile.mkdtemp(prefix="x3-live-")
        a = OpenCodeAdapter(SystemClock())
        a.poll_every = 0.5
        spec = AgentSpec("worker-1", Role.WORKER, Choice(backend="opencode", provider="alibaba-token-plan-cn",
                                                         model="qwen3.8-flash"), cwd=tmp, run_dir=tmp)
        events, sid = [], ""

        def pump(until, secs):
            end = time.time() + secs
            while time.time() < end:
                events.extend(a.events())
                if until():
                    return True
                time.sleep(0.5)
            return False

        try:
            proc = a.start(spec, None)
            sid = a.sid
            a.send("x00000000aa01", "Use your shell tool with background=true to run exactly: sleep 8; exit 3 . "
                                    "Then end your turn at once, saying STARTED.")
            self.assertTrue(pump(lambda: any(isinstance(e, TurnEnded) for e in events), 120), events)
            self.assertTrue(any(isinstance(e, Received) and not e.taken for e in events))
            self.assertEqual([e.message_ids for e in events if isinstance(e, TurnStarted)][0], ("x00000000aa01",))
            self.assertTrue(any(isinstance(e, JobStarted) for e in events))
            self.assertTrue(pump(lambda: sum(isinstance(e, TurnEnded) for e in events) >= 2, 120), "no wake turn")
            ended = [e for e in events if isinstance(e, JobEnded)]
            self.assertEqual(ended[0].exit_code, 3)
            self.assertEqual(len([e for e in events if isinstance(e, TurnStarted)]), 2)
            # a new adapter joins the same server and sees nothing twice
            pos = [e.pos for e in events if e.pos][-1]
            a.detach()
            b = OpenCodeAdapter(SystemClock())
            proc.read_pos = pos
            self.assertTrue(b.reconnect(proc))
            self.assertEqual([e for e in b.events() if isinstance(e, (TurnStarted, TurnEnded))], [])
            a = b
        finally:
            try:
                if sid and a.attached:
                    a._call("DELETE", f"/api/session/{sid}")  # only our own session
            finally:
                a.stop()
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
