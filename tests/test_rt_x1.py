"""X1: the Claude, Qoder and Qwen adapters, on recorded event samples and on a fake CLI process (no real CLI).

The samples are trimmed copies of what the probes recorded (docs/probes/): ids shortened, no paths.
Requirement ids covered: MSG-1, MSG-2, MSG-5, MSG-6, JOB-1, JOB-2, JOB-4, REC-1, REC-2, REC-7, REC-8, REC-9, SES-2,
MOD-5, SIL-4, TERM-4, and the Adapter and event rules of contracts.py.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from rt_helpers import *  # noqa: F401,F403
from orch.runtime.adapters import make
from orch.runtime.adapters import claude as X
from orch.runtime.adapters.base import AdapterError

A1, A2 = "xaaaaaaaaaa01", "xaaaaaaaaaa02"
_p = subprocess.Popen(["true"])
_p.wait()
GONE = _p.pid  # the pid of a process that has ended
SID = "7be3e7e5-9f82-4e07-aea9-819df103e5a6"
HAIKU = "claude-haiku-5-5"


def j(**kw):
    return json.dumps(kw)


def queued(mid, state="queued"):
    return j(type="command_lifecycle", state=state, command_uuid=X.to_uuid(mid), session_id=SID)


def init(model=HAIKU, sid=SID):
    return j(type="system", subtype="init", model=model, session_id=sid)


def echo(mid, text="go"):
    return j(type="user", message={"role": "user", "content": text}, uuid=X.to_uuid(mid), isReplay=True, session_id=SID)


def say(text, model=HAIKU, **kw):
    return j(type="assistant", message={"model": model, "content": [{"type": "text", "text": text}]}, **kw)


def tool(use, command, background=False, name="Bash", model=HAIKU):
    inp = {"command": command}
    if background:
        inp["run_in_background"] = True
    return j(type="assistant", message={"model": model, "content": [{"type": "tool_use", "id": use, "name": name,
                                                                     "input": inp}]})


def tool_done(use, content="ok"):
    return j(type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": use,
                                                                  "content": content, "is_error": False}]})


def result(text="", turns=1, **kw):
    return j(type="result", subtype=kw.pop("subtype", "success"), is_error=kw.pop("is_error", False), num_turns=turns,
             result=text, stop_reason=kw.pop("stop_reason", "end_turn"), terminal_reason=kw.pop("terminal_reason", "completed"),
             session_id=SID, **kw)


def task_started(tid, use="", background=True, desc="job"):
    return j(type="system", subtype="task_started", task_id=tid, tool_use_id=use, description=desc,
             is_backgrounded=background, task_type="local_bash")


def task_end(tid, status="completed", summary=None, out="/tmp/x/tasks/t.output"):
    return j(type="system", subtype="task_notification", task_id=tid, status=status, output_file=out,
             summary=summary or f'Background command "job" completed (exit code 0)')


def note_echo(tid):
    text = f"<task-notification>\n<task-id>{tid}</task-id>\n<status>completed</status>\n</task-notification>"
    return j(type="user", message={"role": "user", "content": text}, uuid="aa20-random", isReplay=True)


def kinds(events):
    return [type(e).__name__ for e in events]


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


class Feed(unittest.TestCase):
    """An adapter put on an output file whose process is gone: events() gives everything, then ProcessExited."""

    cls = X.ClaudeAdapter

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "cli" / "worker-1"
        self.dir.mkdir(parents=True)

    def feed(self, lines, read_pos="0", session=SID, err="", cls=None):
        out = self.dir / "out-0001.jsonl"
        if lines is not None:
            with open(out, "ab") as f:
                f.write(("\n".join(lines) + "\n").encode())
        (self.dir / "pid.json").write_text(json.dumps({"pid": 1, "session": session}))
        if err:
            (self.dir / "err-0001.txt").write_text(err)
        ad = (cls or self.cls)(FakeClock())
        ok = ad.reconnect(ProcRef(pid=1, started=1.0, output_file=str(out), endpoint=str(self.dir / "in-0001.fifo"),
                                  stream="out-0001.jsonl", read_pos=read_pos))
        self.assertFalse(ok)  # the process is gone
        return ad

    def read(self, lines, **kw):
        ad = self.feed(lines, **kw)
        return ad, list(ad.events())


class TestClaudeStream(Feed):
    def test_MSG_1_message_during_a_tool_is_taken_into_the_same_turn(self):
        """Sample 1a: queued at once, echoed at the tool boundary into the open turn, one TurnEnded."""
        ad, ev = self.read([
            queued(A1), queued(A1, "started"), init(), echo(A1), tool("tu1", "sleep 25"),
            task_started("fg1", background=False), queued(A2), task_end("fg1", out=""), tool_done("tu1"),
            echo(A2, "BANANA"), queued(A2, "started"), say("FIRST-DONE BANANA"), queued(A2, "completed"),
            result("FIRST-DONE BANANA", 2), queued(A1, "completed")])
        self.assertEqual(kinds(ev), ["Received", "ModelReported", "TurnStarted", "ToolStart", "Received", "ToolEnd",
                                     "Received", "Text", "TurnEnded", "ProcessExited"])
        self.assertEqual([(r.message_id, r.taken) for r in of(ev, Received)], [(A1, False), (A2, False), (A2, True)])
        self.assertEqual(of(ev, TurnStarted)[0].message_ids, (A1,))
        self.assertEqual(of(ev, TurnEnded)[0].text, "FIRST-DONE BANANA")
        self.assertEqual(of(ev, ModelReported)[0].model, HAIKU)
        self.assertFalse(of(ev, JobStarted))  # a foreground tool is no job

    def test_MSG_1_message_during_text_becomes_the_next_turn(self):
        """Sample 1a, case B: queued at once, echoed only after the result: it starts a turn of its own."""
        ad, ev = self.read([
            queued(A1), queued(A1, "started"), init(), echo(A1), say("1 2 3"), result("1 2 3"), queued(A2),
            queued(A1, "completed"), queued(A2, "started"), init(), echo(A2), say("WORD"), result("WORD")])
        starts = of(ev, TurnStarted)
        self.assertEqual([s.message_ids for s in starts], [(A1,), (A2,)])
        self.assertEqual(len(of(ev, TurnEnded)), 2)
        self.assertEqual(of(ev, Received)[1].taken, False)  # the receipt came while the first turn ran

    def test_one_TurnStarted_per_TurnEnded_with_empty_results_ignored(self):
        """Samples 2b and 2d: two jobs ending together wake once, preceded by an empty result that is no turn."""
        lines = [queued(A1), init(), echo(A1), tool("tu1", "sleep 8; echo F", True), task_started("bF", "tu1"),
                 tool_done("tu1", "Command running in background with ID: bF"), say("ok"), result("ok", 3),
                 task_end("bF"), task_end("bG"), init(), result("", 0, stop_reason=None), init(),
                 say("F: exit code 0"), result("F: exit code 0")]
        lines.insert(4, task_started("bG", "tu1"))
        ad, ev = self.read(lines)
        self.assertEqual(len(of(ev, TurnStarted)), len(of(ev, TurnEnded)))
        self.assertEqual(len(of(ev, TurnEnded)), 2)
        self.assertEqual([e.job_id for e in of(ev, JobEnded)], ["worker-1/bF", "worker-1/bG"])

    def test_JOB_4_wake_turn_starts_with_its_first_output_not_init(self):
        """Samples 2a, 2c: a wake turn has only init, text, result; the job and its end are events."""
        ad, ev = self.read([
            queued(A1), init(), echo(A1), tool("tu1", "sleep 15; exit 3", True), task_started("bB", "tu1"),
            tool_done("tu1"), say("started"), result("started", 2),
            task_end("bB", "failed", 'Background command "x" failed with exit code 3'),
            init(), say("B: exit code 3"), result("B: exit code 3")])
        start = of(ev, JobStarted)[0].job
        self.assertEqual((start.id, start.label, start.state), ("worker-1/bB", "sleep 15; exit 3", JobState.RUNNING))
        end = of(ev, JobEnded)[0]
        self.assertEqual((end.job_id, end.exit_code, end.lost), ("worker-1/bB", 3, False))
        self.assertEqual(of(ev, TurnStarted)[1].message_ids, ())  # the wake turn: no message
        self.assertFalse(of(ev, JobNoticed))
        self.assertEqual(kinds(ev).index("JobEnded") < kinds(ev).index("TurnStarted", kinds(ev).index("JobEnded")), True)

    def test_JOB_2_job_ending_inside_a_turn_is_noticed_by_that_turn(self):
        """Sample 2b: the end is told to the running turn at the tool boundary: JobNoticed, no wake turn."""
        ad, ev = self.read([
            queued(A1), init(), echo(A1), tool("tu1", "sleep 8; echo D", True), task_started("bD", "tu1"),
            tool_done("tu1"), tool("tu2", "sleep 16"), task_started("fg", background=False),
            task_end("bD"), task_end("fg", out=""), tool_done("tu2"), note_echo("bD"), say("D: exit code 0"),
            result("D: exit code 0", 4)])
        self.assertEqual([e.job_id for e in of(ev, JobNoticed)], ["worker-1/bD"])
        self.assertEqual(len(of(ev, TurnStarted)), 1)
        self.assertEqual(kinds(ev).index("JobEnded") < kinds(ev).index("JobNoticed"), True)

    def test_REC_1_error_turns_are_errors_whatever_the_subtype(self):
        """Sample 8b: an API error is `success` with is_error; classes from the structured fields."""
        cases = [("rate_limit", 429, ErrorClass.TRANSIENT, "API Error: Request rejected (429)"),
                 ("billing_error", 400, ErrorClass.BILLING, "Credit balance is too low"),
                 ("server_error", 529, ErrorClass.TRANSIENT, "API Error: 529 Overloaded"),
                 ("authentication_failed", 401, ErrorClass.OTHER, "Invalid API key"),
                 ("model_not_found", 404, ErrorClass.OTHER, "There's an issue with the selected model")]
        for err, status, cls, text in cases:
            with self.subTest(err):
                synth = say(text, "<synthetic>", error=err, is_api_error_message=True, api_error_status=status)
                ad, ev = self.read([queued(A1), init(), echo(A1),
                                    j(type="system", subtype="api_retry", attempt=1, error=err, error_status=status),
                                    synth, result(text, is_error=True, terminal_reason="api_error",
                                                  api_error_status=status)])
                end = of(ev, TurnEnded)[0]
                self.assertEqual((end.status, end.error), (TurnStatus.ERROR, cls))
                self.assertIn(text[:20], end.detail)
                self.assertEqual(end.text, "")
                self.assertIn("", [e.text for e in of(ev, Text)])  # the CLI's own retry is bare progress
                (self.dir / "out-0001.jsonl").unlink()

    def test_REC_1_usage_limit_is_found_without_relying_on_is_error_or_subtype(self):
        """The 5-hour limit arrives as a normal `success` result: found by the synthetic message or its words."""
        text = "You've hit your 5-hour limit · resets 12am"
        variants = {
            "is_error": [say(text, "<synthetic>"), result(text, is_error=True)],
            "synthetic error field": [say(text, "<synthetic>", error="rate_limit"), result(text)],
            "words only": [say(text, "<synthetic>"), result(text)],
        }
        for name, tail in variants.items():
            with self.subTest(name):
                out = self.dir / "out-0001.jsonl"
                out.unlink() if out.exists() else None
                ad, ev = self.read([queued(A1), init(), echo(A1)] + tail)
                end = of(ev, TurnEnded)[0]
                self.assertEqual((end.status, end.error), (TurnStatus.ERROR, ErrorClass.TRANSIENT))
                self.assertIn("5-hour limit", end.detail)
        out.unlink()
        ad, ev = self.read([queued(A1), init(), echo(A1), say("You may hit your limit soon."), result("fine")])
        self.assertEqual(of(ev, TurnEnded)[0].status, TurnStatus.OK)  # an agent quoting the words is no error

    def test_SES_2_missing_session_at_start_is_one_SessionChanged_LOST(self):
        """Sample 5a: --resume of an unknown session: one error result with no turn, then the process is gone."""
        msg = "No conversation found with session ID: 0000"
        ad, ev = self.read([j(type="result", subtype="error_during_execution", is_error=True, num_turns=0,
                              errors=[msg], session_id="0000")])
        self.assertEqual(kinds(ev), ["SessionChanged", "ProcessExited"])
        self.assertEqual(ev[0].change, SessionChange.LOST)
        self.assertTrue(ev[1].reported and not ev[1].asked)

    def test_SES_2_other_session_id_is_DIFFERENT_before_the_turn_starts(self):
        ad, ev = self.read([queued(A1), init(sid="other"), echo(A1), say("hi"), result("hi")])
        self.assertEqual(kinds(ev)[:4], ["Received", "SessionChanged", "ModelReported", "TurnStarted"])
        self.assertEqual((ev[1].change, ev[1].session_id), (SessionChange.DIFFERENT, "other"))

    def test_MOD_5_model_is_reported_once_per_change(self):
        ad, ev = self.read([init(), queued(A1), echo(A1), say("a"), say("b", "claude-sonnet-5-5"), result("b")])
        self.assertEqual([m.model for m in of(ev, ModelReported)], [HAIKU, "claude-sonnet-5-5"])

    def test_compact_boundary_is_COMPACTED_and_its_empty_result_is_no_turn(self):
        """Sample 9b."""
        ad, ev = self.read([
            queued(X.COMPACT_ID), queued(X.COMPACT_ID, "started"), j(type="system", subtype="status", status="compacting"),
            j(type="system", subtype="compact_boundary"), echo(X.COMPACT_ID, "<command-name>/compact</command-name>"),
            result("", 0, stop_reason=None), queued(X.COMPACT_ID, "completed")])
        self.assertEqual(kinds(ev), ["SessionChanged", "ProcessExited"])
        self.assertEqual(ev[0].change, SessionChange.COMPACTED)

    def test_REC_7_a_restart_in_the_middle_of_a_turn_goes_on_without_repeats(self):
        """The saved position of any event: a new adapter reads the rest, with the same keys, no second start."""
        lines = [queued(A1), init(), echo(A1), tool("tu1", "ls"), tool_done("tu1"), say("done"), result("done")]
        ad, whole = self.read(lines)
        for cut in whole[:-1]:
            if not cut.pos:
                continue
            with self.subTest(after=type(cut).__name__):
                again, rest = self.read(None, read_pos=cut.pos)
                tail = whole[whole.index(cut) + 1:]
                self.assertEqual([e.key for e in rest], [e.key for e in tail])
        self.assertEqual(len({e.key for e in whole}), len(whole))

    def test_REC_7_a_record_with_several_events_gives_its_start_as_pos_until_the_last(self):
        two = j(type="assistant", message={"model": HAIKU, "content": [
            {"type": "text", "text": "x"}, {"type": "tool_use", "id": "t9", "name": "Read", "input": {"file_path": "a"}}]})
        ad, ev = self.read([init(), two])
        a, b = of(ev, TurnStarted)[0], of(ev, Text)[0]
        size = (self.dir / "out-0001.jsonl").stat().st_size
        first = len(init()) + 1
        self.assertEqual((a.pos, b.pos, of(ev, ToolStart)[0].pos), (str(first), str(first), str(size)))
        self.assertEqual(len({a.uid, b.uid, of(ev, ToolStart)[0].uid}), 3)
        self.assertEqual(of(ev, ToolStart)[0].label, "Read: a")

    def test_a_half_written_line_waits_for_its_newline(self):
        out = self.dir / "out-0001.jsonl"
        out.write_bytes((init() + "\n" + say("x")[:20]).encode())
        ad, ev = self.read(None)
        self.assertEqual(kinds(ev), ["ModelReported", "ProcessExited"])
        self.assertEqual(ev[0].pos, str(len(init()) + 1))

    def test_JOB_1_jobs_lists_running_and_ended_and_drops_ghosts_of_a_dead_process(self):
        ad, ev = self.read([init(), queued(A1), echo(A1), tool("tu1", "sleep 1", True), task_started("b1", "tu1"),
                            tool("tu2", "sleep 2", True), task_started("b2", "tu2"), task_end("b2")])
        got = {x.id: x for x in ad.jobs()}
        self.assertEqual(list(got), ["worker-1/b2"])  # the process is dead: b1 is gone, the supervisor marks it
        self.assertEqual((got["worker-1/b2"].state, got["worker-1/b2"].exit_code), (JobState.ENDED, 0))

    def test_death_with_an_open_turn_is_TurnEnded_then_ProcessExited_reported(self):
        ad, ev = self.read([queued(A1), init(), echo(A1), say("partial")], err="Segmentation fault")
        self.assertEqual(kinds(ev)[-2:], ["TurnEnded", "ProcessExited"])
        self.assertEqual((ev[-2].status, ev[-2].error), (TurnStatus.ERROR, ErrorClass.TRANSIENT))
        self.assertTrue(ev[-1].reported and not ev[-1].asked)
        self.assertIn("Segmentation", ev[-1].detail)


class TestQoderStream(Feed):
    cls = X.QoderAdapter

    def put(self, ad, mid, text):
        ad._note(mid, "try", text)
        ad._note(mid, "done")

    def test_MSG_2_qoder_takes_a_message_only_after_the_turn_and_matches_by_text(self):
        """Probe 1: no id round trip; the replay echo has a new uuid and the text. The journal gives the id back."""
        self.assertFalse(X.QoderAdapter.caps.sends_while_writing)
        self.assertFalse(X.QoderAdapter.caps.receipts)
        self.assertTrue(X.QoderAdapter.caps.wakes_itself)
        ad = self.feed([init("Efficient")])
        list(ad.events())
        self.put(ad, A1, "say hi")
        self.put(ad, A2, "[m2] also say PING")
        more = [j(type="user", message={"role": "user", "content": [{"type": "text", "text": "say hi"}]},
                  uuid="11af-new", isReplay=True),
                say("ok", "efficient"), result("ok"), init("Efficient"),
                j(type="user", message={"role": "user", "content": [{"type": "text", "text": "[m2]  also say PING"}]},
                  uuid="22bf-new", isReplay=True),
                say("PING", "efficient"), result("PING")]
        with open(self.dir / "out-0001.jsonl", "ab") as f:
            f.write(("\n".join(more) + "\n").encode())
        ev = list(ad.events())
        self.assertEqual([s.message_ids for s in of(ev, TurnStarted)], [(A1,), (A2,)])
        self.assertEqual(len(of(ev, TurnEnded)), 2)
        self.assertIs(ad.arrived(A1), True)
        self.assertIs(ad.arrived(A2), True)

    def test_JOB_4_qoder_wake_turn_has_a_synthetic_echo_and_starts_with_text(self):
        """Probe 2: the wake turn's echo is synthetic and names the job; no TurnStarted before its text."""
        note = j(type="user", message={"role": "user", "content": [{"type": "text", "text":
                 "<task-notification><task-id>b5</task-id></task-notification>"}]}, isReplay=True, isSynthetic=True)
        ad, ev = self.read([
            init("Efficient"), tool("c1", "sleep 8", True, model="efficient"),
            j(type="system", subtype="task_started", task_id="b5", tool_use_id="c1", description="d",
              task_type="local_bash"),
            j(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "b5"}]),
            tool_done("c1"), result("started", 2),
            j(type="system", subtype="task_updated", task_id="b5", patch={"status": "completed", "end_time": 1795608801759}),
            init("Efficient"), note, task_end("b5"), say("done", "efficient"), result("done")])
        self.assertEqual(len(of(ev, TurnStarted)), 2)
        self.assertEqual(of(ev, TurnStarted)[1].message_ids, ())
        self.assertFalse(of(ev, JobNoticed))
        self.assertEqual([m.model for m in of(ev, ModelReported)], ["efficient"])  # init carries what was asked
        self.assertEqual(ad.jobs()[0].ended, 1795608801.759)

    def test_JOB_2_qoder_timeout_is_a_normal_end_not_lost(self):
        """Probe 3: a background job stopped at 600 s is `failed`, summary `timed out after 600000 ms`."""
        ad, ev = self.read([init("Efficient"), tool("c1", "sleep 720", True),
                            j(type="system", subtype="task_started", task_id="b9", tool_use_id="c1", description="d",
                              task_type="local_bash"), tool_done("c1"), result("started"),
                            task_end("b9", "failed", 'Background command "d" timed out after 600000 ms. The command was stopped.')])
        end = of(ev, JobEnded)[0]
        self.assertEqual((end.job_id, end.exit_code, end.lost), ("worker-1/b9", None, False))

    def test_SES_2_qoder_session_error_comes_as_text_and_exit(self):
        """Probe 5: no JSON; stderr text, exit 42, before any message."""
        ad, ev = self.read(None, err='Error resuming session: Invalid session identifier "0000".\n', cls=X.QoderAdapter)
        self.assertEqual(kinds(ev), ["SessionChanged", "ProcessExited"])
        self.assertEqual(ev[0].change, SessionChange.LOST)

    def test_REC_1_qoder_connection_failure_is_transient_from_the_errors_list(self):
        """Probe 9: `errors[]` text, api_error_status null."""
        err = "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"
        ad, ev = self.read([init("Efficient"), tool("c0", "x"), say(err, "<synthetic>"),
                            j(type="result", subtype="error_during_execution", is_error=True, errors=[err],
                              terminal_reason=None, api_error_status=None, session_id=SID)])
        end = of(ev, TurnEnded)[0]
        self.assertEqual((end.status, end.error), (TurnStatus.ERROR, ErrorClass.TRANSIENT))

    def test_qoder_prints_non_json_notices_that_are_skipped(self):
        ad, ev = self.read(["Model set to Efficient (persisted)", init("Efficient"), say("x", "efficient")])
        self.assertEqual(kinds(ev), ["TurnStarted", "ModelReported", "Text", "TurnEnded", "ProcessExited"])


