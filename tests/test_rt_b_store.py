"""orch/runtime/store.py (package B): facts, actions, files. Crashes are in test_rt_b_crash.py."""
import json
import shutil
import tempfile
from pathlib import Path

from rt_helpers import *
from orch import audit, report, timeline
from orch.runtime import store as S
from orch.runtime.store import Store, StoreError

WORK = MessageStatus


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rtb"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "proj" / "20260101-000000"
        self.clock = FakeClock(T0)
        self.n = 0
        self.store = None
        self.addCleanup(lambda: self.store and self.store._release())

    def make(self, *records, **kw):
        snap = snapshot(*records, **kw)
        snap.run = RunInfo(id=self.dir.name, project="proj", goal="Goal", workspace=str(self.tmp),
                           run_dir=str(self.dir), started=T0)
        self.store = Store.create(self.dir, self.clock, snap, parse=self.parse, read_checklist=self.checklist)
        return self.store

    def parse(self, text, role):
        return Reply(text=text, untagged=f"{role}:{text}")

    def checklist(self, path, text):
        return Checklist(path=path, digest=str(text), error="" if text is not None else "missing")

    def reopen(self):
        """Orch dies and starts again."""
        self.store._release()
        self.store = Store(self.dir, self.clock, parse=self.parse, read_checklist=self.checklist)
        return self.store

    def ev(self, kind, agent="worker-1", **kw):
        self.n += 1
        if issubclass(kind, AgentEvent):
            kw = {"stream": f"{agent}-1", "pos": str(self.n), **kw}
        elif issubclass(kind, UserCommand):
            kw.setdefault("id", f"c{self.n}")
        else:
            kw.setdefault("uid", str(self.n))
        return kind(agent, self.clock.now(), **kw)

    def fact(self, kind, agent="worker-1", **kw):
        return self.do(self.ev(kind, agent, **kw))

    def do(self, event, *actions):
        self.assertIsNotNone(self.store.begin(event))
        return self.store.apply(event, actions)

    def act(self, *actions):
        return self.do(self.ev(Reconnected, ""), *actions)

    def send(self, id="m1", to="worker-1", sender="critic", body="Do it.", **kw):
        self.act(Send(Message(id, sender, to, body, **kw)))
        return self.m(id)

    def deliver(self, id):
        m = self.m(id)
        self.assertTrue(self.store.begin_send(id, f"<{id}>", {"g": id}))
        self.fact(Received, m.recipient, message_id=id)

    @property
    def s(self):
        return self.store.snapshot()

    def m(self, id="m1"):
        return self.s.messages[id]

    def a(self, id="worker-1"):
        return self.s.agents[id]

    def t(self, id="worker-1"):
        return self.s.turns[id]


