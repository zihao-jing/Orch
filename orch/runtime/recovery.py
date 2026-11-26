"""Recovery (F2): retries, backups, the steps of a switch, handovers and sessions (REC-1…6, 8, SES-1…6, MOD-6).

Retry (Agent.retry)
  A failure sets `until`, the time of the next try. Nothing is held after a failed turn: whatever message starts a turn
  meanwhile is a try too (contracts, TurnStarted). A failure without a turn (send failed, program gone, no answer) holds
  the agent (SEND_FAILED) until the try. At the try `until` moves on to the time when a try without any answer counts as
  failed. `until` 0: nothing is due; the state only carries the choices tried during a switch, or the unfinished turn of
  an agent that failed.
Switch (Agent.switch), each step saved before its effect, so a restart repeats the last one
  PENDING (user: until the agent is settled; automatic: until no job runs) -> TESTING (the agent is held from here on)
  -> HANDOVER, where the old model writes it -> OPENING -> HANDOVER, where the upper level writes it -> CONFIRMING: one
  first message goes out, the first output that answers it proves the session -> DONE. The upper level is asked only
  when the new program is open: then nothing runs on in the old or a wrong session, and it is not asked for nothing.
  While OPENING or CONFIRMING a failed try is repeated as above: `until` set: waiting; `until` 0: trying since step_at.
An unfinished turn is the agent's last one that ended with an error or was stopped. In the same session it is told to
go on; in another session its messages are sent again (MESSAGE_AGAIN), except those an instruction replaces (SES-6).
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from .contracts import (Action, AdvanceSwitch, Agent, AgentFailed, Choice, CloseGate, CommandReply, Drop, DropReason,
                        ErrorClass, Event, FailCause, ForceSwitch, Gate, GateAnswer, GateCause, GateKind, HoldAgent,
                        HoldReason, MarkFailed, Message, MessageStatus, OpenGate, OrchStarted, ProcessExited, Refusal,
                        ReleaseAgent, Resend, RestartSession, Retry, RetryState, Role, Send, SendFailed, SessionChange,
                        SessionChanged, SmokeResult, SmokeTest, Snapshot, StartSwitch, Switch, SwitchReason, SwitchStep,
                        Text, TextKey, TextRef, Tick, ToolStart, ToUser, Trigger, Turn, TurnEnded, TurnStatus, USER,
                        new_id)

NAME = "recovery"
P, T, H, O, C, D, F = (SwitchStep.PENDING, SwitchStep.TESTING, SwitchStep.HANDOVER, SwitchStep.OPENING,
                       SwitchStep.CONFIRMING, SwitchStep.DONE, SwitchStep.FAILED)
WORKING = (Role.MANAGER, Role.CRITIC, Role.WORKER)  # the roles with a handover, notices and gates
GATES = (GateCause.ALL_FAILED, GateCause.AGENT_FAILED, GateCause.BILLING, GateCause.MISSING_CLI)
CONTINUES = (TextKey.RETRY_CONTINUE, TextKey.RESTART_CONTINUE)
OWN = CONTINUES + (TextKey.MESSAGE_AGAIN, TextKey.HANDOVER_WRITE, TextKey.HANDOVER_INSTRUCTION,
                   TextKey.MANAGER_TAKEOVER, TextKey.SESSION_RETURN, TextKey.SESSION_FRESH)  # never sent again
_REMINDER = re.compile(r"\s*<system-reminder>.*?</system-reminder>\s*", re.S)


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return _Decide(snap, event).run()


def module() -> _Module:
    return _Module()


class _Decide:
    def __init__(self, snap: Snapshot, event: Event):
        self.s, self.ev = snap, event
        # an agent's own event may be decided long after it happened (replay): its time stays its own
        self.now = event.at or snap.now
        self.out: List[Action] = []
        self.n = 0
        self.holds = {a.id: set(a.holds) for a in snap.agents.values()}

    def run(self) -> List[Action]:
        ev, a = self.ev, self.s.agents.get(self.ev.agent)
        if isinstance(ev, Tick):
            for x in self.s.agents.values():
                self.tick(x)
        elif isinstance(ev, OrchStarted):
            for x in self.s.agents.values():
                self.started(x)
        elif isinstance(ev, GateAnswer):
            self.gate_answered(ev)
        elif isinstance(ev, ForceSwitch):
            self.force(ev, a)
        elif a is not None:
            self.agent_event(a, ev)
        return self.out

    def agent_event(self, a: Agent, ev: Event) -> None:
        t = self.s.turns.get(a.id)
        if isinstance(ev, SmokeResult):
            self.smoked(a, ev)
        elif not a.is_open:
            return
        elif isinstance(ev, TurnEnded):
            self.turn_ended(a, ev)
        elif isinstance(ev, (Text, ToolStart)):
            if a.switch and a.switch.step == C and _running(t) and (isinstance(ev, ToolStart) or ev.text.strip()):
                self.done(a, a.switch)
        elif isinstance(ev, SessionChanged):
            self.session(a, ev)
        elif isinstance(ev, SendFailed):
            self.failure(a, ev.error, ev.detail, hold=True)
        elif isinstance(ev, AgentFailed):
            self.failure(a, ev.error, ev.why, self.cut(a), hold=True)
        elif isinstance(ev, ProcessExited):
            if ev.asked or ev.reported or self.s.running_jobs(a.id):  # with a job: the supervisor wakes the agent
                return
            if a.switch or self.waiting(a):  # an idle program with nothing to do opens again with the next message
                self.failure(a, ErrorClass.TRANSIENT, ev.detail, self.cut(a), hold=True)

    # -------------------------------------------------------------------------------------------- small things

    def add(self, action: Action) -> None:
        self.out.append(action)

    def hold(self, a: Agent, reason: HoldReason) -> None:
        if reason not in self.holds[a.id]:
            self.holds[a.id].add(reason)
            self.add(HoldAgent(a.id, reason))

    def release(self, a: Agent, reason: HoldReason) -> None:
        if reason in self.holds[a.id]:
            self.holds[a.id].discard(reason)
            self.add(ReleaseAgent(a.id, reason))

    def send(self, to: str, key: TextKey, values: Optional[Dict[str, Any]] = None, body: str = "", continues: str = "",
             bypass: Tuple[HoldReason, ...] = (), kind: Type[Send] = Send) -> str:
        mid = new_id(NAME, self.ev, self.n)
        self.n += 1
        self.add(kind(message=Message(mid, recipient=to, body=body, text=TextRef(key, values or {}), continues=continues,
                                      needs_answer=to != USER, trigger=Trigger.ORCH), bypass=bypass))
        return mid

    def retry(self, a: Agent, state: Optional[RetryState]) -> None:
        if state != a.retry:
            self.add(Retry(a.id, state))

    def waiting(self, a: Agent) -> List[Message]:
        """Messages to it that still need an answer."""
        return [m for m in self.s.messages.values() if m.recipient == a.id and m.open]

    def unfinished(self, a: Agent) -> Optional[Turn]:
        t = self.s.turns.get(a.id)
        if t is None or t.status not in (TurnStatus.ERROR, TurnStatus.STOPPED) or TextKey.HANDOVER_WRITE in t.keys:
            return None
        if not (t.started_by or t.joined or t.jobs) and t.triggers == [Trigger.USER_CHAT]:
            return None  # the user stopped it in a terminal
        return t

    def cut(self, a: Agent) -> str:
        """Id of the turn that is cut off: the open one (frozen), or the unfinished one."""
        t = self.s.turns.get(a.id)
        if _running(t):
            return t.id  # type: ignore[union-attr]
        t = self.unfinished(a)
        return t.id if t else ""

    def down(self, a: Agent) -> bool:
        """Failed, with nothing left to try."""
        return bool(a.failed_why) and HoldReason.SEND_FAILED in a.holds and a.switch is None

    def blocked(self, a: Agent) -> bool:
        """Its program is not Orch's to restart now."""
        return (not a.is_open or a.stopped_by_user or a.id in self.s.terminals
                or HoldReason.STOPPED in a.holds or HoldReason.TERMINAL in a.holds)

    def upper(self, a: Agent) -> Optional[Agent]:
        if a.role == Role.WORKER:
            return self.s.critic
        return self.s.manager if a.role == Role.CRITIC else None

    def history(self, a: Agent) -> bool:
        return a.session is not None and a.id in self.s.turns

    # ----------------------------------------------------------------------------------------------- failures

    def turn_ended(self, a: Agent, ev: TurnEnded) -> None:
        sw, t = a.switch, self.s.turns.get(a.id)
        writing = sw is not None and sw.step == H and sw.handover_by == a.id and t is not None \
            and TextKey.HANDOVER_WRITE in t.keys
        if ev.status == TurnStatus.ERROR:
            self.failure(a, ev.error or ErrorClass.OTHER, ev.detail, t.id if t else "", ev.retry_after)
        elif ev.status == TurnStatus.STOPPED:
            if writing:
                self.handover_missing(a, sw)  # type: ignore[arg-type]
        elif writing:
            text = (t.reply.text if t and t.reply else t.text if t else "").strip()
            if text:
                self.handed_over(a, replace(sw, handover=text))
            else:
                self.handover_missing(a, sw)  # type: ignore[arg-type]
        elif sw is not None and sw.step == C:
            self.done(a, sw)
        elif sw is None:
            self.release(a, HoldReason.SEND_FAILED)
            self.close_gates(a)

    def failure(self, a: Agent, error: ErrorClass, why: str, turn: str = "", after: float = 0.0,
                hold: bool = False) -> None:
        """One failed try. turn: the turn it cut off; hold: a try without a turn."""
        sw = a.switch
        if sw is not None and (sw.step in (O, C) or sw.step == H and sw.handover_by != a.id):  # the new one is open
            return self.switch_failed(a, sw, error, why, after, hold)
        if sw is not None and sw.step == H and sw.handover_by == a.id:
            return self.handover_missing(a, sw)
        if sw is not None and not (sw.step == P and sw.reason == SwitchReason.USER):
            return  # it is being switched anyway; what was cut off goes on after it
        if self.down(a):
            return
        r = a.retry or RetryState()
        turn, attempt = turn or r.turn, r.attempt + 1
        state = RetryState(attempt, 0.0, error, why, turn, list(r.tried))
        tries = self.s.settings.retries
        if error == ErrorClass.SESSION_LOST and (attempt == 1 or a.session is not None and a.session.confirmed):
            return self.new_session(a, state)
        if error in (ErrorClass.BILLING, ErrorClass.MISSING_CLI):
            return self.choice_failed(a, state)
        if attempt >= tries and not self.s.running_jobs(a.id):  # REC-4: with a job, go on in the open program
            return self.choice_failed(a, state)
        if hold:
            self.hold(a, HoldReason.SEND_FAILED)
        others = self.candidate(a, error, state.tried, a.choice) is not None
        self.retry(a, replace(state, until=self.now + self.wait(attempt, after, others)))

    def wait(self, attempt: int, after: float, others: bool) -> float:
        """The existing waits; with no backup left, at least as long as the provider says."""
        waits = self.s.settings.retry_waits
        wait = waits[min(attempt, len(waits)) - 1] if waits else 0.0
        return wait if others else max(wait, after)

    def try_again(self, a: Agent) -> None:
        """The time `until` has come, outside a switch."""
        r, t = a.retry, self.s.turns.get(a.id)
        assert r is not None
        frozen = _running(t) and t is not None and t.id == r.turn
        if frozen and a.last_output > r.until:  # it writes again
            return self.retry(a, None)
        if self.blocked(a) or (_running(t) and not frozen) or HoldReason.TURN in a.holds and not frozen:
            return
        waiting = self.waiting(a)
        if not r.turn and not waiting:
            self.release(a, HoldReason.SEND_FAILED)
            return self.retry(a, None)
        asked = next((m for m in waiting if m.continues and m.continues == r.turn), None)
        held = HoldReason.SEND_FAILED in a.holds
        if not held and (asked.sent_at if asked else not r.turn):  # the last try got no answer
            r = replace(r, attempt=r.attempt + 1)
            if r.attempt >= self.s.settings.retries and not self.s.running_jobs(a.id):
                return self.choice_failed(a, r)
        if r.error == ErrorClass.SESSION_LOST:
            return self.new_session(a, r)
        restart = a.proc is None or frozen or (r.why in (FailCause.NO_ANSWER.value, FailCause.FROZEN.value)
                                               and not self.s.running_jobs(a.id))
        self.release(a, HoldReason.SEND_FAILED)
        if restart:
            self.add(RestartSession(a.id))
        else:
            for m in waiting:
                if m.sent_at and not m.taken_at and m.status != MessageStatus.ANSWERED and not held:
                    self.add(Resend(m.id, True))
        if r.turn and asked is None:
            self.send(a.id, TextKey.RETRY_CONTINUE, continues=r.turn)
        self.retry(a, replace(r, until=self.now + self.s.settings.unanswered_after))

    def choice_failed(self, a: Agent, state: RetryState, sw: Optional[Switch] = None) -> None:
        """The choice (a switch: its target) does not work: the next one, in order, or the agent has failed."""
        failed = sw.target if sw else a.choice
        tried = state.tried + [failed.label] * (failed.label not in state.tried)
        c = self.candidate(a, state.error, tried, failed)
        turn = state.turn or (sw.turn if sw else "")
        if c is None:
            return self.fail(a, replace(state, tried=tried, turn=turn), sw)
        # no turn here: the switch has it, and must not make its first message's turn look like the unfinished one
        self.retry(a, RetryState(0, 0.0, state.error, state.why, "", tried))
        self.hold(a, HoldReason.SWITCH)
        step = P if not sw and self.s.running_jobs(a.id) else T
        base = replace(self.fresh(a, c, SwitchReason.BACKUP), step=step, turn=turn, error=state.why)
        if sw is None:
            return self.add(StartSwitch(base))
        self.drop_first(sw)
        kept = self.s.messages.get(sw.handover_message)
        # what was written for the new session serves the next one too
        text = sw.handover or (kept.body if kept and not kept.open else "")
        self.add(AdvanceSwitch(replace(base, created=sw.created, unfinished=list(sw.unfinished), forced=sw.forced,
                                       old=sw.old, handover=text, handover_by=sw.handover_by,
                                       handover_request=sw.handover_request,
                                       handover_message=sw.handover_message if kept and kept.open else "")))

    def candidate(self, a: Agent, error: ErrorClass, tried: Sequence[str], failed: Choice) -> Optional[Choice]:
        for c in a.choices:
            if c.label in tried or c.same(failed) or c.same(a.choice):
                continue
            if error == ErrorClass.MISSING_CLI and c.backend == failed.backend:
                continue
            if error == ErrorClass.BILLING and (c.backend, c.provider, c.session_key) == (
                    failed.backend, failed.provider, failed.session_key):
                continue
            return c
        return None

    def fail(self, a: Agent, state: RetryState, sw: Optional[Switch] = None) -> None:
        """REC-2, 3, 6: nothing is left to try."""
        why = state.why or str(state.error)
        self.add(MarkFailed(a.id, why))
        self.retry(a, replace(state, until=0.0) if state.turn else None)
        if sw is not None:
            self.drop_first(sw)
            self.add(AdvanceSwitch(replace(sw, step=F, step_at=self.now, error=why)))
        self.hold(a, HoldReason.SEND_FAILED)
        self.release(a, HoldReason.SWITCH)
        if a.role not in WORKING:  # the monitor: the supervisor tells the critic (REC-10)
            return
        if not a.failed_why:  # else it never worked since they were told
            critic = self.s.critic
            if a.role == Role.WORKER and critic is not None and critic.is_open:
                self.send(critic.id, TextKey.WORKER_FAILED, {
                    "worker": a.id, "why": why, "assignment": a.assignment.line if a.assignment else ""})
            self.send(USER, TextKey.NOTE_AGENT_FAILED, {"agent": a.id, "why": why}, kind=ToUser)
        values: Dict[str, Any] = {"agent": a.id, "why": why}
        one = len(state.tried) == 1
        if one and state.error in (ErrorClass.BILLING, ErrorClass.MISSING_CLI):
            billing = state.error == ErrorClass.BILLING
            cause, key = ((GateCause.BILLING, TextKey.GATE_BILLING) if billing
                          else (GateCause.MISSING_CLI, TextKey.GATE_MISSING_CLI))
        elif len(a.choices) > 1:
            cause, key = GateCause.ALL_FAILED, TextKey.GATE_ALL_FAILED
            values["choices"] = [c.label for c in a.choices]
        else:
            cause, key = GateCause.AGENT_FAILED, TextKey.GATE_AGENT_FAILED
        self.add(OpenGate(self.op_gate(a, cause, key, values)))

    def op_gate(self, a: Agent, cause: GateCause, key: TextKey, values: Dict[str, Any]) -> Gate:
        return Gate(f"op-{cause}-{a.id}", GateKind.OPERATIONAL, words=TextRef(key, values), opened=self.now, agent=a.id,
                    cause=cause)

    def close_gates(self, a: Agent, causes: Sequence[GateCause] = GATES) -> None:
        for g in self.s.gates.values():
            if g.is_open and g.agent == a.id and g.cause in causes:
                self.add(CloseGate(g.id, withdrawn=True))

    # --------------------------------------------------------------------------------- smoke tests, ways back

    def gate_answered(self, ev: GateAnswer) -> None:
        """The user closed the gate of a failed agent (the account is paid, ...): test its choices now."""
        g = self.s.gates.get(ev.gate_id)
        a = self.s.agents.get(g.agent) if g else None
        if g and g.is_open and a and g.cause in GATES and self.down(a):
            self.test_all(a)

    def test_all(self, a: Agent) -> None:
        for c in a.choices or [a.choice]:
            self.add(SmokeTest(a.id, c))

    def smoked(self, a: Agent, ev: SmokeResult) -> None:
        sw = a.switch
        if sw is not None and sw.step == T and ev.choice.same(sw.target):
            return self.tested(a, sw) if ev.ok else self.test_failed(a, sw, ev)
        if not ev.ok or sw is not None or self.blocked(a):
            return
        if self.down(a):
            self.works_again(a, ev.choice)
        elif self.may_return(a) and ev.choice.same(a.choices[0]):  # REC-5
            s = replace(self.fresh(a, a.choices[0], SwitchReason.RETURN), step=O)
            self.hold(a, HoldReason.SWITCH)
            self.add(StartSwitch(s))

    def works_again(self, a: Agent, c: Choice) -> None:
        """REC-6: one of a failed agent's choices works."""
        r = a.retry or RetryState()
        turn = r.turn or self.cut(a)
        if not c.same(a.choice):
            self.hold(a, HoldReason.SWITCH)
            self.retry(a, RetryState(0, 0.0, r.error, r.why))
            jobs = self.s.running_jobs(a.id)
            if not jobs:
                self.release(a, HoldReason.SEND_FAILED)
            return self.add(StartSwitch(replace(self.fresh(a, c, SwitchReason.BACKUP), step=P if jobs else O, turn=turn)))
        self.release(a, HoldReason.SEND_FAILED)
        self.close_gates(a)
        if a.proc is None:
            self.add(RestartSession(a.id))
        if turn and not any(m.continues == turn for m in self.waiting(a)):
            self.send(a.id, TextKey.RETRY_CONTINUE, continues=turn)
        self.retry(a, RetryState(0, self.now + self.s.settings.unanswered_after, r.error, r.why, turn))

    def may_return(self, a: Agent) -> bool:
        """REC-5: on a backup, idle, and the session goes on with the main choice."""
        return bool(a.on_backup and a.choices and not a.choices[0].name and a.session is not None
                    and a.choices[0].session_key == a.session_key and a.switch is None and a.retry is None
                    and not a.failed_why and not a.holds and not self.blocked(a) and self.s.settled(a.id)
                    and a.id not in self.s.discussions)

    def return_due(self, a: Agent) -> bool:
        # CONTRACT: the time a backup came into use is not recorded; the notice about the switch, or the last test, tells
        told = [m.created for m in self.s.messages.values() if m.key == TextKey.NOTE_SWITCHED and m.text
                and m.text.values.get("agent") == a.id]
        return self.now - max([a.last_smoke] + told) >= self.s.settings.return_after

    # ---------------------------------------------------------------------------------------- steps of a switch

    def fresh(self, a: Agent, target: Choice, reason: SwitchReason) -> Switch:
        new = a.sessions.get(target.session_key)
        return Switch(a.id, target, reason, created=self.now, step_at=self.now, old=a.session, new=new,
                      new_session=new is None and self.history(a))

    def step(self, sw: Switch, step: SwitchStep, **kw: Any) -> None:
        self.add(AdvanceSwitch(replace(sw, step=step, step_at=self.now, **kw)))

    def ready(self, a: Agent, sw: Switch) -> bool:
        if self.blocked(a):
            return False
        if sw.forced:
            return True
        if _running(self.s.turns.get(a.id)) or HoldReason.TURN in a.holds:
            return False
        if sw.reason == SwitchReason.USER and not (a.retry or a.failed_why):
            return self.s.settled(a.id)
        # it fails now: what it did not answer moves with it, only its jobs count
        return not (self.s.open_jobs(a.id) if sw.reason == SwitchReason.USER else self.s.running_jobs(a.id))

    def test(self, a: Agent, sw: Switch, **kw: Any) -> None:
        self.hold(a, HoldReason.SWITCH)
        self.step(sw, T, **kw)

    def tested(self, a: Agent, sw: Switch) -> None:
        if not self.ready(a, sw):
            if sw.reason == SwitchReason.USER:
                self.release(a, HoldReason.SWITCH)
            return self.step(sw, P)
        if (sw.reason == SwitchReason.USER and sw.new_session and not sw.forced and a.role in WORKING
                and not (a.retry or a.failed_why)):
            mid = self.send(a.id, TextKey.HANDOVER_WRITE, {"agent": a.id}, bypass=(HoldReason.SWITCH,))
            return self.step(sw, H, handover_by=a.id, handover_request=mid)
        self.open(a, sw)

    def test_failed(self, a: Agent, sw: Switch, ev: SmokeResult) -> None:
        if sw.reason != SwitchReason.USER:
            r = a.retry or RetryState()
            error = ev.error or ErrorClass.OTHER
            return self.choice_failed(a, RetryState(0, 0.0, error, ev.why, sw.turn or r.turn, list(r.tried)), sw)
        self.give_up(a, sw, ev.why)

    def give_up(self, a: Agent, sw: Switch, why: str) -> None:
        """A user's switch, or the way back to the main choice, failed: the agent goes on as before."""
        self.drop_first(sw)
        self.step(sw, F, error=why)
        self.send(USER, TextKey.SMOKE_FAILED, {"agent": a.id, "choice": sw.target.label, "why": why}, kind=ToUser)
        m = self.s.messages.get(sw.handover_message)
        if m is not None and m.status == MessageStatus.ANSWERED:
            self.add(Resend(m.id))  # the old session has not seen it
        opened = sw.step in (O, C) or sw.step == H and sw.handover_by != a.id
        turn = sw.turn or (a.retry.turn if a.retry else "") or ("" if opened and sw.step != O else self.cut(a))
        if a.failed_why and (opened or HoldReason.SEND_FAILED in a.holds):
            self.hold(a, HoldReason.SEND_FAILED)
            self.retry(a, RetryState(0, 0.0, why=why, turn=turn) if turn else None)
        elif opened:
            self.retry(a, None)
            if turn:
                self.send(a.id, TextKey.RESTART_CONTINUE, continues=turn)
        elif turn and not a.retry and not _running(self.s.turns.get(a.id)):  # it failed meanwhile
            self.retry(a, RetryState(1, self.now + self.wait(1, 0.0, True), turn=turn))
        self.release(a, HoldReason.SWITCH)

    def open(self, a: Agent, sw: Switch, kind: Optional[type] = None, **kw: Any) -> None:
        """OPENING: from here on the switch carries what was cut off."""
        turn = sw.turn or (a.retry.turn if a.retry else "")
        for m in self.waiting(a):
            if m.key in CONTINUES + (TextKey.HANDOVER_WRITE,) and m.id != sw.first_message:
                turn = turn or m.continues
                self.add(Drop(m.id, DropReason.REPLACED))
        if a.retry is not None:
            self.retry(a, replace(a.retry, attempt=a.retry.attempt if sw.step in (O, C) else 0, until=0.0, turn=""))
        self.hold(a, HoldReason.SWITCH)
        self.release(a, HoldReason.SEND_FAILED)
        sw = replace(sw, step=O, step_at=self.now, turn=turn, **kw)
        self.add(StartSwitch(sw) if kind is StartSwitch else AdvanceSwitch(sw))

    def new_session(self, a: Agent, state: RetryState) -> None:
        """SES-2, 3: the CLI lost or refuses the session: the same choice, a new session, once."""
        sw = replace(self.fresh(a, a.choice, SwitchReason.SESSION_LOST), new=None, new_session=self.history(a),
                     tried_new=True, turn=state.turn, error=state.why)
        for m in self.waiting(a):
            if m.key in CONTINUES:
                self.add(Drop(m.id, DropReason.REPLACED))
        self.retry(a, replace(state, until=0.0, turn=""))
        self.hold(a, HoldReason.SWITCH)
        self.release(a, HoldReason.SEND_FAILED)
        self.add(StartSwitch(replace(sw, step=O)))

    def session(self, a: Agent, ev: SessionChanged) -> None:
        sw = a.switch
        if ev.change in (SessionChange.LOST, SessionChange.DIFFERENT):
            if sw is None or sw.step in (O, C, H):
                self.failure(a, ErrorClass.SESSION_LOST, ev.detail, self.cut(a))
        elif ev.change in (SessionChange.NEW, SessionChange.RESUMED):
            if sw is not None and sw.step == O:
                self.opened(a, sw)
            elif sw is not None and sw.step == C:
                if ev.change == SessionChange.RESUMED and not self.is_open(sw.first_message):
                    self.go_on(a, sw)
            elif sw is None:
                self.resumed(a)

    def resumed(self, a: Agent) -> None:
        """The program is open again (SIL-4, StartAgents, after a terminal, ...): an unfinished turn goes on."""
        t = self.unfinished(a)
        if t is None or a.retry or a.failed_why or any(m.continues for m in self.waiting(a)):
            return
        for i in t.started_by + t.joined:
            m = self.s.messages.get(i)
            if m and m.session_id and a.session and m.session_id != a.session.session_id:
                return  # it ran in the session of a switch that failed
        self.send(a.id, TextKey.RESTART_CONTINUE, continues=t.id)

    def is_open(self, mid: str) -> bool:
        m = self.s.messages.get(mid)
        return m is not None and m.open

    def opened(self, a: Agent, sw: Switch) -> None:
        """The new program is open. What is needed now: the first message, a handover for it, or nothing."""
        t = self.unfinished(a)
        turn = sw.turn or (t.id if t else "")
        ids = list(sw.unfinished)
        for i in (t.started_by + t.joined if t else []) + [m.id for m in self.s.messages.values()
                                                         if turn and m.reply_turn == turn and m.recipient == a.id]:
            m = self.s.messages.get(i)
            if m is not None and m.key not in OWN and i not in ids and i != sw.handover_message:
                ids.append(i)
        sw = replace(sw, turn=turn, unfinished=ids)
        if sw.first_message:  # opened again during CONFIRMING
            if self.is_open(sw.first_message):
                return self.step(sw, C)
            return self.go_on(a, sw)
        if sw.new_session:
            return self.start_new(a, sw)
        old, new = sw.old, a.sessions.get(sw.target.session_key)
        if old is not None and new is not None and new.session_id != old.session_id and sw.new is not None:  # SES-4
            mid = self.send(a.id, TextKey.SESSION_RETURN, {"turns_dir": self.turns_dir(), "since": new.last_used},
                            bypass=(HoldReason.SWITCH,))
            return self.step(sw, C, first_message=mid)
        if not sw.turn or old is None:
            return self.done(a, sw)  # nothing to say: a rejection would follow with the next message (SES-2)
        key = TextKey.MESSAGE_AGAIN if sw.forced else TextKey.RETRY_CONTINUE
        mid = self.send(a.id, key, continues=sw.turn, bypass=(HoldReason.SWITCH,))
        self.step(sw, C, first_message=mid)

    def go_on(self, a: Agent, sw: Switch) -> None:
        """The first message was taken in, but its turn was cut off."""
        t = self.unfinished(a)
        if t is None:
            return self.done(a, sw) if sw.step == O else None
        mid = self.send(a.id, TextKey.RETRY_CONTINUE, continues=t.id, bypass=(HoldReason.SWITCH,))
        self.step(sw, C, first_message=mid)

    def turns_dir(self) -> str:
        return f"{self.s.run.run_dir}/rounds"

    def start_new(self, a: Agent, sw: Switch) -> None:
        """A new session: its first message, or who is to write it (SES-5)."""
        m = self.s.messages.get(sw.handover_message)
        if m is not None and m.status == MessageStatus.WAITING and not m.sent_at and m.drop is None:
            self.add(Send(message=m, bypass=(HoldReason.SWITCH,)))
            return self.step(sw, C, first_message=m.id)
        body = sw.handover or (m.body if m is not None else "")
        if body:
            mid = self.send(a.id, TextKey.HANDOVER_INSTRUCTION, body=body, bypass=(HoldReason.SWITCH,))
        elif a.role == Role.MANAGER:
            last = self.s.turns.get(a.id)
            mid = self.send(a.id, TextKey.MANAGER_TAKEOVER, {"turns_dir": self.turns_dir(),
                                                             "latest": last.id if last else ""},
                            bypass=(HoldReason.SWITCH,))
        elif a.role in WORKING:
            return self.ask_upper(a, sw)
        else:
            return self.done(a, sw)
        self.step(sw, C, first_message=mid)

    def done(self, a: Agent, sw: Switch) -> None:
        up = self.upper(a)
        instructed = sw.handover_by not in ("", a.id) and up is not None  # by the upper level, or the user for it
        self.step(sw, D)
        given = self.s.messages.get(sw.handover_message) or self.s.messages.get(sw.handover_request)
        if instructed and given is not None:  # SES-6
            for m in self.waiting(a):
                if m.sender == up.id and m.seq < given.seq and m.id != sw.first_message:  # type: ignore[union-attr]
                    self.add(Drop(m.id, DropReason.REPLACED))
        again = [m for m in (self.s.messages.get(i) for i in sw.unfinished) if m is not None
                 and not (instructed and up is not None and m.sender == up.id)
                 and (sw.new is None or m.session_id != sw.new.session_id)]
        other = sw.new_session or (sw.new is not None and sw.old is not None
                                   and sw.new.session_id != sw.old.session_id)
        if other and again:
            self.send(a.id, TextKey.MESSAGE_AGAIN, body="\n\n".join(_again(m) for m in again), continues=sw.turn,
                      bypass=(HoldReason.SWITCH,))  # before the ones that waited: the hold is lifted once it is out
        else:
            self.release(a, HoldReason.SWITCH)
        self.release(a, HoldReason.SEND_FAILED)
        self.close_gates(a, GATES + (GateCause.HANDOVER,))
        if a.retry is not None:
            self.retry(a, replace(a.retry, attempt=0, until=self.now + self.s.settings.unanswered_after, turn=""))
        self.send(USER, TextKey.NOTE_SWITCHED, {"agent": a.id, "choice": sw.target.label, "new_session": sw.new_session},
                  kind=ToUser)

    def switch_failed(self, a: Agent, sw: Switch, error: ErrorClass, why: str, after: float, hold: bool) -> None:
        """OPENING or CONFIRMING failed: a new session (once), another try, or the choice does not work."""
        r = a.retry or RetryState()
        if error == ErrorClass.SESSION_LOST and not sw.tried_new:
            self.drop_first(sw)
            return self.open(a, replace(sw, first_message="", step=T), new=None, tried_new=True, error=why,
                             new_session=self.history(a))
        state = replace(r, attempt=r.attempt + 1, error=error, why=why, until=0.0)
        if error in (ErrorClass.TRANSIENT, ErrorClass.OTHER) and state.attempt < self.s.settings.retries:
            if hold:
                self.hold(a, HoldReason.SEND_FAILED)
            return self.retry(a, replace(state, until=self.now + self.wait(state.attempt, after, False)))
        if sw.reason in (SwitchReason.USER, SwitchReason.RETURN):
            return self.give_up(a, sw, why)
        self.choice_failed(a, state, sw)

    def drop_first(self, sw: Switch) -> None:
        """Orch's own words to a session that is given up."""
        if sw.first_message != sw.handover_message and self.is_open(sw.first_message):
            self.add(Drop(sw.first_message, DropReason.REPLACED))

    # ------------------------------------------------------------------------------------------------ handover

    def handover_missing(self, a: Agent, sw: Switch) -> None:
        """The old model does not write it."""
        self.open(a, replace(sw, handover_by="", handover_request=""))

    def handed_over(self, a: Agent, sw: Switch) -> None:
        if self.ready(a, sw):
            return self.open(a, sw)
        self.add(AdvanceSwitch(sw))  # opened by a Tick once it is settled

    def ask_upper(self, a: Agent, sw: Switch) -> None:
        up = self.upper(a)
        if up is None or not up.is_open or self.down(up):
            return self.ask_user(a, sw, up)
        why = sw.error or str(sw.reason)
        if a.role == Role.WORKER:
            mid = self.send(up.id, TextKey.HANDOVER_REQUEST_CRITIC, {
                "agent": a.id, "why": why, "assignment": a.assignment.line if a.assignment else ""})
        else:
            mid = self.send(up.id, TextKey.HANDOVER_REQUEST_MANAGER, {"agent": a.id, "why": why})
        self.step(sw, H, handover_by=up.id, handover_request=mid)

    def ask_user(self, a: Agent, sw: Switch, up: Optional[Agent]) -> None:
        by = up.id if up else ("critic" if a.role == Role.WORKER else "manager")
        self.add(OpenGate(self.op_gate(a, GateCause.HANDOVER, TextKey.GATE_HANDOVER, {"agent": a.id, "by": by})))
        self.step(sw, H, handover_by=USER)

    def instruction(self, a: Agent, sw: Switch) -> Optional[Message]:
        """The upper level's message to the agent, written once it knew of the request."""
        req = self.s.messages.get(sw.handover_request)
        if req is None or not req.taken_at:
            return None
        return next((m for m in self.s.messages.values()
                     if m.sender == req.recipient and m.recipient == a.id and m.created >= req.taken_at
                     and m.status == MessageStatus.WAITING and not m.sent_at and not m.holds and m.drop is None), None)

    def handover_tick(self, a: Agent, sw: Switch) -> None:
        since = max(sw.step_at, self.s.orch_started)
        if sw.handover_by == a.id:
            if sw.handover:
                if self.ready(a, sw):
                    self.open(a, sw)
            elif not _running(self.s.turns.get(a.id)) and self.now - since >= self.s.settings.handover_wait:
                self.handover_missing(a, sw)
            return
        m = self.instruction(a, sw)
        if m is not None:
            return self.begin(a, replace(sw, handover_message=m.id))
        if sw.handover_by == USER:
            g = self.s.gates.get(f"op-{GateCause.HANDOVER}-{a.id}")
            if g is None or not g.is_open:
                text = g.answer.text.strip() if g and g.answer else ""
                return self.begin(a, replace(sw, handover=text)) if text else self.ask_upper(a, sw)
            return
        up = self.s.agents.get(sw.handover_by)
        if up is None or not up.is_open or self.down(up):
            return self.ask_user(a, sw, up)
        if up.switch or up.retry or up.holds or _running(self.s.turns.get(up.id)) or up.id in self.s.discussions:
            return
        if self.now - max(since, up.idle_since) >= self.s.settings.handover_wait:
            self.ask_user(a, sw, up)

    def begin(self, a: Agent, sw: Switch) -> None:
        """There is an instruction now."""
        self.close_gates(a, (GateCause.HANDOVER,))
        if a.proc is None:  # the new program ended while it waited
            self.open(a, sw, new=sw.new)
        else:
            self.start_new(a, sw)

    # ------------------------------------------------------------------------------------- time, start, command

    def tick(self, a: Agent) -> None:
        sw, r = a.switch, a.retry
        if not a.is_open:
            if sw is not None:
                self.drop_first(sw)
                self.step(sw, F)
                self.release(a, HoldReason.SWITCH)
            if r is not None:
                self.retry(a, None)
            return
        if sw is not None:
            return self.switch_tick(a, sw)
        if HoldReason.SWITCH in a.holds and not any(HoldReason.SWITCH in m.bypass and m.sendable for m in self.waiting(a)):
            self.release(a, HoldReason.SWITCH)
        if self.down(a):
            opened = [g.opened for g in self.s.gates.values() if g.agent == a.id and g.cause in GATES]
            if not self.blocked(a) and self.now - max([a.last_smoke] + opened) >= self.s.settings.smoke_every:
                self.test_all(a)
        elif r is not None:
            if 0 < r.until <= self.now:
                self.try_again(a)
        elif self.may_return(a) and self.return_due(a):
            self.add(SmokeTest(a.id, a.choices[0]))

    def switch_tick(self, a: Agent, sw: Switch) -> None:
        r = a.retry
        if sw.step == P:
            if sw.target.same(a.choice) and sw.reason == SwitchReason.USER:
                return
            if self.ready(a, sw):
                return self.test(a, sw)
            if sw.reason == SwitchReason.USER and r is not None and 0 < r.until <= self.now:
                self.try_again(a)
        elif sw.step == H:
            if not self.blocked(a):
                self.handover_tick(a, sw)
        elif sw.step in (O, C) and not self.blocked(a) and not _running(self.s.turns.get(a.id)):
            if r is not None and r.until > 0:
                if r.until <= self.now:
                    self.switch_again(a, sw, r)
            elif self.now - max(sw.step_at, self.s.orch_started) >= self.s.settings.unanswered_after:
                self.switch_failed(a, sw, ErrorClass.TRANSIENT, FailCause.NO_ANSWER.value, 0.0, False)

    def switch_again(self, a: Agent, sw: Switch, r: RetryState) -> None:
        self.release(a, HoldReason.SEND_FAILED)
        self.retry(a, replace(r, until=0.0))
        if sw.step == O or a.proc is None:
            return self.step(sw, O)
        m = self.s.messages.get(sw.first_message)
        if m is None or not m.open:
            return self.go_on(a, sw)
        if m.sent_at:
            self.add(Resend(m.id, True))
        self.step(sw, C)

    def started(self, a: Agent) -> None:
        """OrchStarted (REC-7): the effect of the last step may not have happened, or its result is lost."""
        sw = a.switch
        if not a.is_open:
            return
        if sw is not None:
            if sw.step == T or sw.step == O and not (a.retry and a.retry.until):
                self.step(sw, sw.step)
        elif (a.proc is None and not self.blocked(a) and not a.retry and not a.failed_why and self.unfinished(a)
              and not any(m.continues for m in self.waiting(a))):
            self.add(RestartSession(a.id))

    def force(self, cmd: ForceSwitch, a: Optional[Agent]) -> None:
        def refuse(why: Refusal) -> None:
            self.add(CommandReply(cmd.id, False, TextRef(TextKey.COMMAND_REFUSED, {"why": why})))

        if a is None:
            return refuse(Refusal.UNKNOWN)
        sw = a.switch
        if sw is None:
            return refuse(Refusal.NOT_OPEN)
        own = sw.step == H and sw.handover_by == a.id
        if sw.forced or not (sw.step in (P, T) or own):
            return refuse(Refusal.SWITCHING)
        if not cmd.confirmed:
            jobs = [j.label or j.id for j in self.s.running_jobs(a.id)]
            return self.add(CommandReply(cmd.id, False, TextRef(TextKey.ASK_FORCE_SWITCH, {
                "agent": a.id, "jobs": jobs, "new_session": sw.new_session}),
                ({"label": "force", "set": {"confirmed": True}},)))
        forced = replace(sw, forced=True)
        if sw.step == P:
            if self.blocked(a):  # done once it is Orch's again
                self.add(AdvanceSwitch(forced))
            else:
                self.test(a, forced)
        elif own:
            self.handover_missing(a, forced)
        else:
            self.add(AdvanceSwitch(forced))
        self.add(CommandReply(cmd.id))


def _running(t: Optional[Turn]) -> bool:
    return t is not None and t.status == TurnStatus.RUNNING


def _again(m: Message) -> str:
    """Its text as it was sent, without the old reminder."""
    return _REMINDER.sub("", m.rendered).strip() or m.body
