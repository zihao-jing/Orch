"""The store (package B): the state of a run, and the only code that changes it.

Files in the run folder
  state.json        everything, replaced in one atomic write; `runtime` holds the records, the other keys are for the old
                    readers (report, audit, commands)
  state.prev.json   the write before it, used if state.json cannot be read
  ledger.jsonl      the ledger: closed messages that left the state (one line each; the last line of an id counts)
  archive.jsonl     gates, jobs and handled event keys that left the state
  rounds/rNNN/      turn files, as today
  locks/run.lock    one Orch per run

How the engine uses it, per event (steps of contracts.py)
  1. ev = store.begin(event)                      None: handled before. Saves it as pending, with its facts.
  2. modules decide on store.snapshot() and ev
  3. intents = store.apply(ev, actions)           one save: actions, intents, "handled"
  4. performs the intents; store.begin_send(id, text, given) right before Adapter.send(); store.intent_done(intent.id)
After a start: first decide store.pending (if any), then perform store.unfinished(), `in_doubt` messages with arrived().

A change is made on copies and takes effect only when its write succeeded, so a snapshot once given out never changes,
and an error in the middle changes nothing.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, TypeVar

from ..recorder import Recorder
from ..report import cost_basis, endpoint_host
from .contracts import (
    CONTRACT_VERSION, USER, Action, AddWorker, AdvanceSwitch, Agent, AgentEvent, AskManagerToCheck,
    Assignment, Checklist, Clock, CloseGate, CloseTerminal, CommandReply, CountRound, Discarded,
    Drop, DropReason, Ending, Event, FailAgent, Gate, GateKind, GoldenBoard, Hold, HoldAgent, HoldDecision, Intent,
    IntentState, Job, JobEnded, JobNoticed, JobsChecked, JobStarted, JobState, MarkFailed, MarkLost, Message,
    MessageStatus, Milestone, ModelReported, NotifyReviewReady, OpenGate, OpenTerminal, ProcessExited, ProcRef,
    Received, RecordDecision, RefreshSession, Reject, Release, ReleaseAgent, Reply, ReportJob, Resend, ResetSilence,
    RestartSession, Retry, Role, SaveChoice, Send, SendFailed, SessionChange, SessionChanged, SessionRef,
    SetDiscussion, SetFinished, SetLifecycle, SetMilestones, SetWatch, Lifecycle, SmokeTest, Snapshot,
    StartAgents, StartSwitch, StopAgents, StopJob, SwitchReason, SwitchStep, Tell, Text, TextKey, Tick, ToolEnd,
    ToolStart, Trigger, Turn, TurnEnded, TurnStarted, TurnStatus, WakeWorker, from_dict, redact, to_dict)

KEEP_MESSAGES, KEEP_GATES, KEEP_JOBS, KEEP_INTENTS = 300, 50, 50, 50  # closed ones kept in the state
KEEP_KEYS = 2000  # handled event keys kept in the state; half of them move to the archive when it is full
PROTOCOL = "runtime-1"  # `communication_protocol` of the turn files

PASSING = (Text, ToolStart, ToolEnd, Tick)  # progress may repeat and is not saved one by one; a Tick has no replay
OPENING = (SwitchStep.OPENING, SwitchStep.CONFIRMING)
# the actions with an "Engine:" line (a Send to the user has none)
ENGINE_ACTIONS = (Send, Release, Resend, ReleaseAgent, RecordDecision, StartSwitch, AdvanceSwitch, RestartSession,
                  RefreshSession, SmokeTest, SaveChoice, FailAgent, AddWorker, SetLifecycle, StopJob,
                  OpenTerminal, CloseTerminal, CommandReply)
# what whoever creates a message fixes; the store raises if a known id comes with another content
FIXED = ("sender", "recipient", "body", "text", "needs_answer", "trigger", "turn", "gates", "continues", "when_idle")

T = TypeVar("T")


class StoreError(Exception):
    """A broken state file, a run another Orch has open, or a call that breaks the order of one event."""


@dataclass
class State:
    """What state.json holds under `runtime`."""
    snap: Snapshot = field(default_factory=Snapshot)
    kept: Dict[str, Turn] = field(default_factory=dict)  # older turns somebody still needs, by id
    intents: Dict[str, Intent] = field(default_factory=dict)
    pending: Optional[Event] = None  # its facts are applied, the modules' actions not yet
    handled: List[str] = field(default_factory=list)  # event keys, oldest first; the rest: archive
    seq: int = 0  # Message.seq
    round: int = 1  # of the next turn's id; never goes back (Snapshot.rounds may)
    turn_no: int = 0  # turns in this round
    turn_counts: Dict[str, int] = field(default_factory=dict)
    session_turns: Dict[str, int] = field(default_factory=dict)  # by session id
    session_costs: Dict[str, float] = field(default_factory=dict)  # running total a CLI reported, by session id
    cost_usd: float = 0.0
    cost_estimate_usd: float = 0.0


def _clean(x: Any) -> Any:
    """Stored form without keys in the error texts."""
    if isinstance(x, dict):
        return {k: redact(v) if k in ("error", "detail", "why", "reason") and isinstance(v, str) else _clean(v)
                for k, v in x.items()}
    return [_clean(v) for v in x] if isinstance(x, list) else x


def _take(obj: T) -> T:
    """An own copy exactly as it would be read back: no options, no keys."""
    return from_dict(type(obj), _clean(to_dict(obj)))


def _add(items: List[T], *new: T) -> None:
    items.extend(x for x in dict.fromkeys(new) if x not in items)


def _iso(t: float) -> Optional[str]:
    return _dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


# ----------------------------------------------------------------------------------------------- the "Store:" lines


class _Change:
    """One change of the state, on copies. Methods _on_<Event> are the facts, _do_<Action> the actions."""

    def __init__(self, store: "Store", now: float):
        st, s = store.state, store.state.snap
        self.store, self.now = store, now
        self.s = replace(s, agents=dict(s.agents), messages=dict(s.messages), turns=dict(s.turns), gates=dict(s.gates),
                         jobs=dict(s.jobs), discussions=dict(s.discussions), terminals=dict(s.terminals),
                         watch=dict(s.watch), milestones=copy.deepcopy(s.milestones), silence=replace(s.silence),
                         golden=replace(s.golden))
        self.st = replace(st, snap=self.s, kept=dict(st.kept), intents=dict(st.intents), handled=list(st.handled),
                          turn_counts=dict(st.turn_counts), session_turns=dict(st.session_turns),
                          session_costs=dict(st.session_costs))
        self.owned: Set[Tuple[int, str]] = set()
        self.created: List[str] = []  # message ids
        self.files: List[Dict[str, Any]] = []  # turn records to write
        self.save = True

    def _own(self, box: Dict[str, T], key: str) -> Optional[T]:
        """The record, as a copy this change may write to."""
        if key in box and (id(box), key) not in self.owned:
            box[key] = copy.deepcopy(box[key])
            self.owned.add((id(box), key))
        return box.get(key)

    def agent(self, i: str) -> Optional[Agent]:
        return self._own(self.s.agents, i)

    def known(self, i: str) -> Agent:
        a = self.agent(i)
        if a is None:
            raise StoreError(f"no agent {i}")
        return a

    def message(self, i: str) -> Optional[Message]:
        return self._own(self.s.messages, i)

    def job(self, i: str) -> Optional[Job]:
        return self._own(self.s.jobs, i)

    def gate(self, i: str) -> Optional[Gate]:
        return self._own(self.s.gates, i)

    def open_turn(self, agent: str) -> Optional[Turn]:
        t = self.s.turns.get(agent)
        return self._own(self.s.turns, agent) if t and t.status == TurnStatus.RUNNING else None

    def milestone(self, i: str) -> Optional[Milestone]:
        return next((m for m in self.s.milestones if m.id == i), None)

    # ------------------------------------------------------------------------------------------ facts

    def facts(self, ev: Event) -> None:
        do = getattr(self, "_on_" + type(ev).__name__, None)
        a = self.agent(ev.agent)
        if isinstance(ev, AgentEvent) and a:
            if ev.pos and a.proc and a.proc.stream == ev.stream:
                a.proc.read_pos = ev.pos
        if do and (a or not isinstance(ev, AgentEvent)):
            do(ev, a, ev.at or self.now)

    def _session(self, a: Agent) -> Optional[SessionRef]:
        """The session the agent's messages go to now."""
        return a.switch.new if a.switch and a.switch.step == SwitchStep.CONFIRMING else a.session

    # messages

    def _delivered(self, m: Message, a: Agent, at: float) -> None:
        if m.status == MessageStatus.WAITING:
            ref = self._session(a)
            m.status, m.delivered_at, m.session_id, m.error = (MessageStatus.DELIVERED, at,
                                                               ref.session_id if ref else "", "")

    def _taken(self, m: Message, a: Agent, at: float) -> None:
        self._delivered(m, a, at)
        if not m.taken_at and m.drop is None:
            m.taken_at = at
            ref = self._session(a)
            if ref:
                ref.given.update(m.given)

    def _answered(self, t: Turn, at: float) -> bool:
        """The turn went on: everything it took in is answered. A notice ends at delivered (MSG-4)."""
        any_ = False
        for i in t.started_by + t.joined:
            m = self.s.messages.get(i)
            if m and m.status == MessageStatus.DELIVERED and m.taken_at and m.needs_answer:
                m = self.message(i)
                m.status, m.answered_at, any_ = MessageStatus.ANSWERED, at, True
        return any_

    def _enter(self, t: Turn, i: str, ids: List[str], a: Agent, at: float) -> None:
        """Message i enters the turn; `ids` is its started_by or joined."""
        m = self.message(i)
        _add(ids, i)
        if m is None:  # the user's own, in a terminal; or a closed one that is in the ledger only
            old = self.store.message(i)
            _add(t.triggers, old.trigger if old else Trigger.USER_CHAT)
            return
        self._taken(m, a, at)
        m.reply_turn = m.reply_turn or t.id
        if not (m.continues and self._carry(t, m.continues)):
            _add(t.triggers, m.trigger)
            if m.key:
                _add(t.keys, m.key)
        self._learn(t, m.jobs)

    def _carry(self, t: Turn, old_id: str) -> bool:
        """The unfinished turn goes on in t (Message.continues)."""
        old = self.st.kept.get(old_id) or next((x for x in self.s.turns.values() if x.id == old_id), None)
        if old is None or old.id == t.id:
            return False
        for name in ("started_by", "joined"):
            _add(getattr(t, name), *[i for i in getattr(old, name) if i not in t.started_by + t.joined])
        for name in ("jobs", "triggers", "keys"):
            _add(getattr(t, name), *getattr(old, name))
        t.draft = t.draft or old.draft
        return True

    def _learn(self, t: Turn, jobs: Sequence[str]) -> None:
        """The turn learned of the end of these jobs."""
        for i in jobs:
            j = self.job(i)
            if j:
                j.woke = True
                _add(t.jobs, i)
        if t.jobs:
            _add(t.triggers, Trigger.JOB)

    def _doubt(self, agent: str) -> None:
        """The CLI's queue is gone: what it had, but had not taken in, may be lost."""
        for i, m in list(self.s.messages.items()):
            if m.recipient == agent and m.status == MessageStatus.DELIVERED and not (m.taken_at or m.answered_at):
                self._doubtful(self.message(i))

    def _doubtful(self, m: Message) -> None:
        m.status, m.delivered_at, m.sent_at = MessageStatus.WAITING, 0.0, m.sent_at or self.now

    def _on_Received(self, ev: Received, a: Agent, at: float) -> None:
        m = self.message(ev.message_id)
        if m is None or m.recipient != a.id:
            return
        self._delivered(m, a, at)
        if not ev.taken or m.drop is not None or m.status == MessageStatus.ANSWERED:
            return
        t = self.open_turn(a.id)
        if t is None:
            self._taken(m, a, at)
        elif m.id not in t.started_by + t.joined:
            self._enter(t, m.id, t.joined, a, at)
            self._draft(t)

    def _on_Discarded(self, ev: Discarded, a: Agent, at: float) -> None:
        m = self.message(ev.message_id)
        if m and m.recipient == a.id and m.status == MessageStatus.DELIVERED and not (m.taken_at or m.answered_at):
            self._doubtful(m)

    def _on_SendFailed(self, ev: SendFailed, a: Optional[Agent], at: float) -> None:
        m = self.message(ev.message_id)
        if m and m.status == MessageStatus.WAITING:
            m.error, m.sent_at, m.checked_at = ev.detail or str(ev.error), 0.0, 0.0

    # turns

    def _on_TurnStarted(self, ev: TurnStarted, a: Agent, at: float) -> None:
        t = self.open_turn(a.id)
        if t:
            for i in ev.message_ids:
                if i not in t.started_by + t.joined:
                    self._enter(t, i, t.joined, a, at)
            self._draft(t)
        else:
            self._start_turn(a, ev.message_ids, at)

    def _start_turn(self, a: Agent, ids: Sequence[str], at: float) -> Turn:
        last = self.s.turns.get(a.id)
        if last:
            self.st.kept[last.id] = last  # pruned if nobody needs it
        self.st.turn_no += 1
        t = Turn(f"r{self.st.round:03d}-t{self.st.turn_no:02d}-{a.id}", a.id, started=at)
        mine = [m for m in self.s.messages.values() if m.recipient == a.id and m.status == MessageStatus.DELIVERED]
        first = list(ids) or [m.id for m in mine if not m.taken_at]
        for i in first + [m.id for m in mine if m.taken_at and not m.reply_turn]:
            if i not in t.started_by:
                self._enter(t, i, t.started_by, a, at)
        if a.retry and a.retry.turn:
            self._carry(t, a.retry.turn)
        if a.caps.wakes_itself:
            self._learn(t, [j.id for j in self.s.jobs.values()
                            if j.agent == a.id and j.state == JobState.ENDED and not j.woke])
        if not t.triggers:
            t.triggers.append(Trigger.USER_CHAT if a.id in self.s.terminals else Trigger.JOB)
        t.draft = t.draft or a.id in self.s.discussions
        self._draft(t)
        self.s.turns[a.id] = t
        self.owned.add((id(self.s.turns), a.id))
        self._answered(t, at)
        a.writing_since, a.idle_since, a.last_output, a.tools = at, 0.0, at, []
        ref = self._session(a)
        if ref:
            ref.last_used = at
        self._file(t, a)
        return t

    def _draft(self, t: Turn) -> None:
        """A turn that took in the apply message is no draft."""
        if TextKey.DISCUSSION_APPLY in t.keys:
            t.draft = False

    def _output(self, a: Agent, at: float, went_on: bool = True) -> None:
        a.last_output = max(a.last_output, at)
        t = self.open_turn(a.id)
        if t and went_on and self._answered(t, at):
            self.save = True

    def _on_Text(self, ev: Text, a: Agent, at: float) -> None:
        self._output(a, at, bool(ev.text))  # "" is the CLI's own retry, not an answer

    def _on_ToolStart(self, ev: ToolStart, a: Agent, at: float) -> None:
        _add(a.tools, ev.tool_id)
        self._output(a, at)

    def _on_ToolEnd(self, ev: ToolEnd, a: Agent, at: float) -> None:
        a.tools = [i for i in a.tools if i != ev.tool_id]
        self._output(a, at, False)  # the model has not spoken yet

    def _on_TurnEnded(self, ev: TurnEnded, a: Agent, at: float) -> None:
        t = self.open_turn(a.id) or self._start_turn(a, (), at)
        ok = ev.status == TurnStatus.OK
        t.status, t.ended, t.text, t.error, t.detail = ev.status, at, ev.text, ev.error, ev.detail
        t.ending = Ending.INTERMEDIATE if self.s.open_jobs(a.id) else Ending.FINAL
        a.tools, a.writing_since, a.idle_since, a.last_output = [], 0.0, at, max(a.last_output, at)
        ref = self._session(a)
        if ok:
            t.reply = self.store.parse(ev.text, a.role)
            self._answered(t, at)
            a.retry, a.failed_why, a.frozen_restart = None, "", 0.0
            if ref and (not ev.session_id or not ref.session_id or ref.session_id == ev.session_id):
                ref.session_id = ref.session_id or ev.session_id
                ref.confirmed = True
        if ref:
            ref.last_used, ref.choice = at, (a.switch.target if ref is not a.session and a.switch else a.choice)
        self._count(t, a, ev, ref)
        self._file(t, a, ev)

    # sessions and process

    def _on_SessionChanged(self, ev: SessionChanged, a: Agent, at: float) -> None:
        sw = a.switch if a.switch and a.switch.step in OPENING else None
        if ev.change == SessionChange.COMPACTED:
            refs = [r for r in [sw.new if sw else None, *a.sessions.values()] if r]
            ref = next((r for r in refs if ev.session_id and r.session_id == ev.session_id), self._session(a))
            if ref:
                ref.given = {}
            return
        if ev.change not in (SessionChange.NEW, SessionChange.RESUMED):
            return
        choice = sw.target if sw else a.choice
        key = choice.session_key
        here, saved = sw.new if sw else a.sessions.get(key), a.sessions.get(key)
        ref = next((r for r in (here, saved) if r and ev.session_id and r.session_id == ev.session_id), None)
        if ref is None and here and not here.session_id and ev.session_id and ev.change == SessionChange.NEW:
            ref = here  # its id is known now
        if ref is None and here and not ev.session_id and ev.change == SessionChange.RESUMED:
            ref = here
        ref = copy.deepcopy(ref) if ref else SessionRef("", started=at, cwd=a.cwd)
        ref.session_id, ref.session_key, ref.choice, ref.last_used = (ev.session_id or ref.session_id, key,
                                                                      copy.deepcopy(choice), at)
        if sw:
            sw.new = ref
        else:
            a.sessions[key], a.session_key = ref, key

    def _on_ModelReported(self, ev: ModelReported, a: Agent, at: float) -> None:
        a.actual_model = ev.model

    def _on_ProcessExited(self, ev: ProcessExited, a: Agent, at: float) -> None:
        if a.proc is None or a.proc.stream in ("", ev.stream):  # else an old process: a new one runs already
            a.proc = None
            self._doubt(a.id)

    # jobs

    def _ended(self, j: Job, state: JobState, at: float, exit_code: Optional[int] = None) -> None:
        j.state, j.ended, j.exit_code = state, j.ended or at, exit_code if exit_code is not None else j.exit_code
        a = self.s.agents.get(j.agent)
        if a and not a.is_open:
            j.woke = True  # its result is only recorded

    def _add_job(self, job: Job, agent: str, at: float) -> None:
        if job.id in self.s.jobs or job.id in self.store.old_jobs:
            return
        j = replace(job, agent=job.agent or agent, started=job.started or at, command=redact(job.command))
        self.s.jobs[j.id] = j
        self.owned.add((id(self.s.jobs), j.id))
        if j.state != JobState.RUNNING:
            self._ended(j, j.state, at)

    def _on_JobStarted(self, ev: JobStarted, a: Agent, at: float) -> None:
        if ev.job:
            self._add_job(ev.job, a.id, at)

    def _on_JobEnded(self, ev: JobEnded, a: Agent, at: float) -> None:
        j = self.job(ev.job_id)
        if j and j.state == JobState.RUNNING:
            self._ended(j, JobState.LOST if ev.lost else JobState.ENDED, at, ev.exit_code)
        elif j and not ev.lost:  # found lost, or seen in the list, before its event came
            self._ended(j, JobState.ENDED, at, ev.exit_code)

    def _on_JobNoticed(self, ev: JobNoticed, a: Agent, at: float) -> None:
        t = self.open_turn(a.id)
        if t:
            self._learn(t, [ev.job_id])

    def _on_JobsChecked(self, ev: JobsChecked, a: Optional[Agent], at: float) -> None:
        if a is None:
            return
        for seen in ev.jobs:
            j = self.job(seen.id)
            if j is None:
                self._add_job(seen, a.id, at)
                continue
            if not j.command and seen.command:
                j.command = redact(seen.command)
            j.last_progress = max(j.last_progress, seen.last_progress)
            if (j.state == JobState.RUNNING and seen.state != JobState.RUNNING) or \
                    (j.state == JobState.LOST and seen.state == JobState.ENDED):
                self._ended(j, seen.state, seen.ended or at, seen.exit_code)

    # commands

    def _on_StopAgents(self, ev: StopAgents, _a: Optional[Agent], at: float) -> None:
        for i in ev.agents or list(self.s.agents):
            a = self.agent(i)
            if a:
                a.stopped_by_user = True
                for j in self.s.running_jobs(i):
                    self.job(j.id).stopped_by_user = True

    def _on_StartAgents(self, ev: StartAgents, _a: Optional[Agent], at: float) -> None:
        for i in ev.agents or list(self.s.agents):
            a = self.agent(i)
            if a:
                a.stopped_by_user = False

    # ---------------------------------------------------------------------------------------- actions

    def actions(self, actions: Sequence[Action]) -> None:
        self.reviews = {a.milestone_id for a in actions if isinstance(a, NotifyReviewReady)}
        later = []
        for a in actions:
            if isinstance(a, (Hold, Release, Drop)) and a.message_id not in self.s.messages:
                later.append(a)  # its Send comes later in the list
            else:
                self._do(a)
        for a in later:
            self._do(a)
        self._after_creating()

    def _do(self, a: Action) -> None:
        if isinstance(a, Send):
            self._create(a)
        do = getattr(self, "_do_" + type(a).__name__, None)
        if do:
            do(a)

    # messages

    def _create(self, a: Send) -> None:
        m = a.message
        quiet = isinstance(a, Tell) or m.recipient == USER
        new = Message(m.id, holds=list(m.holds), bypass=list(a.bypass), created=self.now,
                      jobs=list(a.job_ids) if isinstance(a, WakeWorker) else [], **{n: getattr(m, n) for n in FIXED})
        new.needs_answer = new.needs_answer and not quiet
        if not m.id:
            raise StoreError("a message without an id")
        if m.id in self.s.messages:
            old = self.message(m.id)
            changed = [n for n in FIXED if getattr(old, n) != getattr(new, n)]
            if changed:
                raise StoreError(f"message {m.id} exists with another {', '.join(changed)}")
            if old.status == MessageStatus.WAITING:
                _add(old.bypass, *a.bypass)
            return
        if m.id in self.store.old_messages:
            return
        self._store_new(new)

    def _store_new(self, m: Message) -> None:
        self.st.seq += 1
        m.seq = self.st.seq
        self.s.messages[m.id] = m
        self.owned.add((id(self.s.messages), m.id))
        self.created.append(m.id)
        for i in m.gates:
            g = self.gate(i)
            if g:
                g.sent_in = m.id

    def _after_creating(self) -> None:
        """What follows from the messages once every hold and drop of the event is known."""
        for i, m in list(self.s.messages.items()):  # to the user: delivered by being stored
            if m.recipient == USER and m.status == MessageStatus.WAITING and not m.holds:
                m = self.message(i)
                m.status, m.delivered_at = MessageStatus.DELIVERED, self.now
        for i in self.created:
            m = self.s.messages[i]
            frm, to = self.agent(m.sender), self.agent(m.recipient)
            if m.drop is not None or not (frm and to):
                continue
            if frm.role == Role.CRITIC and to.role == Role.WORKER:
                line = next((ln.strip() for ln in m.body.splitlines() if ln.strip()), "")
                if line:
                    to.assignment = Assignment(m.id, line[:160], self.now)
                to.open_report, to.open_report_at = "", 0.0
            elif frm.role == Role.WORKER and to.role == Role.CRITIC and m.key == TextKey.WORKER_FINAL:
                frm.open_report, frm.open_report_at = m.id, self.now

    def _do_Reject(self, a: Reject) -> None:
        m = a.rejected
        if not m.id or m.id in self.s.messages or m.id in self.store.old_messages:
            return
        self._store_new(Message(m.id, created=self.now, status=MessageStatus.FAILED, drop=DropReason.REJECTED,
                                **{n: getattr(m, n) for n in FIXED}))

    def _do_WakeWorker(self, a: WakeWorker) -> None:
        for i in a.job_ids:
            j = self.job(i)
            if j:
                j.fallback_sent = True

    def _do_AskManagerToCheck(self, a: AskManagerToCheck) -> None:
        self.s.silence.tries += 1
        self.s.silence.asked_at = self.now

    def _do_NotifyReviewReady(self, a: NotifyReviewReady) -> None:
        ms = self.milestone(a.milestone_id)
        if ms:
            ms.notified = a.digest

    def _do_Hold(self, a: Hold) -> None:
        m = self.message(a.message_id)
        if m and m.status == MessageStatus.WAITING:
            _add(m.holds, a.reason)

    def _do_Release(self, a: Release) -> None:
        m = self.message(a.message_id)
        if m:
            m.holds = [h for h in m.holds if h != a.reason]

    def _do_Drop(self, a: Drop) -> None:
        m = self.message(a.message_id)
        if m and m.status != MessageStatus.ANSWERED and m.drop is None:
            m.status, m.drop = MessageStatus.FAILED, a.reason

    def _do_Resend(self, a: Resend) -> None:
        m = self.message(a.message_id)
        if m:
            m.checked_at = self.now

    def _do_HoldAgent(self, a: HoldAgent) -> None:
        g = self.agent(a.agent)
        if g:
            _add(g.holds, a.reason)

    def _do_ReleaseAgent(self, a: ReleaseAgent) -> None:
        g = self.agent(a.agent)
        if g:
            g.holds = [h for h in g.holds if h != a.reason]

    # gates

    def _do_OpenGate(self, a: OpenGate) -> None:
        g, old = a.gate, self.s.gates.get(a.gate.id)
        known = old is not None or g.id in self.store.old_gates
        if (old and old.is_open) or (known and not g.cause):
            return
        self.s.gates.pop(g.id, None)  # opened again: it counts as a new one
        self.s.gates[g.id] = replace(g, opened=self.now, closed=0.0, answer=None, withdrawn=False, sent_in="")
        self.owned.add((id(self.s.gates), g.id))
        if g.kind in (GateKind.PAUSE, GateKind.OPERATIONAL):
            self.s.paused = True

    def _do_CloseGate(self, a: CloseGate) -> None:
        g = self.gate(a.gate_id)
        if g is None or not g.is_open:
            return
        g.closed, g.withdrawn = self.now, a.withdrawn
        g.answer = replace(a.answer, at=a.answer.at or self.now) if a.answer else None
        if not self.s.open_gates():
            self.s.paused = False

    def _do_HoldDecision(self, a: HoldDecision) -> None:
        ms = self.milestone(a.milestone_id)
        if ms:
            ms.held_decision = a.decision

    # recovery and sessions

    def _do_Retry(self, a: Retry) -> None:
        g = self.agent(a.agent)
        if g:
            g.retry = a.state

    def _do_StartSwitch(self, a: StartSwitch) -> None:
        g = self.agent(a.switch.agent)
        if g is None or (g.switch and g.switch.step in OPENING):
            return
        same = a.switch.reason == SwitchReason.USER and a.switch.target.same(g.choice)
        g.switch = None if same else replace(a.switch, created=a.switch.created or self.now,
                                             step_at=a.switch.step_at or self.now)

    def _do_AdvanceSwitch(self, a: AdvanceSwitch) -> None:
        g, sw = self.agent(a.switch.agent), a.switch
        if g is None:
            return
        g.switch = replace(sw, step_at=sw.step_at or self.now)
        if sw.step == SwitchStep.DONE:
            g.choice, g.session_key = sw.target, sw.target.session_key
            if sw.new:
                g.sessions[g.session_key] = replace(sw.new, session_key=g.session_key)
        if sw.step in (SwitchStep.DONE, SwitchStep.FAILED):
            g.switch = None

    def _do_RestartSession(self, a: RestartSession) -> None:
        g = self.agent(a.agent)
        if g and a.frozen:
            g.frozen_restart = self.now

    def _do_SmokeTest(self, a: SmokeTest) -> None:
        g = self.agent(a.agent)
        if g:
            g.last_smoke = self.now

    def _do_SaveChoice(self, a: SaveChoice) -> None:
        g = self.agent(a.agent)
        if g:
            g.choices = [a.choice] + g.choices[1:]

    def _do_MarkFailed(self, a: MarkFailed) -> None:
        g = self.agent(a.agent)
        if g:
            g.failed_why = a.why or "failed"

    # workers and jobs

    def _do_AddWorker(self, a: AddWorker) -> None:
        if a.agent.id not in self.s.agents:  # ids are never reused
            self.s.agents[a.agent.id] = copy.deepcopy(a.agent)
            self.owned.add((id(self.s.agents), a.agent.id))

    def _do_SetLifecycle(self, a: SetLifecycle) -> None:
        g = self.agent(a.agent)
        if g is None or g.lifecycle == Lifecycle.DELETED:
            return
        g.lifecycle = a.lifecycle
        for j in self.s.open_jobs(g.id):
            if not g.is_open and j.state != JobState.RUNNING:
                self.job(j.id).woke = True

    def _do_StopJob(self, a: StopJob) -> None:
        j = self.job(a.job_id)
        if j:
            j.stopped_by_user = True

    def _do_MarkLost(self, a: MarkLost) -> None:
        j = self.job(a.job_id)
        if j and j.state == JobState.RUNNING:
            self._ended(j, JobState.LOST, self.now)

    def _do_ReportJob(self, a: ReportJob) -> None:
        j = self.job(a.job_id)
        if j:
            j.reported = True

    # the rest

    def _do_CountRound(self, a: CountRound) -> None:
        self.s.rounds = 0 if a.reset else self.s.rounds + 1
        if not a.reset:
            self.st.round, self.st.turn_no = self.st.round + 1, 0

    def _do_ResetSilence(self, a: ResetSilence) -> None:
        self.s.silence.tries = 0

    def _do_SetWatch(self, a: SetWatch) -> None:
        self.s.watch[a.watch.worker] = a.watch

    def _do_SetMilestones(self, a: SetMilestones) -> None:
        old = {m.id: m for m in self.s.milestones}
        new = list(a.milestones)
        for m in new:
            was = old.pop(m.id, None)
            if was:
                m.held_decision = was.held_decision
                if m.id in self.reviews:
                    m.notified = was.notified
        self.s.milestones = new + list(old.values())  # an id never disappears (MS-12)

    def _do_SetFinished(self, a: SetFinished) -> None:
        self.s.finished = a.finished

    def _do_SetDiscussion(self, a: SetDiscussion) -> None:
        if a.discussion is None:
            self.s.discussions.pop(a.agent, None)
            return
        self.s.discussions[a.agent] = a.discussion
        t = self.open_turn(a.agent)
        if t:
            t.draft = True
            self._draft(t)

    def _do_OpenTerminal(self, a: OpenTerminal) -> None:
        self.s.terminals[a.terminal.agent] = replace(a.terminal, opened=a.terminal.opened or self.now)

    def _do_CloseTerminal(self, a: CloseTerminal) -> None:
        self.s.terminals.pop(a.agent, None)

    def intend(self, cause: str, actions: Sequence[Action], again: bool = False) -> List[Intent]:
        """One intent per action the engine performs. again: the cause may have been here before a crash."""
        out = []
        for n, a in enumerate(actions):
            if not isinstance(a, ENGINE_ACTIONS) or (isinstance(a, Send) and a.message.recipient == USER):
                continue
            key = f"{cause}#{n}"
            while key in self.st.intents and not again:  # two ticks at one time
                key += "'"
            out.append(self.st.intents.setdefault(key, Intent(key, a, created=self.now)))
        return out

    # ------------------------------------------------------------------------------------- turn files

    def _count(self, t: Turn, a: Agent, ev: TurnEnded, ref: Optional[SessionRef]) -> None:
        """Counts and costs. Some CLIs report a session's running total: a turn costs the increase."""
        counts, sid = self.st.turn_counts, ev.session_id or (ref.session_id if ref else "")
        counts["total"] = counts.get("total", 0) + 1
        counts[str(t.status)] = counts.get(str(t.status), 0) + 1
        cost, total = ev.meta.get("turn_cost_usd"), ev.meta.get("total_cost_usd")
        if cost is None and isinstance(total, (int, float)):
            before = self.st.session_costs.get(sid, 0.0) if sid else 0.0
            cost = ev.meta["turn_cost_usd"] = round(total - before if total >= before else total, 6)
            if sid:
                self.st.session_costs[sid] = total
        if isinstance(cost, (int, float)):
            self.st.cost_usd = round(self.st.cost_usd + cost, 6)
            if self._basis(a, ev) == "estimate":
                self.st.cost_estimate_usd = round(self.st.cost_estimate_usd + cost, 6)

    def _basis(self, a: Agent, ev: TurnEnded) -> str:
        return cost_basis(a.choice.backend, [a.choice.model] + list(ev.meta.get("models") or []),
                          endpoint_host(a.choice.env), ev.detail)

    def _file(self, t: Turn, a: Agent, ev: Optional[TurnEnded] = None) -> None:
        """The turn's record, with today's fields."""
        s, ref = self.s, self._session(a)
        choice = a.switch.target if a.switch and ref is a.switch.new and ref else a.choice
        rnd, no = re.match(r"r(\d+)-t(\d+)-", t.id).groups()
        taken = [s.messages[i] for i in t.started_by + t.joined if i in s.messages]
        # what it was given itself, not what an unfinished turn before it was
        given = [m.rendered or m.body for m in sorted(taken, key=lambda m: m.seq) if m.reply_turn == t.id]
        sid = (ev.session_id if ev else "") or (ref.session_id if ref else "")
        n = self.st.session_turns.get(sid, 0) if sid else 0
        rec: Dict[str, Any] = {
            "turn_id": t.id, "round": int(rnd), "seq": int(no), "agent": t.agent,
            "role": "verifier" if a.role == Role.CRITIC else str(a.role), "purpose": self._purpose(t, a),
            "backend": choice.backend, "model": a.actual_model or choice.model, "cwd": ref.cwd if ref else a.cwd,
            "endpoint": endpoint_host(choice.env), "session_id": sid or None, "resumed_session": n > 0,
            "session_turn": n + 1, "status": str(t.status), "started_at": _iso(t.started), "ended_at": _iso(t.ended),
            "duration_s": round(t.ended - t.started, 2) if t.ended else None,
            "system_prompt": self.store.system_prompts.get(a.id, ""), "input": "\n\n".join(given), "output": t.text,
            "milestone": next((i for i, m in enumerate(s.milestones) if m is s.milestone), None),
            "communication_protocol": PROTOCOL, "triggers": [str(x) for x in t.triggers],
            "messages": t.started_by + t.joined, "jobs": t.jobs, "draft": t.draft}
        if ev:
            rec.update(meta=ev.meta, stderr=t.detail, error=str(t.error) if t.error else None,
                       ending=str(t.ending), cost_basis=self._basis(a, ev))
            if sid:
                self.st.session_turns[sid] = n + 1
        self.files.append(rec)

    def _purpose(self, t: Turn, a: Agent) -> str:
        if t.triggers == [Trigger.USER_CHAT]:
            return "chat"
        if a.role == Role.MANAGER:
            return ("review" if TextKey.CHECKLIST_COMPLETE in t.keys else "plan" if TextKey.PLAN_REQUEST in t.keys
                    else "message")
        return {Role.CRITIC: "verify", Role.WORKER: "work", Role.MONITOR: "check"}.get(a.role, "audit")

    # ----------------------------------------------------------------------------------------- finish

    def status(self) -> None:
        for i in self.s.agents:
            status = self.s.status_of(i)
            if self.s.agents[i].status != status:
                self.agent(i).status = status

    def leaving(self) -> List[Tuple[str, Dict[str, Any]]]:
        """What leaves the state: [(archive file, record)]."""
        s, st, out = self.s, self.st, []
        need = {m.continues for m in s.messages.values()
                if m.drop is None and m.status in (MessageStatus.WAITING, MessageStatus.DELIVERED)}
        for a in s.agents.values():
            need |= {a.retry.turn if a.retry else "", a.switch.turn if a.switch else ""}
        current = {t.id for t in s.turns.values()}
        st.kept = {i: t for i, t in st.kept.items() if i in need and i not in current}

        turns = list(s.turns.values()) + list(st.kept.values())
        used = {i for t in turns for i in t.started_by + t.joined}
        for a in s.agents.values():
            used |= {a.open_report, a.assignment.message_id if a.assignment else ""}
            if a.switch:
                w = a.switch
                used |= {w.handover_request, w.handover_message, w.first_message, *w.unfinished}
        used |= {d.apply_message for d in s.discussions.values()} | {w.asked for w in s.watch.values()}
        ids = {t.id for t in turns}
        closed = [m for m in s.messages.values() if m.id not in used and m.turn not in ids and not m.open
                  and (m.drop is not None or m.status == MessageStatus.ANSWERED
                       or (m.status == MessageStatus.DELIVERED and (m.recipient == USER or m.taken_at)))]
        # (a notice its agent has not taken in yet is needed: it may be in doubt later)
        for m in closed[:-KEEP_MESSAGES]:
            del s.messages[m.id]
            out.append(("ledger", to_dict(m)))
        for g in self._oldest([g for g in s.gates.values() if not g.is_open and not (
                g.is_answer_gate and g.answer and not g.sent_in and not g.withdrawn)], KEEP_GATES):
            del s.gates[g.id]
            out.append(("archive", {"gate": to_dict(g)}))
        busy = {i for t in turns for i in t.jobs}
        for j in self._oldest([j for j in s.jobs.values() if not j.open and j.id not in busy], KEEP_JOBS):
            del s.jobs[j.id]
            out.append(("archive", {"job": to_dict(j)}))
        for i in [i for i, x in st.intents.items() if x.state != IntentState.PLANNED][:-KEEP_INTENTS]:
            del st.intents[i]
        if len(st.handled) > KEEP_KEYS:
            half = KEEP_KEYS // 2
            out.append(("archive", {"keys": st.handled[:half]}))
            st.handled = st.handled[half:]
        return out

    @staticmethod
    def _oldest(items: List[Any], keep: int) -> List[Any]:
        return sorted(items, key=lambda x: getattr(x, "closed", 0) or getattr(x, "ended", 0))[:-keep]