class TestMessages(Base):
    def test_MSG_3_record_and_statuses(self):
        self.make()
        m = self.send(trigger=Trigger.CRITIC)
        self.assertEqual((m.status, m.sender, m.recipient, m.body, m.created, m.seq),
                         (WORK.WAITING, "critic", "worker-1", "Do it.", T0, 1))
        self.clock.advance(5)
        self.assertTrue(self.store.begin_send("m1", "Do it. <r>", {"golden": "ab"}))
        self.assertEqual((self.m().status, self.m().sent_at, self.m().attempts, self.m().rendered),
                         (WORK.WAITING, T0 + 5, 1, "Do it. <r>"))
        self.fact(Received, message_id="m1")
        self.assertEqual((self.m().status, self.m().delivered_at, self.m().session_id),
                         (WORK.DELIVERED, T0 + 5, "s-worker-1"))
        self.assertEqual(self.a().session.given, {})
        self.fact(TurnStarted, message_ids=("m1",))
        m = self.m()
        self.assertEqual((m.status, m.taken_at, m.answered_at, m.reply_turn),
                         (WORK.ANSWERED, T0 + 5, T0 + 5, "r001-t01-worker-1"))
        self.assertEqual(self.a().session.given, {"golden": "ab"})

    def test_MSG_3_replayed_event_changes_nothing(self):
        self.make()
        self.send()
        self.store.begin_send("m1", "x")
        ev = self.ev(TurnStarted, message_ids=("m1",))
        self.do(ev)
        before = self.s
        self.assertIsNone(self.store.begin(ev))
        self.assertEqual(self.store.apply(ev, [SetFinished()]), [])
        self.assertEqual(self.s, before)
        self.reopen()
        self.assertIsNone(self.store.begin(ev))

    def test_MSG_4_to_user_and_notices_end_at_delivered(self):
        self.make()
        self.assertEqual(self.act(ToUser(message("u1", USER, "manager", status="waiting"))), [])
        self.assertEqual(self.m("u1").status, WORK.DELIVERED)
        self.assertFalse(self.m("u1").needs_answer or self.m("u1").open)
        self.act(Tell(Message("n1", ORCH, "worker-1", "Note.")))
        self.assertFalse(self.m("n1").needs_answer)
        self.deliver("n1")
        self.fact(TurnStarted)
        self.assertEqual((self.m("n1").status, self.t().started_by, self.m("n1").reply_turn),
                         (WORK.DELIVERED, ["n1"], self.t().id))

    def test_held_message_to_user_waits_for_its_release_and_a_drop_wins(self):
        self.make()
        a = ToUser(Message("u1", "critic", USER, "Draft"))
        self.act(Hold("u1", HoldReason.DRAFT), a, ToUser(Message("u2", "critic", USER, "x")),
                 Drop("u2", DropReason.SILENT))
        self.assertEqual((self.m("u1").status, self.m("u2").status, self.m("u2").drop),
                         (WORK.WAITING, WORK.FAILED, DropReason.SILENT))
        self.act(Release("u1", HoldReason.DRAFT))
        self.assertEqual(self.m("u1").status, WORK.DELIVERED)

    def test_MSG_6_known_id_is_not_created_again_and_must_be_the_same(self):
        self.make()
        self.send()
        self.act(Send(self.m(), bypass=(HoldReason.SWITCH,)))
        self.assertEqual((self.m().bypass, self.m().seq, len(self.s.messages)), ([HoldReason.SWITCH], 1, 1))
        ev = self.ev(Reconnected, "")
        self.store.begin(ev)
        with self.assertRaises(StoreError):
            self.store.apply(ev, [SetFinished(), Send(Message("m1", "critic", "worker-1", "Other."))])
        self.assertFalse(self.s.finished)  # nothing of it was applied
        self.store.apply(ev, [])

    def test_MSG_6_a_message_goes_once(self):
        self.make()
        self.send()
        self.assertEqual([m.id for m in self.store.to_send()], ["m1"])
        self.assertTrue(self.store.begin_send("m1", "x"))
        self.assertFalse(self.store.begin_send("m1", "x"))
        self.assertEqual(self.store.to_send(), [])
        self.fact(SendFailed, message_id="m1", detail="HTTP 500 api_key=abcdef123456")
        self.assertEqual((self.m().sent_at, self.m().error), (0, "HTTP 500 api_key=***"))
        self.assertTrue(self.store.begin_send("m1", "x"))
        self.assertEqual((self.m().attempts, self.m().error), (2, ""))
        self.store.not_arrived("m1")
        self.assertTrue(self.store.begin_send("m1", "x"))
        self.fact(Received, message_id="m1")
        self.fact(SendFailed, message_id="m1")  # it did arrive
        self.assertEqual(self.m().status, WORK.DELIVERED)
        self.fact(TurnStarted)
        self.store.not_arrived("m1")
        self.assertEqual(self.m().status, WORK.ANSWERED)

    def test_holds_order_and_when_idle(self):
        self.make()
        self.send("m1")
        self.send("m2", "critic", "worker-1")
        self.act(HoldAgent("worker-1", HoldReason.SWITCH), Hold("m2", HoldReason.QUESTION))
        self.assertEqual(self.store.to_send(), [])
        self.act(Send(self.m("m1"), bypass=(HoldReason.SWITCH,)), Release("m2", HoldReason.QUESTION))
        self.assertEqual([m.id for m in self.store.to_send()], ["m1", "m2"])
        self.act(ReleaseAgent("worker-1", HoldReason.SWITCH), Drop("m1", DropReason.REPLACED))
        self.assertFalse(self.store.begin_send("m1", "x"))
        self.send("m3", "critic", ORCH, when_idle=True)
        self.deliver("m2")
        self.assertNotIn("m3", [m.id for m in self.store.to_send()])  # m2 is unanswered
        self.fact(TurnStarted, "critic")
        self.assertNotIn("m3", [m.id for m in self.store.to_send()])
        self.fact(TurnEnded, "critic")
        self.assertEqual([m.id for m in self.store.to_send()], ["m3"])

    def test_taken_into_the_running_turn_is_answered_with_the_next_output(self):
        self.make()
        self.fact(TurnStarted)
        self.send("m1", sender="user", trigger=Trigger.USER_CHAT)
        self.store.begin_send("m1", "x")
        self.fact(Received, message_id="m1", taken=True)
        self.assertEqual((self.t().joined, self.t().triggers, self.m().status, self.m().reply_turn),
                         (["m1"], [Trigger.JOB, Trigger.USER_CHAT], WORK.DELIVERED, self.t().id))
        self.fact(Text, text="")  # the CLI's own retry
        self.fact(ToolEnd, tool_id="t")
        self.assertEqual(self.m().status, WORK.DELIVERED)
        self.fact(Text, text="Right.")
        self.assertEqual(self.m().status, WORK.ANSWERED)
        self.reopen()
        self.assertEqual(self.m().status, WORK.ANSWERED)

    def test_taken_before_a_turn_starts_begins_it(self):
        self.make()
        self.send("m1")
        self.store.begin_send("m1", "x")
        self.fact(Received, message_id="m1", taken=True)
        self.send("m2")
        self.deliver("m2")
        self.fact(TurnStarted, message_ids=("m2",))
        self.assertEqual(self.t().started_by, ["m2", "m1"])

    def test_doubt_after_exit_discard_and_a_new_process(self):
        self.make()
        for i in ("m1", "m2", "m3"):
            self.send(i)
        self.store.begin_send("m1", "x")
        self.deliver("m2")
        self.deliver("m3")
        self.fact(Received, message_id="m3", taken=True)
        self.fact(ProcessExited, stream="worker-1-0")  # an older process: nothing
        self.assertIsNotNone(self.a().proc)
        self.fact(ProcessExited)
        self.assertIsNone(self.a().proc)
        self.assertEqual([m.id for m in self.store.in_doubt("worker-1")], ["m1", "m2"])
        self.assertTrue(self.m("m2").sent_at and not self.m("m2").delivered_at)
        self.assertEqual(self.m("m3").status, WORK.DELIVERED)
        self.store.set_proc("worker-1", ProcRef(7, 1.0, stream="worker-1-2"))
        self.fact(Received, stream="worker-1-2", message_id="m2")
        self.fact(Discarded, stream="worker-1-2", message_id="m2")
        self.assertEqual(self.m("m2").status, WORK.WAITING)
        self.fact(Received, stream="worker-1-2", message_id="m2", uid="r2")
        self.store.set_proc("worker-1", ProcRef(7, 1.0, stream="worker-1-2"))  # the same one: reconnected
        self.assertEqual(self.m("m2").status, WORK.DELIVERED)
        self.store.set_proc("worker-1", ProcRef(8, 2.0, stream="worker-1-3"))
        self.assertEqual(self.m("m2").status, WORK.WAITING)

    def test_ledger_keeps_every_message(self):
        self.make()
        for i in range(S.KEEP_MESSAGES + 5):
            self.act(ToUser(Message(f"u{i}", "manager", USER, f"Text {i}")))
        self.send("open")
        self.assertEqual(len(self.s.messages), S.KEEP_MESSAGES + 1)
        self.assertNotIn("u0", self.s.messages)
        with open(self.dir / "messages.jsonl", "ab") as f:
            f.write(b'{"id": "torn')
        self.reopen()
        self.assertEqual(self.store.message("u0").body, "Text 0")
        self.act(ToUser(Message("u0", "manager", USER, "Text 0")))  # known: not created again
        self.assertNotIn("u0", self.s.messages)
        self.act(ToUser(Message("new", "manager", USER, "n")))
        self.assertEqual(self.store.message("u5").body, "Text 5")  # not swallowed by the torn line
        got, more = self.store.messages(before=3, limit=1)
        self.assertEqual(([m.id for m in got], more), (["u1"], True))
        self.assertEqual([m.id for m in self.store.messages(q="text 30")[0]], [f"u{i}" for i in [30, 300, 301, 302, 303, 304]])
        got, more = self.store.messages(limit=0)
        self.assertEqual((len(got), more), (S.KEEP_MESSAGES + 7, False))


