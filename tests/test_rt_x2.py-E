"""X2: the Codex app-server adapter, against a fake server (tests/test_rt_x2_fake.py). No real CLI, no network.

Requirement ids of the package: JOB-1 (a job is a command still running at turn end, read from the CLI's record),
JOB-4 (Codex never wakes itself: wakes_itself False, nothing starts a turn when a job ends). Also MSG-1 (steering),
MSG-6 (arrived), REC-7 (reconnect, reading what was missed), SES-2 (session rejected), REC-9 (jobs outlive Orch).
"""
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

from rt_helpers import *  # noqa: F403
from orch.runtime.adapters import make
from orch.runtime.adapters.base import AdapterError
from orch.runtime.adapters.codex import CodexAdapter, _WS, _error_class

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_rt_x2_fake.py")


class Rig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp", prefix="x2")
        self.exe = os.path.join(self.dir, "codex")
        with open(self.exe, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IXUSR)
        self.clock, self.adapters, self.procs = FakeClock(), [], []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ad in self.adapters:
            try:
                if ad.proc and ad._alive(ad.proc.pid, "", ad.proc.started):
                    ad.stop()
            except Exception:
                pass
            ad.detach()
        shutil.rmtree(self.dir, ignore_errors=True)

    def spec(self, agent="worker-1", **options):
        return AgentSpec(agent, Role.WORKER, Choice(backend="codex", model="fake-model", effort="low",
                                                    options={"executable": self.exe, **options}),
                         cwd=self.dir, system_prompt="be brief", run_dir=self.dir)

    def adapter(self):
        ad = CodexAdapter(self.clock)
        self.adapters.append(ad)
        return ad

    def started(self, **options):
        ad = self.adapter()
        spec = self.spec(**options)
        proc = ad.start(spec, None)
        ad.spec = spec
        return ad, proc, spec

    def until(self, ad, pred, timeout=8.0):
        """Events of `ad` until pred(all so far) holds."""
        got, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            got += list(ad.events())
            if pred(got):
                return got
            time.sleep(0.05)
        self.fail("timed out; events so far: " + ", ".join(type(e).__name__ for e in got))

    def kinds(self, events):
        return [type(e).__name__ for e in events]

    def ctl(self, proc, method, **params):
        ws = _WS(proc.endpoint)
        try:
            ws.send({"id": 1, "method": method, "params": params})
            end = time.monotonic() + 5
            while time.monotonic() < end:
                for m in ws.read(0.2):
                    if m.get("id") == 1:
                        return m
        finally:
            ws.close()

    def ended(self, events):
        return any(isinstance(e, TurnEnded) for e in events)


class TestOpen(Rig):
    def test_start_makes_a_session_reports_model_and_keeps_a_pid_file(self):
        ad, proc, spec = self.started()
        ev = list(ad.events())
        self.assertEqual(self.kinds(ev), ["SessionChanged", "ModelReported"])
        self.assertEqual(ev[0].change, SessionChange.NEW)
        self.assertEqual(ev[0].session_id, proc.stream)
        self.assertEqual(ev[0].uid, f"start:{ad._popen.pid}")
        self.assertEqual(ev[1].model, "fake-model")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "cli", "worker-1", "pid")))
        self.assertEqual(proc.pid, ad._popen.pid)

    def test_capabilities_match_the_probe(self):
        caps = make("codex", FakeClock()).caps
        self.assertTrue(caps.sends_while_writing and caps.reconnects and caps.job_record and caps.shares_terminal)
        self.assertTrue(caps.compacts and caps.receipts)
        self.assertFalse(caps.wakes_itself)  # JOB-4 does not hold for Codex

    def test_missing_executable_is_missing_cli(self):
        ad = self.adapter()
        with self.assertRaises(AdapterError) as cm:
            ad.start(self.spec(executable="/nonexistent/codex"), None)
        self.assertEqual(cm.exception.error, ErrorClass.MISSING_CLI)

    def test_SES_2_resume_of_an_unknown_session_is_reported_lost(self):
        ad = self.adapter()
        ad.start(self.spec(), SessionRef("01a21293-0000-7000-8000-000000000000"))
        ev = list(ad.events())
        self.assertEqual([e.change for e in ev if isinstance(e, SessionChanged)], [SessionChange.LOST])
        with self.assertRaises(AdapterError) as cm:
            ad.send("x00000000aaaa", "hello")
        self.assertEqual(cm.exception.error, ErrorClass.SESSION_LOST)

    def test_websocket_client_handles_big_frames(self):
        ad, proc, _ = self.started()
        r = self.ctl(proc, "x/echo", text="y" * 200000)
        self.assertEqual(len(r["result"]["text"]), 200000)