# ---------------------------------------------------------------------------------------------------- the store


class Store:
    """parse = labels.parse, read_checklist = milestones.read_checklist, read_board = golden.read_board; the engine
    passes them in. readonly: no lock, no writes (the dashboard of a run whose Orch is closed)."""

    def __init__(self, run_dir: Any, clock: Clock, parse: Optional[Callable[[str, Role], Reply]] = None,
                 read_checklist: Optional[Callable[..., Checklist]] = None,
                 read_board: Optional[Callable[..., GoldenBoard]] = None, readonly: bool = False):
        self.run_dir, self.clock, self.readonly = Path(run_dir), clock, readonly
        self.parse = parse or (lambda text, role: Reply(text=text, untagged=text))
        self.read_checklist, self.read_board = read_checklist, read_board
        self.system_prompts: Dict[str, str] = {}  # by agent, for the turn files; the engine fills it
        self.old_messages: Set[str] = set()  # ids in the ledger
        self.old_gates: Set[str] = set()
        self.old_jobs: Set[str] = set()
        self._keys: Set[str] = set()  # every event ever handled
        self._mutex = threading.RLock()
        self._dirty = False  # progress that is not saved yet
        self._status = "running"
        self._rec: Optional[Recorder] = None
        if not readonly:
            self._claim()
        try:
            self.state = self._load()
            self._load_archives()
        except BaseException:
            self._release()
            raise
        if not readonly:
            self.state.snap.orch_started = clock.now()

    @classmethod
    def create(cls, run_dir: Any, clock: Clock, snap: Snapshot, **kw: Any) -> "Store":
        """Start the record of a run (also of a migrated one: turn files in it are continued, its costs kept)."""
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        old = _read_json(run / "state.json")
        if old is None and (run / "state.json").exists():
            raise StoreError(f"cannot read {run / 'state.json'}")
        old = old or {}
        if "runtime" in old or "runtime" in (_read_json(run / "state.prev.json") or {}):
            raise StoreError(f"{run} has a record already")
        rounds = [int(m.group(1)) for m in (re.match(r"r(\d+)$", p.name) for p in (run / "rounds").glob("r*")) if m]
        st = State(snap=replace(_take(snap), checklist=Checklist()), round=max(rounds, default=0) + 1)
        st.seq = max((m.seq for m in st.snap.messages.values()), default=0)
        st.turn_counts = dict(old.get("turn_counts") or {})
        st.session_costs = dict(old.get("session_costs") or {})
        st.cost_usd, st.cost_estimate_usd = old.get("cost_usd") or 0.0, old.get("cost_estimate_usd") or 0.0
        for i in st.snap.agents:
            st.snap.agents[i].status = st.snap.status_of(i)
        _write(run, _dump(st, "running", clock.now()))
        return cls(run, clock, **kw)

    def close(self) -> None:
        """Orch closes; agents and jobs go on."""
        if not self.readonly and self._rec:
            with self._mutex:
                self._status = "stopped"
                self._write(self.state)
            self._release()

    # ------------------------------------------------------------------------------------- one event

    @property
    def pending(self) -> Optional[Event]:
        """The event that was begun and not decided when Orch stopped."""
        return self.state.pending

    def handled(self, key: str) -> bool:
        return key in self._keys

    def begin(self, event: Event) -> Optional[Event]:
        """Save the event as pending with its facts; returns it as the modules are to see it (without secrets). None: it
        was handled before. Progress is not saved one by one and a Tick not at all."""
        with self._mutex:
            st = self.state
            if st.pending and st.pending.key == event.key:
                return st.pending
            if self.handled(event.key):
                return None
            if st.pending:
                raise StoreError(f"{st.pending.key} is not decided yet")
            ev = _take(event)
            with self._change() as c:
                c.save = not isinstance(ev, PASSING)
                if c.save:
                    c.st.pending = ev
                c.facts(ev)
            return ev

    def apply(self, event: Event, actions: Sequence[Action]) -> List[Intent]:
        """The actions of all modules about this event, in one save with their intents and "handled"."""
        with self._mutex:
            passing = isinstance(event, PASSING)
            if not passing and self.handled(event.key):
                return []
            if not passing and (self.state.pending is None or self.state.pending.key != event.key):
                raise StoreError(f"{event.key} was not begun")
            if passing and not actions and not (self._dirty and isinstance(event, Tick)):
                return []
            own = [_take(a) for a in actions]
            with self._change() as c:
                c.actions(own)
                new = c.intend(event.key, own)
                if not passing:
                    c.st.pending = None
                    c.st.handled.append(event.key)
            if not passing:
                self._keys.add(event.key)
            return new

    def act(self, cause: str, actions: Sequence[Action]) -> List[Intent]:
        """Actions of the engine itself while it performs something; once per cause (the intent's id), so doing it again
        after a crash adds nothing."""
        with self._mutex:
            own = [_take(a) for a in actions]
            with self._change() as c:
                c.actions(own)
                return [i for i in c.intend(cause, own, again=True) if i.state == IntentState.PLANNED]

    def unfinished(self) -> List[Intent]:
        """Intents still to be performed, oldest first."""
        return [i for i in self.state.intents.values() if i.state == IntentState.PLANNED]

    def intent_done(self, intent_id: str, error: str = "") -> None:
        with self._mutex, self._change() as c:
            i = c.st.intents.get(intent_id)
            if i:
                c.st.intents[intent_id] = replace(i, state=IntentState.FAILED if error else IntentState.DONE,
                                                  done=c.now, error=redact(error))

    # --------------------------------------------------------------------------------------- sending

    def can_send(self, m: Message) -> bool:
        s = self.state.snap
        a = s.agents.get(m.recipient)
        if a is None or not a.is_open or not m.sendable or set(a.holds) - set(m.bypass):
            return False
        t = s.turns.get(a.id)
        return not (m.when_idle and ((t and t.status == TurnStatus.RUNNING) or any(
            x.recipient == a.id and x.open and x.sent_at for x in s.messages.values())))

    def to_send(self) -> List[Message]:
        """What may go now, in order."""
        return [m for m in self.state.snap.messages.values() if self.can_send(m)]

    def begin_send(self, message_id: str, rendered: str, given: Optional[Dict[str, str]] = None) -> bool:
        """Saved right before Adapter.send(). False: it may not go (held, closed or sent meanwhile): do not send."""
        with self._mutex:
            m = self.state.snap.messages.get(message_id)
            if m is None or not self.can_send(m):
                return False
            with self._change() as c:
                m = c.message(message_id)
                m.rendered, m.given, m.sent_at, m.error = rendered, dict(given or {}), c.now, ""
                m.attempts += 1
            return True

    def in_doubt(self, agent: str) -> List[Message]:
        """Handed to the CLI, and no word of it since: ask arrived()."""
        return [m for m in self.state.snap.waiting_for(agent) if m.sent_at]

    def not_arrived(self, message_id: str) -> None:
        """arrived() said no: it may go again."""
        with self._mutex, self._change() as c:
            m = c.message(message_id)
            if m and m.drop is None and not m.taken_at and m.status in (MessageStatus.WAITING, MessageStatus.DELIVERED):
                m.status, m.sent_at, m.checked_at, m.delivered_at = MessageStatus.WAITING, 0.0, 0.0, 0.0

    # -------------------------------------------------------------------------- what the engine writes

    def configure(self, **kw: Any) -> None:
        """run, settings, catalog, night, orch_started; golden_path."""
        with self._mutex, self._change() as c:
            if "golden_path" in kw:
                c.s.golden.path = kw.pop("golden_path")
            for k, v in kw.items():
                if k not in ("run", "settings", "catalog", "night", "orch_started"):
                    raise StoreError(f"not the engine's: {k}")
                setattr(c.s, k, [_take(x) for x in v] if isinstance(v, (list, tuple)) else
                        _take(v) if hasattr(v, "__dataclass_fields__") else v)

    def add_agent(self, agent: Agent) -> None:
        """An agent of the config; a known id stays as it is."""
        with self._mutex, self._change() as c:
            c._do_AddWorker(AddWorker(agent=_take(agent)))

    def set_agent(self, agent: str, **kw: Any) -> None:
        """caps and what the config says: choices, display, cwd, fresh_session."""
        with self._mutex, self._change() as c:
            a = c.known(agent)
            for k, v in kw.items():
                if k not in ("caps", "choices", "display", "cwd", "fresh_session"):
                    raise StoreError(f"not the engine's: {k}")
                setattr(a, k, [_take(x) for x in v] if k == "choices" else _take(v) if k == "caps" else v)

    def set_proc(self, agent: str, proc: Optional[ProcRef]) -> None:
        """The CLI Adapter.start() opened (or None). Another process than before: messages it had are in doubt. A
        reconnect to the same one keeps the read position."""
        with self._mutex, self._change() as c:
            a = c.known(agent)
            old, new = a.proc, _take(proc) if proc else None
            if new:
                new.endpoint = redact(new.endpoint)
            if old and new and (old.pid, old.started, old.stream) == (new.pid, new.started, new.stream):
                new.read_pos = old.read_pos or new.read_pos
            else:
                c._doubt(agent)
            a.proc = new

    # --------------------------------------------------------------------------------------- reading

    def snapshot(self) -> Snapshot:
        """The state now, with the checklist and the golden board as they are on disk. Never changes afterwards; the
        records are the store's own, so nobody may change them either."""
        s = self.state.snap
        ms, checklist, golden = s.milestone, Checklist(), s.golden
        if ms and ms.checklist_path and self.read_checklist:
            checklist = self.read_checklist(ms.checklist_path, self._text(ms.checklist_path))
        if golden.path and self.read_board:
            golden = self.read_board(golden.path, self._text(golden.path), golden)
        return replace(s, now=self.clock.now(), checklist=checklist, golden=golden)

    def _text(self, path: str) -> Optional[str]:
        try:
            return Path(self.state.snap.run.workspace, path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def message(self, message_id: str) -> Optional[Message]:
        """Any message of the run."""
        m = self.state.snap.messages.get(message_id)
        if m is None and message_id in self.old_messages:
            rows = [r for r in _read_lines(self.run_dir / "ledger.jsonl") if r.get("id") == message_id]
            m = from_dict(Message, rows[-1]) if rows else None
        return m

    def messages(self, before: int = 0, limit: int = 50, q: str = "", agent: str = "") -> Tuple[List[Message], bool]:
        """The newest `limit` messages with seq < before (0: no limit), in order, and whether there are older ones."""
        rows: Dict[str, Any] = {r["id"]: r for r in _read_lines(self.run_dir / "ledger.jsonl") if r.get("id")}
        rows.update(self.state.snap.messages)
        out = []
        for m in rows.values():
            m = from_dict(Message, m) if isinstance(m, dict) else m
            if (not before or m.seq < before) and (not agent or agent in (m.sender, m.recipient)) \
                    and q.lower() in f"{m.body}\n{m.rendered}".lower():
                out.append(m)
        out.sort(key=lambda m: m.seq)
        return out[-limit:] if limit else out, bool(limit) and len(out) > limit

    # --------------------------------------------------------------------------------------- writing

    @contextmanager
    def _change(self) -> Iterator[_Change]:
        """The change becomes the state when the block ended and its write succeeded, else nothing happened. Order of
        the writes: archives and turn files first (doing that again after a crash changes nothing), then state.json."""
        if self.readonly or not self._rec:
            raise StoreError("the record is not open for writing")
        c = _Change(self, self.clock.now())
        yield c
        c.status()
        if c.save:
            self._archive(c.leaving())
            for rec in c.files:
                self._write_turn(rec)
            self._write(c.st)
            self._dirty = False
        else:
            self._dirty = self._dirty or bool(c.owned)
        self.state = c.st

    def _write(self, st: State) -> None:
        _write(self.run_dir, _dump(st, self._status, self.clock.now()))

    def _write_turn(self, rec: Dict[str, Any]) -> None:
        d = self._rec.round_dir(rec["round"])
        stem = f"t{rec['seq']:02d}-{rec['agent']}"
        _replace(d / f"{stem}.json", json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        _replace(d / f"{stem}.input.md", rec["input"])
        if rec["status"] != TurnStatus.RUNNING:
            _replace(d / f"{stem}.output.md", rec["output"])

    def _archive(self, rows: List[Tuple[str, Dict[str, Any]]]) -> None:
        for name in ("ledger", "archive"):
            mine = [r for n, r in rows if n == name]
            if not mine:
                continue
            with open(self.run_dir / f"{name}.jsonl", "ab+") as f:
                if f.seek(0, os.SEEK_END):  # a line torn by a crash must not swallow the next one
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in mine)
                        .encode("utf-8", "replace"))
                f.flush()
                os.fsync(f.fileno())
            for r in mine:
                self._archived(name, r)

    def _archived(self, name: str, r: Dict[str, Any]) -> None:
        if name == "ledger":
            self.old_messages.add(r.get("id", ""))
        elif "gate" in r:
            self.old_gates.add(r["gate"]["id"])
        elif "job" in r:
            self.old_jobs.add(r["job"]["id"])
        else:
            self._keys.update(r.get("keys") or [])

    # --------------------------------------------------------------------------------------- opening

    def _claim(self) -> None:
        if not (self.run_dir / "state.json").exists() and not (self.run_dir / "state.prev.json").exists():
            raise StoreError(f"no record in {self.run_dir}")
        self._rec = Recorder(self.run_dir, self.run_dir.parent.parent)
        holder = self._rec.claim()
        if holder:
            self._rec = None
            raise StoreError(f"another Orch has this run open: {holder}")

    def _release(self) -> None:
        if self._rec:
            self._rec.release()
            self._rec = None

    def _load(self) -> State:
        path, prev = self.run_dir / "state.json", self.run_dir / "state.prev.json"
        st = _read_state(path)
        if st:
            return st
        if "runtime" not in (_read_json(path) or {"runtime": 0}):
            raise StoreError(f"{path} is of an older Orch: it must be migrated first")
        st = _read_state(prev)
        if st is None:
            raise StoreError(f"cannot read {path}")
        if not self.readonly and path.exists():
            os.replace(path, self.run_dir / "state.broken.json")  # so it never becomes state.prev.json
        # One write is lost; it may be the one before a transmission. So nothing that could have gone out counts as not
        # sent: the engine asks arrived() first (MSG-6).
        for m in st.snap.messages.values():
            if m.sendable and m.recipient != USER:
                m.sent_at = m.created or self.clock.now()
        return st

    def _load_archives(self) -> None:
        for name in ("ledger", "archive"):
            for r in _read_lines(self.run_dir / f"{name}.jsonl"):
                self._archived(name, r)
        self._keys.update(self.state.handled)