class TestTurns(Base):
    def test_id_triggers_and_ending(self):
        self.make()
        self.send("m1", trigger=Trigger.CRITIC, text=TextRef(TextKey.JOB_WAKEUP, {"jobs": []}))
        self.deliver("m1")
        self.fact(TurnStarted)
        self.assertEqual((self.t().id, self.t().triggers, self.t().keys, self.a().status),
                         ("r001-t01-worker-1", [Trigger.CRITIC], [TextKey.JOB_WAKEUP], AgentStatus.WRITING))
        self.fact(ToolStart, tool_id="t1")
        self.assertEqual(self.a().tools, ["t1"])
        self.fact(JobStarted, job=job("worker-1/j1"))
        self.clock.advance(9)
        self.fact(TurnEnded, text="Started.")
        t, a = self.t(), self.a()
        self.assertEqual((t.status, t.ending, t.reply.untagged, t.ended), (TurnStatus.OK, Ending.INTERMEDIATE,
                                                                           "worker:Started.", T0 + 9))
        self.assertEqual((a.status, a.tools, a.idle_since, a.writing_since), (AgentStatus.WAITING_JOB, [], T0 + 9, 0))
        self.fact(JobEnded, job_id="worker-1/j1", exit_code=0)
        self.assertEqual(self.a().status, AgentStatus.IDLE)
        self.fact(TurnStarted)  # it woke itself
        self.assertEqual((self.t().id, self.t().jobs, self.t().triggers), ("r001-t02-worker-1", ["worker-1/j1"],
                                                                           [Trigger.JOB]))
        self.assertTrue(self.s.jobs["worker-1/j1"].woke)
        self.fact(TurnEnded, text="Done.")
        self.assertEqual(self.t().ending, Ending.FINAL)

    def test_without_wakes_itself_only_the_message_tells_of_the_job(self):
        self.make(agent("worker-1", caps=replace(ALL_CAPS, wakes_itself=False)), job(state="ended"))
        self.fact(TurnStarted)
        self.assertFalse(self.s.jobs["worker-1/j1"].woke)
        self.fact(TurnEnded)
        self.act(WakeWorker(Message("w1", ORCH, "worker-1", trigger=Trigger.JOB), job_ids=("worker-1/j1",)))
        self.assertTrue(self.s.jobs["worker-1/j1"].fallback_sent)
        self.deliver("w1")
        self.fact(TurnStarted)
        self.assertEqual((self.t().jobs, self.s.jobs["worker-1/j1"].woke), (["worker-1/j1"], True))

    def test_turn_without_a_message(self):
        self.make(Terminal("critic"))
        self.fact(TurnStarted, "critic")
        self.assertEqual(self.t("critic").triggers, [Trigger.USER_CHAT])
        self.fact(TurnStarted, message_ids=("typed by the user",))
        self.assertEqual(self.t().triggers, [Trigger.USER_CHAT])
        self.fact(TurnStarted, "worker-1", message_ids=("another",))  # joins the open one
        self.assertEqual((self.t().id, self.t().joined), ("r001-t02-worker-1", ["another"]))

    def test_end_without_a_start_opens_one(self):
        self.make()
        self.fact(TurnEnded, text="", status=TurnStatus.ERROR, error=ErrorClass.BILLING, detail="sk-abcdefgh12345")
        t = self.t()
        self.assertEqual((t.status, t.error, t.detail, t.reply), (TurnStatus.ERROR, ErrorClass.BILLING, "***", None))

    def test_ok_clears_failure_error_does_not(self):
        self.make(agent("worker-1", failed_why="x", retry=RetryState(2), frozen_restart=5.0))
        self.fact(TurnEnded, status=TurnStatus.STOPPED)
        self.assertTrue(self.a().failed_why and self.a().retry and self.a().frozen_restart)
        self.assertEqual(self.a().status, AgentStatus.FAILED)
        self.fact(TurnEnded)
        self.assertEqual((self.a().failed_why, self.a().retry, self.a().frozen_restart), ("", None, 0))

    def start_failed(self):
        self.send("m1", trigger=Trigger.USER_CHAT, sender=USER)
        self.deliver("m1")
        self.fact(TurnStarted)
        self.send("m2", trigger=Trigger.CRITIC)
        self.store.begin_send("m2", "x")
        self.fact(Received, message_id="m2", taken=True)
        old = self.t()
        ev = self.ev(TurnEnded, status=TurnStatus.ERROR, error=ErrorClass.TRANSIENT)
        self.do(ev, Retry("worker-1", RetryState(1, turn=old.id)))
        return old

    def test_REC_7_the_continuing_turn_gets_what_the_unfinished_one_had(self):
        self.make()
        old = self.start_failed()
        self.assertEqual(self.m("m2").status, WORK.DELIVERED)
        self.act(Send(Message("r1", ORCH, "worker-1", text=TextRef(TextKey.RETRY_CONTINUE), continues=old.id)))
        self.deliver("r1")
        self.reopen()
        self.fact(TurnStarted, message_ids=("r1",))
        t = self.t()
        self.assertEqual((t.started_by, t.joined, t.triggers, t.keys),
                         (["r1", "m1"], ["m2"], [Trigger.USER_CHAT, Trigger.CRITIC], []))
        self.assertEqual(self.m("m2").status, WORK.ANSWERED)

    def test_REC_7_any_turn_started_while_retrying_continues_it(self):
        self.make()
        old = self.start_failed()
        self.fact(TurnStarted)  # its job woke it
        self.assertEqual((self.t().started_by, self.t().joined), (["m1"], ["m2"]))
        self.assertIsNotNone(self.a().retry)
        self.assertNotEqual(self.t().id, old.id)

    def test_REC_7_a_turn_still_needed_is_kept(self):
        self.make()
        old = self.start_failed()
        self.act(Retry("worker-1", None),
                 Send(Message("r1", ORCH, "worker-1", text=TextRef(TextKey.RESTART_CONTINUE), continues=old.id)))
        self.fact(TurnStarted)  # another turn comes first
        self.fact(TurnEnded)
        self.assertNotEqual(self.t().id, old.id)
        self.reopen()
        self.assertEqual(list(self.store.state.kept), [old.id])
        self.fact(TurnStarted)
        self.deliver("r1")
        self.fact(Received, message_id="r1", taken=True, uid="t")
        self.assertEqual((self.t().started_by, self.t().joined, Trigger.USER_CHAT in self.t().triggers),
                         (["m1"], ["r1", "m2"], True))
        self.fact(TurnEnded)
        self.assertEqual(self.store.state.kept, {})

    def test_draft_turns(self):
        self.make(Discussion("critic"))
        self.fact(TurnStarted, "critic")
        self.assertTrue(self.t("critic").draft)
        self.act(SetDiscussion("critic", None))
        self.assertTrue(self.t("critic").draft)  # stays so
        self.fact(TurnEnded, "critic")
        self.act(SetDiscussion("critic", Discussion("critic", phase=DiscussionPhase.APPLYING, apply_message="ap")),
                 Send(Message("ap", ORCH, "critic", text=TextRef(TextKey.DISCUSSION_APPLY), when_idle=True)))
        self.deliver("ap")
        self.fact(TurnStarted, "critic")
        self.assertFalse(self.t("critic").draft or self.s.drafting("critic"))
        self.fact(TurnStarted, "manager")
        self.act(SetDiscussion("manager", Discussion("manager")))
        self.assertTrue(self.t("manager").draft)  # also what it was writing already

    def test_an_apply_turn_that_goes_on_is_no_draft(self):
        self.make(Discussion("critic", phase=DiscussionPhase.APPLYING, apply_message="ap"))
        self.act(Send(Message("ap", ORCH, "critic", text=TextRef(TextKey.DISCUSSION_APPLY))))
        self.deliver("ap")
        self.fact(TurnStarted, "critic")
        self.do(self.ev(TurnEnded, "critic", status=TurnStatus.ERROR), Retry("critic", RetryState(1, turn=self.t("critic").id)))
        self.fact(TurnStarted, "critic")
        self.assertEqual((self.t("critic").started_by, self.t("critic").draft), (["ap"], False))

    def test_progress_is_not_saved_one_by_one(self):
        self.make()
        self.fact(TurnStarted)
        written = (self.dir / "state.json").read_text()
        self.clock.advance(3)
        for _ in range(3):
            ev = self.ev(Text, text="abc")
            self.store.begin(ev)
            self.store.apply(ev, [])
        self.assertEqual((self.dir / "state.json").read_text(), written)
        self.assertEqual((self.a().last_output, self.a().proc.read_pos), (T0 + 3, str(self.n)))
        self.assertIsNone(self.store.pending)
        self.do(Tick("", T0 + 3))
        self.assertEqual(self.reopen() and self.a().proc.read_pos, str(self.n))


