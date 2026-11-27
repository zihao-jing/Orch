"""F2 recovery (docs/redesign.md 2.10, 2.11, MOD-6): REC-1…6, 8; SES-1…6; MOD-6. Scenarios 9 to 12 of 4.4.

`World` applies the actions as the store does (only what recovery reads back), so a scenario runs event by event.
"""
from rt_helpers import *  # noqa: F401,F403

import copy

from orch.runtime import models
from orch.runtime.recovery import module

SONNET = Choice("claude", model="sonnet", model_id="sonnet", name="sonnet")  # a backup that keeps the session
W = "worker-1"


class World:
    def __init__(self, snap: Snapshot):
        self.s, self.n, self.log = snap, 0, []

    def a(self, agent_id: str = W) -> Agent:
        return self.s.agents[agent_id]

    def do(self, ev: Event, mod: Any = None) -> List[Action]:
        self.n += 1
        if isinstance(ev, AgentEvent):
            ev = replace(ev, pos=f"p{self.n}")
        elif isinstance(ev, EngineEvent) and not isinstance(ev, Tick):
            ev = replace(ev, uid=ev.uid or f"u{self.n}")
        acts = decide(mod or module(), self.s, ev)
        self.apply(acts)
        self.log += acts
        return acts

    def later(self, dt: float) -> List[Action]:
        self.s.now += dt
        return self.do(Tick("", self.s.now))

    def ev(self, kind: type, agent_id: str = W, **kw: Any) -> List[Action]:
        return self.do(event(kind, self.s, agent_id, **kw))

    def end(self, agent_id: str = W, text: str = "Done.", status: str = "ok", error: Optional[str] = None,
            started_by: Sequence[str] = (), **kw: Any) -> List[Action]:
        """A turn of the agent ends (it took in `started_by`)."""
        self.n += 1
        kw.setdefault("id", f"r001-t{self.n:02d}-{agent_id}")
        self.s, ev = turn_ended(self.s, agent_id, text, error=error, started_by=list(started_by), **kw)
        if status == "stopped":
            self.s.turns[agent_id].status, ev = TurnStatus.STOPPED, replace(ev, status=TurnStatus.STOPPED)
        return self.do(ev)

    def opened(self, change: str = "new", agent_id: str = W) -> List[Action]:
        """The engine opened the CLI: the store's part of SessionChanged."""
        a = self.a(agent_id)
        sw = a.switch
        key = sw.target.session_key if sw else a.session_key
        ref = a.sessions.get(key) if change == "resumed" else None
        if ref is None:
            self.n += 1
            ref = SessionRef(f"s{self.n}", key, sw.target if sw else a.choice, "/work", started=self.s.now)
        a.sessions[key] = ref
        if sw:
            sw.new = ref
        else:
            a.session_key = key
        a.proc = ProcRef(pid=200 + self.n, started=self.s.now, stream=f"{agent_id}-{self.n}")
        return self.ev(SessionChanged, agent_id, session_id=ref.session_id, change=change)

    def message(self, mid: str, to: str = W, sender: str = "critic", **kw: Any) -> Message:
        self.n += 1
        kw.setdefault("status", "waiting")
        m = message(mid, to, sender, created=self.s.now, seq=1000 + self.n, **kw)
        self.s.messages[mid] = m
        return m

    def sent(self, mid: str) -> None:
        m = self.s.messages[mid]
        m.sent_at = m.delivered_at = self.s.now
        m.status = MessageStatus.DELIVERED

    def taken(self, mid: str) -> None:
        self.sent(mid)
        self.s.messages[mid].taken_at = self.s.now

    def apply(self, actions: Sequence[Action]) -> None:
        s = self.s
        for x in actions:
            a = s.agents.get(getattr(x, "agent", "") or getattr(getattr(x, "switch", None), "agent", ""))
            if isinstance(x, Send):
                if x.message.id in s.messages:
                    s.messages[x.message.id].bypass = list(x.bypass)
                    continue
                m = replace(x.message, created=s.now, seq=len(s.messages) + 5000, bypass=list(x.bypass))
                if isinstance(x, ToUser):
                    m.status = MessageStatus.DELIVERED
                s.messages[m.id] = m
            elif isinstance(x, Drop):
                s.messages[x.message_id].drop, s.messages[x.message_id].status = x.reason, MessageStatus.FAILED
            elif isinstance(x, HoldAgent) and x.reason not in a.holds:
                a.holds.append(x.reason)
            elif isinstance(x, ReleaseAgent) and x.reason in a.holds:
                a.holds.remove(x.reason)
            elif isinstance(x, Retry):
                a.retry = copy.deepcopy(x.state)
            elif isinstance(x, (StartSwitch, AdvanceSwitch)):
                sw = copy.deepcopy(x.switch)
                a = s.agents[sw.agent]
                if isinstance(x, StartSwitch) and sw.reason == SwitchReason.USER and sw.target.same(a.choice):
                    a.switch = None
                elif sw.step == SwitchStep.DONE:
                    a.choice, a.session_key, a.switch = sw.target, sw.target.session_key, None
                elif sw.step == SwitchStep.FAILED:
                    a.switch = None
                else:
                    a.switch = sw
            elif isinstance(x, MarkFailed):
                a.failed_why = x.why
            elif isinstance(x, OpenGate):
                s.gates[x.gate.id] = replace(x.gate, closed=0.0)
            elif isinstance(x, CloseGate):
                s.gates[x.gate_id].closed, s.gates[x.gate_id].withdrawn = s.now, x.withdrawn
            elif isinstance(x, SmokeTest):
                a.last_smoke = s.now

    def to(self, recipient: str = W) -> List[Message]:
        """What reached the recipient or waits for it, in order."""
        return sorted((m for m in self.s.messages.values() if m.recipient == recipient and m.drop is None),
                      key=lambda m: m.seq)