class TestTurns(Rig):
    def test_a_message_gives_receipt_turn_and_text(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000a1", "hello")
        ev = self.until(ad, self.ended)
        k = self.kinds(ev)
        self.assertLess(k.index("Received"), k.index("TurnStarted"))
        rec = next(e for e in ev if isinstance(e, Received))
        self.assertEqual((rec.message_id, rec.taken), ("x0000000000a1", False))
        started = next(e for e in ev if isinstance(e, TurnStarted))
        self.assertEqual(started.message_ids, ("x0000000000a1",))
        end = next(e for e in ev if isinstance(e, TurnEnded))
        self.assertEqual((end.text, end.status), ("ok: hello", TurnStatus.OK))
        self.assertTrue(end.pos.startswith(started.uid.split(":")[1] + "/"))  # "<turn id>/<item id>"
        self.assertTrue(any(isinstance(e, Text) and e.text == "o" for e in ev))
        self.assertEqual(ev[-1].stream, proc.stream)

    def test_MSG_1_a_message_during_a_turn_is_steered_into_it(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000b1", "LONG work")
        self.until(ad, lambda e: any(isinstance(x, ToolStart) or isinstance(x, TurnStarted) for x in e))
        ad.send("x0000000000b2", "also this")
        ev = self.until(ad, lambda e: any(isinstance(x, Received) and x.taken for x in e))
        taken = [e for e in ev if isinstance(e, Received) and e.taken]
        self.assertEqual([e.message_id for e in taken], ["x0000000000b2"])
        self.assertFalse([e for e in ev if isinstance(e, TurnStarted)])  # the same turn
        self.ctl(proc, "x/finish", threadId=proc.stream)
        ev = self.until(ad, self.ended)
        self.assertEqual(self.kinds(ev).count("TurnEnded"), 1)

    def test_messages_of_a_terminal_are_not_ours(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000c1", "LONG")
        self.until(ad, lambda e: any(isinstance(x, TurnStarted) for x in e))
        self.ctl(proc, "turn/start", threadId=proc.stream, input=[{"type": "text", "text": "typed"}],
                 clientUserMessageId="3f1c9a52-0000-4000-8000-000000000001")
        time.sleep(0.3)
        self.assertFalse([e for e in ad.events() if isinstance(e, Received)])

    def test_failed_turn_carries_the_error_class(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000d1", "FAIL please")
        ev = self.until(ad, self.ended)
        end = next(e for e in ev if isinstance(e, TurnEnded))
        self.assertEqual((end.status, end.error), (TurnStatus.ERROR, ErrorClass.BILLING))
        self.assertIn("usage limit", end.detail)

    def test_error_classes(self):
        self.assertEqual(_error_class("rateLimitExceeded", ""), ErrorClass.TRANSIENT)
        self.assertEqual(_error_class({"httpConnectionFailed": {"httpStatusCode": 502}}, ""), ErrorClass.TRANSIENT)
        self.assertEqual(_error_class("contextWindowExceeded", ""), ErrorClass.SESSION_LOST)
        self.assertEqual(_error_class("modelNotFound", "no such model"), ErrorClass.OTHER)
        self.assertEqual(_error_class("unauthorized", ""), ErrorClass.BILLING)

    def test_MSG_6_arrived_looks_for_the_message_in_the_thread(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000e1", "hello")
        self.until(ad, self.ended)
        self.assertTrue(ad.arrived("x0000000000e1"))
        self.assertFalse(ad.arrived("x0000000000e2"))

    def test_compaction_is_no_turn(self):
        ad, proc, _ = self.started()
        list(ad.events())
        self.assertTrue(ad.compact())
        ev = self.until(ad, lambda e: any(isinstance(x, SessionChanged) for x in e))
        self.assertEqual(self.kinds(ev), ["SessionChanged"])
        self.assertEqual(ev[0].change, SessionChange.COMPACTED)


class TestJobs(Rig):
    def job_turn(self, ad, message_id="x0000000000f1"):
        ad.send(message_id, "JOB start it")
        return self.until(ad, self.ended)

    def test_JOB_1_only_a_command_still_running_at_turn_end_is_a_job(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000f0", "CMD quick")
        ev = self.until(ad, self.ended)
        self.assertEqual([k for k in self.kinds(ev) if k.startswith("Job")], [])
        self.assertIn("ToolEnd", self.kinds(ev))
        ev = self.job_turn(ad)
        k = self.kinds(ev)
        self.assertLess(k.index("JobStarted"), k.index("TurnEnded"))
        job = next(e for e in ev if isinstance(e, JobStarted)).job
        self.assertEqual(job.agent, "worker-1")
        self.assertTrue(job.id.startswith("worker-1/exec-"))
        self.assertEqual([j.id for j in ad.jobs()], [job.id])

    def test_JOB_1_the_end_comes_later_with_its_exit_code_and_JOB_4_no_turn_follows(self):
        ad, proc, _ = self.started()
        self.job_turn(ad)
        self.ctl(proc, "x/endJob", code=3)
        ev = self.until(ad, lambda e: any(isinstance(x, JobEnded) for x in e))
        end = next(e for e in ev if isinstance(e, JobEnded))
        self.assertEqual(end.exit_code, 3)
        time.sleep(0.4)
        later = list(ad.events())
        self.assertFalse([e for e in ev + later if isinstance(e, TurnStarted)])  # JOB-4: nothing wakes the model
        self.assertFalse(CodexAdapter.caps.wakes_itself)
        done = [j for j in ad.jobs() if j.state == JobState.ENDED]
        self.assertEqual([j.exit_code for j in done], [3])

    def test_the_open_turns_commands_are_not_in_jobs(self):
        ad, proc, _ = self.started()
        self.job_turn(ad)
        self.assertEqual(len(ad.jobs()), 1)

    def test_stop_job_ends_one_job(self):
        ad, proc, _ = self.started()
        self.job_turn(ad)
        jid = ad.jobs()[0].id
        self.assertTrue(ad.stop_job(jid))
        ev = self.until(ad, lambda e: any(isinstance(x, JobEnded) for x in e))
        self.assertEqual(next(e for e in ev if isinstance(e, JobEnded)).exit_code, 143)
        self.assertFalse(ad.stop_job("worker-1/unknown"))


class TestReconnect(Rig):
    def test_REC_7_catching_up_gives_the_events_the_live_stream_gave(self):
        ad, proc, spec = self.started()
        list(ad.events())
        before = proc.read_pos
        live = self.until_job_turn(ad)
        ad.detach()
        ad2 = self.adapter()
        ad2.spec = spec
        self.assertTrue(ad2.reconnect(ProcRef(pid=proc.pid, started=proc.started, endpoint=proc.endpoint,
                                              stream=proc.stream, read_pos=before)))
        replay = list(ad2.events())
        key = lambda e: (type(e).__name__, e.uid, e.pos)  # noqa: E731
        want = {key(e) for e in live if not isinstance(e, (Received, Text, ModelReported, SessionChanged))}
        got = {key(e) for e in replay if not isinstance(e, (Text, ModelReported))}
        self.assertEqual(got, want)
        self.assertEqual({e.key for e in replay if isinstance(e, JobStarted)},
                         {e.key for e in live if isinstance(e, JobStarted)})

    def test_REC_9_a_job_that_ended_while_orch_was_away_is_known_afterwards(self):
        ad, proc, spec = self.started()
        list(ad.events())
        self.until_job_turn(ad)
        saved = ProcRef(pid=proc.pid, started=proc.started, endpoint=proc.endpoint, stream=proc.stream,
                        read_pos=next(e for e in [ad.cursor] if e))
        ad.detach()
        self.ctl(proc, "x/endJob", code=0)  # nobody is connected
        ad2 = self.adapter()
        self.assertTrue(ad2.reconnect(saved))
        self.assertEqual([e for e in ad2.events() if isinstance(e, TurnStarted)], [])  # nothing new to replay
        jobs = ad2.jobs()
        self.assertEqual([(j.state, j.exit_code) for j in jobs], [(JobState.ENDED, 0)])
        self.assertEqual(ad2.agent, "worker-1")

    def test_start_joins_a_server_an_earlier_start_left_and_its_job_lives(self):
        ad, proc, spec = self.started()
        list(ad.events())
        self.until_job_turn(ad)
        ad.detach()
        ad2 = self.adapter()
        proc2 = ad2.start(spec, SessionRef(proc.stream))
        self.assertEqual(proc2.pid, proc.pid)  # joined: the server and its job were not touched
        ev = list(ad2.events())
        self.assertEqual([e.change for e in ev if isinstance(e, SessionChanged)], [SessionChange.RESUMED])
        self.assertEqual(len(ad2.jobs()), 1)

    def test_reconnect_to_a_dead_server_is_false(self):
        ad, proc, spec = self.started()
        ad.stop()
        ad2 = self.adapter()
        self.assertFalse(ad2.reconnect(proc))

    def test_a_lost_connection_is_repaired_and_nothing_is_missed(self):
        ad, proc, _ = self.started()
        list(ad.events())
        ad._ws.close()  # the socket drops, the server lives
        ad._ws = None
        self.ctl(proc, "turn/start", threadId=proc.stream, input=[{"type": "text", "text": "hi"}],
                 clientUserMessageId="x0000000000h1")
        ev = self.until(ad, self.ended)
        self.assertEqual(next(e for e in ev if isinstance(e, TurnEnded)).text, "ok: hi")
        self.assertEqual(next(e for e in ev if isinstance(e, TurnStarted)).message_ids, ("x0000000000h1",))

    def until_job_turn(self, ad):
        ad.send("x0000000000g1", "JOB go")
        return self.until(ad, self.ended)


class TestProcess(Rig):
    def test_a_server_that_dies_ends_the_open_turn_then_exits(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000i1", "LONG")
        self.until(ad, lambda e: any(isinstance(x, TurnStarted) for x in e))
        os.kill(proc.pid, 9)
        ev = self.until(ad, lambda e: any(isinstance(x, ProcessExited) for x in e))
        end = next(e for e in ev if isinstance(e, TurnEnded))
        self.assertEqual((end.status, end.error), (TurnStatus.ERROR, ErrorClass.TRANSIENT))
        gone = next(e for e in ev if isinstance(e, ProcessExited))
        self.assertEqual((gone.asked, gone.reported), (False, True))
        self.assertEqual(self.kinds(ev).count("ProcessExited"), 1)
        time.sleep(0.2)
        self.assertEqual(list(ad.events()), [])  # reported once

    def test_stop_ends_the_server_and_reports_it_asked(self):
        ad, proc, _ = self.started()
        ad.send("x0000000000j1", "LONG")
        self.until(ad, lambda e: any(isinstance(x, TurnStarted) for x in e))
        ad.stop()
        ev = list(ad.events())
        end = next(e for e in ev if isinstance(e, TurnEnded))
        self.assertEqual(end.status, TurnStatus.STOPPED)
        gone = next(e for e in ev if isinstance(e, ProcessExited))
        self.assertTrue(gone.asked)
        self.assertFalse(gone.reported)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "cli", "worker-1", "pid")))

    def test_detach_leaves_the_server_running(self):
        ad, proc, _ = self.started()
        ad.detach()
        os.kill(proc.pid, 0)
        self.assertEqual(list(ad.events()), [])


class TestAround(Rig):
    def test_terminal_command_joins_the_session_with_the_agents_settings(self):
        ad = self.adapter()
        spec = self.spec(sandbox="workspace-write")
        cmd = ad.terminal_command(spec, SessionRef("tid-1"))
        self.assertEqual(cmd[:2], [self.exe, "resume"])
        self.assertEqual(cmd[2], "--remote")
        self.assertEqual(cmd[3], "unix://" + os.path.join(self.dir, "cli", "worker-1", "s.sock"))
        self.assertEqual(cmd[-1], "tid-1")
        joined = " ".join(cmd)
        for want in ('model="fake-model"', 'model_reasoning_effort="low"', "developer_instructions=", "sandbox_mode="):
            self.assertIn(want, joined)
        self.assertNotIn("bypass", joined)  # never a permission-bypassing flag unless the agent's options say so
        self.assertNotIn("--yolo", joined)

    def test_catalog_lists_efforts_and_skips_hidden_models(self):
        ad, proc, _ = self.started()
        cat = ad.catalog()
        self.assertEqual([(m.id, m.efforts, m.default_effort, m.label) for m in cat],
                         [("fake-a", ["low", "high"], "low", "Fake A")])

    def test_transcript_has_the_users_own_lines_only(self):
        ad, proc, spec = self.started()
        ad.send("x0000000000k1", "ours")
        self.until(ad, self.ended)
        self.ctl(proc, "turn/start", threadId=proc.stream, input=[{"type": "text", "text": "typed in the terminal"}],
                 clientUserMessageId="3f1c9a52-0000-4000-8000-000000000002")
        time.sleep(0.4)
        said = ad.transcript(SessionRef(proc.stream), 0.0)
        self.assertEqual([(s.by, s.text) for s in said], [(USER, "typed in the terminal")])

    def test_server_is_started_with_unload_delay_and_the_agents_own_sandbox(self):
        ad, proc, _ = self.started(sandbox="workspace-write")
        with open(os.path.join(self.dir, "cli", "worker-1", "pid")) as f:
            self.assertIn(str(proc.pid), f.read())
        out = os.popen(f"ps -o command= -p {proc.pid}").read()
        self.assertIn("thread_unload_delay_secs=", out)
        self.assertIn('sandbox_mode="workspace-write"', out)
        self.assertNotIn("dangerously", out)


@unittest.skipUnless(os.environ.get("ORCH_LIVE") == "1", "live test: ORCH_LIVE=1 (real codex, own socket, temp folder)")
class TestLive(Rig):
    def test_live_turn_job_and_reconnect(self):
        """One real turn and one real job on the cheapest model, in a temp folder, on this test's own server."""
        ad = self.adapter()
        spec = AgentSpec("live", Role.WORKER, Choice(backend="codex", model="gpt-5.6-luna", effort="low"),
                         cwd=self.dir, system_prompt="Be extremely brief.", run_dir=self.dir)
        proc = ad.start(spec, None)
        ad.spec = spec
        try:
            ad.send("x0000000000l1", "Reply with the single word PONG.")
            ev = self.until(ad, self.ended, 120)
            end = next(e for e in ev if isinstance(e, TurnEnded))
            self.assertEqual(end.status, TurnStatus.OK, end.detail)
            self.assertIn("PONG", end.text)
            ad.send("x0000000000l2", "Start the command `sh -c 'sleep 20; exit 3'` as a background job: call your shell "
                    "tool with yield_time_ms 500 so you do not wait. Do not poll it. Then reply STARTED.")
            ev = self.until(ad, self.ended, 120)
            self.assertIn("JobStarted", self.kinds(ev))
            self.assertEqual(len(ad.jobs()), 1)
            ad.detach()
            ad2 = self.adapter()
            self.assertTrue(ad2.reconnect(ProcRef(pid=proc.pid, started=proc.started, endpoint=proc.endpoint,
                                                  stream=proc.stream, read_pos=ad.cursor)))
            ev = self.until(ad2, lambda e: any(isinstance(x, JobEnded) for x in e), 90)
            self.assertEqual(next(e for e in ev if isinstance(e, JobEnded)).exit_code, 3)
            self.assertTrue(ad2.arrived("x0000000000l2"))
            print("live ok:", [type(e).__name__ for e in ev])
            ad2.stop()
        finally:
            if ad._alive(proc.pid, "", proc.started):
                ad._kill(proc.pid)


if __name__ == "__main__":
    unittest.main()
