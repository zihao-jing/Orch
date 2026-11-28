"""Package X4: the Antigravity adapter (stream-json, kept open) and the `command` adapter (one process per turn).

Recorded agy lines are copied from docs/probes/agy.md and the probe samples (tmp/probes/p5). Tests with real processes
use small fake executables written into a temporary folder; no live CLI, no network.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from rt_helpers import FakeClock

from orch.runtime.adapters.agy import COMPACT, AgyAdapter, _epoch, models_from
from orch.runtime.adapters.base import AdapterError
from orch.runtime.adapters.oneshot import CommandAdapter
from orch.runtime.contracts import (AgentSpec, Choice, ErrorClass, JobEnded, JobNoticed, JobStarted, ModelReported,
                                    ProcRef, ProcessExited, SessionChange, SessionChanged, SessionRef, Text, ToolEnd, TurnEnded,
                                    TurnStarted, TurnStatus)

CONV = "be77b97a-6aa4-45af-9521-f210ab8aa3f4"


def L(obj):
    return json.dumps(obj) + "\n"


# Recorded: probe agy-stream, one turn with the answer ONE (init and steps shortened, ids kept).
RECORDED_TURN = [
    L({"event": "init", "conversation_id": CONV, "init": {"model": "gemini-3.8-flash-low", "cwd": "/tmp/p"}}),
    L({"event": "step_update", "step_update": {"conversation_id": CONV, "step_index": 0, "state": "DONE",
                                               "step_type": "user_input"}}),
    L({"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response",
                                               "text_delta": "ONE"}}),
    L({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                               "text_delta": "\n", "duration_seconds": 3.19}}),
    L({"event": "result", "result": {"conversation_id": CONV, "status": "SUCCESS", "response": "ONE\n",
                                     "num_turns": 1}}),
]

# Recorded shape (probe agy-job2): an async run_command, its job ends inside the same turn (system_message), then the
# result. The task id in the tool output is the one the result text names: "<conversation>/task-N".
JOB_TURN = [
    L({"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool",
                                               "tool_name": "run_command",
                                               "tool_info": {"parameters": {"CommandLine": "sleep 45; echo done"}}}}),
    L({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool",
                                               "tool_name": "run_command",
                                               "tool_info": {"parameters": {"CommandLine": "sleep 45; echo done"},
                                                             "output": f"Started task {CONV}/task-2 in background."}}}),
    L({"event": "step_update", "step_update": {"step_index": 5, "state": "DONE", "step_type": "system_message"}}),
    L({"event": "result", "result": {"conversation_id": CONV, "status": "SUCCESS",
                                     "response": f"Task `{CONV}/task-2` has completed (exited with code 0)."}}),
]


def agy_adapter(clock: FakeClock, path: str, *, want: str = "", queue=("m1",)) -> AgyAdapter:
    """An adapter reading a recorded output file; the process counts as alive while the test reads."""
    a = AgyAdapter(clock)
    a.agent, a.joined, a.out_path, a.stream, a.want = "w1", True, path, "out-test", want
    a.brain = os.path.join(os.path.dirname(path), "brain")  # never the user's own agy files
    a.queue.extend(queue)
    return a


def write(path: str, *lines: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line)


def read_all(a: AgyAdapter) -> list:
    with mock.patch.object(AgyAdapter, "_alive_now", return_value=True):
        return list(a.events())


def poll(a, until, timeout: float = 15.0) -> list:
    """Read events of a real process until `until(events)` holds."""
    out = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        out += list(a.events())
        if until(out):
            return out
        time.sleep(0.05)
    raise AssertionError(f"no events in time: {[type(e).__name__ for e in out]}")


class Real:
    """A temporary folder with fake executables; removed afterwards."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="rt_x4_")
        self.run_dir = os.path.join(self.dir, "run")
        os.makedirs(self.run_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def script(self, name: str, body: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} -u {os.path.join(self.dir, name + '.py')} \"$@\"\n")
        with open(os.path.join(self.dir, name + ".py"), "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def spec(self, agent: str, choice: Choice, system: str = "") -> AgentSpec:
        return AgentSpec(agent=agent, choice=choice, cwd=self.dir, system_prompt=system, run_dir=self.run_dir)


class AgyArgsTest(unittest.TestCase):
    """The model id carries the effort: --effort is never passed together with it (probe: model conflicts)."""

    def setUp(self) -> None:
        self.a = AgyAdapter(FakeClock())

    def test_model_id_with_effort_gets_no_effort_flag(self) -> None:
        args = self.a._args(Choice(model="gemini-3.8-flash-low", effort="low"), None)
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertNotIn("--effort", args)

    def test_effort_replaces_the_suffix_of_the_model_id(self) -> None:
        args = self.a._args(Choice(model="gemini-3.8-flash-low", effort="high"), None)
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertNotIn("--effort", args)

    def test_plain_model_takes_effort_flag(self) -> None:
        args = self.a._args(Choice(model="gemini-3.8-flash", effort="high"), None)
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash")

    def test_no_permission_bypass_unless_the_agent_sets_it(self) -> None:
        self.assertNotIn("--dangerously-skip-permissions", self.a._args(Choice(model="m"), None))
        args = self.a._args(Choice(model="m", options={"dangerously_skip_permissions": True}), None)
        self.assertIn("--dangerously-skip-permissions", args)

    def test_resume_passes_the_conversation(self) -> None:
        args = self.a._args(Choice(model="m"), SessionRef(session_id=CONV))
        self.assertEqual(args[args.index("--conversation") + 1], CONV)


class AgyParseTest(unittest.TestCase):
    """Events from recorded output (docs/probes/agy.md)."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tmp = tempfile.mkdtemp(prefix="rt_x4_")
        self.path = os.path.join(self.tmp, "out.jsonl")
        open(self.path, "w").close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recorded_turn_gives_session_model_text_and_end(self) -> None:
        """init gives the conversation and model; the taken message starts the turn; the result ends it."""
        write(self.path, *RECORDED_TURN)
        evs = read_all(agy_adapter(self.clock, self.path))
        kinds = [type(e) for e in evs]
        self.assertEqual(kinds[:3], [ModelReported, SessionChanged, TurnStarted])
        self.assertEqual(evs[0].model, "gemini-3.8-flash-low")
        self.assertEqual(evs[1].change, SessionChange.NEW)
        self.assertEqual(evs[1].session_id, CONV)
        self.assertEqual(evs[2].message_ids, ("m1",))
        end = evs[-1]
        self.assertIsInstance(end, TurnEnded)
        self.assertEqual((end.status, end.text, end.session_id), (TurnStatus.OK, "ONE\n", CONV))
        self.assertEqual([e.text for e in evs if isinstance(e, Text)], ["ONE", "\n"])

    def test_unknown_conversation_id_is_reported_as_lost(self) -> None:
        """Probe: an unknown --conversation id silently starts a NEW conversation; Orch must report it, not go on."""
        write(self.path, *RECORDED_TURN)
        evs = read_all(agy_adapter(self.clock, self.path, want="00000000-0000-4000-8000-000000000000"))
        sc = next(e for e in evs if isinstance(e, SessionChanged))
        self.assertEqual(sc.change, SessionChange.LOST)
        self.assertEqual(sc.session_id, CONV)

    def test_requested_conversation_is_resumed(self) -> None:
        write(self.path, *RECORDED_TURN)
        evs = read_all(agy_adapter(self.clock, self.path, want=CONV))
        self.assertEqual(next(e for e in evs if isinstance(e, SessionChanged)).change, SessionChange.RESUMED)

    def test_async_job_is_noticed_inside_the_open_turn(self) -> None:
        """JobNoticed (and the job's end) comes before the TurnEnded of the turn that reports it."""
        write(self.path, L({"event": "init", "conversation_id": CONV, "init": {}}), *JOB_TURN)
        evs = read_all(agy_adapter(self.clock, self.path))
        job_id = f"w1/{CONV}/task-2"
        started = next(e for e in evs if isinstance(e, JobStarted))
        self.assertEqual(started.job.id, job_id)
        tool_end = next(i for i, e in enumerate(evs) if isinstance(e, ToolEnd))
        self.assertLess(evs.index(started), tool_end)
        ended = [i for i, e in enumerate(evs) if isinstance(e, JobEnded)]
        noticed = [i for i, e in enumerate(evs) if isinstance(e, JobNoticed)]
        end = [i for i, e in enumerate(evs) if isinstance(e, TurnEnded)]
        self.assertEqual(len(ended), 1)
        self.assertEqual(evs[noticed[0]].job_id, job_id)
        self.assertLess(ended[0], noticed[0])
        self.assertLess(noticed[0], end[-1])

    def test_failed_result_is_classified_and_stopped_one_is_not_an_error(self) -> None:
        """Probe: our own kill gives a result with status ERROR; that is STOPPED, not a failure."""
        write(self.path, L({"event": "result", "result": {"status": "ERROR", "response": "",
                                                          "error": "credit balance is too low"}}))
        a = agy_adapter(self.clock, self.path, queue=())
        a.turn_open = True
        evs = read_all(a)
        self.assertEqual((evs[-1].status, evs[-1].error), (TurnStatus.ERROR, ErrorClass.BILLING))
        write(self.path, L({"event": "result", "result": {"status": "ERROR", "response": "MARKER",
                                                          "error": "stream input cancelled: context canceled"}}))
        a.stopping = True
        self.assertEqual(read_all(a)[-1].status, TurnStatus.STOPPED)

    def test_partial_line_waits_for_its_end(self) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(RECORDED_TURN[0][:20])
        a = agy_adapter(self.clock, self.path)
        self.assertEqual(read_all(a), [])
        self.assertEqual(a.off, 0)

    def test_multi_event_record_keeps_start_position_and_unique_uids(self) -> None:
        """Contract: all but the last event of a record carry the record's start as pos; uids tell them apart."""
        write(self.path, L({"event": "init", "conversation_id": CONV, "init": {}}), *JOB_TURN[:2])
        evs = read_all(agy_adapter(self.clock, self.path))
        job_started = next(e for e in evs if isinstance(e, JobStarted))
        tool_end = next(e for e in evs if isinstance(e, ToolEnd))
        self.assertNotEqual(job_started.uid, tool_end.uid)
        self.assertEqual(job_started.uid.split(":")[0], tool_end.uid.split(":")[0])  # same record
        self.assertLess(int(job_started.pos), int(tool_end.pos))  # the last one carries the record's end


# Recorded (probe P6, agy 1.5.19, ids kept short): the stream does not say the command went to the background; the
# tool step stays ACTIVE until the command ends. The transcript says it (GENERIC, same step_index) and names the task
# that ended (SYSTEM_MESSAGE sender=<conv>/task-2).
BG_STREAM = [
    L({"event": "init", "conversation_id": CONV, "init": {"model": "gemini-3.8-flash-low"}}),
    L({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}}),
    L({"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool",
                                               "tool_name": "run_command", "tool_info": {"name": "run_command",
                                               "parameters": {"CommandLine": "sleep 97.31 && echo JOBDONE"}}}}),
]
BG_STREAM_END = [
    L({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool",
                                               "tool_name": "run_command", "duration_seconds": 7.88,
                                               "tool_info": {"name": "run_command",
                                                             "parameters": {"CommandLine": "sleep 97.31 && echo JOBDONE"},
                                                             "output": "Command exited with code 143.\n\nNo output from the command."}}}),
    L({"event": "step_update", "step_update": {"step_index": 4, "state": "DONE", "step_type": "system_message"}}),
    L({"event": "result", "result": {"conversation_id": CONV, "status": "SUCCESS",
                                     "response": "STARTED\nThe background command was terminated (SIGTERM / exit code 143).\n"}}),
]
BG_TRANSCRIPT = [
    L({"step_index": 2, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-28T11:04:21Z",
       "content": f"Created At: 2026-11-28T19:04:21+08:00\nTool is running as a background task with task id: {CONV}/task-2\n"
                  f"Task Description: sleep 97.31 && echo JOBDONE\nTask logs are available at: file:///x/task-2.log\n"}),
]
BG_TRANSCRIPT_END = [
    L({"step_index": 4, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": "2026-11-28T11:04:29Z",
       "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n[Message] "
                  f"timestamp=2026-11-28T11:04:29Z sender={CONV}/task-2 priority=MESSAGE_PRIORITY_HIGH content=Task id "
                  f"\"{CONV}/task-2\" finished with result:\nCommand exited with code 143.\n"}),
]
# Recorded (probe P6): transcript.jsonl of a conversation with a terminal turn after an Orch turn.
TRANSCRIPT = [
    L({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-11-28T10:56:56Z",
       "content": "<USER_REQUEST>\n<system_instructions>\nYou are w1.\n</system_instructions>\n\nRemember PELICAN.\n"
                  "</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-28T18:56:56+08:00.\n"
                  "</ADDITIONAL_METADATA>"}),
    L({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-11-28T10:56:56Z",
       "model": "gemini-3.8-flash-low", "content": "OK"}),
    L({"step_index": 4, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-11-28T10:58:19Z",
       "content": "<USER_REQUEST>\nWhat was the code word? Reply with it and then the word TERMINAL.\n</USER_REQUEST>\n"
                  "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-28T18:58:19+08:00.\n</ADDITIONAL_METADATA>"}),
    L({"step_index": 5, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "created_at": "2026-11-28T10:58:19Z",
       "content": "[Notice] All your subagents and background tasks have been stopped due to server restart."}),
    L({"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-11-28T10:58:19Z",
       "model": "gemini-3.8-flash-low", "content": "PELICAN TERMINAL"}),
]
# Recorded (probe P6): `agy models`, shortened.
MODELS_OUT = ("Fetching available models...\ngemini-4-neon-high\tGemini 4 Neon (High)\n"
              "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\ngemini-pro-orchestrator\tOrchestrator (Carbon)\n")