class TestTurnFiles(Base):
    def run_turns(self):
        self.make(milestone("m1", "done"), milestone("m2"))
        self.store.system_prompts["critic"] = "You are the critic."
        self.send("a1", "critic", "manager", "Build the thing so that it passes all of the tests.", trigger=Trigger.MANAGER)
        self.deliver("a1")
        self.fact(TurnStarted, "critic", message_ids=("a1",))
        self.clock.advance(20)
        self.fact(TurnEnded, "critic", text="All done. A reply long enough to be counted as one.", session_id="s-critic",
                  meta={"total_cost_usd": 0.5, "models": ["opus"]})
        self.act(CountRound("x"))
        self.fact(TurnStarted, "critic")
        self.fact(TurnEnded, "critic", text="More.", meta={"total_cost_usd": 0.75})
        self.fact(TurnStarted)

    def test_readable_by_report_timeline_and_audit(self):
        self.run_turns()
        p = timeline.turn_path(self.dir, "r001-t01-critic")
        d = json.loads(p.read_text())
        self.assertEqual((d["role"], d["purpose"], d["status"], d["input"], d["duration_s"], d["milestone"],
                          d["session_id"], d["resumed_session"], d["system_prompt"]),
                         ("verifier", "verify", "ok", "<a1>", 20.0, 1, "s-critic", False, "You are the critic."))
        self.assertEqual(p.with_name("t01-critic.output.md").read_text(), d["output"])
        self.assertEqual(p.with_name("t01-critic.input.md").read_text(), "<a1>")
        turns = audit.load_turns(self.dir)
        self.assertEqual([t["turn_id"] for t in turns], ["r001-t01-critic", "r002-t01-critic"])
        self.assertEqual([(t["meta"]["turn_cost_usd"], t["session_turn"]) for t in turns], [(0.5, 1), (0.25, 2)])
        digest, stats = audit.build(self.dir, 1, 2)
        self.assertEqual(stats["turns"], 2)
        self.assertIn("r002-t01-critic", digest)
        page = report.render_run_report(self.dir)
        self.assertIn("All done.", page)
        self.assertIn("$0.75", page)
        self.assertIn("r002-t02-worker-1", page)  # the one still running
        self.assertIn(self.dir.name, report.render_index(self.tmp))

    def test_continues_the_numbers_of_an_older_record(self):
        (self.dir / "rounds" / "r004").mkdir(parents=True)
        (self.dir / "state.json").write_text(json.dumps({"turn_counts": {"total": 9, "ok": 9}, "cost_usd": 2.0}))
        self.run_turns()
        self.assertEqual(self.t("critic").id, "r006-t01-critic")
        st = json.loads((self.dir / "state.json").read_text())
        self.assertEqual((st["turn_counts"]["ok"], st["cost_usd"], st["round"]), (11, 2.75, 6))


