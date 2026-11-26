"""X1 live test: the real Claude CLI with haiku in a temporary folder, a few short turns. Only with ORCH_LIVE=1."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from rt_helpers import *  # noqa: F401,F403
from orch.runtime.adapters.base import SystemClock
from orch.runtime.adapters.claude import ClaudeAdapter

LIVE = os.environ.get("ORCH_LIVE") == "1"


def kinds(events):
    return [type(e).__name__ for e in events]


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


@unittest.skipUnless(LIVE, "set ORCH_LIVE=1 to run against the real CLI")
class TestClaudeLive(unittest.TestCase):
    def pump(self, ad, until, timeout=120.0):
        got, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            got += list(ad.events())
            if until(got):
                return got
            time.sleep(0.5)
        self.fail(f"timeout, saw {kinds(got)}")

    def test_turns_job_wake_reconnect_and_stop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name) / "work"
        work.mkdir()
        spec = AgentSpec("worker-1", Role.WORKER,
                         Choice(backend="claude", model="haiku", effort="low", options={"allowed_tools": ["Bash"]}),
                         str(work), "Answer in one short line.", str(Path(tmp.name) / "run"))
        ad = ClaudeAdapter(SystemClock())
        self.addCleanup(ad.stop)
        proc = ad.start(spec, None)
        ids = [new_id("live", Tick(at=float(i))) for i in range(4)]

        # a plain turn: receipt, start by our id, model, end
        ad.send(ids[0], "Reply with exactly the word PONG.")
        ev = self.pump(ad, lambda e: of(e, TurnEnded))
        self.assertEqual(of(ev, Received)[0].message_id, ids[0])
        self.assertEqual(of(ev, TurnStarted)[0].message_ids, (ids[0],))
        self.assertIn("haiku", of(ev, ModelReported)[0].model)
        self.assertEqual(of(ev, TurnEnded)[0].status, TurnStatus.OK)
        self.assertIn("PONG", of(ev, TurnEnded)[0].text)
        self.assertEqual(len(of(ev, TurnStarted)), len(of(ev, TurnEnded)))
        self.assertTrue(all(e.key for e in ev))

        # a background job: the turn ends with it running, its end wakes the session by itself
        ad.send(ids[1], "Start one Bash command with run_in_background true: sleep 6; echo JOB-OK. "
                        "Then reply started. When it ends, reply with its output only.")
        ev = self.pump(ad, lambda e: of(e, TurnEnded))
        self.assertEqual(len(of(ev, JobStarted)), 1)
        self.assertEqual([j.state for j in ad.jobs()], [JobState.RUNNING])
        last = [e for e in ev if e.pos][-1]
        saved = ProcRef(proc.pid, proc.started, proc.endpoint, proc.output_file, proc.stream, last.pos)

        # Orch closes and comes back: same process, nothing repeated
        ad.detach()
        ad2 = ClaudeAdapter(SystemClock())
        self.addCleanup(ad2.stop)
        self.assertTrue(ad2.reconnect(saved))
        ev2 = self.pump(ad2, lambda e: of(e, JobEnded) and len(of(e, TurnEnded)) >= 1)
        self.assertEqual((of(ev2, JobEnded)[0].exit_code, of(ev2, JobEnded)[0].lost), (0, False))
        self.assertEqual(of(ev2, TurnStarted)[0].message_ids, ())  # the wake turn
        self.assertIn("JOB-OK", of(ev2, TurnEnded)[0].text)
        self.assertEqual(len(of(ev2, TurnStarted)), len(of(ev2, TurnEnded)))
        self.assertFalse({e.key for e in ev} & {e.key for e in ev2})

        # a message after the reconnect, then stop: SIGTERM, ProcessExited(asked)
        ad2.send(ids[2], "Reply with exactly the word AGAIN.")
        ev3 = self.pump(ad2, lambda e: of(e, TurnEnded))
        self.assertIn("AGAIN", of(ev3, TurnEnded)[0].text)
        self.assertIs(ad2.arrived(ids[2]), True)
        ad2.stop()
        ev4 = list(ad2.events())
        self.assertEqual(kinds(ev4), ["ProcessExited"])
        self.assertTrue(ev4[0].asked)


if __name__ == "__main__":
    unittest.main()