def world(*records: Any, choices: Sequence[Choice] = (CLAUDE, CODEX), workers: int = 2, **kw: Any) -> World:
    s = snapshot(*records, workers=workers, **kw)
    for a in s.agents.values():
        a.choices = list(choices)
    return World(s)


def smoke(w: World, ok: bool = True, choice: Optional[Choice] = None, **kw: Any) -> List[Action]:
    return w.ev(SmokeResult, choice=choice or w.a().switch.target, ok=ok, **kw)


def fail_five_times(test: Any, w: World) -> None:
    """REC-1: the first turn and four tries end with an API error."""
    w.message("m1", body="Train the model.")
    w.taken("m1")
    w.end(error="transient", started_by=["m1"])
    for attempt in range(2, 6):
        test.assertEqual(w.a().retry.attempt, attempt - 1)
        acts = w.later(w.a().retry.until - w.s.now)
        go = assert_has(test, acts, Send, key=TextKey.RETRY_CONTINUE)
        w.taken(go.message.id)
        w.end(error="transient", started_by=[go.message.id])


# ------------------------------------------------------------------------------------------------ retries, backups


class TestRetry(unittest.TestCase):
    def test_manual_retry_continues_failed_turn_once_on_current_model(self):
        w = world(choices=[CLAUDE])
        w.message("m1")
        w.taken("m1")
        w.end(error="billing", started_by=["m1"])
        self.assertTrue(public_state(w.s)["agents"][W]["can_retry"])
        acts = w.do(RetryAgent(W, w.s.now, id="click"))
        assert_has(self, acts, ReleaseAgent, reason=HoldReason.SEND_FAILED)
        assert_has(self, acts, Send, key=TextKey.RETRY_CONTINUE, continues=w.s.turns[W].id)
        assert_has(self, acts, CommandReply, ok=True)
        assert_none(self, acts, StartSwitch)
        self.assertFalse(w.s.can_retry(W))
        duplicate = w.do(RetryAgent(W, w.s.now, id="second-click"))
        assert_has(self, duplicate, CommandReply, ok=False)
        assert_none(self, duplicate, Send)

    def test_REC_1_five_tries_with_the_waits_then_the_backup(self):
        w = world()
        w.message("m1")
        w.taken("m1")
        acts = w.end(error="transient", started_by=["m1"])
        self.assertEqual(assert_has(self, acts, Retry).state.until, w.s.now + 60)
        assert_none(self, acts, HoldAgent)  # nothing is held after a failed turn
        fail_five_times(self, world())
        w = world()
        fail_five_times(self, w)
        sw = w.a().switch
        self.assertEqual((sw.target.name, sw.reason, sw.step), ("codex backup", SwitchReason.BACKUP, SwitchStep.TESTING))
        self.assertIn(HoldReason.SWITCH, w.a().holds)

    def test_REC_1_a_send_failure_holds_the_agent_until_the_try(self):
        w = world()
        w.message("m1")
        acts = w.ev(SendFailed, message_id="m1", error="transient")
        assert_has(self, acts, HoldAgent, reason=HoldReason.SEND_FAILED)
        acts = w.later(61)
        assert_has(self, acts, ReleaseAgent, reason=HoldReason.SEND_FAILED)
        assert_none(self, acts, Send)  # the message itself goes again

    def test_REC_2_billing_goes_straight_to_a_backup_that_avoids_it(self):
        w = world(choices=[CLAUDE, SONNET, CODEX])
        acts = w.end(error="billing")
        assert_none(self, acts, Retry, state__until=w.s.now + 60)
        self.assertEqual(assert_has(self, acts, StartSwitch).switch.target.name, "codex backup")  # sonnet: same account

    def test_REC_2_missing_cli_skips_the_same_backend(self):
        w = world(choices=[CLAUDE, SONNET])
        acts = w.end(error="missing_cli")
        assert_has(self, acts, OpenGate, gate__id=f"op-{GateCause.MISSING_CLI}-{W}")
        assert_none(self, acts, StartSwitch)

    def test_REC_2_REC_3_scenario_11_billing_stops_one_worker_only(self):
        w = world(choices=[CLAUDE])
        w.message("m1")
        w.taken("m1")
        acts = w.end(error="billing", started_by=["m1"])
        assert_has(self, acts, MarkFailed, agent=W)
        assert_has(self, acts, OpenGate, gate__id=f"op-{GateCause.BILLING}-{W}")
        told = assert_has(self, acts, Send, recipient="critic", key=TextKey.WORKER_FAILED)
        self.assertTrue(told.message.needs_answer)
        assert_has(self, acts, ToUser, key=TextKey.NOTE_AGENT_FAILED)
        self.assertFalse([x for x in acts if getattr(x, "agent", "") == "worker-2"])
        self.assertEqual(w.later(5), [])  # the others go on; nothing more for it
        acts = w.later(1800)  # REC-6: tested again every 30 minutes
        assert_has(self, acts, SmokeTest, agent=W)
        acts = smoke(w, choice=CLAUDE)
        assert_has(self, acts, ReleaseAgent, reason=HoldReason.SEND_FAILED)
        assert_has(self, acts, CloseGate, withdrawn=True)
        assert_has(self, acts, Send, key=TextKey.RETRY_CONTINUE, continues=w.s.turns[W].id)

    def test_REC_3_a_failed_critic_gets_a_gate(self):
        w = world(choices=[CLAUDE])
        acts = w.end("critic", error="transient")
        for _ in range(4):
            acts = w.later(w.a("critic").retry.until - w.s.now)
            go = find(acts, Send, key=TextKey.RETRY_CONTINUE)[0]
            w.taken(go.message.id)
            acts = w.end("critic", error="transient", started_by=[go.message.id])
        assert_has(self, acts, OpenGate, gate__id=f"op-{GateCause.AGENT_FAILED}-critic")
        assert_none(self, acts, Send, key=TextKey.WORKER_FAILED)

    def test_REC_4_with_a_running_job_it_keeps_trying_in_the_open_program(self):
        w = world(job())
        w.a().retry = RetryState(4, 0.0, turn="")
        acts = w.end(error="transient")
        self.assertEqual(assert_has(self, acts, Retry).state.attempt, 5)
        assert_none(self, acts, StartSwitch)
        w.a().retry = replace(w.a().retry, attempt=4)
        acts = w.end(error="billing")
        self.assertEqual(assert_has(self, acts, StartSwitch).switch.step, SwitchStep.PENDING)  # until the job ends
        w.s.jobs["worker-1/j1"].state = JobState.ENDED
        self.assertEqual(assert_has(self, w.later(1), AdvanceSwitch).switch.step, SwitchStep.TESTING)

    def test_REC_6_all_choices_fail_gate_then_one_works(self):
        """Scenario 11: all choices fail -> gate -> a choice works -> gate closes."""
        w = world()
        fail_five_times(self, w)
        acts = smoke(w, ok=False, error="billing", why="no credit")
        assert_has(self, acts, MarkFailed)
        g = assert_has(self, acts, OpenGate, gate__id=f"op-{GateCause.ALL_FAILED}-{W}").gate
        self.assertEqual(g.words.values["choices"], ["opus", "codex backup"])
        acts = w.do(GateAnswer("", w.s.now, id="g1", gate_id=g.id))
        self.assertEqual(len(find(acts, SmokeTest)), 2)
        acts = smoke(w, choice=CODEX)
        self.assertEqual(assert_has(self, acts, StartSwitch).switch.step, SwitchStep.OPENING)
        w.opened()
        req = assert_has(self, w.log[-3:], Send, recipient="critic", key=TextKey.HANDOVER_REQUEST_CRITIC)
        w.taken(req.message.id)
        w.later(1)
        w.message("i1", body="Go on with step 3.")
        acts = w.later(1)
        assert_has(self, acts, Send, message__id="i1")
        w.taken("i1")
        acts = w.end(started_by=["i1"])
        assert_has(self, acts, CloseGate, gate_id=g.id)
        assert_has(self, acts, ToUser, key=TextKey.NOTE_SWITCHED)
        self.assertNotIn(HoldReason.SEND_FAILED, w.a().holds)

    def test_REC_5_returns_to_the_main_choice_only_when_the_session_goes_on(self):
        w = world(choices=[CLAUDE, SONNET], now=T0 + 20000)
        w.a().choice = SONNET
        acts = w.later(1)
        assert_has(self, acts, SmokeTest, choice=CLAUDE)
        acts = smoke(w, choice=CLAUDE)
        sw = assert_has(self, acts, StartSwitch).switch
        self.assertEqual((sw.reason, sw.step, sw.new_session), (SwitchReason.RETURN, SwitchStep.OPENING, False))
        w.opened("resumed")
        self.assertIsNone(w.a().switch)  # nothing to tell: the same session
        self.assertTrue(w.a().choice.same(CLAUDE))
        w = world(now=T0 + 20000)
        w.a().choice, w.a().session_key = CODEX, CODEX.session_key  # another session: stays
        w.a().sessions[CODEX.session_key] = SessionRef("s-codex", CODEX.session_key, CODEX, confirmed=True)
        self.assertEqual(w.later(1), [])