class TestSessions(Base):
    def test_REC_8_sessions_per_key_and_never_confirmed_by_opening(self):
        self.make(agent("worker-1", sessions={}, session_key="", proc=None))
        self.fact(SessionChanged, session_id="", change=SessionChange.NEW, uid="start:1")
        self.fact(SessionChanged, session_id="S1", change=SessionChange.NEW, uid="session:S1")
        ref = self.a().session
        self.assertEqual((ref.session_id, ref.session_key, ref.cwd, ref.confirmed), ("S1", "claude", "/work", False))
        self.fact(SessionChanged, session_id="S9", change=SessionChange.DIFFERENT)
        self.fact(SessionChanged, session_id="S1", change=SessionChange.LOST)
        self.assertEqual(self.a().session.session_id, "S1")
        self.fact(TurnEnded, session_id="S9")
        self.assertFalse(self.a().session.confirmed)
        self.fact(TurnEnded, session_id="S1")
        self.assertTrue(self.a().session.confirmed)
        self.fact(SessionChanged, session_id="S1", change=SessionChange.RESUMED, uid="start:2")
        self.assertTrue(self.a().session.confirmed)

    def test_compaction_empties_given(self):
        self.make()
        self.send()
        self.deliver("m1")
        self.fact(TurnStarted)
        self.assertEqual(self.a().session.given, {"g": "m1"})
        self.fact(SessionChanged, session_id="s-worker-1", change=SessionChange.COMPACTED)
        self.assertEqual(self.a().session.given, {})

    def test_REC_7_REC_8_switch_keeps_the_old_session_until_done(self):
        self.make()
        old = self.a().session
        sw = Switch("worker-1", target=CODEX, old=old, reason=SwitchReason.BACKUP)
        self.act(StartSwitch(sw))
        self.act(AdvanceSwitch(replace(self.a().switch, step=SwitchStep.OPENING)))
        self.fact(SessionChanged, session_id="C1", change=SessionChange.NEW)
        self.assertEqual((self.a().session, self.a().switch.new.session_id), (old, "C1"))
        self.act(StartSwitch(Switch("worker-1", target=CLAUDE)))  # past the point where it can be replaced
        self.assertEqual(self.a().switch.step, SwitchStep.OPENING)
        self.act(AdvanceSwitch(replace(self.a().switch, step=SwitchStep.CONFIRMING)))
        self.reopen()
        self.assertEqual(self.a().switch.step, SwitchStep.CONFIRMING)
        self.send()
        self.deliver("m1")
        self.assertEqual(self.m().session_id, "C1")
        self.fact(TurnStarted)
        self.fact(TurnEnded, session_id="C1")
        self.assertTrue(self.a().switch.new.confirmed)
        self.act(AdvanceSwitch(replace(self.a().switch, step=SwitchStep.DONE)))
        a = self.a()
        self.assertEqual((a.switch, a.choice.name, a.session.session_id, a.sessions[old.session_key]),
                         (None, "codex backup", "C1", old))
        self.assertEqual(json.loads(timeline.turn_path(self.dir, self.t().id).read_text())["backend"], "codex")

    def test_REC_8_failed_switch_changes_nothing(self):
        self.make()
        before = self.a()
        self.act(StartSwitch(Switch("worker-1", target=CODEX)))
        self.act(AdvanceSwitch(replace(self.a().switch, step=SwitchStep.FAILED)))
        self.assertEqual(self.a(), before)
        self.act(StartSwitch(Switch("worker-1", target=CODEX)))
        self.act(StartSwitch(Switch("worker-1", target=replace(CLAUDE, model_id="other"))))  # the current one again
        self.assertIsNone(self.a().switch)

    def test_REC_7_saves_process_read_position_and_model(self):
        self.make()
        self.fact(ModelReported, model="opus-4")
        self.store.set_proc("worker-1", ProcRef(9, 5.0, "/pipe", "/out", "worker-1-1", "40"))
        self.fact(TurnStarted, pos="55")
        self.fact(Received, pos="", uid="x", message_id="none")
        self.fact(TurnEnded, stream="worker-1-0", pos="99")  # the output of an older process
        self.reopen()
        self.assertEqual((self.a().proc, self.a().actual_model),
                         (ProcRef(9, 5.0, "/pipe", "/out", "worker-1-1", "55"), "opus-4"))
        self.store.set_proc("worker-1", ProcRef(9, 5.0, "/pipe", "/out", "worker-1-1", "0"))
        self.assertEqual(self.a().proc.read_pos, "55")


class TestJobs(Base):
    def test_checked_jobs(self):
        self.make(job("worker-1/j1"), job("worker-1/j2"))
        self.fact(JobsChecked, jobs=(job("worker-1/j1", state="ended", exit_code=3, ended=T0 - 5),
                                     job("worker-1/j3", last_progress=7.0)))
        j = self.s.jobs
        self.assertEqual((j["worker-1/j1"].state, j["worker-1/j1"].exit_code, j["worker-1/j1"].ended),
                         (JobState.ENDED, 3, T0 - 5))
        self.assertEqual((j["worker-1/j2"].state, "worker-1/j3" in j), (JobState.RUNNING, True))
        self.act(MarkLost("worker-1/j2"), ReportJob("worker-1/j3"), StopJob("worker-1/j3"))
        self.assertEqual((self.s.jobs["worker-1/j2"].state, self.s.jobs["worker-1/j2"].ended), (JobState.LOST, T0))
        self.assertTrue(self.s.jobs["worker-1/j3"].reported and self.s.jobs["worker-1/j3"].stopped_by_user)
        self.fact(JobStarted, job=job("worker-1/j1"))  # known: nothing
        self.assertEqual(self.s.jobs["worker-1/j1"].state, JobState.ENDED)

    def test_noticed_only_in_a_turn(self):
        self.make(job(state="ended"))
        self.fact(JobNoticed, job_id="worker-1/j1")
        self.assertFalse(self.s.jobs["worker-1/j1"].woke)
        self.send(trigger=Trigger.CRITIC)
        self.store.set_agent("worker-1", caps=replace(ALL_CAPS, wakes_itself=False))
        self.deliver("m1")
        self.fact(TurnStarted)
        self.fact(JobNoticed, job_id="worker-1/j1")
        self.assertEqual((self.t().jobs, self.t().triggers), (["worker-1/j1"], [Trigger.CRITIC, Trigger.JOB]))

    def test_an_old_job_that_left_the_state_is_not_added_again(self):
        self.make()
        for i in range(S.KEEP_JOBS + 2):
            self.fact(JobStarted, job=job(f"worker-1/j{i}"))
            self.fact(JobEnded, job_id=f"worker-1/j{i}")
            self.fact(TurnStarted)
            self.fact(TurnEnded)
        self.assertEqual(len(self.s.jobs), S.KEEP_JOBS + 1)
        self.reopen()
        self.fact(JobsChecked, jobs=(job("worker-1/j0", state="ended"),))
        self.assertNotIn("worker-1/j0", self.s.jobs)

    def test_stop_agents(self):
        self.make(job(), workers=2)
        self.fact(StopAgents, "", agents=("worker-1",))
        self.assertTrue(self.a().stopped_by_user and self.s.jobs["worker-1/j1"].stopped_by_user)
        self.assertFalse(self.a("worker-2").stopped_by_user)
        self.fact(StartAgents, "")
        self.assertFalse(self.a().stopped_by_user)


