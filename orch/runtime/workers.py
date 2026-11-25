"""Workers (WRK-2, 5, 6): the user adds, closes, reopens and deletes workers; nothing else changes the registry. Pure.

Closing or deleting a worker that has a running job asks the user (finish or stop). A closed worker is never revived
by late messages (WRK-4): what waits for it is dropped. Adding or reopening sends nothing: the critic sees it in its
next reminder (WRK-6).
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

from .contracts import (
    Action, Agent, AddWorker, CommandReply, Drop, DropReason, Event, JobsChoice, Lifecycle, Message, MessageStatus,
    ORCH, Refusal, Role, Send,
    SetLifecycle, Snapshot, StopJob, Tell, TextKey, TextRef, Trigger, USER, WorkerAdd, WorkerClose, WorkerDelete,
    WorkerReopen, new_id)

NAME = "workers"
_ASK = {WorkerClose: TextKey.ASK_JOBS_CLOSE, WorkerDelete: TextKey.ASK_JOBS_DELETE}


def decide(snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
    if isinstance(event, WorkerAdd):
        return _add(snap, event)
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


def _add(snap: Snapshot, cmd: WorkerAdd) -> List[Action]:
    """WRK-2. Set up like worker `like`, or like the first one."""
    like = snap.agents.get(cmd.like) if cmd.like else next(iter(snap.by_role(Role.WORKER)), None)
    if like is None or like.role != Role.WORKER:
        return _refuse(cmd, Refusal.UNKNOWN)
    wid = cmd.agent or _next_id(snap)
    if wid in snap.agents:
        return _refuse(cmd, Refusal.ALREADY)
    choices = [c for c in like.choices] or [like.choice]
    agent = Agent(wid, Role.WORKER, wid, like.cwd, "", like.fresh_session, Lifecycle.OPEN, choice=choices[0],
                  choices=list(choices))
    return [AddWorker(agent, like.id), CommandReply(cmd.id, data={"agent": wid})]


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

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return decide(snap, event, prior)


def module() -> _Module:
    return _Module()
