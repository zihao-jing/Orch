"""Gates (GATE-1, 3..9; MS-8, 9 gate part): questions, pauses, answers and the held review decision. Pure.

The manager's reply opens question and pause gates and holds an ACCEPT or REVISE while answer gates are open. A user's
answer closes its gate; once no answer gate is open, all answers go to the manager in one message (GATE-4), the
messages held for them are dropped, and a held decision is lifted. Plan, milestone and change gates are opened by
milestones; here only their answers are taken.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .contracts import (
    Action, Answer, CloseGate, CommandReply, Decision, Drop, DropReason, Event, Gate, GateAnswer,
    GateKind, Hold, HoldDecision, HoldReason, Message, OpenGate, Refusal, RecordDecision, Role, Send, Snapshot, Tell, TextKey,
    TextRef, Trigger, TurnEnded, TurnStatus, USER, new_id, ORCH)

NAME = "gates"


def decide(snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
    if isinstance(event, TurnEnded):
        return _manager_reply(snap, event, prior)
    if isinstance(event, GateAnswer):
        return _answer(snap, event)
    return _send_answers(snap, event, [], None)


# ------------------------------------------------------------------------------------------------ manager reply


def _manager_reply(snap: Snapshot, ev: TurnEnded, prior: Sequence[Action]) -> List[Action]:
    """GATE-1, 6, 7; MS-8, 9. Nothing has effect in a discussion (assumption 4); only the manager's words count
    (GATE-8)."""
    agent, turn = snap.agents.get(ev.agent), snap.turns.get(ev.agent)
    if (agent is None or agent.role != Role.MANAGER or turn is None or turn.reply is None
            or turn.status != TurnStatus.OK or snap.drafting(ev.agent)):
        return []
    reply, acts, n = turn.reply, [], 0
    ms = snap.milestone
    opened = 0
    for q in reply.questions:
        gid = new_id(NAME, ev, n)
        n += 1
        if snap.night:  # the manager's own recommendation acts at once
            acts.append(RecordDecision(_night_text(q.text, q.options), ev.agent, turn.id))
            continue
        acts.append(OpenGate(Gate(gid, GateKind.QUESTION, q.text, None, list(q.options), ev.at, 0.0, ev.agent,
                                  None, turn.id, ms.id if ms and reply.decision else "", reply.summary)))
        opened += 1
    pauses = list(reply.pauses) + ([""] if reply.decision == Decision.PAUSE and not reply.pauses else [])
    for text in pauses:  # every pause opens a pause gate (GATE-9); `decision PAUSE` works the same (MS-8)
        gid = new_id(NAME, ev, n)
        n += 1
        acts.append(OpenGate(Gate(gid, GateKind.PAUSE, text, None, [], ev.at, 0.0, ev.agent, None, turn.id,
                                  ms.id if ms and reply.decision else "", reply.summary)))
        opened += 1
    if ms and reply.decision:
        if reply.decision == Decision.PAUSE or opened or snap.open_gates(answer_only=True):
            acts.append(HoldDecision(ms.id, reply.decision))
            if reply.decision != Decision.PAUSE:  # the message of a held ACCEPT/REVISE waits too (MS-9)
                acts += [Hold(a.message.id, HoldReason.GATES) for a in prior
                         if type(a) in (Send, Tell) and a.message.recipient != USER
                         and (a.message.sender == ev.agent or a.message.key == TextKey.DIRECT_MESSAGE_NOTICE)]
    return acts


def _night_text(question: str, options: List[str]) -> str:
    return question + "".join(f"\n- {o}" for o in options)


# ------------------------------------------------------------------------------------------------ user answers


def _refuse(cmd: Any, why: Refusal) -> List[Action]:
    return [CommandReply(cmd.id, False, TextRef(TextKey.COMMAND_REFUSED, {"why": why}))]


def _answer(snap: Snapshot, cmd: GateAnswer) -> List[Action]:
    g = snap.gates.get(cmd.gate_id)
    if g is None:
        return _refuse(cmd, Refusal.UNKNOWN)
    if not g.is_open:
        return _refuse(cmd, Refusal.CLOSED)
    text = cmd.text.strip()
    if cmd.option is not None and not 0 <= cmd.option < len(g.options):
        return _refuse(cmd, Refusal.UNKNOWN)
    given = bool(text) or cmd.option is not None or cmd.approve is not None
    if g.kind in (GateKind.QUESTION, GateKind.PLAN, GateKind.MILESTONE, GateKind.MILESTONE_CHANGE) and not given:
        return _refuse(cmd, Refusal.EMPTY)
    approve = cmd.approve
    if approve is None and g.kind in (GateKind.PLAN, GateKind.MILESTONE, GateKind.MILESTONE_CHANGE):
        approve = False  # words only: a request for changes
    answer = Answer(text, cmd.option, approve, cmd.at)
    acts: List[Action] = [CloseGate(g.id, answer)]
    if g.kind in (GateKind.QUESTION, GateKind.PLAN, GateKind.MILESTONE, GateKind.MILESTONE_CHANGE):
        acts.append(RecordDecision(_answer_text(g, answer), USER, g.id))
    acts.append(CommandReply(cmd.id))
    return _send_answers(snap, cmd, acts, g.id, answer)


def _answer_text(g: Gate, a: Answer) -> str:
    words = a.text or (g.options[a.option] if a.option is not None else "")
    return f"{g.text}: {words}".strip(": ") if words else g.text


def _entry(g: Gate, a: Answer) -> Dict[str, Any]:
    option = g.options[a.option] if a.option is not None and a.option < len(g.options) else ""
    return {"kind": g.kind, "question": g.text, "answer": a.text or option, "approve": a.approve}


# -------------------------------------------------------------------------------------- the one answer message


def _send_answers(snap: Snapshot, ev: Event, acts: List[Action], closing: Optional[str],
                  answer: Optional[Answer] = None) -> List[Action]:
    """GATE-4: once no answer gate is open and an answer is unsent (or a decision waits), one message to the manager."""
    manager = snap.manager
    if manager is None:
        return acts
    still_open = [g for g in snap.open_gates(answer_only=True) if g.id != closing]
    if still_open:
        return acts
    entries, ids = [], []
    for g in sorted(snap.gates.values(), key=lambda g: (g.opened, g.id)):
        a = answer if g.id == closing else g.answer
        if g.id == closing or (g.closed and not g.withdrawn):
            if a is not None and not g.sent_in and g.kind != GateKind.PAUSE and _carries(g, a):
                entries.append(_entry(g, a))
                ids.append(g.id)
    held = [m for m in snap.messages.values()
            if m.drop is None and (HoldReason.QUESTION in m.holds or HoldReason.GATES in m.holds)]
    waiting = [m for m in snap.milestones if m.held_decision is not None]
    if not entries and not waiting:
        return acts
    unsent = [{"to": m.recipient, "body": m.body} for m in held if m.sender != ORCH]
    again = waiting[0].title if waiting else ""
    msg = Message(new_id(NAME, ev, 10), ORCH, manager.id, "",
                  TextRef(TextKey.GATE_ANSWERS, {"answers": entries, "unsent": unsent, "decide_again": again}),
                  True, Trigger.GATE_ANSWER, gates=ids)
    acts = acts + [Send(msg)]
    acts += [Drop(m.id, DropReason.ANSWERED_GATES) for m in held]
    acts += [HoldDecision(m.id, None) for m in waiting]
    return acts


def _carries(g: Gate, a: Answer) -> bool:
    """An approved change of milestones takes effect with its CloseGate and needs no message to the manager."""
    return not (g.kind == GateKind.MILESTONE_CHANGE and a.approve)


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        return decide(snap, event, prior)


def module() -> _Module:
    return _Module()
