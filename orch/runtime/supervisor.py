"""Supervisor (SIL-1..5, MSG-5, JOB-2, 5..8, REC-10): everything that is decided from time and state. Pure.

Timers follow "Timers" in contracts.py. They run on Tick; a run's own start (`orch_started`) limits all of them, so
nothing counts time from before this Orch began. Lost jobs are found when the engine reports JobsChecked.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence

from .contracts import (
    Action, Agent, AgentStatus, AskManagerToCheck, CountRound, SendToCritic, SwitchStep, Event, FailAgent, FailCause, Gate, GateCause, GateKind,
    Drop, Health, HoldReason, Job, JobsChecked, JobState, MarkLost, Message, MessageStatus, OpenGate, ORCH, ReportJob, Resend, ResetSilence,
    RestartSession, Role, Send, SetWatch, Snapshot, Tell, TextKey, TextRef, Tick, ToUser, Trigger, TurnEnded,
    TurnStatus, USER, Watch, WakeWorker, new_id)

NAME = "supervisor"
_WORKER_REPLIES = (TextKey.WORKER_FINAL, TextKey.WORKER_INTERMEDIATE)  # a chat's reply is no longer sent to the critic


def decide(snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
    if isinstance(event, TurnEnded):
        return _rounds(snap, event, prior) + _monitor_reply(snap, event)
    if isinstance(event, SendToCritic):
        return _rounds(snap, event, prior)
    if isinstance(event, JobsChecked):
        return _lost(snap, event)
    if isinstance(event, Tick):
        return _Timers(snap, event).run()
    return []


def _writing(snap: Snapshot, a: Agent) -> bool:
    t = snap.turns.get(a.id)
    return a.status == AgentStatus.WRITING or bool(t and t.status == TurnStatus.RUNNING)


def _free(snap: Snapshot, a: Agent, switching_ok: bool = False) -> bool:
    """Nothing keeps timers of this agent standing still: open, not failed, retrying, switching, held or stopped."""
    return (a.is_open and not (a.failed_why or a.retry or (a.switch and not switching_ok) or a.holds
                               or a.stopped_by_user) and not _writing(snap, a))


def _reachable(snap: Snapshot, name: str) -> Optional[Agent]:
    """The agent, if it is open and has not failed: only then something can still be answered by it."""
    a = snap.agents.get(name)
    return a if a is not None and a.is_open and not a.failed_why else None


# ------------------------------------------------------------------------------------------- events


def _rounds(snap: Snapshot, ev: Event, prior: Sequence[Action]) -> List[Action]:
    """SIL-5: each worker reply routing sends to the critic is one round; every multiple of the limit opens a gate."""
    dropped = {a.message_id for a in prior if isinstance(a, Drop)}
    ids = [a.message.id for a in prior if type(a) is Send and a.message.key in _WORKER_REPLIES
           and snap.critic is not None and a.message.recipient == snap.critic.id and a.message.id not in dropped]
    if not ids:
        return []
    acts: List[Action] = [CountRound(i) for i in ids]
    limit, before = snap.settings.round_limit, snap.rounds
    if limit > 0 and (before + len(ids)) // limit > before // limit:
        acts.append(_op_gate(GateCause.ROUND_LIMIT, "", TextKey.GATE_ROUND_LIMIT, {"rounds": before + len(ids)}, ev.at))
    return acts


def _op_gate(cause: GateCause, agent: str, key: TextKey, values: Dict[str, Any], at: float,
             kind: GateKind = GateKind.OPERATIONAL) -> OpenGate:
    return OpenGate(Gate(f"op-{cause.value}-{agent}", kind, "", TextRef(key, values), [], at, 0.0, agent, cause))


def _lost(snap: Snapshot, ev: JobsChecked) -> List[Action]:
    """JOB-7: a job recorded as running that the CLI's record no longer has ended without a result."""
    listed = {j.id for j in ev.jobs}
    return [MarkLost(j.id) for j in snap.jobs.values() if j.agent == ev.agent and j.state == JobState.RUNNING
            and j.id not in listed]