# ------------------------------------------------------------------------------------------------ switches


class TestSwitch(unittest.TestCase):
    def user_switch(self, w: World, target: Choice, **kw: Any) -> List[Action]:
        return w.do(event(ChooseModel, w.s, W, backup=target.name, model_id=target.model_id if not target.name else "",
                          id=f"c{w.n}", **kw), models.module({}))

    def test_MOD_6_waits_until_idle_with_no_job(self):
        w = world(job())
        self.user_switch(w, CODEX)
        self.assertEqual(w.a().switch.step, SwitchStep.PENDING)
        self.assertEqual(w.later(5), [])
        del w.s.jobs["worker-1/j1"]  # ended, and its agent knows
        acts = w.later(5)
        self.assertEqual(assert_has(self, acts, AdvanceSwitch).switch.step, SwitchStep.TESTING)

    def test_MOD_6_SES_5_the_old_model_writes_the_handover(self):
        w = world()
        self.user_switch(w, CODEX)
        w.later(1)
        acts = smoke(w)
        ask = assert_has(self, acts, Send, key=TextKey.HANDOVER_WRITE)
        self.assertEqual(ask.bypass, (HoldReason.SWITCH,))
        self.assertEqual(w.a().switch.handover_by, W)
        w.taken(ask.message.id)
        acts = w.end(text="Step 3 is next; tests pass.", started_by=[ask.message.id])
        self.assertEqual(w.a().switch.step, SwitchStep.OPENING)
        acts = w.opened()
        first = assert_has(self, acts, Send, key=TextKey.HANDOVER_INSTRUCTION)
        self.assertEqual(first.message.body, "Step 3 is next; tests pass.")
        w.taken(first.message.id)
        acts = w.end(started_by=[first.message.id])
        self.assertTrue(w.a().choice.same(CODEX))
        assert_has(self, acts, ReleaseAgent, reason=HoldReason.SWITCH)

    def test_MOD_7_a_failed_smoke_test_switches_nothing(self):
        w = world()
        self.user_switch(w, CODEX)
        w.later(1)
        acts = smoke(w, ok=False, why="model not found")
        assert_has(self, acts, ToUser, key=TextKey.SMOKE_FAILED)
        self.assertIsNone(w.a().switch)
        self.assertEqual(w.a().holds, [])

    def test_SES_1_a_backup_with_the_same_session_key_keeps_the_session(self):
        w = world(choices=[CLAUDE, SONNET])
        self.user_switch(w, SONNET)
        w.later(1)
        acts = smoke(w)
        self.assertFalse(w.a().switch.new_session)
        assert_none(self, acts, Send)
        w.opened("resumed")
        self.assertTrue(w.a().choice.same(SONNET))
        self.assertEqual(w.a().session.session_id, f"s-{W}")

    def test_SES_2_SES_3_a_lost_session_gets_one_new_one(self):
        w = world()
        w.end(error="session_lost")
        sw = w.a().switch
        self.assertEqual((sw.reason, sw.step, sw.tried_new), (SwitchReason.SESSION_LOST, SwitchStep.OPENING, True))
        w.opened()
        w.later(1)
        self.assertEqual(w.a().switch.step, SwitchStep.HANDOVER)  # the critic writes it
        acts = w.ev(SessionChanged, change="lost")  # SES-3: it failed again: the choice failed
        sw = w.a().switch
        self.assertEqual((sw.target.name, sw.step), ("codex backup", SwitchStep.TESTING))

    def test_SES_2_a_different_session_id_is_a_rejection(self):
        w = world()
        acts = w.ev(SessionChanged, session_id="other", change="different")
        self.assertEqual(assert_has(self, acts, StartSwitch).switch.reason, SwitchReason.SESSION_LOST)

    def test_SES_5_upper_level_silent_then_a_gate_and_the_users_text(self):
        w = world()
        fail_five_times(self, w)
        smoke(w)
        w.opened()
        req = [m for m in w.to("critic") if m.key == TextKey.HANDOVER_REQUEST_CRITIC][0]
        self.assertEqual(req.text.values["agent"], W)
        acts = w.later(w.s.settings.handover_wait)
        g = assert_has(self, acts, OpenGate, gate__cause=GateCause.HANDOVER).gate
        w.s.gates[g.id].closed = w.s.now
        w.s.gates[g.id].answer = Answer(text="Go on with the tests.")
        acts = w.later(1)
        first = assert_has(self, acts, Send, key=TextKey.HANDOVER_INSTRUCTION)
        self.assertEqual(first.message.body, "Go on with the tests.")

    def test_SES_5_a_new_manager_reads_its_saved_turns(self):
        w = world()
        w.message("m1", "manager", "user")
        w.taken("m1")
        w.end("manager", error="transient", started_by=["m1"])
        for _ in range(4):
            acts = w.later(w.a("manager").retry.until - w.s.now)
            go = find(acts, Send, key=TextKey.RETRY_CONTINUE)[0]
            w.taken(go.message.id)
            w.end("manager", error="transient", started_by=[go.message.id])
        w.ev(SmokeResult, "manager", choice=CODEX, ok=True)
        acts = w.opened(agent_id="manager")
        first = assert_has(self, acts, Send, key=TextKey.MANAGER_TAKEOVER)
        self.assertTrue(first.message.text.values["turns_dir"].endswith("/rounds"))

    def test_SES_4_SES_6_scenario_10_retries_backup_critic_handover_and_return(self):
        """Scenario 10: transient errors -> retries -> backup with a new session -> handover by the critic -> return
        to the main session (SES-4)."""
        w = world()
        w.message("old", body="Earlier note.")  # the critic's own unanswered message: replaced
        w.message("u1", sender=USER, body="Also log the loss.", trigger="user_chat")
        w.taken("u1")
        fail_five_times(self, w)
        w.s.turns[W].started_by.append("u1")
        w.s.messages["u1"].status = MessageStatus.ANSWERED
        smoke(w)
        acts = w.opened()
        req = assert_has(self, acts, Send, recipient="critic", key=TextKey.HANDOVER_REQUEST_CRITIC)
        w.taken(req.message.id)
        w.message("i1", body="Continue at step 3.")
        acts = w.later(1)
        assert_has(self, acts, Send, message__id="i1", bypass=(HoldReason.SWITCH,))
        w.taken("i1")
        acts = w.end(started_by=["i1"])
        assert_has(self, acts, Drop, message_id="old", reason=DropReason.REPLACED)
        again = assert_has(self, acts, Send, key=TextKey.MESSAGE_AGAIN)
        self.assertIn("Also log the loss.", again.message.body)
        self.assertNotIn("Train the model.", again.message.body)  # the critic's: the instruction replaces it
        self.assertIn(HoldReason.SWITCH, w.a().holds)  # until MESSAGE_AGAIN is out
        w.sent(again.message.id)
        assert_has(self, w.later(1), ReleaseAgent, reason=HoldReason.SWITCH)
        # back to the main choice: its saved session is reopened and told what happened since (SES-4)
        w.taken(again.message.id)
        w.end(started_by=[again.message.id])
        w.do(event(ChooseModel, w.s, W, id="back"), models.module({}))
        self.assertFalse(w.a().switch.new_session)
        w.later(1)
        smoke(w)
        self.assertEqual(w.a().switch.step, SwitchStep.OPENING)
        acts = w.opened("resumed")
        ret = assert_has(self, acts, Send, key=TextKey.SESSION_RETURN)
        w.taken(ret.message.id)
        w.end(started_by=[ret.message.id])
        self.assertTrue(w.a().choice.same(CLAUDE))
        self.assertEqual(w.a().session.session_id, f"s-{W}")

    def test_REC_8_a_failed_switch_keeps_the_old_session(self):
        w = world()
        self.user_switch(w, CODEX, force=True, confirmed=True)
        w.later(1)
        smoke(w)
        w.opened()
        req = [m for m in w.to("critic") if m.key == TextKey.HANDOVER_REQUEST_CRITIC][0]
        w.taken(req.id)
        w.message("i1", body="Go on.")
        w.later(1)
        self.assertEqual(w.a().switch.step, SwitchStep.CONFIRMING)
        w.taken("i1")
        acts = w.end(error="billing", started_by=["i1"])
        assert_has(self, acts, AdvanceSwitch, switch__step=SwitchStep.FAILED)
        assert_has(self, acts, ToUser, key=TextKey.SMOKE_FAILED)
        self.assertTrue(w.a().choice.same(CLAUDE))
        self.assertEqual(w.a().session.session_id, f"s-{W}")

    def test_REC_8_a_backup_on_another_backend_is_reopened_as_it_is(self):
        """Scenario 9: Orch stops while agents work and a worker is on a backup on another backend."""
        w = world()
        w.a().choice = CODEX
        w.a("worker-2").proc = None
        w.s.turns["worker-2"] = turn("worker-2", status="stopped", started_by=["m2"], ended=T0 - 5)
        w.message("m2", "worker-2")
        w.s.messages["m2"].status = MessageStatus.ANSWERED
        acts = w.do(OrchStarted("", w.s.now))
        assert_actions(self, acts, [(RestartSession, {"agent": "worker-2"})])  # worker-1 goes on where it is
        acts = w.opened("resumed", "worker-2")
        assert_has(self, acts, Send, key=TextKey.RESTART_CONTINUE, continues=w.s.turns["worker-2"].id)
        stored = from_dict(Choice, to_dict(CODEX))
        self.assertEqual(models.resolve(stored, {}, {"backend": "claude"}).backend, "codex")

    def test_MOD_6_scenario_12_forced_switch_during_a_job(self):
        w = world(job())
        acts = self.user_switch(w, CODEX, force=True)
        self.assertEqual(assert_has(self, acts, CommandReply, ok=False).text.key, TextKey.ASK_FORCE_SWITCH)
        self.user_switch(w, CODEX, force=True, confirmed=True)
        acts = w.later(1)
        assert_has(self, acts, AdvanceSwitch, switch__step=SwitchStep.TESTING)
        acts = smoke(w)
        assert_none(self, acts, Send, key=TextKey.HANDOVER_WRITE)  # forced: the old model writes nothing
        self.assertEqual(w.a().switch.step, SwitchStep.OPENING)

    def test_MOD_6_force_switch_command(self):
        w = world(job())
        self.user_switch(w, CODEX)
        acts = w.do(ForceSwitch(W, w.s.now, id="f1"))
        assert_has(self, acts, CommandReply, ok=False, text__key=TextKey.ASK_FORCE_SWITCH)
        acts = w.do(ForceSwitch(W, w.s.now, id="f2", confirmed=True))
        assert_has(self, acts, CommandReply, ok=True)
        self.assertEqual((w.a().switch.step, w.a().switch.forced), (SwitchStep.TESTING, True))
        acts = w.do(ForceSwitch("worker-2", w.s.now, id="f3"))
        self.assertEqual(assert_has(self, acts, CommandReply).text.values["why"], Refusal.NOT_OPEN)