class AgyJobsTest(unittest.TestCase):
    """agy 1.5.19: background jobs are known from agy's transcript (probe P6)."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tmp = tempfile.mkdtemp(prefix="rt_x4_")
        self.path = os.path.join(self.tmp, "out.jsonl")
        open(self.path, "w").close()
        logs = os.path.join(self.tmp, "brain", CONV, ".system_generated", "logs")
        os.makedirs(logs)
        self.tr = os.path.join(logs, "transcript_full.jsonl")
        open(self.tr, "w").close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_background_command_is_a_job_that_ends_inside_the_turn(self) -> None:
        a = agy_adapter(self.clock, self.path)
        write(self.path, *BG_STREAM)
        evs = read_all(a)
        self.assertIn("ToolStart", [type(e).__name__ for e in evs])
        write(self.tr, *BG_TRANSCRIPT)
        evs = read_all(a)  # the job is known while its tool step is still open
        started = next(e for e in evs if isinstance(e, JobStarted))
        jid = f"w1/{CONV}/task-2"
        self.assertEqual((started.job.id, started.job.command), (jid, "sleep 97.31 && echo JOBDONE"))
        self.assertEqual([e.tool_id for e in evs if isinstance(e, ToolEnd)], ["2"])
        self.assertEqual([j.state.value for j in a.jobs()], ["running"])
        write(self.tr, *BG_TRANSCRIPT_END)
        write(self.path, *BG_STREAM_END)
        evs = read_all(a)
        kinds = [type(e) for e in evs]
        self.assertEqual(kinds.count(JobEnded), 1)
        self.assertEqual(next(e for e in evs if isinstance(e, JobEnded)).exit_code, 143)
        self.assertNotIn(ToolEnd, kinds)  # ended with the job's start
        self.assertLess(kinds.index(JobNoticed), kinds.index(TurnEnded))
        self.assertEqual(a.jobs()[0].exit_code, 143)

    def test_canceled_job_ends_from_the_transcript_alone(self) -> None:
        """Probe P6: a job Orch stopped is "canceled with result" in the transcript, with no exit code."""
        a = agy_adapter(self.clock, self.path)
        write(self.path, *BG_STREAM)
        write(self.tr, *BG_TRANSCRIPT)
        read_all(a)
        write(self.tr, L({"step_index": 5, "type": "SYSTEM_MESSAGE", "created_at": "2026-11-28T11:26:43Z",
                          "content": f"[Message] timestamp=2026-11-28T11:26:43Z sender={CONV}/task-2 priority=LOW "
                                     f"content=Task id \"{CONV}/task-2\" was canceled with result:\nTool execution "
                                     "was canceled\n"}))
        evs = read_all(a)
        self.assertEqual([(type(e), getattr(e, "exit_code", None)) for e in evs], [(JobEnded, None), (JobNoticed, None)])

    def test_transcript_read_before_the_tool_step_gives_no_open_tool(self) -> None:
        a = agy_adapter(self.clock, self.path)
        write(self.path, *BG_STREAM[:2])
        read_all(a)
        write(self.tr, *BG_TRANSCRIPT)
        evs = read_all(a)  # the transcript's step came before the stream's
        write(self.path, BG_STREAM[2])
        evs += read_all(a)
        self.assertNotIn("ToolStart", [type(e).__name__ for e in evs])  # transcript first: the step is a job
        self.assertEqual(len([e for e in evs if isinstance(e, JobStarted)]), 1)

    def test_steps_from_before_the_process_are_not_its_jobs(self) -> None:
        a = agy_adapter(self.clock, self.path)
        a.tr_since = _epoch("2026-11-28T12:00:00Z")
        write(self.tr, *BG_TRANSCRIPT)
        write(self.path, *BG_STREAM)
        self.assertEqual([e for e in read_all(a) if isinstance(e, JobStarted)], [])

    def test_reconnect_reads_the_jobs_again_from_the_transcript(self) -> None:
        write(self.path, *BG_STREAM)
        write(self.tr, *BG_TRANSCRIPT)
        d = os.path.join(self.tmp, "cli", "w1")
        os.makedirs(d)
        a = AgyAdapter(self.clock)
        a.brain = os.path.join(self.tmp, "brain")
        self.assertFalse(a.reconnect(ProcRef(pid=1, started=0, endpoint=os.path.join(d, "in.fifo"),
                                             output_file=self.path, stream="out-test", read_pos="0")))
        self.assertEqual((a.agent, a.conv), ("w1", CONV))
        read_all(a)
        self.assertEqual([j.id for j in a.jobs()], [f"w1/{CONV}/task-2"])

    def test_stop_job_ends_the_one_shell_group(self) -> None:
        a = agy_adapter(self.clock, self.path)
        a.pid = 500
        write(self.path, *BG_STREAM)
        write(self.tr, *BG_TRANSCRIPT)
        read_all(a)
        jid = f"w1/{CONV}/task-2"
        rows = [(500, 1, 500, "agy --input-format stream-json"), (501, 500, 501, "/bin/zsh -c sleep 97.31 && echo JOBDONE"),
                (502, 501, 501, "sleep 97.31")]
        with mock.patch("orch.runtime.adapters.agy._ps", return_value=rows), \
                mock.patch.object(AgyAdapter, "_alive_now", return_value=True), \
                mock.patch("orch.runtime.adapters.agy.os.killpg") as kill:
            self.assertTrue(a.stop_job(jid))
            kill.assert_called_once_with(501, 15)
            kill.reset_mock()
            twin = rows + [(503, 500, 503, "/bin/zsh -c sleep 97.31 && echo JOBDONE")]
            with mock.patch("orch.runtime.adapters.agy._ps", return_value=twin):
                self.assertFalse(a.stop_job(jid))  # two alike: which one is not known
            kill.assert_not_called()
            self.assertFalse(a.stop_job("w1/other/task-9"))


class AgyRestTest(unittest.TestCase):
    """Compact, terminal, transcript, model list, arrived (probe P6)."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tmp = tempfile.mkdtemp(prefix="rt_x4_")
        self.path = os.path.join(self.tmp, "out.jsonl")
        open(self.path, "w").close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compact_turn_is_reported_as_compacted_not_as_a_turn(self) -> None:
        a = agy_adapter(self.clock, self.path, queue=(COMPACT,))
        a.compacting = False
        write(self.path, L({"event": "init", "conversation_id": CONV, "init": {}}),
              L({"event": "step_update", "step_update": {"step_index": 15, "state": "DONE", "step_type": "user_input"}}),
              L({"event": "result", "result": {"conversation_id": CONV, "status": "SUCCESS", "response": ""}}))
        evs = read_all(a)
        kinds = [type(e) for e in evs]
        self.assertNotIn(TurnStarted, kinds)
        self.assertNotIn(TurnEnded, kinds)
        self.assertEqual(evs[-1].change, SessionChange.COMPACTED)
        self.assertFalse(a.turn_open)

    def test_compact_refused_before_anything_was_said_or_while_busy(self) -> None:
        a = agy_adapter(self.clock, self.path, queue=())
        a.need_system = True
        self.assertFalse(a.compact())
        a.need_system, a.turn_open = False, True
        self.assertFalse(a.compact())

    def test_terminal_command_is_interactive_on_the_conversation(self) -> None:
        spec = AgentSpec(agent="w1", choice=Choice(backend="agy", model="gemini-3.8-flash-low",
                                                   options={"executable": "/x/agy", "mode": "auto"}))
        cmd = AgyAdapter(self.clock).terminal_command(spec, SessionRef(session_id=CONV))
        self.assertEqual(cmd[0], "/x/agy")
        self.assertNotIn("--input-format", cmd)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], CONV)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.8-flash-low")

    def test_transcript_gives_user_lines_and_replies_after_since(self) -> None:
        logs = os.path.join(self.tmp, "brain", CONV, ".system_generated", "logs")
        os.makedirs(logs)
        write(os.path.join(logs, "transcript.jsonl"), *TRANSCRIPT)
        a = AgyAdapter(self.clock)
        a.agent, a.brain = "w1", os.path.join(self.tmp, "brain")
        everything = a.transcript(SessionRef(session_id=CONV), 0)
        self.assertEqual([(s.by, s.text) for s in everything][:2], [("user", "Remember PELICAN."), ("w1", "OK")])
        later = a.transcript(SessionRef(session_id=CONV), _epoch("2026-11-28T10:58:00Z"))
        self.assertEqual([(s.by, s.text) for s in later],
                         [("user", "What was the code word? Reply with it and then the word TERMINAL."),
                          ("w1", "PELICAN TERMINAL")])
        self.assertEqual(a.transcript(SessionRef(session_id="no-such"), 0), [])

    def test_models_listing(self) -> None:
        ms = models_from(MODELS_OUT)
        self.assertEqual([m.model for m in ms], ["gemini-4-neon-high", "gemini-3.8-flash-low", "gemini-pro-orchestrator"])
        self.assertEqual(ms[1].label, "Gemini 3.8 Flash (Low)")

    def test_arrived(self) -> None:
        a = agy_adapter(self.clock, self.path, queue=("m1", "m2"))
        write(self.path, L({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE",
                                                                    "step_type": "user_input"}}))
        read_all(a)
        with mock.patch.object(AgyAdapter, "_alive_now", return_value=False):
            self.assertTrue(a.arrived("m1"))  # taken in
            self.assertFalse(a.arrived("m2"))  # queued in a process that died
        self.assertIsNone(a.arrived("m9"))


