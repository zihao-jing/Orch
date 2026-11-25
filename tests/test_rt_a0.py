"""Package A0: the contracts (stored form, ids, holds, text keys, dashboard state), the mock CLI and the helpers.

COVERAGE is the walk through all requirement ids: the types that express each one.
"""
from __future__ import annotations

from rt_helpers import *  # noqa: F401,F403
from rt_helpers import C, ROOT, CODEX, CliReply, FakeClock, MockAdapter, MockCLI, mock_agents

import dataclasses
import json
import re
import unittest

from orch.runtime import adapters
from orch.runtime.adapters.base import AdapterError, BaseAdapter, classify_error

# id -> names in contracts (Class, Class.field, Enum.MEMBER); "prompt": wording only, texts.system_prompt / texts.render
COVERAGE = {
    "MSG-1": "Send Caps.sends_while_writing", "MSG-2": "HoldReason Hold HoldAgent HOLD_OWNERS",
    "MSG-3": "Message MessageStatus Received.taken", "MSG-4": "Message.needs_answer Tell ToUser",
    "MSG-5": "Resend Message.checked_at Message.attempts FailAgent Settings.unanswered_after",
    "MSG-6": "Intent new_id Message.rendered Message.sendable Adapter.arrived Discarded", "MSG-7": "Message.seq TextKey.WORKER_FINAL Message.text",
    "MSG-8": "Reject TextKey.UNKNOWN_WORKER TextKey.NO_WORKERS", "MSG-9": "Chat Tell TextKey.DIRECT_MESSAGE_NOTICE",
    "MSG-10": "Reply.text",
    "LAB-1": "Reply.messages Reply.silent Problem.REMOVED", "LAB-2": "Reply.untagged", "LAB-3": "Reply Decision",
    "LAB-4": "Reply.silent DropReason.SILENT",
    "LAB-5": "Reply.broken ReturnToSender Turn.keys TextKey.LABEL_BROKEN_PASSED",
    "LAB-6": "Turn.triggers Turn.joined Message.continues SendToCritic TextKey.WORKER_TO_CRITIC",
    "WRK-1": "Agent Assignment Agent.assignment", "WRK-2": "WorkerAdd WorkerClose WorkerReopen WorkerDelete AddWorker",
    "WRK-3": "SetLifecycle Lifecycle.DELETED", "WRK-4": "DropReason.WORKER_CLOSED",
    "WRK-5": "WorkerClose.jobs JobsChoice CommandReply.choices StopJob", "WRK-6": "TextKey.REM_WORKER_LINE",
    "WRK-7": "prompt", "WRK-8": "TextKey.WORKER_FINAL", "WRK-9": "TextKey.REM_WORKERS", "WRK-10": "prompt",
    "WRK-11": "TextKey.REM_ASK_AND_END",
    "JOB-1": "prompt", "JOB-2": "Ending Turn.ending Job.open AgentStatus.WAITING_JOB JobsChecked",
    "JOB-3": "TextKey.WORKER_INTERMEDIATE", "JOB-4": "Trigger.JOB Turn.jobs Job.woke",
    "JOB-5": "WakeWorker.job_ids Job.fallback_sent Settings.wakeup_after JobNoticed Caps.wakes_itself",
    "JOB-6": "JobsChecked ReportJob Job.last_progress", "JOB-7": "MarkLost Job.stopped_by_user TextKey.JOB_LOST",
    "JOB-8": "TextKey.CHECKLIST_COMPLETE",
    "MS-1": "Checklist ChecklistStep", "MS-2": "Checklist.error Milestone.asked_to_fix TextKey.CHECKLIST_UNREADABLE",
    "MS-3": "Checklist.steps", "MS-4": "NotifyReviewReady Milestone.notified", "MS-5": "NotifyReviewReady",
    "MS-6": "Milestone.rubric Milestone.score Reply.decision GateKind.MILESTONE", "MS-7": "prompt",
    "MS-8": "Decision.PAUSE OpenGate TextKey.GATE_ANSWERS", "MS-9": "HoldDecision HoldReason.GATES",
    "MS-10": "ChangeMilestones TextKey.MILESTONE_CHANGE_REQUEST Turn.keys",
    "MS-11": "GateKind.MILESTONE_CHANGE Gate.old Gate.new Discussion.milestone_change",
    "MS-12": "Milestone.id MilestoneStatus.DROPPED SetMilestones", "MS-13": "CloseGate.withdrawn TextKey.NOTE_REVIEW_WITHDRAWN",
    "MS-14": "SetFinished ReopenMilestone TextKey.MILESTONE_START", "MS-15": "Snapshot.milestones",
    "SIL-1": "Message.open Job.woke Snapshot.discussions Snapshot.gates Snapshot.terminals Agent.retry Agent.switch "
             "Agent.stopped_by_user",
    "SIL-2": "AskManagerToCheck Silence ResetSilence GateCause.SILENCE", "SIL-3": "TextKey.SILENCE_CHECK",
    "SIL-4": "RestartSession.frozen Agent.frozen_restart Agent.tools FailAgent GateCause.FROZEN",
    "SIL-5": "CountRound Snapshot.rounds GateCause.ROUND_LIMIT",
    "GATE-1": "Question.options OpenGate", "GATE-2": "HoldReason.QUESTION",
    "GATE-3": "GateAnswer TextKey.GATE_ANSWERS Trigger.GATE_ANSWER", "GATE-4": "Message.gates Gate.sent_in CloseGate",
    "GATE-5": "TextKey.REM_OPEN_GATES", "GATE-6": "Snapshot.paused GateKind.PAUSE", "GATE-7": "Reply.pauses",
    "GATE-8": "Problem.REMOVED", "GATE-9": "OpenGate Snapshot.paused",
    "DISC-1": "DiscussionStart SetDiscussion HoldReason.DRAFT Snapshot.drafting Turn.draft", "DISC-2": "prompt",
    "DISC-3": "DiscussionApply TextKey.DISCUSSION_APPLY DiscussionPhase.APPLYING Message.when_idle",
    "DISC-4": "DiscussionEnd DropReason.DRAFT_ENDED", "DISC-5": "HoldReason.DRAFT ToUser", "DISC-6": "Chat Turn.triggers",
    "TERM-1": "TerminalOpen Adapter.terminal_command", "TERM-2": "TerminalRead Discussion.by_terminal Adapter.transcript",
    "TERM-3": "Terminal", "TERM-4": "HoldReason.TERMINAL Caps.shares_terminal Terminal.shared",
    "TERM-5": "TerminalOpen.stop_job TextKey.ASK_JOB_TERMINAL StopJob", "TERM-6": "TerminalTakeBack Snapshot.waiting_for",
    "MOD-1": "ChooseModel Choice.public Snapshot.catalog", "MOD-2": "ModelInfo.efforts Adapter.catalog",
    "MOD-3": "SaveChoice Choice.name", "MOD-4": "ConfirmMigration", "MOD-5": "ModelReported Agent.actual_model",
    "MOD-6": "Switch SwitchStep.PENDING Snapshot.settled ForceSwitch TextKey.ASK_FORCE_SWITCH Switch.unfinished",
    "MOD-7": "SmokeTest SmokeResult SwitchStep.TESTING",
    "SES-1": "Choice.session_key SessionRef", "SES-2": "SessionChange.DIFFERENT SessionChange.LOST ErrorClass.SESSION_LOST",
    "SES-3": "Switch.tried_new", "SES-4": "Agent.sessions TextKey.SESSION_RETURN",
    "SES-5": "Switch.handover_by TextKey.HANDOVER_REQUEST_CRITIC TextKey.MANAGER_TAKEOVER GateCause.HANDOVER",
    "SES-6": "Switch.handover_message DropReason.REPLACED Message.session_id Resend",
    "REC-1": "Retry RetryState.turn Settings.retry_waits MarkFailed ProcessExited.reported", "REC-2": "ErrorClass.BILLING GateCause.MISSING_CLI",
    "REC-3": "TextKey.WORKER_FAILED GateCause.AGENT_FAILED", "REC-4": "Snapshot.running_jobs Retry",
    "REC-5": "SwitchReason.RETURN Settings.return_after", "REC-6": "SmokeTest Agent.last_smoke GateCause.ALL_FAILED CloseGate",
    "REC-7": "ProcRef SessionRef AdvanceSwitch DropReason.MERGED", "REC-8": "SessionRef.choice RestartSession Switch.old",
    "REC-9": "StopAgents StopOrch Adapter.detach", "REC-10": "Watch SetWatch Health TextKey.NOBODY_WATCHES",
    "UI-1": "AgentStatus public_state", "UI-2": "Message.seq Snapshot.checklist", "UI-3": "UserCommand Runtime",
    "UI-4": "AuditNotes", "UI-5": "prompt", "UI-6": "prompt",
    "PRM-1": "TextKey", "PRM-2": "prompt", "PRM-3": "Rendered", "PRM-4": "Rendered SessionRef.given Agent.open_report",
    "PRM-5": "GoldenBoard SetGolden SessionRef.given SessionChange.COMPACTED", "PRM-6": "prompt", "PRM-7": "prompt",
    "PRM-8": "TextKey.MONITOR_CHECK", "PRM-9": "TextKey.CHECKLIST_COMPLETE SessionRef.given",
    "AUD-1": "prompt", "AUD-2": "Message Job Gate", "AUD-3": "AuditNotes", "AUD-4": "RunInfo.version",
}