class TestQwenStream(Feed):
    cls = X.QwenAdapter

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "qwen"

    def status(self, jid, **kw):
        d = self.home / "tmp" / "abc" / "background-shells" / SID
        d.mkdir(parents=True, exist_ok=True)
        (d / f"shell-{jid}.status").write_text(json.dumps({"id": jid, "command": "sleep 3; exit 3",
                                                           "startTime": "2026-11-25T12:01:37.704Z", "pid": GONE, **kw}))

    def adapter(self, lines):
        ad = self.feed(lines)
        ad.config_home = str(self.home)
        return ad

    def test_JOB_4_qwen_never_wakes_itself_and_a_turn_starts_with_its_first_output(self):
        self.assertFalse(X.QwenAdapter.caps.wakes_itself)
        self.assertFalse(X.QwenAdapter.caps.sends_while_writing)
        ad = self.adapter([
            j(type="system", subtype="init", session_id=SID, model="qwen3.8-flash"),
            j(type="stream_event", event={"type": "message_start"}),
            j(type="assistant", message={"model": "qwen3.8-flash", "content": [
                {"type": "tool_use", "id": "tq1", "name": "run_shell_command",
                 "input": {"command": "sleep 3; exit 3", "is_background": True}}]}),
            tool_done("tq1", "Background shell bg_0a9f9750 started (pid 26490)."),
            result("started", 2)])
        ev = list(ad.events())
        self.assertEqual(kinds(ev), ["ModelReported", "TurnStarted", "ToolStart", "ToolEnd", "JobStarted", "TurnEnded",
                                     "ProcessExited"])
        self.assertEqual(ev[1].message_ids, ())
        self.assertEqual(of(ev, JobStarted)[0].job.id, "worker-1/bg_0a9f9750")

    def test_JOB_1_qwen_job_end_is_read_from_its_status_file(self):
        """Probe 5: no job event in the stream; shell-<id>.status has status and an error text with the code."""
        ad = self.adapter([
            j(type="system", subtype="init", session_id=SID, model="m"),
            j(type="assistant", message={"model": "m", "content": [
                {"type": "tool_use", "id": "tq1", "name": "run_shell_command",
                 "input": {"command": "sleep 3; exit 3", "is_background": True}}]}),
            tool_done("tq1", "Background shell bg_0a9f9750 started (pid 26490)."), result("started", 2)])
        list(ad.events())
        self.status("bg_0a9f9750", status="running")
        self.assertEqual(list(ad.events()), [])
        self.assertEqual(ad.jobs(), [])  # running, but its process is not there: nobody can say how it ended
        self.status("bg_0a9f9750", status="failed", error="exited with code 3", endTime="2026-11-25T12:01:41.715Z")
        ev = list(ad.events())
        self.assertEqual([(e.job_id, e.exit_code, e.uid) for e in ev],
                         [("worker-1/bg_0a9f9750", 3, "job:worker-1/bg_0a9f9750:ended")])
        self.assertEqual(list(ad.events()), [])  # once
        self.assertEqual([(x.state, x.exit_code) for x in ad.jobs()], [(JobState.ENDED, 3)])

    def test_MOD_5_qwen_reports_the_model_it_fell_back_to(self):
        """Probe 5: -m <unknown> is ignored silently; init.model tells."""
        ad, ev = self.read([j(type="system", subtype="init", session_id=SID, model="qwen4-max"),
                            j(type="assistant", message={"model": "qwen4-max", "content": [{"type": "text", "text": "x"}]})])
        self.assertEqual(of(ev, ModelReported)[0].model, "qwen4-max")

    def test_SES_2_qwen_unknown_session_is_a_plain_exit(self):
        ad, ev = self.read(None, err="No saved session found with ID 0000. Run `qwen --resume` ...", cls=X.QwenAdapter)
        self.assertEqual(kinds(ev), ["SessionChanged", "ProcessExited"])

    def test_compress_ends_with_COMPACTED_when_the_cli_has_no_boundary(self):
        ad = self.adapter([j(type="system", subtype="init", session_id=SID, model="m")])
        list(ad.events())
        ad._compacting = True
        with open(self.dir / "out-0001.jsonl", "ab") as f:
            f.write((say("Context compressed (16370 -> ~14627).", "m") + "\n" + result("") + "\n").encode())
        ev = [e for e in ad.events() if not isinstance(e, ProcessExited)]
        self.assertEqual([(type(e).__name__, getattr(e, "change", None)) for e in ev],
                         [("SessionChanged", SessionChange.COMPACTED)])