# ------------------------------------------------------------------------------------------------ restarts


def scenario_12() -> Tuple[World, List[Any]]:
    """A forced switch to the backup during a job, with the critic writing the handover: one step per entry."""
    w = world(job())
    w.message("u1", sender=USER, body="Check the plot.", trigger="user_chat")
    w.sent("u1")
    t = turn(W, status="running", started_by=["u1"])
    w.s.turns[W] = t
    w.a().status = AgentStatus.WRITING

    def choose(w: World) -> None:
        w.do(event(ChooseModel, w.s, W, backup=CODEX.name, force=True, confirmed=True, id="c1"), models.module({}))

    def stopped(w: World) -> None:  # the engine stopped it for the switch
        w.taken("u1")
        w.a().status = AgentStatus.IDLE
        w.end(status="stopped", started_by=["u1"])

    def request_taken(w: World) -> None:
        req = [m for m in w.to("critic") if m.key == TextKey.HANDOVER_REQUEST_CRITIC][0]
        w.taken(req.id)
        w.later(1)

    def written(w: World) -> None:
        if "i1" not in w.s.messages:
            w.message("i1", body="Go on with the plot.")
        w.later(1)

    def answered(w: World) -> None:
        w.taken("i1")
        w.end(started_by=["i1"])

    def again_out(w: World) -> None:
        for m in w.to():
            if m.key == TextKey.MESSAGE_AGAIN:
                w.sent(m.id)
        w.later(1)

    steps = [choose, lambda w: w.later(1), stopped, lambda w: smoke(w), lambda w: w.opened(), request_taken, written,
             answered, again_out]
    return w, steps