def _monitor_reply(snap: Snapshot, ev: TurnEnded) -> List[Action]:
    """REC-10: read the check's answer. Unreadable or failed twice: the critic is told. Unhealthy: told once per
    change of status, then at most hourly. Healthy: nothing."""
    mon, turn = snap.agents.get(ev.agent), snap.turns.get(ev.agent)
    if mon is None or mon.role != Role.MONITOR or turn is None or turn.status == TurnStatus.STOPPED:
        return []
    ids = set(turn.started_by + turn.joined)
    w = next((x for x in snap.watch.values() if x.asked and x.asked in ids), None)
    if w is None:
        return []
    out = _Out(ev)
    health = turn.reply.health if turn.reply is not None and turn.status == TurnStatus.OK else None
    if health is None:
        out.actions.append(SetWatch(_watch_failed(snap, out, mon, w, "unreadable" if turn.reply else "error")))
        return out.actions
    nw = replace(w, asked="", checked_at=ev.at, health=health, failures=0, told_unwatched=False)
    critic = snap.critic
    if health == Health.HEALTHY:
        nw = replace(nw, reported=Health.HEALTHY)
    elif critic and (w.reported != health or ev.at - w.reported_at >= snap.settings.report_every):
        worker = snap.agents.get(w.worker)
        minutes = int((ev.at - worker.last_output) // 60) if worker and worker.last_output else 0
        out.add(Tell, critic.id, TextKey.MONITOR_REPORT, {"worker": w.worker, "health": health, "minutes": minutes},
                needs_answer=False, body=turn.reply.text)
        nw = replace(nw, reported=health, reported_at=ev.at)
    out.actions.append(SetWatch(nw))
    return out.actions


def _watch_failed(snap: Snapshot, out: "_Out", mon: Agent, w: Watch, why: str) -> Watch:
    """One failed check: shown to the user; the second one in a row tells the critic that nobody watches."""
    failures, told, critic = w.failures + 1, w.told_unwatched, snap.critic
    out.add(ToUser, USER, TextKey.NOTE_MONITOR_FAILED, {"monitor": mon.id, "worker": w.worker, "why": why})
    if failures >= 2 and not told and critic:
        out.add(Tell, critic.id, TextKey.NOBODY_WATCHES, {"worker": w.worker}, needs_answer=False)
        told = True
    return replace(w, asked="", checked_at=snap.now, failures=failures, told_unwatched=told)


class _Out:
    """Actions of one event, with ids counted in order."""

    def __init__(self, event: Event):
        self.event, self.n, self.actions = event, 0, []  # type: Event, int, List[Action]

    def message(self, recipient: str, key: TextKey, values: Dict[str, Any], trigger: Trigger = Trigger.ORCH,
                needs_answer: bool = True, body: str = "") -> Message:
        m = Message(new_id(NAME, self.event, self.n), ORCH, recipient, body, TextRef(key, values),
                    needs_answer and recipient != USER, trigger)
        self.n += 1
        return m

    def add(self, kind: type, recipient: str, key: TextKey, values: Dict[str, Any], **kw: Any) -> Message:
        m = self.message(recipient, key, values, **kw)
        self.actions.append(kind(message=m))
        return m


# -------------------------------------------------------------------------------------------- timers


class _Timers:
    def __init__(self, snap: Snapshot, ev: Tick):
        self.snap, self.ev, self.now = snap, ev, snap.now
        self.out = _Out(ev)
        self.start = snap.orch_started

    def base(self, *times: float) -> float:
        return max([self.start] + [t for t in times if t])

    def run(self) -> List[Action]:
        self.unanswered()
        self.frozen()
        self.wakeups()
        self.no_progress()
        self.monitor()
        self.silence()
        return self.out.actions

    # MSG-5
    def unanswered(self) -> None:
        s, failed = self.snap, set()
        for m in s.messages.values():
            a = s.agents.get(m.recipient)
            if a is None or not m.open or not m.sent_at or m.holds or not _free(s, a, True) or a.id in failed:
                continue
            if self.now - self.base(m.sent_at, m.checked_at, a.idle_since) < s.settings.unanswered_after:
                continue
            if m.checked_at:  # asked and sent again already
                failed.add(a.id)
                self.out.actions.append(FailAgent(a.id, FailCause.NO_ANSWER))
            else:
                self.out.actions.append(Resend(m.id, True))

    # SIL-4: a writing agent with no output for 40 minutes and no tool call or job running. The TURN hold does not
    # stop it (a program that takes no input into a running turn is still writing); a retry counts from its wait.
    def frozen(self) -> None:
        s = self.snap
        for a in s.agents.values():
            holds = [h for h in a.holds if h != HoldReason.TURN]
            if (a.role not in (Role.MANAGER, Role.CRITIC, Role.WORKER) or not a.is_open or a.status != AgentStatus.WRITING
                    or a.tools or s.running_jobs(a.id) or a.failed_why or holds or a.stopped_by_user
                    or (a.switch and a.switch.step in (SwitchStep.TESTING, SwitchStep.OPENING))):
                continue
            quiet = self.now - self.base(a.last_output, a.frozen_restart, a.retry.until if a.retry else 0.0)
            if quiet < s.settings.frozen_after:
                continue
            if not a.frozen_restart:
                self.out.actions.append(RestartSession(a.id, True))
            elif a.role == Role.WORKER:
                self.out.actions.append(FailAgent(a.id, FailCause.FROZEN))
            else:
                g = s.gates.get(f"op-frozen-{a.id}")  # opened again once the user's close is 40 minutes old
                if g is None or (g.closed and self.now - g.closed >= s.settings.frozen_after):
                    self.out.actions.append(_op_gate(GateCause.FROZEN, a.id, TextKey.GATE_FROZEN,
                                                     {"agent": a.id, "minutes": int(quiet // 60)}, self.now))

    # JOB-5, JOB-7: one message for the jobs of a worker that wait for their wakeup
    def wakeups(self) -> None:
        s = self.snap
        for w in s.agents.values():
            if not _free(s, w):
                continue
            waiting = [j for j in s.jobs.values() if j.agent == w.id and j.state in (JobState.ENDED, JobState.LOST)
                       and not (j.woke or j.stopped_by_user or j.fallback_sent)]
            if w.caps.wakes_itself:  # it should wake by itself: give it a minute
                waiting = [j for j in waiting
                           if self.now - self.base(j.ended, w.idle_since) >= s.settings.wakeup_after]
            for key, state in ((TextKey.JOB_WAKEUP, JobState.ENDED), (TextKey.JOB_LOST, JobState.LOST)):
                group = [j for j in waiting if j.state == state]
                if not group:
                    continue
                m = self.out.message(w.id, key, {"jobs": [{"job": j.label or j.id, "exit_code": j.exit_code}
                                                          for j in group]}, Trigger.JOB)
                self.out.actions.append(WakeWorker(message=m, job_ids=tuple(j.id for j in group)))
                if w.caps.wakes_itself:  # the fallback is not the normal way here (JOB-5): the user sees it
                    self.out.add(ToUser, USER, TextKey.NOTE_WAKEUP_SENT,
                                 {"worker": w.id, "jobs": [j.label or j.id for j in group]})

    # JOB-6
    def no_progress(self) -> None:
        s, critic = self.snap, self.snap.critic
        if critic is None:
            return
        for j in s.jobs.values():
            w = s.agents.get(j.agent)
            if (j.state != JobState.RUNNING or j.reported or w is None or not w.is_open or j.stopped_by_user):
                continue
            quiet = self.now - self.base(j.started, j.last_progress)
            if quiet >= s.settings.job_quiet_after:
                self.out.actions.append(ReportJob(j.id))
                self.out.add(Tell, critic.id, TextKey.JOB_NO_PROGRESS,
                             {"worker": w.id, "job": j.label or j.id, "minutes": int(quiet // 60)}, needs_answer=False)

    # REC-10: one check at a time, of a worker that is at work; one SetWatch per worker
    def monitor(self) -> None:
        s = self.snap
        mon = next(iter(s.by_role(Role.MONITOR)), None)
        marks: Dict[str, Watch] = {}
        for w in s.watch.values():  # a check that went nowhere counts as a failure
            m = s.messages.get(w.asked) if w.asked else None
            if w.asked and (m is None or m.drop is not None or m.status == MessageStatus.FAILED):
                marks[w.worker] = _watch_failed(s, self.out, mon, w, "no_answer") if mon else replace(w, asked="")
        under_way = any(w.asked in s.messages and s.messages[w.asked].open for w in s.watch.values() if w.asked)
        if mon is not None and mon.is_open and not under_way and not _writing(s, mon):
            items = [f"- [{'x' if done else ' '}] {text}" for step in s.checklist.steps for done, text in step.items]
            for w in s.workers:
                if w.failed_why or w.stopped_by_user or w.id in marks and marks[w.id].asked:
                    continue
                watch = marks.get(w.id) or s.watch.get(w.id, Watch(w.id))
                working = _writing(s, w) or s.open_jobs(w.id)
                if not working or self.now - self.base(watch.checked_at, w.writing_since) < s.settings.check_every:
                    continue
                if mon.failed_why or not _free(s, mon):  # nobody can check: that is a failed check
                    if mon.failed_why:
                        marks[w.id] = _watch_failed(s, self.out, mon, watch, "failed")
                    continue
                minutes = int((self.now - w.last_output) // 60) if w.last_output else 0
                m = self.out.add(Send, mon.id, TextKey.MONITOR_CHECK, {
                    "worker": w.id, "status": w.status, "minutes": minutes,
                    "assignment": w.assignment.line if w.assignment else "",
                    "live_file": f"{s.run.run_dir}/live/{w.id}.jsonl", "items": items}, trigger=Trigger.MONITOR)
                marks[w.id] = replace(watch, asked=m.id, checked_at=self.now)
                break
        self.out.actions += [SetWatch(w) for w in marks.values()]

    # SIL-1, 2
    def active(self) -> bool:
        s = self.snap
        if s.discussions or s.terminals or s.open_gates():
            return True
        for a in s.agents.values():
            if a.role == Role.AUDITOR:
                continue
            if a.is_open and (_writing(s, a) or a.retry or a.switch or a.stopped_by_user or HoldReason.STOPPED in a.holds):
                return True
        for j in s.jobs.values():  # running, or ended and not yet with its agent
            a = _reachable(s, j.agent)
            if j.open and a is not None and not a.stopped_by_user:
                return True
        return any(m.open and _reachable(s, m.recipient) for m in s.messages.values())

    def work_restarted(self) -> bool:
        s = self.snap
        if any(_writing(s, a) for a in s.agents.values() if _reachable(s, a.id) and a.role in (Role.CRITIC, Role.WORKER)):
            return True
        if any(j.state == JobState.RUNNING and _reachable(s, j.agent) for j in s.jobs.values()):
            return True
        return any(m.open and (a := _reachable(s, m.recipient)) is not None and a.role in (Role.CRITIC, Role.WORKER)
                   for m in s.messages.values())

    def silence(self) -> None:
        s, sil = self.snap, self.snap.silence
        if s.paused or s.finished:
            return
        mgr = s.manager
        if sil.tries and self.work_restarted():
            self.out.actions.append(ResetSilence())
            return
        if mgr is None or not mgr.is_open or mgr.failed_why or self.active():
            return
        since = self.base(sil.asked_at, *(g.closed for g in s.gates.values()),
                          *(x for a in s.agents.values() if a.is_open for x in (a.idle_since, a.last_output)))
        if self.now - since < s.settings.silence_after:
            return
        if sil.tries < s.settings.silence_tries:
            m = self.out.message(mgr.id, TextKey.SILENCE_CHECK,
                                 {"attempt": sil.tries + 1, "attempts": s.settings.silence_tries})
            self.out.actions.append(AskManagerToCheck(message=m))
        else:  # the third try changed nothing: the user decides
            g = _op_gate(GateCause.SILENCE, "", TextKey.GATE_SILENCE, {"attempts": sil.tries}, self.now, GateKind.PAUSE)
            self.out.actions += [g, ResetSilence()]



class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return decide(snap, event, prior)


def module() -> _Module:
    return _Module()