# ------------------------------------------------------------------------------------------- a fake CLI process

FAKE = r'''
import json, os, signal, subprocess, sys, time
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
log = os.environ["FAKE_LOG"]
def note(x):
    with open(log, "a") as f: f.write(x + "\n")
note("ARGS " + json.dumps(sys.argv[1:]))
note("ENV " + json.dumps({k: os.environ.get(k, "") for k in ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")}))
def term(*_):
    note("TERM")
    if sc.get("term") != "ignore":
        sys.exit(0)
signal.signal(signal.SIGTERM, term)
if sc.get("child"):
    p = subprocess.Popen(["sleep", "300"], start_new_session=True)
    note("CHILD %d" % p.pid)
args = sys.argv[1:]
sid = next((args[i + 1] for i, a in enumerate(args) if a in ("--session-id", "--resume")), "")
def out(lines, uuid="", text=""):
    for l in lines:
        sys.stdout.write(l.replace("{uuid}", uuid).replace("{text}", text).replace("{sid}", sid) + "\n")
    sys.stdout.flush()
out(sc.get("start", []))
blocks = list(sc.get("on_message", []))
for line in sys.stdin:
    m = json.loads(line)
    c = m["message"]["content"]
    note("GOT " + json.dumps(m))
    if blocks:
        out(blocks.pop(0), m.get("uuid", ""), c if isinstance(c, str) else c[0]["text"])
    if sc.get("die_after_message"):
        sys.exit(3)
'''


