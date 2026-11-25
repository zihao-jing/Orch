"""Routing (LAB-1..6, MSG-7..10, GATE-2 holds, JOB-3 marking, WRK-8): where a finished reply and a chat go. Pure.

It reads the parsed `Turn.reply` (labels.parse); it never parses. Replies of the monitor and auditor, and those that
answer HANDOVER_WRITE, are not routed. What a reply holds back (a question) is held here and dropped by gates.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from .contracts import (
    Action, Agent, BrokenLabel, Chat, CommandReply, Drop, DropReason, Ending, Event, HoldReason, Hold, Lifecycle,
    Message, Problem, Refusal, Reject, ReturnToSender, Role, SaveNotes, Send, SendToCritic, Snapshot,
    Tell, TextKey, TextRef, ToUser, Trigger, TurnEnded, TurnStatus, USER, new_id)

NAME = "routing"
_USER_WORDS = ("user", "you")


def _norm(s: str) -> str:
    return re.sub(r"^the\s+", "", (s or "").strip().lower())


def _open_workers(snap: Snapshot) -> List[Agent]:
    return snap.workers


def resolve(snap: Snapshot, sender: str, to: str) -> Tuple[Optional[str], Optional[Problem]]:
    """(recipient id or USER, None), (None, NO_RECIPIENT) for an address this sender may not use, or (None, None)
    for a name that is no open worker or agent (MSG-8)."""
    want = _norm(to)
    role = snap.agents[sender].role
    if want in _USER_WORDS:
        return (USER, None) if role == Role.MANAGER else (None, Problem.NO_RECIPIENT)
    found = next((a for a in snap.agents.values() if want in (_norm(a.id), _norm(a.display))), None)
    if found is None and want in ("critic", "manager"):
        found = snap.critic if want == "critic" else snap.manager
    if found is None and want == "worker":
        found = snap.workers[0] if len(snap.workers) == 1 else None
    if found is None:
        return None, None
    if found.id == sender or found.role in (Role.MONITOR, Role.AUDITOR):
        return None, Problem.NO_RECIPIENT
    if found.role == Role.WORKER:
        return (found.id, None) if found.is_open else (None, None)
    if role == Role.CRITIC:  # the critic is never given a manager address
        return None, Problem.NO_RECIPIENT
    return found.id, None


class _Out:
    """The actions of one event, with ids counted in order."""

    def __init__(self, event: Event):
        self.event, self.n, self.actions = event, 0, []  # type: Event, int, List[Action]

    def message(self, sender: str, recipient: str, body: str = "", key: Optional[TextKey] = None,
                values: Optional[dict] = None, trigger: Trigger = Trigger.ORCH, needs_answer: bool = True,
                turn: str = "") -> Message:
        m = Message(new_id(NAME, self.event, self.n), sender, recipient, body,
                    TextRef(key, values or {}) if key else None, needs_answer and recipient != USER, trigger,
                    turn=turn)
        self.n += 1
        return m

    def add(self, kind: type, message: Message, **kw: Any) -> Message:
        self.actions.append(kind(message=message, **kw))
        return message


def decide(snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
    if isinstance(event, TurnEnded):
        return _reply(snap, event)
    if isinstance(event, Chat):
        return _chat(snap, event)
    if isinstance(event, SendToCritic):
        return _to_critic(snap, event)
    return []


# --------------------------------------------------------------------------------------------------- replies


def _reply(snap: Snapshot, ev: TurnEnded) -> List[Action]:
    agent, turn = snap.agents.get(ev.agent), snap.turns.get(ev.agent)
    if (agent is None or turn is None or turn.reply is None or turn.status != TurnStatus.OK
            or agent.role not in (Role.MANAGER, Role.CRITIC, Role.WORKER) or TextKey.HANDOVER_WRITE in turn.keys):
        return []
    out = _Out(ev)
    if agent.role == Role.WORKER:
        _worker(snap, out, agent, turn)
    else:
        _upper(snap, out, agent, turn)
    return out.actions


def _worker(snap: Snapshot, out: _Out, agent: Agent, turn) -> None:
    """LAB-2, LAB-6, WRK-4, JOB-3, WRK-8: the whole reply, to the critic unless only the user's chat started it."""
    text = turn.reply.text
    if set(turn.triggers) == {Trigger.USER_CHAT}:
        m = out.message(agent.id, USER, text, trigger=Trigger.WORKER, turn=turn.id)
        out.add(ToUser, m)
    else:
        if turn.ending == Ending.INTERMEDIATE:
            key, values = TextKey.WORKER_INTERMEDIATE, {"worker": agent.id,
                                                       "jobs": [j.label for j in snap.open_jobs(agent.id)]}
        else:
            key, values = TextKey.WORKER_FINAL, {"worker": agent.id}
        m = out.add(Send, out.message(agent.id, "critic", text, key, values, Trigger.WORKER, turn=turn.id))
        if not agent.is_open:  # recorded, never routed (WRK-4, WRK-5)
            out.actions.append(Drop(m.id, DropReason.WORKER_CLOSED))