class TestWorkers(Base):
    def test_WRK_1_registry(self):
        self.make()
        self.send("m1", "worker-1", "critic", "\n  Fix the parser.\nThen the tests.", trigger=Trigger.CRITIC)
        self.assertEqual(self.a().assignment, Assignment("m1", "Fix the parser.", T0))
        self.act(Send(Message("r1", "worker-1", "critic", "Fixed.", TextRef(TextKey.WORKER_FINAL, {"worker": "worker-1"}))))
        self.assertEqual((self.a().open_report, self.a().open_report_at), ("r1", T0))
        self.act(Send(Message("d1", "critic", "worker-1", "Never sent.")), Drop("d1", DropReason.REJECTED))
        self.assertEqual((self.a().open_report, self.a().assignment.message_id), ("r1", "m1"))
        self.send("m2", "worker-1", "critic", "Now the docs.")
        self.assertEqual((self.a().open_report, self.a().assignment.line), ("", "Now the docs."))
        self.act(Reject(Message("x1", ORCH, "critic", "Unknown."), rejected=Message("x2", "critic", "worker-9", "Hi")))
        self.assertEqual((self.m("x2").status, self.m("x2").drop), (WORK.FAILED, DropReason.REJECTED))

    def test_WRK_3_reopening_keeps_the_session_and_deleting_is_final(self):
        self.make()
        ref = self.a().session
        self.act(SetLifecycle("worker-1", Lifecycle.CLOSED))
        self.assertEqual(self.a().status, AgentStatus.CLOSED)
        self.reopen()
        self.act(SetLifecycle("worker-1", Lifecycle.OPEN))
        self.assertEqual((self.a().session, self.a().status), (ref, AgentStatus.IDLE))
        self.act(SetLifecycle("worker-1", Lifecycle.DELETED))
        self.act(SetLifecycle("worker-1", Lifecycle.OPEN), AddWorker(agent("worker-1")))
        self.assertEqual(self.a().lifecycle, Lifecycle.DELETED)
        self.act(AddWorker(agent("worker-2"), "worker-1"))
        self.assertEqual([a.id for a in self.s.workers], ["worker-2"])

    def test_WRK_4_late_events_never_revive_a_closed_worker(self):
        self.make(job())
        self.send()
        self.send("m2")
        self.store.begin_send("m1", "x")
        self.act(SetLifecycle("worker-1", Lifecycle.CLOSED))
        self.assertFalse(self.store.begin_send("m2", "x"))
        self.fact(Received, message_id="m1")
        self.fact(TurnStarted)
        self.fact(TurnEnded, text="Late.")
        self.fact(JobEnded, job_id="worker-1/j1")
        self.assertEqual((self.a().status, self.a().lifecycle), (AgentStatus.CLOSED, Lifecycle.CLOSED))
        self.assertTrue(self.s.jobs["worker-1/j1"].woke)  # its result is only recorded
        self.assertEqual(self.t().text, "Late.")