class TestProcess(unittest.TestCase):
    """A real process behind the adapter: the fake CLI prints what the scenario says."""

    cls = X.ClaudeAdapter

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "fake_cli.py").write_text(FAKE)
        self.run_dir = root / "run"
        self.work = root / "work"
        self.work.mkdir()
        self.log = root / "fake.log"
        self.started = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ad in self.started:
            try:
                if ad._pid and ad._alive():
                    os.kill(ad._pid, signal.SIGKILL)
            except OSError:
                pass
        for line in self.lines("CHILD"):
            try:
                os.kill(int(line.split()[1]), signal.SIGKILL)
            except OSError:
                pass

    def scenario(self, **sc):
        p = Path(self.tmp.name) / "scenario.json"
        p.write_text(json.dumps(sc))
        os.environ["FAKE_SCENARIO"], os.environ["FAKE_LOG"] = str(p), str(self.log)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_SCENARIO", "FAKE_LOG")])

    def lines(self, prefix):
        try:
            return [x for x in self.log.read_text().splitlines() if x.startswith(prefix)]
        except OSError:
            return []

    def spec(self, **kw):
        options = {"executable": str(Path(self.tmp.name) / "fake_cli.py")}
        options.update(kw.pop("options", {}))
        if os.path.exists(options["executable"]):  # the fake is a python file: run it through a shebang
            text = Path(options["executable"]).read_text()
            if not text.startswith("#!"):
                Path(options["executable"]).write_text(f"#!{sys.executable}\n" + text)
            os.chmod(options["executable"], 0o755)
        return AgentSpec("worker-1", Role.WORKER, Choice(backend="claude", model="haiku", effort="low", options=options),
                         str(self.work), "Be brief.", str(self.run_dir))

    def adapter(self, cls=None):
        ad = (cls or self.cls)(FakeClock())
        self.started.append(ad)
        return ad

    def wait(self, ad, kind, count=1, timeout=10.0, got=None):
        got = [] if got is None else got
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got += list(ad.events())
            if len(of(got, kind)) >= count:
                return got
            time.sleep(0.05)
        self.fail(f"no {kind.__name__} in {kinds(got)}")

    def test_JOB_1_the_command_has_the_settings_the_design_asks_for(self):
        """3.2: both timeout variables raised, deny rules for nohup, setsid and &, no permission-bypassing flag."""
        self.scenario()
        ad = self.adapter()
        proc = ad.start(self.spec(), None)
        self.assertGreater(proc.pid, 0)
        self.assertTrue(os.path.exists(proc.endpoint) and os.path.exists(proc.output_file))
        end = time.monotonic() + 5
        while not self.lines("ENV") and time.monotonic() < end:
            time.sleep(0.05)
        args = json.loads(self.lines("ARGS")[0][5:])
        env = json.loads(self.lines("ENV")[0][4:])
        self.assertEqual(env, {"BASH_DEFAULT_TIMEOUT_MS": "86400000", "BASH_MAX_TIMEOUT_MS": "86400000"})
        for flag in ("-p", "--input-format", "stream-json", "--replay-user-messages", "--verbose", "--session-id"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        rules = args[args.index("--disallowedTools") + 1]
        for rule in ("Bash(nohup *)", "Bash(setsid *)", "Bash(*&)"):
            self.assertIn(rule, rules)
        self.assertEqual(args[args.index("--append-system-prompt") + 1], "Be brief.")
        self.assertNotIn("--permission-mode", args)
        self.assertFalse([a for a in args if "bypass" in a.lower() or "dangerously" in a.lower()])
        ad.stop()

    def test_the_agents_own_configuration_may_set_the_permission_mode(self):
        self.scenario()
        spec = self.spec(options={"permission_mode": "acceptEdits", "allowed_tools": ["Bash", "Read"]})
        cmd = X.ClaudeAdapter(FakeClock())._args(spec, "sid", False, False)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Bash,Read")
        qoder = X.QoderAdapter(FakeClock())._args(spec, "sid", False, False)
        self.assertEqual(qoder[qoder.index("--permission-mode") + 1], "accept_edits")
        self.assertIn("Bash(nohup:*)", qoder)
        self.assertNotIn("BASH_MAX", " ".join(X.QoderAdapter(FakeClock())._env(spec)))

    def test_MSG_6_the_message_id_travels_in_the_uuid_and_arrived_knows_it(self):
        """Send, receipt, turn; arrived() by the journal; the pid file and the pipe are in the run folder."""
        mid = new_id("t", Tick(at=1.0))
        self.scenario(on_message=[[queued(mid), queued(mid, "started"), init(sid="{sid}"), echo(mid, "{text}"),
                                   say("{text}!"), result("hello!")]])
        ad = self.adapter()
        ad.start(self.spec(), None)
        self.assertIs(ad.arrived(mid), False)
        ad.send(mid, "hello")
        ad._note("x000000000009", "try", "t")  # written to the pipe or not: cannot tell
        self.assertIsNone(ad.arrived("x000000000009"))
        ev = self.wait(ad, TurnEnded)
        got = json.loads(self.lines("GOT")[0][4:])
        self.assertEqual((got["uuid"], got["message"]["content"]), (X.to_uuid(mid), "hello"))
        self.assertEqual(kinds(ev)[:3], ["SessionChanged", "Received", "ModelReported"])
        self.assertEqual(ev[0].change, SessionChange.NEW)
        self.assertEqual(ev[0].uid, f"start:{ad._pid}")
        self.assertEqual(of(ev, TurnStarted)[0].message_ids, (mid,))
        self.assertIs(ad.arrived(mid), True)
        meta = json.loads((self.run_dir / "cli" / "worker-1" / "pid.json").read_text())
        self.assertEqual(meta["pid"], ad._pid)
        ad.stop()

    def test_REC_7_orch_closes_and_a_new_adapter_reconnects_to_the_same_process(self):
        """The CLI keeps running after detach(); the saved ProcRef and position are all a new Orch needs."""
        m1, m2 = "xaaaaaaaaaa01", "xaaaaaaaaaa02"
        self.scenario(on_message=[[queued(m1), echo(m1), say("one"), result("one")],
                                  [queued(m2), echo(m2), say("two"), result("two")]])
        a = self.adapter()
        proc = a.start(self.spec(), None)
        a.send(m1, "first")
        ev = self.wait(a, TurnEnded)
        last = [e for e in ev if e.pos][-1]
        saved = ProcRef(proc.pid, proc.started, proc.endpoint, proc.output_file, proc.stream, last.pos)
        a.detach()
        b = self.adapter()
        self.assertTrue(b.reconnect(saved))
        self.assertEqual(list(b.events()), [])
        self.assertIs(b.arrived(m1), True)  # the journal survived
        b.send(m2, "second")
        ev2 = self.wait(b, TurnEnded)
        self.assertEqual(of(ev2, TurnEnded)[0].text, "two")
        self.assertEqual(of(ev2, TurnStarted)[0].message_ids, (m2,))
        self.assertTrue(set(e.key for e in ev2).isdisjoint(e.key for e in ev))
        b.stop()

    def test_REC_9_stop_sends_SIGTERM_first_and_an_open_turn_ends_stopped(self):
        self.scenario(on_message=[[queued(A1), echo(A1), say("working")]])
        ad = self.adapter()
        ad.start(self.spec(), None)
        ad.send(A1, "go")
        ev = self.wait(ad, Text)
        ad.stop()
        ev += list(ad.events())
        self.assertEqual(self.lines("TERM"), ["TERM"])
        self.assertEqual(kinds(ev)[-2:], ["TurnEnded", "ProcessExited"])
        self.assertEqual(ev[-2].status, TurnStatus.STOPPED)
        self.assertTrue(ev[-1].asked)
        self.assertFalse(ad._alive())
        self.assertEqual(list(ad.events()), [])  # exactly one report

    def test_REC_9_after_SIGKILL_the_process_groups_of_the_jobs_are_killed_too(self):
        self.scenario(term="ignore", child=True)
        ad = self.adapter()
        ad.term_grace = 0.5
        ad.start(self.spec(), None)
        end = time.monotonic() + 5
        while not self.lines("CHILD") and time.monotonic() < end:
            time.sleep(0.05)
        child = int(self.lines("CHILD")[0].split()[1])
        ad.stop()
        self.assertFalse(ad._alive())
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(child, 0)
        self.assertEqual(self.lines("TERM"), ["TERM"])

    def test_SIL_4_a_process_that_dies_alone_is_reported_with_its_open_turn(self):
        self.scenario(on_message=[[queued(A1), echo(A1), say("x")]], die_after_message=True)
        ad = self.adapter()
        ad.start(self.spec(), None)
        ad.send(A1, "go")
        ev = self.wait(ad, ProcessExited)
        self.assertEqual(kinds(ev)[-2:], ["TurnEnded", "ProcessExited"])
        self.assertEqual((ev[-2].status, ev[-1].exit_code, ev[-1].asked, ev[-1].reported), (TurnStatus.ERROR, 3, False, True))
        with self.assertRaises(AdapterError):
            ad.send(A2, "again")

    def test_REC_8_a_second_start_ends_or_joins_the_cli_an_earlier_start_left(self):
        self.scenario()
        a = self.adapter()
        a.start(self.spec(), None)
        sid = a._sid
        old_pid = a._pid
        a.detach()  # Orch died before saving the ProcRef
        b = self.adapter()
        proc = b.start(self.spec(), SessionRef(sid))  # same session: joined
        self.assertEqual(proc.pid, old_pid)
        self.assertEqual(list(b.events())[0].change, SessionChange.RESUMED)
        b.detach()
        c = self.adapter()
        proc = c.start(self.spec(), None)  # a new session: the old process is ended first
        self.assertNotEqual(proc.pid, old_pid)
        self.assertFalse(X._running(old_pid, 0))
        c.stop()

    def test_a_missing_program_is_a_MISSING_CLI_error(self):
        self.scenario()
        spec = self.spec(options={"executable": "/nonexistent/claude"})
        with self.assertRaises(AdapterError) as cm:
            self.adapter().start(spec, None)
        self.assertEqual(cm.exception.error, ErrorClass.MISSING_CLI)

    def test_TERM_4_terminal_command_resumes_the_session_with_the_agents_settings(self):
        cmd = X.ClaudeAdapter(FakeClock()).terminal_command(self.spec(), SessionRef("s-1"))
        self.assertNotIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-1")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertIn("--append-system-prompt", cmd)
        self.assertEqual(X.QwenAdapter(FakeClock()).terminal_command(self.spec(), SessionRef("s-1"))[1:3],
                         ["--resume", "s-1"])

    def test_compact_sends_the_slash_command_with_its_own_id(self):
        self.scenario(on_message=[[queued(X.COMPACT_ID), j(type="system", subtype="compact_boundary"),
                                   echo(X.COMPACT_ID), result("", 0, stop_reason=None)]])
        ad = self.adapter()
        ad.start(self.spec(), None)
        self.assertTrue(ad.compact())
        ev = self.wait(ad, SessionChanged, 2)
        self.assertEqual([e.change for e in of(ev, SessionChanged)], [SessionChange.NEW, SessionChange.COMPACTED])
        self.assertFalse(of(ev, Received) or of(ev, TurnStarted))
        self.assertIn("/compact", self.lines("GOT")[0])
        ad.stop()

    def test_catalog_and_the_adapter_table(self):
        cat = X.ClaudeAdapter(FakeClock()).catalog()
        self.assertIn("max", cat[0].efforts)
        self.assertEqual(X.QwenAdapter(FakeClock()).catalog(), [])  # effort is hidden for Qwen (MOD-2)
        q = X.QoderAdapter(FakeClock())
        q._list_models = lambda: ["Auto", "Efficient"]
        self.assertEqual([m.model for m in q.catalog()], ["Auto", "Efficient"])
        for name, cls in (("claude", X.ClaudeAdapter), ("qoder", X.QoderAdapter), ("qwen", X.QwenAdapter)):
            self.assertIsInstance(make(name, FakeClock()), cls)

    def test_transcript_reads_the_session_history(self):
        hist = Path(self.tmp.name) / "projects" / "proj"
        hist.mkdir(parents=True)
        (hist / "s-9.jsonl").write_text("\n".join([
            j(type="user", message={"role": "user", "content": "hello"}, timestamp="2026-11-25T12:00:00.000Z"),
            j(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
              timestamp="2026-11-25T12:00:05.000Z"),
            j(type="user", message={"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
              timestamp="2026-11-25T12:00:06.000Z")]))
        ad = X.ClaudeAdapter(FakeClock())
        ad.history, ad._agent = str(Path(self.tmp.name) / "projects"), "worker-1"
        said = ad.transcript(SessionRef("s-9"), since=0)
        self.assertEqual([(s.by, s.text) for s in said], [(USER, "hello"), ("worker-1", "hi")])

    def test_uuid_round_trip(self):
        for mid in (A1, new_id("m", Tick(at=5.0), 3)):
            self.assertEqual(X.from_uuid(X.to_uuid(mid)), mid)
        self.assertEqual(X.from_uuid("0b31-random"), "")


if __name__ == "__main__":
    unittest.main()
