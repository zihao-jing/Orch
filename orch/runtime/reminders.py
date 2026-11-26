"""Which reminder lines go with a message (PRM-3, 4, 5). Pure: the same snapshot, message and random source give the
same lines, so a replay or resend never changes them. texts.py words them and puts them into one tag.

Every turn: only the critic's worker lines and labels. What a session has not seen yet (golden board, pool of workers,
...) is remembered in SessionRef.given and shown once. Everything else is picked at random, each with its own chance.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (Agent, AgentStatus, DiscussionPhase, Gate, Message, Role, Snapshot, SwitchStep, TextKey,
                        TextRef)

PROMPTS = Path(__file__).with_name("prompts")

# the chance per message. All are drawn for every message, in this order, so one never changes another
CHANCES: Dict[TextKey, float] = {
    TextKey.REM_GOLDEN_USE: 0.25,
    TextKey.REM_GOLDEN_KEEP: 0.25,
    TextKey.REM_MESSAGE_CRITIC: 0.3,  # also REM_OPEN_GATES
    TextKey.REM_WORKERS: 0.15,
    TextKey.REM_UNWATCHED: 0.25,
    TextKey.REM_DISCUSSION: 0.4,
    TextKey.REM_NIGHT: 0.25,
    TextKey.REM_ASK_AND_END: 0.1,
    TextKey.REM_WRITING: 0.25,
    TextKey.REM_GOLDEN_SIZE: 0.25,  # only while the board stays over its limit
}

# messages that ask for nothing but a text to pass on
NONE_FOR = (TextKey.HANDOVER_WRITE,)


def writing_rules(role: Role) -> List[str]:
    """A role's rules in prompts/style.md: those for all first; the first one of all is the brevity line."""
    out: Dict[str, List[str]] = {}
    section = ""
    for line in (PROMPTS / "style.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
        elif line.startswith("- "):
            out.setdefault(section, []).append(line[2:].strip())
    return out.get("all", []) + out.get(role.value, [])


def given_to(agent: Agent) -> Dict[str, str]:
    """What the session that the next message enters was shown. During a switch that is the session being opened."""
    s = agent.switch
    if s is not None and s.step in (SwitchStep.OPENING, SwitchStep.CONFIRMING):
        ref = s.new
        return dict(ref.given) if ref else {}
    return dict(agent.session.given) if agent.session else {}


def short(text: str, limit: int = 120) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def lines(snap: Snapshot, message: Message, rng: Any) -> Tuple[List[TextRef], Dict[str, str]]:
    """The reminder lines of a message, and what to remember as given. rng: anything with random() and sample()."""
    agent = snap.agents.get(message.recipient)
    due = {key: rng.random() < chance for key, chance in CHANCES.items()}
    if agent is None or message.key in NONE_FOR:
        return [], {}
    seen = given_to(agent)
    out: List[TextRef] = []
    given: Dict[str, str] = {}
    if agent.role == Role.CRITIC:
        _critic(snap, message, due, seen, out, given)
    elif agent.role == Role.MANAGER:
        _manager(snap, message, due, seen, out, given)
    elif agent.role == Role.WORKER:
        if due[TextKey.REM_ASK_AND_END]:
            out.append(TextRef(TextKey.REM_ASK_AND_END))
    else:
        return [], {}
    if due[TextKey.REM_WRITING]:
        first, *rest = writing_rules(agent.role)
        out.append(TextRef(TextKey.REM_WRITING, {"rules": [first] + rng.sample(rest, 1)}))
    return out, given


def _critic(snap: Snapshot, message: Message, due: Dict[TextKey, bool], seen: Dict[str, str], out: List[TextRef],
            given: Dict[str, str]) -> None:
    # a failed worker, or one I stopped, keeps its status line but is not offered for work
    workers = [w.id for w in snap.workers if w.status != AgentStatus.FAILED and not w.stopped_by_user]
    for w in snap.workers:
        out.append(TextRef(TextKey.REM_WORKER_LINE, worker_line(snap, w, message)))
    out.append(TextRef(TextKey.REM_LABELS, {"workers": workers}))
    pool = ",".join(workers)
    if seen.get("workers") != pool:
        out.append(TextRef(TextKey.REM_WORKERS, {"workers": workers}))
        given["workers"] = pool
    elif due[TextKey.REM_WORKERS] and len(workers) > 1:
        out.append(TextRef(TextKey.REM_WORKERS, {"workers": workers}))
    unwatched = [w for w in workers if w in snap.watch and snap.watch[w].told_unwatched]
    if unwatched and due[TextKey.REM_UNWATCHED]:
        out.append(TextRef(TextKey.REM_UNWATCHED, {"workers": unwatched}))
    _checklist(snap, seen, out, given)
    if not snap.golden.words:  # the critic is not pointed to a board that names what it must not know
        _golden(snap, due, seen, out, given)
    _discussion(snap, message, due, seen, out, given)


def _manager(snap: Snapshot, message: Message, due: Dict[TextKey, bool], seen: Dict[str, str], out: List[TextRef],
             given: Dict[str, str]) -> None:
    g = snap.golden
    talking = _discussion(snap, message, due, seen, out, given)
    if due[TextKey.REM_MESSAGE_CRITIC] and not talking:
        out.append(TextRef(TextKey.REM_MESSAGE_CRITIC))
        gates = [gate_line(x) for x in snap.open_gates()]
        if gates:
            out.append(TextRef(TextKey.REM_OPEN_GATES, {"gates": gates}))
    _checklist(snap, seen, out, given)
    _golden(snap, due, seen, out, given)
    if g.path and due[TextKey.REM_GOLDEN_KEEP]:
        out.append(TextRef(TextKey.REM_GOLDEN_KEEP, {"path": g.path}))
    me = snap.agents[message.recipient]  # only the manager keeps notes
    if me.state_edited != seen.get("state", ""):
        given["state"] = me.state_edited  # "" again once it wrote them anew, so a later edit is told
        if me.state_edited:
            out.append(TextRef(TextKey.REM_STATE_EDITED, {"path": me.state_file}))
    over = g.chars > snap.settings.golden_limit > 0
    if over and (seen.get("golden_size") != "over" or due[TextKey.REM_GOLDEN_SIZE]):  # once, then by chance while over
        out.append(TextRef(TextKey.REM_GOLDEN_SIZE, {"path": g.path, "chars": g.chars,
                                                     "limit": snap.settings.golden_limit}))
        given["golden_size"] = "over"
    elif not over and seen.get("golden_size") == "over":
        given["golden_size"] = ""  # the next growth is told again
    if snap.night and (seen.get("night") != "on" or due[TextKey.REM_NIGHT]):
        out.append(TextRef(TextKey.REM_NIGHT))
        given["night"] = "on"
    elif not snap.night and seen.get("night") == "on":
        out.append(TextRef(TextKey.REM_NIGHT_OVER))
        given["night"] = ""


def _checklist(snap: Snapshot, seen: Dict[str, str], out: List[TextRef], given: Dict[str, str]) -> None:
    """The critic's checklist path, once per session and again when it moves (critic and manager alike)."""
    path = snap.checklist.path or (snap.milestone.checklist_path if snap.milestone else "")
    if path and seen.get("checklist") != path:
        out.append(TextRef(TextKey.REM_CHECKLIST, {"path": path}))
        given["checklist"] = path


def _golden(snap: Snapshot, due: Dict[TextKey, bool], seen: Dict[str, str], out: List[TextRef],
            given: Dict[str, str]) -> None:
    g = snap.golden
    if not g.chars:
        return
    new = seen.get("golden") != g.digest
    if new:
        out.append(TextRef(TextKey.REM_GOLDEN_PATH, {"path": g.path}))
        given["golden"] = g.digest
    if new or due[TextKey.REM_GOLDEN_USE]:
        out.append(TextRef(TextKey.REM_GOLDEN_USE))


def _discussion(snap: Snapshot, message: Message, due: Dict[TextKey, bool], seen: Dict[str, str], out: List[TextRef],
                given: Dict[str, str]) -> bool:
    d = snap.discussions.get(message.recipient)
    if message.key == TextKey.DISCUSSION_APPLY:
        given["discussion"] = ""
        return False
    if d is not None:
        mark = str(d.started)
        if d.phase == DiscussionPhase.OPEN and (seen.get("discussion") != mark or due[TextKey.REM_DISCUSSION]):
            out.append(TextRef(TextKey.REM_DISCUSSION))
            given["discussion"] = mark
        return True
    if seen.get("discussion"):
        given["discussion"] = ""  # gone: REM_DISCUSSION_OVER is no longer said
    return False


def worker_line(snap: Snapshot, w: Agent, message: Optional[Message] = None) -> Dict[str, Any]:
    """The values of REM_WORKER_LINE. The report this very message holds does not count as unanswered."""
    status: Any = w.status
    if w.stopped_by_user:
        status = "stopped"
    elif w.status != AgentStatus.FAILED and w.switch is not None and w.switch.step != SwitchStep.PENDING:
        status = "switching"
    elif w.status != AgentStatus.FAILED and w.retry is not None:
        status = "retrying"
    return {"worker": w.id, "status": status, "assignment": w.assignment.line if w.assignment else "",
            "open_report": bool(w.open_report) and not (message and message.id == w.open_report),
            "idle_since": w.idle_since, "jobs": [short(j.label, 60) for j in snap.running_jobs(w.id)]}


def gate_line(g: Gate) -> Any:
    """The gate's own words cut to one line; Orch's words as {"key", "values"}, rendered for the reader by texts."""
    first = next((x for x in g.text.splitlines() if x.strip()), "")
    if first:
        return short(first, 80)
    if g.words is not None:
        return {"key": g.words.key.value, "values": dict(g.words.values)}
    return {"kind": g.kind.value}