class TestRestart(unittest.TestCase):
    def outcome(self, w: World) -> List[Tuple[str, Any, str]]:
        return [(m.recipient, m.key, m.body) for r in (W, "critic") for m in w.to(r)]

    def test_REC_7_scenario_12_a_restart_at_each_step_loses_and_doubles_nothing(self):
        w, steps = scenario_12()
        for step in steps:
            step(w)
        want = self.outcome(w)
        self.assertTrue(w.a().choice.same(CODEX))
        self.assertEqual(w.a().holds, [])
        self.assertIn((W, TextKey.MESSAGE_AGAIN, ""), [(r, k, "") for r, k, _ in want])
        for i in range(1, len(steps)):
            w, steps = scenario_12()
            for step in steps[:i]:
                step(w)
            before = w.a().switch
            acts = w.do(OrchStarted("", w.s.now))
            if before is not None and before.step in (SwitchStep.TESTING, SwitchStep.OPENING):
                assert_has(self, acts, AdvanceSwitch, switch__step=before.step)  # the last step is repeated
            assert_none(self, acts, Send)
            for step in steps[i:]:
                step(w)
            self.assertEqual(self.outcome(w), want, f"restart after step {i}")
            self.assertTrue(w.a().choice.same(CODEX))

    def test_REC_7_the_engine_opening_twice_sends_one_first_message(self):
        w, steps = scenario_12()
        for step in steps[:5]:
            step(w)
        before = self.outcome(w)
        w.ev(SessionChanged, session_id=w.a().switch.new.session_id, change="new")
        self.assertEqual(self.outcome(w), before)


if __name__ == "__main__":
    unittest.main()