def _dump(st: State, status: str, now: float) -> str:
    """state.json: `runtime`, and what the readers of old runs look at."""
    s = st.snap
    sessions = {}
    for a in s.agents.values():
        t = s.turns.get(a.id)
        sid = a.session.session_id if a.session else ""
        sessions[a.id] = {"session_id": sid or None, "role": "verifier" if a.role == Role.CRITIC else str(a.role),
                          "backend": a.choice.backend, "model": a.choice.label, "session_key": a.session_key,
                          "turns": st.session_turns.get(sid, 0), "last_turn": t.id if t else None,
                          "last_status": str(t.status) if t else None}
    counts = {"total": 0, "ok": 0, "error": 0, "timeout": 0, "stopped": 0, **st.turn_counts}
    runtime = to_dict(st)
    runtime["snap"].pop("checklist")  # read anew
    return json.dumps({
        "monitor": s.run.project, "run_id": s.run.id, "run_dir": s.run.run_dir, "mode": "managed",
        "status": "completed" if s.finished else status, "goal": s.run.goal, "workspace": s.run.workspace,
        "working_dir": s.run.workspace, "max_rounds": s.settings.round_limit, "started_at": _iso(s.run.started),
        "updated_at": _iso(now), "ended_at": None, "round": st.round, "sessions": sessions, "rounds": [],
        "turn_counts": counts, "cost_usd": st.cost_usd, "cost_estimate_usd": st.cost_estimate_usd,
        "runtime_version": CONTRACT_VERSION, "runtime": runtime}, ensure_ascii=False, default=str)