def _upper(snap: Snapshot, out: _Out, agent: Agent, turn) -> None:
    reply, me = turn.reply, agent.id
    trigger = Trigger(agent.role.value)
    drafting = snap.drafting(me)
    held_by_question = bool(reply.questions) and not drafting and not snap.night and agent.role == Role.MANAGER
    broken = list(reply.broken)
    sent: List[Message] = []
    for om in reply.messages:
        to, problem = resolve(snap, me, om.to)
        if problem:
            broken.append(BrokenLabel(f'<message to="{om.to}">', problem, om.body))
        elif to is None:
            _reject(snap, out, agent, om.to, om.body, trigger, turn.id)
        elif to == USER:
            out.add(ToUser, out.message(me, USER, om.body, trigger=trigger, turn=turn.id))
        else:
            sent.append(out.add(Send, out.message(me, to, om.body, trigger=trigger, turn=turn.id)))
            if snap.agents[to].role == Role.WORKER and agent.role == Role.MANAGER:  # MSG-9
                sent.append(_notice(out, to, om.body))
    if held_by_question:  # GATE-2: nothing of this reply goes to the critic or workers
        out.actions += [Hold(m.id, HoldReason.QUESTION) for m in sent]
    if agent.role == Role.CRITIC:
        if reply.untagged:
            m = out.add(Send, out.message(me, "manager", reply.untagged, TextKey.CRITIC_REPORT, None, trigger,
                                          turn=turn.id))
            if reply.silent:  # LAB-4: shown on the dashboard only
                out.actions.append(Drop(m.id, DropReason.SILENT))
    else:
        if reply.untagged:
            out.add(ToUser, out.message(me, USER, reply.untagged, trigger=trigger, turn=turn.id))
        if reply.state:
            out.actions.append(SaveNotes(me, reply.state))
    if not drafting:
        _broken(snap, out, agent, turn, broken)


def _notice(out: _Out, worker: str, body: str) -> Message:
    return out.add(Tell, out.message(ORCH_ID, "critic", body, TextKey.DIRECT_MESSAGE_NOTICE, {"worker": worker},
                                     needs_answer=False))


ORCH_ID = "orch"


def _reject(snap: Snapshot, out: _Out, agent: Agent, to: str, body: str, trigger: Trigger, turn_id: str) -> None:
    """MSG-8: tell the sender who is available; with no workers the manager asks the user."""
    available = [w.id for w in snap.workers]
    key = TextKey.NO_WORKERS if not available and agent.role == Role.MANAGER else TextKey.UNKNOWN_WORKER
    values = {} if key == TextKey.NO_WORKERS else {"to": to, "available": available}
    note = out.message(ORCH_ID, agent.id, "", key, values)
    rejected = Message(new_id(NAME, out.event, out.n), agent.id, to, body, trigger=trigger, turn=turn_id)
    out.n += 1
    out.actions.append(Reject(message=note, rejected=rejected))
    if not available and agent.role == Role.CRITIC and snap.manager:
        out.add(Send, out.message(ORCH_ID, "manager", "", TextKey.NO_WORKERS))