class AgyProcessTest(Real, unittest.TestCase):
    """A fake agy in stream-json: init, then one turn per line on stdin. Real FIFO, pid file and process group."""

    FAKE = (
        "import sys, json\n"
        "print(json.dumps({'event': 'init', 'conversation_id': 'c-fake', 'init': {'model': 'm-low'}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'event': 'step_update', 'step_update': {'step_index': 0, 'state': 'DONE',"
        " 'step_type': 'user_input'}}), flush=True)\n"
        "    print(json.dumps({'event': 'result', 'result': {'conversation_id': 'c-fake', 'status': 'SUCCESS',"
        " 'response': 'OK'}}), flush=True)\n"
    )

    def setUp(self) -> None:
        Real.setUp(self)
        self.clock = FakeClock()
        self.exe = self.script("fake-agy", self.FAKE)
        self.a = AgyAdapter(self.clock)
        self.choice = Choice(backend="agy", model="m-low", options={"executable": self.exe})

    def tearDown(self) -> None:
        self.a.stop()
        Real.tearDown(self)

    def test_turn_round_trip_then_stop_ends_the_process(self) -> None:
        ref = self.a.start(self.spec("w1", self.choice), None)
        self.assertTrue(ref.endpoint.endswith("in.fifo"))
        evs = poll(self.a, lambda es: any(isinstance(e, SessionChanged) for e in es))
        self.assertEqual(next(e for e in evs if isinstance(e, SessionChanged)).session_id, "c-fake")
        self.a.send("m1", "hello")
        evs += poll(self.a, lambda es: any(isinstance(e, TurnEnded) for e in es))
        self.assertEqual(next(e for e in evs if isinstance(e, TurnStarted)).message_ids, ("m1",))
        self.assertEqual(next(e for e in evs if isinstance(e, TurnEnded)).status, TurnStatus.OK)
        self.a.stop()
        evs += poll(self.a, lambda es: any(isinstance(e, ProcessExited) for e in es))
        self.assertTrue(next(e for e in evs if isinstance(e, ProcessExited)).asked)

    def test_missing_executable_is_a_missing_cli_error(self) -> None:
        bad = Choice(backend="agy", options={"executable": os.path.join(self.dir, "no-such-agy")})
        with self.assertRaises(AdapterError) as ctx:
            self.a.start(self.spec("w1", bad), None)
        self.assertEqual(ctx.exception.error, ErrorClass.MISSING_CLI)