def _replace(path: Path, text: str) -> None:
    """The whole file or nothing, also when the computer stops."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8", "replace"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write(run_dir: Path, text: str) -> None:
    path, prev = run_dir / "state.json", run_dir / "state.prev.json"
    if path.exists():
        link = run_dir / "state.prev.json.tmp"
        try:
            if link.exists():
                link.unlink()
            os.link(path, link)
            os.replace(link, prev)
        except OSError:
            pass  # a file system without links: no second copy
    _replace(path, text)
    try:
        fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _read_state(path: Path) -> Optional[State]:
    """None: missing, torn, or not of this runtime."""
    d = (_read_json(path) or {}).get("runtime")
    if not isinstance(d, dict) or "snap" not in d:
        return None
    try:
        intents = {}
        for i, x in (d.get("intents") or {}).items():
            try:
                intents[i] = from_dict(Intent, x)
            except ValueError:  # an action of another version of Orch
                intents[i] = from_dict(Intent, {**x, "action": None, "state": IntentState.FAILED.value})
        try:
            pending = from_dict(Event, d["pending"]) if d.get("pending") else None
        except ValueError:
            pending = None
        st = from_dict(State, {**d, "intents": {}, "pending": None})
        st.intents, st.pending = intents, pending
        return st
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _read_lines(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        lines = path.read_bytes().split(b"\n")
    except OSError:
        return
    for ln in lines:
        try:
            r = json.loads(ln) if ln.strip() else None
        except ValueError:  # torn by a crash
            continue
        if isinstance(r, dict):
            yield r
