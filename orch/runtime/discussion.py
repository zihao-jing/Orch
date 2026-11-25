"""Discussion with the manager or the critic (docs/redesign.md DISC-1 to 6). Pure: `decide(snapshot, event, prior)`.

While a discussion is open the agent talks only to the user: whatever it writes to others is a draft and is held
(DISC-1). Incoming messages are not touched: they enter its conversation at once (DISC-2). Applying sends one message,
when the agent has no reply under way; the reply to it follows the normal rules (DISC-3). Ending drops the drafts and
never incoming messages (DISC-4). Questions, pauses and decisions of a draft are ignored by their owners through
`Snapshot.drafting` (assumption 4). A chat with a worker is no discussion (DISC-6).

Text is never written here: the apply message carries a text key and the user's own words.
"""
from __future__ import annotations

from dataclasses import replace
from typing import List, Sequence

from . import contracts as C
from .contracts import (Discussion, DiscussionPhase as P, DropReason, HoldReason, Refusal, Role, Snapshot,
                        TextKey as K, TextRef)

NAME = "discussion"
ROLES = (Role.MANAGER, Role.CRITIC)


def _refusal(event: C.UserCommand, why: Refusal) -> C.CommandReply:
    return C.CommandReply(event.id, False, TextRef(K.COMMAND_REFUSED, {"why": why.value}))


def _drafts(snap: Snapshot, agent: str) -> List[C.Message]:
    """What the agent wrote while the discussion was open, held until it ends."""
    return [m for m in snap.messages.values()
            if m.sender == agent and HoldReason.DRAFT in m.holds and m.drop is None]


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: C.Event, prior: Sequence[C.Action] = ()) -> List[C.Action]:
        if isinstance(event, C.DiscussionStart):
            return self.start(snap, event)
        if isinstance(event, (C.DiscussionApply, C.DiscussionEnd)):
            return self.finish(snap, event)
        if isinstance(event, C.TerminalOpen):
            return self.terminal(snap, event, prior)
        if isinstance(event, C.ChangeMilestones):
            return self.change(snap, event, prior)
        if isinstance(event, C.TurnEnded):
            return self.turn_ended(snap, event, prior)
        return []

    def target(self, snap: Snapshot, event: C.UserCommand):
        """(agent, refusal): the agent a command is about, or why it cannot be."""
        a = snap.agents.get(event.agent)
        if a is None:
            return None, Refusal.UNKNOWN
        if a.role not in ROLES:
            return None, Refusal.WRONG_ROLE  # DISC-6
        if not a.is_open:
            return None, Refusal.CLOSED
        return a, None

    def start(self, snap: Snapshot, ev: C.DiscussionStart) -> List[C.Action]:
        a, why = self.target(snap, ev)
        if why is None and a.id in snap.discussions:
            why = Refusal.ALREADY
        if why:
            return [_refusal(ev, why)]
        return [C.SetDiscussion(a.id, Discussion(a.id, started=snap.now)), C.CommandReply(ev.id)]

    def finish(self, snap: Snapshot, ev) -> List[C.Action]:
        """Apply (DISC-3) or end (DISC-4). A discussion opened by a terminal stays while the terminal is open."""
        a, why = self.target(snap, ev)
        d = snap.discussions.get(ev.agent)
        if why is None and d is None:
            why = Refusal.NOT_OPEN
        elif why is None and d.phase != P.OPEN:
            why = Refusal.ALREADY
        elif why is None and ev.agent in snap.terminals:
            why = Refusal.TERMINAL_OPEN
        if why:
            return [_refusal(ev, why)]
        drops = [C.Drop(m.id, DropReason.DRAFT_ENDED) for m in _drafts(snap, a.id)]  # the new message replaces them
        if isinstance(ev, C.DiscussionEnd):
            return drops + [C.SetDiscussion(a.id, None), C.CommandReply(ev.id)]
        msg = C.Message(C.new_id(NAME, ev), C.USER, a.id, text=TextRef(K.DISCUSSION_APPLY, {"request": ev.text.strip()}),
                        trigger=C.Trigger.USER_CHAT, when_idle=True)  # never joins a draft turn
        return drops + [C.Send(msg), C.SetDiscussion(a.id, replace(d, phase=P.APPLYING, apply_message=msg.id)),
                        C.CommandReply(ev.id)]

    def terminal(self, snap: Snapshot, ev: C.TerminalOpen, prior: Sequence[C.Action]) -> List[C.Action]:
        """TERM-2: a terminal on the manager or critic starts a discussion."""
        a = snap.agents.get(ev.agent)
        opened = any(isinstance(x, C.OpenTerminal) and x.terminal.agent == ev.agent for x in prior)
        if not (opened and a and a.role in ROLES) or ev.agent in snap.discussions:
            return []
        return [C.SetDiscussion(a.id, Discussion(a.id, started=snap.now, by_terminal=True))]

    def change(self, snap: Snapshot, ev: C.ChangeMilestones, prior: Sequence[C.Action]) -> List[C.Action]:
        """MS-11: the user asked for a milestone change inside the discussion: applying it opens the gate."""
        m = snap.manager
        d = snap.discussions.get(m.id) if m else None
        sent = any(isinstance(x, C.Send) and x.message.key == K.MILESTONE_CHANGE_REQUEST for x in prior)
        if not (d and d.phase == P.OPEN and not d.milestone_change and sent):
            return []
        return [C.SetDiscussion(m.id, replace(d, milestone_change=True))]

    def turn_ended(self, snap: Snapshot, ev: C.TurnEnded, prior: Sequence[C.Action]) -> List[C.Action]:
        a, d = snap.agents.get(ev.agent), snap.discussions.get(ev.agent)
        t = snap.turns.get(ev.agent)
        if a is None or a.role not in ROLES or t is None:
            return []
        out: List[C.Action] = []
        if snap.drafting(a.id):
            # DISC-1, 5: what it wrote to others is a draft; with the discussion already over it is dropped at once
            dropped = {x.message_id for x in prior if isinstance(x, C.Drop)}
            for x in prior:
                m = x.message if isinstance(x, C.Send) else None
                if m and m.sender == a.id and m.recipient not in (C.USER, a.id) and m.id not in dropped:
                    out.append(C.Hold(m.id, HoldReason.DRAFT) if d else C.Drop(m.id, DropReason.DRAFT_ENDED))
        elif d and d.phase == P.APPLYING and d.apply_message in t.started_by + t.joined \
                and t.status == C.TurnStatus.OK:
            # the reply to the apply message ended: the discussion is over, drafts that were still being written go
            out += [C.Drop(m.id, DropReason.DRAFT_ENDED) for m in _drafts(snap, a.id)]
            out.append(C.SetDiscussion(a.id, None))
        return out


def module() -> _Module:
    return _Module()
