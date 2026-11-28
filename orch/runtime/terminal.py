"""The terminal command and the take-over of a session (docs/redesign.md TERM-1 to 6). Pure: `decide(snapshot, event,
prior)`. What the CLI does (opening the right program, closing Orch's copy, reading the transcript) is the engine's and
the adapter's: here only the decisions.

While a terminal is open every message to the agent waits (TERM-4); the hold is lifted when the transcript has been
read, after Orch's CLI is open again. Where the terminal joins the open session (`Caps.shares_terminal`) Orch's copy
stays open, so nothing running ends.
"""
from __future__ import annotations

import shlex
from typing import List, Sequence

from . import contracts as C
from .contracts import (AgentStatus, HoldReason, Refusal, Role, Snapshot, TextKey as K, TextRef, Trigger, TurnStatus)

NAME = "terminal"


def command(project: str, agent: str) -> str:
    """TERM-1: the one command the panel shows."""
    return f"orch terminal {shlex.quote(project)} {shlex.quote(agent)}"


def _refusal(event: C.UserCommand, why: Refusal) -> C.CommandReply:
    return C.CommandReply(event.id, False, TextRef(K.COMMAND_REFUSED, {"why": why.value}))


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: C.Event, prior: Sequence[C.Action] = ()) -> List[C.Action]:
        if isinstance(event, C.TerminalOpen):
            return self.open(snap, event)
        if isinstance(event, (C.TerminalClose, C.TerminalTakeBack)):
            return self.close(snap, event)
        if isinstance(event, C.TerminalRead):
            return self.read(snap, event)
        return []

    def open(self, snap: Snapshot, ev: C.TerminalOpen) -> List[C.Action]:
        a = snap.agents.get(ev.agent)
        why = None
        if a is None:
            why = Refusal.UNKNOWN
        elif a.role in (Role.MONITOR, Role.AUDITOR):
            why = Refusal.WRONG_ROLE
        elif a.lifecycle == C.Lifecycle.DELETED:
            why = Refusal.DELETED
        elif not a.is_open:
            why = Refusal.CLOSED
        elif a.id in snap.terminals or HoldReason.TERMINAL in a.holds:
            why = Refusal.TERMINAL_OPEN  # also while a closed one is being read
        elif a.switch is not None:
            why = Refusal.SWITCHING
        elif a.session is None:
            why = Refusal.NOT_POSSIBLE  # nothing to open
        if why:
            return [_refusal(ev, why)]
        t = snap.turns.get(a.id)
        writing = a.status == AgentStatus.WRITING or (t is not None and t.status == TurnStatus.RUNNING)
        jobs = snap.running_jobs(a.id)
        ends = not a.caps.shares_terminal and (writing or bool(jobs))  # TERM-5: Orch's CLI must close
        if ends and not ev.stop_job:
            return [C.CommandReply(ev.id, False, TextRef(K.ASK_JOB_TERMINAL, {"agent": a.id, "jobs": [j.label or j.id for j in jobs]}),
                                   choices=({"label": "stop", "set": {"stop_job": True}},))]
        out: List[C.Action] = []
        if ends and jobs:
            out += [C.StopJob(j.id) for j in jobs]  # no notice to the critic: its reminder shows the worker's state
        out += [C.HoldAgent(a.id, HoldReason.TERMINAL),
                C.OpenTerminal(C.Terminal(a.id, snap.now, shared=a.caps.shares_terminal), command_id=ev.id),
                C.CommandReply(ev.id)]
        return out

    def close(self, snap: Snapshot, ev) -> List[C.Action]:
        """`orch terminal` ended, or the dashboard's take-back button (TERM-6): the engine takes the session back."""
        if ev.agent not in snap.terminals:
            return [_refusal(ev, Refusal.NOT_OPEN if ev.agent in snap.agents else Refusal.UNKNOWN)]
        return [C.CloseTerminal(ev.agent), C.CommandReply(ev.id)]

    def read(self, snap: Snapshot, ev: C.TerminalRead) -> List[C.Action]:
        """TERM-2, 3: the CLI is open again. Release the agent's messages; record each reply for the user. What the user
        typed to a worker stays between the two: the critic is not told (no DIRECT_MESSAGE_NOTICE any more)."""
        a = snap.agents.get(ev.agent)
        if a is None:
            return []
        out: List[C.Action] = []
        n = 0
        for said in ev.said:
            text = said.text.strip()
            if not text or said.by != a.id:
                continue
            msg = C.Message(C.new_id(NAME, ev, n), a.id, C.USER, body=text, needs_answer=False,
                            trigger=Trigger(a.role.value))
            out.append(C.ToUser(msg))
            n += 1
        if HoldReason.TERMINAL in a.holds:
            out.append(C.ReleaseAgent(a.id, HoldReason.TERMINAL))
        return out


def module() -> _Module:
    return _Module()