class CommandTest(Real, unittest.TestCase):
    """One process per turn: the prompt goes to stdin (or as configured), output is read when the process ends."""

    def setUp(self) -> None:
        Real.setUp(self)
        self.clock = FakeClock()

    def start(self, options: dict, system: str = "") -> CommandAdapter:
        a = CommandAdapter(self.clock)
        a.start(self.spec("w2", Choice(backend="command", options=options), system), None)
        return a

    def test_turn_output_and_session_announcement(self) -> None:
        cmd = [sys.executable, "-c", "import sys; print('got:' + sys.stdin.read())"]
        a = self.start({"command": cmd})
        a.send("m1", "hi")
        evs = poll(a, lambda es: any(isinstance(e, TurnEnded) for e in es))
        sc = next(e for e in evs if isinstance(e, SessionChanged))
        self.assertEqual((sc.change, bool(sc.session_id)), (SessionChange.NEW, True))
        self.assertEqual(next(e for e in evs if isinstance(e, TurnStarted)).message_ids, ("m1",))
        end = next(e for e in evs if isinstance(e, TurnEnded))
        self.assertEqual((end.status, end.text), (TurnStatus.OK, "got:hi"))
        a.stop()

    def test_system_prompt_goes_in_front_of_the_first_turn_only(self) -> None:
        cmd = [sys.executable, "-c", "import sys; print(sys.stdin.read()[:40].replace(chr(10), ' '))"]
        a = self.start({"command": cmd}, system="SYS RULES")
        a.send("m1", "hi")
        first = next(e for e in poll(a, lambda es: any(isinstance(e, TurnEnded) for e in es))
                     if isinstance(e, TurnEnded))
        self.assertIn("SYS RULES", first.text)
        a.stop()

    def test_failed_process_is_a_turn_error_and_the_agent_stays_open(self) -> None:
        a = self.start({"command": [sys.executable, "-c", "import sys; sys.exit(3)"]})
        a.send("m1", "hi")
        evs = poll(a, lambda es: any(isinstance(e, TurnEnded) for e in es))
        self.assertEqual(next(e for e in evs if isinstance(e, TurnEnded)).status, TurnStatus.ERROR)
        a.send("m2", "again")  # a new process for the next message
        evs += poll(a, lambda es: sum(isinstance(e, TurnEnded) for e in evs + es) >= 2)
        self.assertEqual(sum(isinstance(e, TurnEnded) for e in evs), 2)

    def test_stop_ends_the_running_turn_and_the_process(self) -> None:
        a = self.start({"command": [sys.executable, "-c", "import time; time.sleep(60)"]})
        a.send("m1", "hi")
        a.stop()
        evs = poll(a, lambda es: any(isinstance(e, ProcessExited) for e in es), timeout=15)
        self.assertEqual(next(e for e in evs if isinstance(e, TurnEnded)).status, TurnStatus.STOPPED)
        self.assertTrue(next(e for e in evs if isinstance(e, ProcessExited)).asked)

    def test_send_while_a_turn_runs_is_refused(self) -> None:
        a = self.start({"command": [sys.executable, "-c", "import time; time.sleep(60)"]})
        a.send("m1", "hi")
        with self.assertRaises(AdapterError):
            a.send("m2", "again")
        a.stop()

    def test_missing_executable_is_a_missing_cli_error(self) -> None:
        a = self.start({"command": [os.path.join(self.dir, "no-such-tool")]})
        with self.assertRaises(AdapterError) as ctx:
            a.send("m1", "hi")
        self.assertEqual(ctx.exception.error, ErrorClass.MISSING_CLI)

    def test_command_option_is_required(self) -> None:
        with self.assertRaises(AdapterError):
            CommandAdapter(self.clock).start(self.spec("w2", Choice(backend="command", options={})), None)

    def test_no_jobs_no_receipts_no_reconnect(self) -> None:
        a = CommandAdapter(self.clock)
        self.assertEqual(a.caps.__dict__, {k: False for k in a.caps.__dict__})
        self.assertEqual(a.jobs(), [])
        self.assertIsNone(a.arrived("m1"))
        self.assertFalse(a.reconnect(None))


if __name__ == "__main__":
    unittest.main()