class TestGatesAndWork(Base):
    def test_pause_ends_with_the_last_gate(self):
        self.make()
        self.act(OpenGate(gate("g1", "pause")), OpenGate(gate("g2", "question")))
        self.assertTrue(self.s.paused)
        self.act(CloseGate("g1"))
        self.assertTrue(self.s.paused)
        self.act(CloseGate("g2", Answer("Yes", 0)))
        self.assertEqual((self.s.paused, self.s.gates["g2"].answer), (False, Answer("Yes", 0, None, T0)))
        self.act(Send(Message("m1", ORCH, "manager", gates=["g2"])))
        self.assertEqual(self.s.gates["g2"].sent_in, "m1")
        self.act(OpenGate(gate("g2")))
        self.assertFalse(self.s.gates["g2"].is_open)

    def test_cause_gate_opens_again(self):
        self.make()
        g = gate("op-billing-critic", "operational", cause="billing")
        self.act(OpenGate(g))
        self.act(OpenGate(replace(g, text="again")))
        self.assertEqual(self.s.gates[g.id].text, "Which one?")
        self.act(CloseGate(g.id, withdrawn=True))
        self.act(OpenGate(replace(g, text="again")))
        self.assertEqual((self.s.gates[g.id].text, self.s.gates[g.id].withdrawn, self.s.paused), ("again", False, True))

    def test_MS_15_milestones_live_only_in_state_json(self):
        self.make(milestone("m1", held_decision=Decision.REVISE, notified="d1"))
        new = (milestone("m1", title="New", held_decision=None, notified="old"), milestone("m2", "pending"))
        self.act(NotifyReviewReady(Message("n1", ORCH, "manager"), milestone_id="m1", digest="d2"), SetMilestones(new))
        ms = self.s.milestones
        self.assertEqual([(m.id, m.title, m.held_decision, m.notified) for m in ms],
                         [("m1", "New", Decision.REVISE, "d2"), ("m2", "Milestone m2", None, "")])
        self.act(SetMilestones((milestone("m3"),)), HoldDecision("m1", None))
        self.assertEqual([m.id for m in self.s.milestones], ["m3", "m1", "m2"])  # ids never disappear
        self.assertIsNone(self.s.milestones[1].held_decision)
        st = json.loads((self.dir / "state.json").read_text())
        self.assertEqual([m["id"] for m in st["runtime"]["snap"]["milestones"]], ["m3", "m1", "m2"])
        self.assertEqual(self.reopen() and [m.id for m in self.s.milestones], ["m3", "m1", "m2"])

    def test_checklist_and_board_are_read_for_each_snapshot(self):
        path = self.tmp / "check.md"
        self.make(milestone("m1", checklist_path="check.md"))
        self.assertEqual(self.s.checklist.error, "missing")
        path.write_text("- [ ] one")
        self.assertEqual(self.s.checklist.digest, "- [ ] one")
        self.store.read_board = lambda p, text, last, names: GoldenBoard(p, digest=text, words=list(names))
        self.store.configure(golden_path="golden.md")
        (self.tmp / "golden.md").write_text("G")
        self.act(SetGolden(GoldenBoard("", "zz", leak_told="told")))
        self.assertEqual(self.s.golden, GoldenBoard("golden.md", "G", 0, ["manager", "the manager"], "told"))

    def test_small_actions(self):
        self.make()
        self.act(AskManagerToCheck(Message("s1", ORCH, "manager")), CountRound(""), SetFinished(),
                 SetWatch(Watch("worker-1", failures=1)), MarkFailed("worker-1", "password=hunter22"),
                 OpenTerminal(Terminal("critic")), RestartSession("critic", True), SmokeTest("critic"),
                 SaveChoice("critic", CODEX))
        s = self.s
        self.assertEqual((s.silence.tries, s.silence.asked_at, s.rounds, s.finished, s.watch["worker-1"].failures),
                         (1, T0, 1, True, 1))
        self.assertEqual((s.agents["worker-1"].failed_why, s.agents["worker-1"].status), ("password=***", AgentStatus.FAILED))
        self.assertEqual((s.terminals["critic"].opened, s.agents["critic"].frozen_restart, s.agents["critic"].last_smoke,
                          s.agents["critic"].choices[0].name), (T0, T0, T0, "codex backup"))
        self.act(ResetSilence(), CountRound("", reset=True), CloseTerminal("critic"), Resend("s1"))
        self.assertEqual((self.s.silence.tries, self.s.rounds, self.s.terminals, self.m("s1").checked_at), (0, 0, {}, T0))
        self.fact(TurnStarted)
        self.assertEqual(self.t().id, "r002-t01-worker-1")  # a turn id is never used twice

    def test_engine_fields(self):
        self.make()
        self.store.configure(night=True, settings=Settings(round_limit=7), catalog=[ModelInfo("x")])
        self.store.set_agent("critic", display="Critic")
        self.store.add_agent(agent("auditor"))
        for bad in (lambda: self.store.configure(paused=True), lambda: self.store.set_agent("critic", status="idle"),
                    lambda: self.store.set_proc("nobody", None)):
            with self.assertRaises(StoreError):
                bad()
        self.reopen()
        self.assertEqual((self.s.night, self.s.settings.round_limit, self.s.catalog[0].id, self.a("critic").display,
                          self.a("auditor").role), (True, 7, "x", "Critic", Role.AUDITOR))


class TestIntents(Base):
    def test_one_per_engine_action(self):
        self.make()
        ev = self.ev(Chat, "critic", text="Hi")
        new = self.do(ev, Send(Message("m1", USER, "critic", "Hi")), ToUser(Message("u1", "critic", USER, "x")),
                      Hold("m1", HoldReason.DRAFT), SaveNotes("critic", "n"), CommandReply(ev.id))
        self.assertEqual([(i.id, type(i.action)) for i in new],
                         [(f"user/{ev.id}#0", Send), (f"user/{ev.id}#3", SaveNotes), (f"user/{ev.id}#4", CommandReply)])
        self.store.intent_done(new[0].id)
        self.store.intent_done(new[1].id, "token=abcdefgh")
        self.reopen()
        self.assertEqual([i.id for i in self.store.unfinished()], [new[2].id])
        self.assertEqual(self.store.state.intents[new[1].id].error, "token=***")
        for _ in range(2):  # as after a crash
            self.assertEqual(len(self.store.act(new[2].id, [Send(Message("m2", ORCH, "critic", "y")),
                                                            HoldAgent("critic", HoldReason.TURN)])), 1)
        self.assertEqual(len(self.store.unfinished()), 2)

    def test_order_of_one_event(self):
        self.make()
        one, two = self.ev(Chat, "critic"), self.ev(Chat, "critic")
        with self.assertRaises(StoreError):
            self.store.apply(one, [])
        self.store.begin(one)
        with self.assertRaises(StoreError):
            self.store.begin(two)
        self.assertEqual(self.store.begin(one), one)  # reported again: still the one to decide
        self.store.apply(one, [])

    def test_two_ticks_at_one_time(self):
        self.make()
        for _ in range(2):
            self.do(Tick("", T0), SaveNotes("critic", "x"))
        self.assertEqual(len(self.store.unfinished()), 2)