def subclasses(c):
    return [x for s in c.__subclasses__() for x in [s] + subclasses(s)]


class Echo:
    """A module for the helper tests: passes a critic's messages on, holding them when the reply has a question."""
    name = "routing"

    def decide(self, snap, ev, prior=()):
        if not isinstance(ev, C.TurnEnded) or ev.agent != "critic":
            return []
        out = []
        for n, m in enumerate(snap.turns["critic"].reply.messages):
            msg = C.Message(C.new_id(self.name, ev, n), "critic", m.to, m.body, trigger=C.Trigger.CRITIC)
            out.append(C.Send(msg))
            if snap.turns["critic"].reply.questions:
                out.append(C.Hold(msg.id, C.HoldReason.QUESTION))
        return out


class TestContracts(unittest.TestCase):
    def test_every_requirement_is_expressible(self):
        ids = set(re.findall(r"\*\*([A-Z]+-\d+)", (ROOT / "docs/redesign.md").read_text()))
        self.assertEqual(len(ids), 118)
        self.assertEqual(ids, set(COVERAGE))
        for rid, names in COVERAGE.items():
            for name in names.split():
                first, _, rest = name.partition(".")
                obj = C if name == "prompt" else getattr(C, first)  # raises: not in the contracts
                if rest:
                    known = getattr(obj, "__dataclass_fields__", {})
                    self.assertTrue(rest in known or hasattr(obj, rest), f"{rid}: {name}")

    def test_text_keys_have_values(self):
        self.assertEqual(set(C.TEXT_VALUES), set(C.TextKey))

    def test_owners_are_known(self):
        known = set(C.MODULE_ORDER) | {"engine"}
        for a in subclasses(C.Action):
            self.assertTrue(a.emitters and set(a.emitters) <= known, a)
        for c in subclasses(C.UserCommand):
            self.assertIn(c.handler, known)
            if c.handler != "engine":
                self.assertIn(c.handler, C.CommandReply.emitters)
        self.assertEqual(set(C.HOLD_OWNERS), set(C.HoldReason))
        for reason, (who, lifts) in C.HOLD_OWNERS.items():
            kinds = (C.Hold, C.Release) if reason in ("question", "gates", "draft") else (C.HoldAgent, C.ReleaseAgent)
            self.assertIn(who, kinds[0].emitters)
            self.assertIn(lifts, kinds[1].emitters)

    def test_stored_form_round_trip(self):
        a = agent("worker-1", status="failed", switch=C.Switch("worker-1", CODEX, step=C.SwitchStep.OPENING),
                  retry=C.RetryState(2, 5.0, C.ErrorClass.BILLING), assignment=C.Assignment("m1", "Do it"))
        s = snapshot(a, message("m1", key=C.TextKey.JOB_LOST, values={"job": "x"}, holds=[C.HoldReason.DRAFT]), job(),
                     gate("g1", "milestone_change", new=[milestone("m2", "pending")]), C.Watch("worker-1"),
                     C.Discussion("critic"), C.Terminal("manager"), C.Checklist(steps=[C.ChecklistStep("s", False, [(True, "i")])]),
                     turn("critic", reply=C.Reply(messages=[C.OutMessage("worker-1", "x")], decision=C.Decision.ACCEPT)),
                     catalog=[C.ModelInfo("x", efforts=["low"])])
        data = json.loads(json.dumps(C.to_dict(s)))
        back = C.from_dict(C.Snapshot, data)
        self.assertEqual(back, s)
        self.assertIs(back.agents["worker-1"].status, C.AgentStatus.FAILED)

    def test_stored_form_of_every_event_command_and_action(self):
        for c in [x for x in subclasses(C.Event) + subclasses(C.Action) if x is not C.UserCommand]:
            x = c(id="c1") if issubclass(c, C.UserCommand) else c()
            base = C.Action if issubclass(c, C.Action) else C.Event
            self.assertEqual(C.from_dict(base, json.loads(json.dumps(C.to_dict(x)))), x)
        i = C.Intent("k#0", C.OpenGate(gate("g1")), C.IntentState.DONE)
        self.assertEqual(C.from_dict(C.Intent, C.to_dict(i)), i)

    def test_unknown_and_missing_keys(self):
        m = C.from_dict(C.Message, {"id": "m1", "new": 1, "status": "later", "text": {"key": "job_lost", "x": 2}})
        self.assertEqual((m.id, m.status, m.key, m.attempts), ("m1", "later", C.TextKey.JOB_LOST, 0))
        self.assertEqual(C.from_dict(C.Switch, {}).agent, "")
        with self.assertRaises(ValueError):
            C.from_dict(C.UserCommand, {"type": "Send"})

    def test_commands_are_checked(self):
        good = {"type": "StopAgents", "id": "c1", "agents": ["critic"], "at": 5}
        self.assertEqual(C.from_dict(C.UserCommand, good).agents, ("critic",))
        self.assertEqual(C.from_dict(C.UserCommand, {"type": "WorkerClose", "id": "c", "jobs": None}).jobs, None)
        for bad in ({**good, "id": ""}, {**good, "agents": "critic"}, {**good, "agents": [1]}, {**good, "at": "x"},
                    {**good, "agent": 3}, {"type": "WorkerClose", "id": "c", "jobs": "maybe"}, {"id": "c"}, [], None,
                    {"type": "SetNight", "id": "c", "on": "yes"}, {"type": "GateAnswer", "id": "c", "option": True},
                    {"type": "JobStop", "id": "c"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                C.from_dict(C.UserCommand, bad)

    def test_redact(self):
        for text in ("Invalid key sk-abcdef1234567890", 'api_key="abcdef123456"', "Authorization: Bearer abcdef123456",
                     "bad http://u:abcdef123456@host/v1", "GET https://h/v1?key=abcdef123456 failed", "Api-Key abcdef123456", "token: abcdef123456"):
            self.assertNotIn("abcdef12", C.redact(text))
        for text in ("API Error: 529 overloaded", "session 7be3e7e5-9f82-4e07-aea9-819df103e5a6 not found", "see https://h/v1",
                     "maximum token: 1048576", "session_limit_reached", "key: session_limit_reached", "Invalid API key provided"):
            self.assertEqual(C.redact(text), text)

    def test_session_key_and_secrets(self):
        ch = C.Choice("claude", "", "opus", env={"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_API_KEY": "secret"},
                      options={"key": "secret"})
        self.assertEqual(ch.session_key, "claude|ANTHROPIC_BASE_URL=http://x")  # as Variant.session_key
        self.assertEqual(C.Choice("opencode", "zai").session_key, "opencode")
        back = C.from_dict(C.Choice, C.to_dict(ch))
        self.assertNotIn("secret", json.dumps(C.to_dict(ch)))
        self.assertEqual((back.session_key, back.options), (ch.session_key, {}))
        self.assertTrue(ch.same(back) and not ch.same(dataclasses.replace(ch, effort="low")))
        odd = C.Choice("claude", env={"ANTHROPIC_BASE_URL": "http://me:secret@x/v1?t=secret"})
        self.assertEqual(odd.session_key, "claude|ANTHROPIC_BASE_URL=http://x/v1")
        self.assertNotIn("secret", json.dumps(C.to_dict(odd)))
        s = snapshot(agent("worker-1", choice=ch, switch=C.Switch("worker-1", ch), holds=[]), message("m1", rendered="long"))
        shown = json.dumps(C.public_state(s))
        for word in ("secret", "http://x", "long", "session_key"):
            self.assertNotIn(word, shown)
        self.assertEqual(C.public_state(s)["agents"]["worker-1"]["waiting"], 0)

    def test_role_of_old_records(self):
        self.assertIs(C.Role("verifier"), C.Role.CRITIC)
        self.assertEqual(f"{C.Role.CRITIC}", "critic")

    def test_ids_depend_only_on_module_event_and_number(self):
        ev = C.TurnEnded("critic", 5.0, "s", "9")
        self.assertRegex(C.new_id("routing", ev), "^x[0-9a-f]{12}$")
        self.assertEqual(C.new_id("routing", ev), C.new_id("routing", C.from_dict(C.Event, C.to_dict(ev))))
        self.assertEqual(len({C.new_id("routing", ev), C.new_id("gates", ev), C.new_id("routing", ev, 1),
                              C.new_id("routing", dataclasses.replace(ev, pos="10"))}), 4)
        self.assertEqual(C.Chat("manager", 1.0, "c7").key, C.Chat("manager", 2.0, "c7").key)

    def test_drafting(self):
        s = snapshot(C.Discussion("critic", phase=C.DiscussionPhase.APPLYING, apply_message="m2"), turn("critic"))
        self.assertTrue(s.drafting("critic"))
        s.turns["critic"].started_by = ["m2"]
        self.assertFalse(s.drafting("critic"))
        self.assertFalse(s.drafting("manager"))
        s.turns["manager"] = turn("manager", draft=True)  # its discussion ended while it wrote
        self.assertTrue(s.drafting("manager"))

    def test_status_and_settled(self):
        s = snapshot(turn("worker-1"))
        self.assertEqual((s.status_of("worker-1"), s.settled("worker-1")), ("writing", False))
        s.turns["worker-1"].status = C.TurnStatus.OK
        self.assertEqual((s.status_of("worker-1"), s.settled("worker-1")), ("idle", True))
        s.jobs["worker-1/j1"] = j = job()
        self.assertEqual((s.status_of("worker-1"), s.settled("worker-1")), ("waiting_job", False))
        j.state = C.JobState.ENDED
        self.assertEqual((s.status_of("worker-1"), s.settled("worker-1")), ("idle", False))
        for kw in ({"woke": True}, {"stopped_by_user": True}):
            self.assertTrue(snapshot(job(state="ended", **kw)).settled("worker-1"))
        s = snapshot(message("m1", "worker-1"), agent("critic", failed_why="x"), agent("worker-2", lifecycle="closed"))
        self.assertFalse(s.settled("worker-1"))
        self.assertEqual((s.status_of("critic"), s.status_of("worker-2")), ("failed", "closed"))

    def test_sendable(self):
        self.assertTrue(message("m", status="waiting").sendable)
        for kw in ({"sent_at": 5.0}, {"holds": [C.HoldReason.DRAFT]}, {"drop": C.DropReason.SILENT}, {"status": "delivered"}):
            self.assertFalse(message("m", **{"status": "waiting", **kw}).sendable)

    def test_message_open(self):
        self.assertTrue(message("m").open)
        for kw in ({"status": "answered"}, {"needs_answer": False}, {"drop": "silent", "status": "failed"}):
            self.assertFalse(message("m", **kw).open)


class TestMock(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cli, self.ad = mock_agents(self.clock, "worker-1")["worker-1"]
        self.names(0)

    def names(self, seconds=10):
        self.clock.advance(seconds)
        self.last = list(self.ad.events())
        return [type(e).__name__ for e in self.last]

    def test_turn_and_order(self):
        self.cli.script("Hello.", CliReply(error=C.ErrorClass.TRANSIENT, detail="API Error"))
        self.ad.send("m1", "Hi")
        self.ad.send("m2", "Also")  # joins the running turn
        self.assertEqual(self.names(), ["Received", "TurnStarted", "Text", "Received", "Received", "TurnEnded"])
        self.assertEqual([e.taken for e in self.last if isinstance(e, C.Received)], [False, False, True])
        self.assertEqual(self.last[-1].text, "Hello.")
        self.assertEqual([e.pos for e in self.last], ["2", "3", "4", "5", "6", "7"])
        self.ad.send("m3", "Next")
        self.names()
        self.assertEqual((self.last[-1].error, self.last[-1].detail), (C.ErrorClass.TRANSIENT, "API Error"))
        self.assertTrue(self.ad.arrived("m2"))
        self.assertFalse(self.ad.arrived("m9"))

    def test_message_in_the_queue(self):
        self.cli.script(CliReply("One.", takes=False), "Two.", CliReply("Three.", takes=False))
        self.ad.send("m1", "Hi")
        self.ad.send("m2", "Also")
        self.assertEqual(self.names(), ["Received", "TurnStarted", "Text", "Received", "TurnEnded",
                                        "TurnStarted", "Text", "TurnEnded"])
        self.assertEqual([e.message_ids for e in self.last if isinstance(e, C.TurnStarted)], [("m1",), ("m2",)])
        self.ad.send("m3", "x")
        self.ad.send("m4", "lost with the CLI")
        self.names(0)
        self.cli.exit()
        self.names(0)
        self.assertEqual((self.ad.arrived("m3"), self.ad.arrived("m4")), (True, False))

    def test_job_ends_inside_a_turn_and_empty_result(self):
        self.cli.script(CliReply("Both.", after=20, jobs=["a"]))
        self.ad.send("m1", "Go")
        self.cli.job_end("a", 0, after=5, noticed=True)
        self.assertEqual(self.names(30), ["Received", "TurnStarted", "JobStarted", "Text", "JobEnded", "JobNoticed", "TurnEnded"])
        self.cli.empty_result()
        self.assertEqual((self.names(), self.last[0].text), (["Text"], ""))

    def test_job_wakes_or_not(self):
        self.cli.script(CliReply("Started.", jobs=["a", "b"]))
        self.ad.send("m1", "Go")
        self.names()
        self.assertEqual(len(self.ad.jobs()), 2)
        self.cli.job_end("a", 0, after=5, wake="Done.")
        self.assertEqual(self.names(), ["JobEnded", "TurnStarted", "Text", "TurnEnded"])
        self.cli.job_end("b", 3, after=5)
        self.assertEqual(self.names(), ["JobEnded"])
        self.cli.job_start("c")
        self.names()
        self.cli.job_end("c", report=False)
        self.assertEqual(self.names(), [])
        self.assertEqual({j.id: j.state for j in self.ad.jobs()}["worker-1/c"], C.JobState.ENDED)
        self.cli.job_end("c", lost=True, forget=True)
        self.names()
        self.assertTrue(self.last[0].lost)
        self.assertEqual(len(self.ad.jobs()), 2)

    def test_replay_after_reconnect_and_duplicates(self):
        proc = self.ad.proc
        self.ad.send("m1", "Go")
        self.names()
        keys = [e.key for e in self.last]
        self.cli.duplicate(2)
        self.names()
        self.assertEqual([e.key for e in self.last], keys[-2:])
        self.ad.detach()
        self.assertEqual(self.names(), [])
        with self.assertRaises(AdapterError):
            self.ad.send("m2", "x")
        proc.read_pos = "2"  # saved up to the message's receipt
        again = MockAdapter(self.clock, self.cli)
        self.assertTrue(again.reconnect(proc))
        self.assertEqual([e.key for e in again.events()], keys[1:])

    def test_exit_and_second_cli(self):
        proc = self.ad.proc
        with self.assertRaises(AdapterError):  # REC-8
            MockAdapter(self.clock, self.cli).start(C.AgentSpec("worker-1"), None)
        self.cli.script(CliReply(exit=9))
        self.ad.send("m1", "Go")
        self.assertEqual(self.names()[-2:], ["TurnEnded", "ProcessExited"])
        self.assertEqual((self.last[-2].error, self.last[-1].exit_code, self.last[-1].reported), ("transient", 9, True))
        late = MockAdapter(self.clock, self.cli)
        self.assertFalse(late.reconnect(proc))  # gone, but what it wrote is still read
        self.assertEqual([e.key for e in late.events()][-4:], [e.key for e in self.last][-4:])
        self.ad.start(C.AgentSpec("worker-1"), C.SessionRef("worker-1-s1"))
        self.assertEqual(self.names(), ["SessionChanged"])
        self.assertEqual((self.last[0].change, self.last[0].session_id), ("resumed", "worker-1-s1"))
        self.ad.stop()
        self.names()
        self.assertTrue(self.last[-1].asked)

    def test_stop_and_start_drop_nothing(self):
        self.ad.send("m1", "Go")
        self.clock.advance(10)
        self.ad.send("m2", "Again")
        self.ad.stop()
        self.ad.start(C.AgentSpec("worker-1"), C.SessionRef("worker-1-s1"))
        turn_ = ["Received", "TurnStarted", "Text", "TurnEnded"]
        self.assertEqual(self.names(), turn_ * 2 + ["ProcessExited", "SessionChanged"])
        self.assertEqual((self.last[7].status, self.last[8].asked, self.last[8].reported), ("stopped", True, False))
        self.assertEqual(len({e.key for e in self.last}), len(self.last))

    def test_left_over_cli(self):
        self.ad.detach()  # Orch died before it saved the ProcRef
        new = MockAdapter(self.clock, self.cli)
        new.start(C.AgentSpec("worker-1"), None)
        self.assertEqual([(type(e).__name__, e.uid) for e in new.events()], [("SessionChanged", "start:2")])

    def test_session_loss(self):
        for script, change in ((self.cli.lose_session, "lost"), (self.cli.other_session, "different")):
            self.ad.stop()
            script()
            self.ad.start(C.AgentSpec("worker-1"), C.SessionRef("worker-1-s1"))
            self.names()
            self.assertEqual(self.last[-1].change, change)

    def test_frozen_swallowed_refused_stopped(self):
        self.cli.script(CliReply(receipt=False, starts=False), CliReply(ends=False, tools=["run: x"]))
        self.ad.send("m1", "Lost")
        self.assertEqual((self.names(), self.ad.arrived("m1")), ([], False))
        self.ad.send("m2", "Go")
        self.assertEqual(self.names(5000), ["Received", "TurnStarted", "ToolStart", "Text"])
        self.ad.stop()
        self.ad.start(C.AgentSpec("worker-1"), C.SessionRef("worker-1-s1"))
        self.assertEqual(self.names(0), ["TurnEnded", "ProcessExited", "SessionChanged"])
        self.assertEqual(self.last[0].status, C.TurnStatus.STOPPED)
        self.cli.refuse_sends(1, C.ErrorClass.BILLING)
        with self.assertRaises(AdapterError) as e:
            self.ad.send("m3", "x")
        self.assertEqual(e.exception.error, C.ErrorClass.BILLING)
        self.ad.send("m3", "x")

    def test_one_shot_program(self):
        cli, ad = mock_agents(self.clock, "critic", caps=C.Caps())["critic"]
        ad.send("m1", "a")
        with self.assertRaises(AdapterError):
            ad.send("m2", "b")
        self.assertIsNone(ad.arrived("m1"))
        self.clock.advance(5)
        self.assertEqual([type(e).__name__ for e in ad.events()], ["SessionChanged", "TurnStarted", "Text", "TurnEnded"])

    def test_any_event(self):
        self.cli.emit(C.ModelReported, after=3, model="x")
        self.ad.compact()
        self.assertEqual(self.names(), ["SessionChanged", "ModelReported"])

    def test_protocol_and_registry(self):
        methods = [n for n in vars(C.Adapter) if not n.startswith("_") and callable(getattr(C.Adapter, n))]
        self.assertEqual([n for n in methods if not hasattr(BaseAdapter, n)], [])
        self.assertEqual(len(methods), 13)
        self.assertIsInstance(adapters.make("mock", self.clock), MockAdapter)
        with self.assertRaises(ValueError):
            adapters.make("none", self.clock)

    def test_classify_error(self):
        for text, want in (("Your credit balance is too low", "billing"), ("executable not found: claude", "missing_cli"),
                           ("API Error: 529 overloaded", "transient"), ("odd", "other")):
            self.assertEqual(classify_error(text), want)


class TestHelpers(unittest.TestCase):
    def test_example_hold_stops_a_send(self):
        reply = C.Reply(untagged="Hm.", messages=[C.OutMessage("worker-1", "Go on.")], questions=[C.Question("Which?")])
        snap, ev = turn_ended(snapshot(), "critic", reply=reply)
        actions = decide(Echo(), snap, ev)
        assert_actions(self, actions, [(C.Send, {"recipient": "worker-1", "body": "Go on."}),
                                       (C.Hold, {"reason": C.HoldReason.QUESTION})])
        self.assertEqual(sent_to(actions, "worker-1"), [])
        with self.assertRaises(AssertionError):
            assert_actions(self, actions, [C.Send])
        assert_none(self, actions, C.Drop)

    def test_agent_hold_and_bypass(self):
        held = snapshot(agent("worker-1", holds=[C.HoldReason.SWITCH]))
        m = message("m1", "worker-1", status="waiting")
        self.assertEqual(sent_to([C.Send(m)], "worker-1", held), [])
        self.assertEqual(sent_to([C.Send(m, (C.HoldReason.SWITCH,))], "worker-1", held), [m])

    def test_turn_ended_facts(self):
        s0 = snapshot(job("worker-1/j1"), message("m1", "worker-1", "critic"), workers=2)
        s, ev = turn_ended(s0, "worker-1", "Started.", started_by=["m1"])
        self.assertEqual((s.turns["worker-1"].ending, s.agents["worker-1"].status), ("intermediate", "waiting_job"))
        self.assertEqual((s.turns["worker-1"].triggers, s.messages["m1"].status), (["critic"], "answered"))
        self.assertEqual(s0.messages["m1"].status, "delivered")
        s, ev = turn_ended(snapshot(job("worker-1/j1", state="ended"), message("m1", "worker-1", "user"),
                                    message("m2", "worker-1", "critic"), milestones=[]),
                           "worker-1", "Ok.", started_by=["m1"], joined=["m2"], jobs=["worker-1/j1"])
        t = s.turns["worker-1"]
        self.assertEqual((t.triggers, t.ending, s.milestones), (["user_chat", "critic", "job"], "final", []))
        s, ev = turn_ended(snapshot(), "worker-1", error="billing", detail="no credit")
        self.assertIsNone(s.turns["worker-1"].reply)
        self.assertEqual(ev.error, C.ErrorClass.BILLING)

    def test_decide_checks(self):
        class Bad:
            name = "gates"

            def __init__(self, *out):
                self.out = out

            def decide(self, snap, ev, prior=()):
                return list(self.out)

        s = snapshot()
        for out in (C.CountRound(), C.Send(C.Message("", recipient="critic")),
                    C.Send(C.Message("m1", recipient="critic", text=C.TextRef(C.TextKey.JOB_LOST))),
                    C.Send(C.Message("m1", recipient="critic", text=C.TextRef(C.TextKey.JOB_LOST, {"jobs": [], "x": 1}))),
                    C.Tell(C.Message("m1", recipient="critic")), C.Send(C.Message("m1")),
                    C.ToUser(C.Message("m1", recipient="critic", needs_answer=False)),
                    C.ToUser(C.Message("m1", recipient=C.USER))):
            with self.assertRaises(AssertionError):
                decide(Bad(out), s, tick(s))
        self.assertEqual(len(decide(Bad(C.CloseGate("g1")), s, tick(s))), 1)

    def test_event_builder(self):
        s = snapshot()
        self.assertEqual(event(C.WorkerClose, s, "worker-1", jobs="stop").jobs, C.JobsChoice.STOP)
        self.assertEqual(event(C.Received, s, "critic", message_id="m1").stream, "critic-1")

    def test_package_data(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('"orch.runtime" = ["prompts/*"', text)
        for d in ("prompts", "page"):
            self.assertTrue((ROOT / "orch/runtime" / d).is_dir())


if __name__ == "__main__":
    unittest.main()
