"""Workers (WRK-2, 5, 6): the user adds, closes, reopens and deletes workers; nothing else changes the registry. Pure.

Closing or deleting a worker that has a running job asks the user (finish or stop). A closed worker is never revived
by late messages (WRK-4): what waits for it is dropped. Adding or reopening sends nothing: the critic sees it in its
next reminder (WRK-6).
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Sequence

from .contracts import (
    Action, Agent, AddWorker, CommandReply, Drop, DropReason, Event, JobsChoice, Lifecycle, Message, MessageStatus,
    ORCH, Refusal, Role, SaveChoice, Send,
    SetLifecycle, Snapshot, StopJob, Tell, TextKey, TextRef, Trigger, USER, WorkerAdd, WorkerClose, WorkerDelete,
    WorkerReopen, new_id)

NAME = "workers"
_ASK = {WorkerClose: TextKey.ASK_JOBS_CLOSE, WorkerDelete: TextKey.ASK_JOBS_DELETE}


def decide(snap: Snapshot, event: Event, prior: Sequence[Action] = (),
           models: Optional[Dict[str, Any]] = None) -> List[Action]:
    if isinstance(event, WorkerAdd):
        return _add(snap, event, models)
    if isinstance(event, (WorkerClose, WorkerDelete)):
        return _end(snap, event)
    if isinstance(event, WorkerReopen):
        return _reopen(snap, event)
    # WRK-4: nothing another module sends now may reach a worker that is not open
    return [Drop(a.message.id, DropReason.WORKER_CLOSED) for a in prior
            if isinstance(a, Send) and a.message.recipient in snap.agents
            and snap.agents[a.message.recipient].role == Role.WORKER and not snap.agents[a.message.recipient].is_open]


def _refuse(cmd: Any, why: Refusal) -> List[Action]:
    return [CommandReply(cmd.id, False, TextRef(TextKey.COMMAND_REFUSED, {"why": why}))]


def _worker(snap: Snapshot, cmd: Any) -> Optional[Agent]:
    a = snap.agents.get(cmd.agent)
    return a if a is not None and a.role == Role.WORKER else None


def _next_id(snap: Snapshot) -> str:
    """Ids are never reused: one more than the highest ever, deleted workers included."""
    nums = [int(m.group(1)) for a in snap.agents if (m := re.fullmatch(r"worker-(\d+)", a))]
    return f"worker-{max(nums, default=0) + 1}"


def _add(snap: Snapshot, cmd: WorkerAdd, models: Optional[Dict[str, Any]] = None) -> List[Action]:
    """WRK-2. Set up like worker `like`, or like the first one."""
    like = snap.agents.get(cmd.like) if cmd.like else next(iter(snap.by_role(Role.WORKER)), None)
    if like is None or like.role != Role.WORKER:
        return _refuse(cmd, Refusal.UNKNOWN)
    wid = cmd.agent or _next_id(snap)
    reserved = {USER, ORCH} | {a.id for a in snap.agents.values() if a.role != Role.WORKER}
    if not re.fullmatch(r"[a-z0-9-]+", wid) or wid in reserved:
        return _refuse(cmd, Refusal.NOT_POSSIBLE)
    if wid in snap.agents:
        return _refuse(cmd, Refusal.ALREADY)
    choices = [c for c in like.choices] or [like.choice]
    picked = None
    if cmd.model_id:
        picked = _picked(snap, cmd, models or {})
        if picked is None:
            return _refuse(cmd, Refusal.UNKNOWN)
        choices = [picked] + choices[1:]   # its own main model; the backups of `like` stay
    agent = Agent(wid, Role.WORKER, wid, like.cwd, "", like.fresh_session, Lifecycle.OPEN, choice=choices[0],
                  choices=list(choices))
    acts: List[Action] = [AddWorker(agent, like.id)]
    if picked is not None:
        acts.append(SaveChoice(wid, picked))   # its agent.yaml names the chosen model
    return acts + [CommandReply(cmd.id, data={"agent": wid})]


def _picked(snap: Snapshot, cmd: WorkerAdd, models: Dict[str, Any]) -> Optional[Any]:
    """The catalog model asked for, with its effort; None when it is not listed or the effort does not fit it."""
    base = models.get(cmd.model_id)
    if base is None:
        return None
    c = copy.deepcopy(base)
    if cmd.effort:
        info = next((m for m in snap.catalog if m.id == cmd.model_id), None)
        if info is not None and cmd.effort not in info.efforts:
            return None
        c.effort = cmd.effort
    return c


def _end(snap: Snapshot, cmd: Any) -> List[Action]:
    """WRK-5: close or delete. With a running job: ask first; finish lets it run, stop ends it and tells the critic."""
    a = _worker(snap, cmd)
    if a is None:
        return _refuse(cmd, Refusal.UNKNOWN)
    target = Lifecycle.CLOSED if isinstance(cmd, WorkerClose) else Lifecycle.DELETED
    if a.lifecycle == Lifecycle.DELETED:
        return _refuse(cmd, Refusal.DELETED)
    if a.lifecycle == target:
        return _refuse(cmd, Refusal.ALREADY)
    running = snap.running_jobs(a.id)
    if running and cmd.jobs is None:
        choices = tuple({"label": c.value, "set": {"jobs": c.value}} for c in (JobsChoice.FINISH, JobsChoice.STOP))
        return [CommandReply(cmd.id, False, TextRef(_ASK[type(cmd)], {"worker": a.id,
                                                                      "jobs": [j.label or j.id for j in running]}),
                             choices)]
    acts: List[Action] = []
    if running and cmd.jobs == JobsChoice.STOP:
        acts += [StopJob(j.id) for j in running]
        if snap.critic:
            acts.append(Tell(_notice(cmd, snap.critic.id, a.id, [j.label or j.id for j in running])))
    acts.append(SetLifecycle(a.id, target))
    # WRK-4: what is still on its way to it is recorded, never sent; late results of it are not routed
    acts += [Drop(m.id, DropReason.WORKER_CLOSED) for m in snap.messages.values()
             if m.recipient == a.id and (m.open or m.status == MessageStatus.WAITING) and m.drop is None]
    acts.append(CommandReply(cmd.id))
    return acts


def _notice(cmd: Any, critic: str, worker: str, jobs: List[str]) -> Message:
    return Message(new_id(NAME, cmd), ORCH, critic, "", TextRef(TextKey.JOBS_STOPPED, {"worker": worker, "jobs": jobs}),
                   False, Trigger.ORCH)


def _reopen(snap: Snapshot, cmd: WorkerReopen) -> List[Action]:
    """WRK-3, 6: the saved session goes on with the next message; the critic is not woken."""
    a = _worker(snap, cmd)
    if a is None:
        return _refuse(cmd, Refusal.UNKNOWN)
    if a.lifecycle == Lifecycle.DELETED:
        return _refuse(cmd, Refusal.DELETED)
    if a.is_open:
        return _refuse(cmd, Refusal.ALREADY)
    return [SetLifecycle(a.id, Lifecycle.OPEN), CommandReply(cmd.id)]


class _Module:
    name = NAME

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        from .models import catalog_choices
        self.models = catalog_choices(settings) if settings else {}   # for a new worker's own model

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return decide(snap, event, prior, self.models)


def module(settings: Optional[Dict[str, Any]] = None) -> _Module:
    return _Module(settings)