class TestFiles(Base):
    def test_no_options_keys_or_environment_anywhere(self):
        secret = "sk-verysecret123456"
        c = replace(CLAUDE, options={"x": secret}, env={"ANTHROPIC_API_KEY": secret,
                                                        "ANTHROPIC_BASE_URL": f"https://me:{secret}@h.example/v1?k={secret}"})
        self.make(agent("worker-1", choice=c))
        self.store.add_agent(agent("worker-2", choice=c))
        self.store.set_agent("critic", choices=[c])
        self.store.set_proc("critic", ProcRef(1, endpoint=f"http://u:{secret}@localhost:1/?t={secret}"))
        self.act(StartSwitch(Switch("critic", target=c, error=f"bad {secret}")), SaveChoice("manager", c),
                 SmokeTest("manager", c), Retry("worker-1", RetryState(why=f"key {secret}")),
                 AddWorker(agent("worker-3", choice=c)), CommandReply("c", text=TextRef(
                     TextKey.COMMAND_REFUSED, {"why": secret})))
        self.fact(SmokeResult, "critic", choice=c, why=secret)
        self.fact(AgentFailed, "critic", why=secret)
        self.fact(SessionChanged, session_id="n", detail=secret)
        self.fact(TurnEnded, status=TurnStatus.ERROR, detail=f"401 {secret}")
        ev = self.ev(ProcessExited, "critic", detail=f"Authorization: Bearer {secret}")
        self.assertNotIn(secret, self.store.begin(ev).detail)  # nor do the modules see it
        for p in self.dir.rglob("*"):
            if p.is_file():
                self.assertNotIn(secret, p.read_text(), p.name)
        self.assertEqual(self.a().choice.env, {"ANTHROPIC_BASE_URL": "https://h.example/v1"})
        before = self.s.agents
        self.assertEqual(self.reopen() and self.s.agents, before)  # memory holds exactly what the file does

    def test_one_orch_per_run(self):
        self.make()
        with self.assertRaises(StoreError):
            Store(self.dir, self.clock)
        with self.assertRaises(StoreError):
            Store.create(self.dir, self.clock, snapshot())
        ro = Store(self.dir, self.clock, readonly=True)
        self.assertEqual(ro.snapshot().agents, self.s.agents)
        with self.assertRaises(StoreError):
            ro.configure(night=True)
        self.store.close()
        self.assertEqual(json.loads((self.dir / "state.json").read_text())["status"], "stopped")
        self.store = Store(self.dir, self.clock)

    def test_torn_file_and_lost_write(self):
        self.make()
        self.send("m1")
        self.send("held", body="x")
        self.act(Hold("held", HoldReason.QUESTION))
        self.store.begin_send("m1", "x")  # this write will be lost, the CLI has the message
        path = self.dir / "state.json"
        path.write_bytes(path.read_bytes()[:2000])
        self.reopen()
        self.assertEqual([m.id for m in self.store.in_doubt("worker-1")], ["m1"])  # asked first, not sent again
        self.assertFalse(self.store.begin_send("m1", "x"))
        self.assertEqual(self.m("held").sent_at, 0)
        self.configure_and_reopen()
        for name in ("state.json", "state.prev.json"):
            (self.dir / name).write_text("{")
        with self.assertRaises(StoreError):
            self.reopen()
        self.store = None
        with self.assertRaises(StoreError):
            Store.create(self.dir, self.clock, snapshot())  # never silently a new record

    def configure_and_reopen(self):
        self.store.configure(night=True)
        self.assertTrue(self.reopen() and self.s.night)

    def test_older_or_newer_file(self):
        self.make()
        self.send()
        self.act(SaveNotes("critic", "x"))
        path = self.dir / "state.json"
        d = json.loads(path.read_text())
        r = d["runtime"]
        del r["kept"], r["session_turns"], r["snap"]["night"], r["snap"]["agents"]["critic"]["tools"]
        r["snap"]["messages"]["m1"]["new_field"] = 1
        r["pending"] = {"type": "NoSuchEvent"}
        for i in r["intents"].values():
            i["action"]["type"] = "NoSuchAction"
        path.write_text(json.dumps(d))
        self.reopen()
        self.assertEqual((self.m().body, self.s.night, self.store.unfinished(), self.store.pending),
                         ("Do it.", False, [], None))

    def test_a_record_of_the_old_runtime_is_refused(self):
        self.dir.mkdir(parents=True)
        (self.dir / "state.json").write_text(json.dumps({"status": "running", "messages": []}))
        with self.assertRaises(StoreError):
            Store(self.dir, self.clock)

    def test_handled_keys_move_to_the_archive(self):
        self.make()
        old, S.KEEP_KEYS = S.KEEP_KEYS, 10
        self.addCleanup(setattr, S, "KEEP_KEYS", old)
        first = self.ev(Chat, "critic")
        self.do(first)
        for _ in range(30):
            self.fact(Reconnected, "critic")
        self.assertLessEqual(len(self.store.state.handled), 10)
        self.reopen()
        self.assertIsNone(self.store.begin(first))

    def test_old_gates_are_archived(self):
        self.make()
        for i in range(S.KEEP_GATES + 3):
            self.clock.advance(1)
            self.act(OpenGate(gate(f"g{i}", "pause")))
            self.act(CloseGate(f"g{i}"))
        self.assertEqual(len(self.s.gates), S.KEEP_GATES)
        self.reopen()
        self.act(OpenGate(gate("g0", "pause")))  # a known id
        self.assertNotIn("g0", self.s.gates)

    def test_odd_text_is_saved(self):
        self.make()
        self.fact(TurnEnded, text="half \ud83d of a pair, é, \x00")
        self.reopen()
        self.assertIn("of a pair, é", self.t().text)

    def test_a_snapshot_given_out_never_changes(self):
        self.make()
        self.send()
        snap = self.s
        kept = copy.deepcopy(snap)
        self.deliver("m1")
        self.fact(TurnStarted)
        self.act(CountRound(""), HoldAgent("critic", HoldReason.TURN), OpenGate(gate("g")), SetMilestones((milestone("m9"),)))
        self.assertEqual(snap, kept)

    def test_a_failed_write_changes_nothing(self):
        self.make()
        write, S._write = S._write, lambda *a: (_ for _ in ()).throw(OSError("no space"))
        self.addCleanup(setattr, S, "_write", write)
        before, ev = self.s, self.ev(StopAgents, "")
        with self.assertRaises(OSError):
            self.store.begin(ev)
        self.assertEqual((self.s, self.store.pending), (before, None))
        S._write = write
        self.do(ev)
        self.assertTrue(self.a().stopped_by_user)


if __name__ == "__main__":
    unittest.main()