def _broken(snap: Snapshot, out: _Out, agent: Agent, turn, broken: List[BrokenLabel]) -> None:
    """LAB-5: back to the sender once; the second time passed on, marked as broken."""
    again = TextKey.LABEL_RETURNED in turn.keys
    for b in broken:
        if not again:
            note = out.message(ORCH_ID, agent.id, b.body, TextKey.LABEL_RETURNED,
                               {"label": b.label, "problem": b.problem})
            out.actions.append(ReturnToSender(message=note, turn=turn.id))
            continue
        values = {"sender": agent.id, "label": b.label, "problem": b.problem}
        if agent.role == Role.CRITIC and snap.manager:
            out.add(Send, out.message(agent.id, "manager", b.body, TextKey.LABEL_BROKEN_PASSED, values,
                                      Trigger.CRITIC, turn=turn.id))
        elif agent.role == Role.MANAGER:
            out.add(ToUser, out.message(agent.id, USER, b.body, TextKey.LABEL_BROKEN_PASSED, values,
                                        Trigger.MANAGER, turn=turn.id))


# ----------------------------------------------------------------------------------------------------- chat


def _refuse(cmd: Any, why: Refusal) -> List[Action]:
    return [CommandReply(cmd.id, False, TextRef(TextKey.COMMAND_REFUSED, {"why": why}))]


def _chat(snap: Snapshot, cmd: Chat) -> List[Action]:
    """The user writes to an agent: at once (MSG-1); the critic is shown a message to a worker (MSG-9). Gates stay
    open and the chat closes none (GATE-5)."""
    a = snap.agents.get(cmd.agent)
    if a is None:
        return _refuse(cmd, Refusal.UNKNOWN)
    if not cmd.text.strip():
        return _refuse(cmd, Refusal.EMPTY)
    if a.lifecycle != Lifecycle.OPEN:
        return _refuse(cmd, Refusal.CLOSED if a.lifecycle == Lifecycle.CLOSED else Refusal.DELETED)
    if a.role == Role.AUDITOR:
        return _refuse(cmd, Refusal.WRONG_ROLE)
    out = _Out(cmd)
    out.add(Send, out.message(USER, a.id, cmd.text.strip(), trigger=Trigger.USER_CHAT))
    if a.role == Role.WORKER and snap.critic:
        _notice(out, a.id, cmd.text.strip())
    out.actions.append(CommandReply(cmd.id))
    return out.actions


def _to_critic(snap: Snapshot, cmd: SendToCritic) -> List[Action]:
    """LAB-6: the user passes a worker's reply on."""
    m = snap.messages.get(cmd.message_id)
    if m is None or m.sender not in snap.agents or m.recipient != USER or snap.agents[m.sender].role != Role.WORKER:
        return _refuse(cmd, Refusal.UNKNOWN)
    if snap.critic is None:
        return _refuse(cmd, Refusal.UNKNOWN)
    if any(x.key == TextKey.WORKER_TO_CRITIC and x.turn == m.turn and x.sender == m.sender and x.drop is None
           for x in snap.messages.values()):
        return _refuse(cmd, Refusal.ALREADY)
    out = _Out(cmd)
    out.add(Send, out.message(m.sender, "critic", m.body, TextKey.WORKER_TO_CRITIC,
                              {"worker": m.sender, "question": _asked(snap, m)}, Trigger.WORKER, turn=m.turn))
    out.actions.append(CommandReply(cmd.id))
    return out.actions


def _asked(snap: Snapshot, reply: Message) -> str:
    """The user's chat message the reply answers."""
    turn = snap.turns.get(reply.sender)
    ids = (turn.started_by + turn.joined) if turn and turn.id == reply.turn else []
    chats = [snap.messages[i] for i in ids if i in snap.messages and snap.messages[i].sender == USER]
    if not chats:
        chats = [x for x in snap.messages.values()
                 if x.sender == USER and x.recipient == reply.sender and x.seq < reply.seq]
    return chats[-1].body if chats else ""


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return decide(snap, event, prior)


def module() -> _Module:
    return _Module()
