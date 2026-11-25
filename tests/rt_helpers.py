"""Helpers for the tests of orch/runtime (docs/redesign.md). Import this first: it puts the repository on the path.

A test of a pure module: build a snapshot, give it an event, check the actions.

    from rt_helpers import *
    from orch.runtime.routing import module

    class TestSilent(unittest.TestCase):
        def test_LAB_4_silent_reply_still_sends_worker_messages(self):
            reply = Reply(untagged="All fine.", messages=[OutMessage("worker-1", "Go on.")], silent=True)
            snap, event = turn_ended(snapshot(), "critic", reply=reply)
            actions = decide(module(), snap, event)
            assert_has(self, actions, Send, recipient="worker-1", body="Go on.")
            self.assertFalse(sent_to(actions, "manager"))     # only what is neither held nor dropped

A test of a timer, and of one module holding what another one sends:

    class TestUnanswered(unittest.TestCase):
        def test_MSG_5_checks_after_five_minutes(self):
            snap = snapshot(message("m1", "worker-1", status="delivered", sent_at=T0), now=T0 + 301)
            assert_actions(self, decide(module(), snap, tick(snap)), [(Resend, {"message_id": "m1"})])

        def test_DISC_1_draft(self):
            snap, event = turn_ended(snapshot(Discussion("critic")), "critic", "...")
            send = Send(message("m9", "worker-1", "critic", status="waiting"))
            actions = decide(module(), snap, event, prior=[send])
            assert_has(self, actions, Hold, message_id="m9", reason=HoldReason.DRAFT)

`decide` also checks that the module may emit its actions, that deciding twice gives the same, and that the snapshot was
not changed. Enum values may be given as strings ("delivered"). For a running CLI: `mock_agents` (see adapters/mock.py;
its Reply is called CliReply here).
"""
from __future__ import annotations

import copy
import sys
import unittest  # noqa: F401  (for `from rt_helpers import *`)
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type, Union

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from orch.runtime import contracts as C  # noqa: E402
from orch.runtime.contracts import *  # noqa: E402,F401,F403
from orch.runtime.adapters.mock import ALL_CAPS, FakeClock, MockAdapter, MockCLI  # noqa: E402,F401
from orch.runtime.adapters.mock import Reply as CliReply  # noqa: E402,F401

T0 = 1000.0  # the time of a snapshot unless `now` says otherwise

CLAUDE = C.Choice(backend="claude", model="opus", model_id="opus")
CODEX = C.Choice(backend="codex", model="gpt", model_id="gpt", name="codex backup")


def _enums(cls: type, kw: Dict[str, Any]) -> Dict[str, Any]:
    """Keyword values in their stored form ("waiting") as the field's type."""
    hints = C.typing.get_type_hints(cls)
    return {k: C._load(hints[k], v) if isinstance(v, str) and not isinstance(v, C.Enum) and k in hints else v
            for k, v in kw.items()}


def agent(id: str, role: Union[str, Role, None] = None, **kw: Any) -> Agent:
    """An open, idle agent with a running CLI and a confirmed session. The role follows from the id."""
    r = C.Role(role or id.split("-")[0])
    choice = kw.pop("choice", CLAUDE)
    kw = _enums(C.Agent, kw)
    ref = C.SessionRef(f"s-{id}", choice.session_key, choice, "/work", started=T0 - 600, last_used=T0 - 60, confirmed=True)
    base: Dict[str, Any] = dict(
        role=r, display={"manager": "the manager", "critic": "the critic"}.get(r.value, id), cwd="/work", choice=choice,
        choices=[choice], caps=replace(ALL_CAPS), sessions={ref.session_key: ref}, session_key=ref.session_key,
        proc=C.ProcRef(pid=100, started=T0 - 600, stream=f"{id}-1"), idle_since=T0 - 60, last_output=T0 - 60)
    if kw.get("status") == C.AgentStatus.WRITING:
        base.update(idle_since=0.0, writing_since=T0 - 30)
    return C.Agent(id, **{**base, **kw})


def message(id: str, recipient: str = "critic", sender: str = ORCH, body: str = "Text.", **kw: Any) -> Message:
    """A message; by default delivered and waiting for an answer. key=TextKey.X, values={...} give it a text."""
    key, values = kw.pop("key", None), kw.pop("values", {})
    kw = _enums(C.Message, kw)
    kw.setdefault("status", C.MessageStatus.DELIVERED)
    if key is not None:
        kw["text"] = C.TextRef(key, values)
    trig = {"user": "user_chat", "orch": "orch"}.get(sender, sender.split("-")[0])
    kw.setdefault("trigger", C.Trigger(trig))
    kw.setdefault("created", T0 - 10)
    if kw["status"] != C.MessageStatus.WAITING:
        kw.setdefault("sent_at", kw["created"])
        kw.setdefault("delivered_at", kw["created"])
        kw.setdefault("attempts", 1)
    return C.Message(id, sender, recipient, body, **kw)


def job(id: str = "worker-1/j1", **kw: Any) -> Job:
    return C.Job(id, **{"agent": id.split("/")[0], "label": "train.py", "started": T0 - 100, **_enums(C.Job, kw)})


def gate(id: str, kind: Union[str, GateKind] = "question", **kw: Any) -> Gate:
    return C.Gate(id, C.GateKind(kind), **{"text": "Which one?", "opened": T0 - 100, "agent": "manager",
                                           **_enums(C.Gate, kw)})


def milestone(id: str = "m1", status: str = "current", **kw: Any) -> Milestone:
    return C.Milestone(id, kw.pop("title", f"Milestone {id}"), kw.pop("text", "It works."), C.MilestoneStatus(status),
                       **kw)


def turn(agent_id: str, id: str = "", **kw: Any) -> Turn:
    return C.Turn(id or f"r001-t01-{agent_id}", agent_id, **{"started": T0 - 30, **_enums(C.Turn, kw)})


_PLACES = {C.Agent: "agents", C.Message: "messages", C.Job: "jobs", C.Gate: "gates", C.Milestone: "milestones"}
_BY_AGENT = {C.Turn: "turns", C.Discussion: "discussions", C.Terminal: "terminals"}
_SINGLE = {C.Checklist: "checklist", C.GoldenBoard: "golden", C.Settings: "settings", C.RunInfo: "run",
           C.Silence: "silence"}


def snapshot(*records: Any, workers: int = 1, now: float = T0, **kw: Any) -> Snapshot:
    """Manager, critic and `workers` workers, all idle, plus the given records, each put where it belongs (an Agent
    replaces the one with its id). Without a Milestone in `records`, m1 is the current one."""
    s = C.Snapshot(now=now, orch_started=now - 3600, **kw)
    for a in [agent("manager"), agent("critic")] + [agent(f"worker-{i + 1}") for i in range(workers)]:
        s.agents.setdefault(a.id, a)
    for rec in records:
        t = type(rec)
        if t is C.Milestone:
            s.milestones.append(rec)
        elif t in _PLACES:
            getattr(s, _PLACES[t])[rec.id] = rec
        elif t in _BY_AGENT:
            getattr(s, _BY_AGENT[t])[rec.agent] = rec
        elif t is C.Watch:
            s.watch[rec.worker] = rec
        elif t in _SINGLE:
            setattr(s, _SINGLE[t], rec)
        else:
            raise TypeError(f"no place for {t.__name__}")
    if not s.milestones:
        s.milestones.append(milestone())
    for i, m in enumerate(s.messages.values()):
        m.seq = m.seq or i + 1
    return s


def tick(snap: Snapshot) -> Tick:
    return C.Tick("", snap.now)


def event(kind: Type[Any], snap: Snapshot, agent_id: str = "", **kw: Any) -> Any:
    """Any event or command at the snapshot's time, with an identity."""
    if issubclass(kind, C.AgentEvent):
        kw = {"stream": f"{agent_id}-1", "pos": 7, **kw}
    if issubclass(kind, C.UserCommand):
        kw.setdefault("id", "c1")
    return kind(agent_id, snap.now, **_enums(kind, kw))


def turn_ended(snap: Snapshot, agent_id: str, text: str = "", reply: Optional[Reply] = None,
               triggers: Sequence[Union[str, Trigger]] = ("orch",), error: Optional[str] = None, detail: str = "",
               started_by: Iterable[str] = (), **turn_kw: Any) -> Tuple[Snapshot, TurnEnded]:
    """(snapshot, event) just after a turn of the agent ended, with the facts applied as the store does: the Turn, its
    parsed reply (untagged text unless `reply` is given), ending and status by the running jobs. started_by: messages
    of the snapshot; they give the triggers and keys."""
    s = copy.deepcopy(snap)
    msgs = [s.messages[i] for i in started_by]
    for m in msgs:
        m.status, m.answered_at = C.MessageStatus.ANSWERED, snap.now - 30
    err = C.ErrorClass(error) if error else None
    if reply is None:
        reply = C.Reply(text=text, untagged=text)
    reply = replace(reply, text=reply.text or text or reply.untagged)
    jobs = s.running_jobs(agent_id)
    s.turns[agent_id] = t = turn(
        agent_id, ended=s.now, text=reply.text, started_by=list(started_by), error=err, detail=detail,
        triggers=[m.trigger for m in msgs] or [C.Trigger(x) for x in triggers], keys=[m.key for m in msgs if m.key])
    t.status = C.TurnStatus.ERROR if err else C.TurnStatus.OK
    t.reply = None if err else reply
    t.ending = C.Ending.INTERMEDIATE if jobs else C.Ending.FINAL
    for k, v in _enums(C.Turn, turn_kw).items():
        setattr(t, k, v)
    a = s.agents[agent_id]
    a.status = C.AgentStatus.WAITING_JOB if jobs else C.AgentStatus.IDLE
    a.idle_since, a.writing_since, a.last_output, a.tools = s.now, 0.0, s.now, []
    return s, C.TurnEnded(agent_id, s.now, f"{agent_id}-1", 9, text=t.text, status=t.status, error=err, detail=detail)


def decide(module: Any, snap: Snapshot, ev: Any, prior: Sequence[Action] = ()) -> List[Action]:
    """module.decide, with the checks every module must pass (purity, replay, emitters, text keys and values)."""
    before = copy.deepcopy(snap)
    actions = module.decide(snap, ev, tuple(prior))
    assert snap == before, "the module changed the snapshot"
    assert actions == module.decide(copy.deepcopy(snap), copy.deepcopy(ev), tuple(prior)), "not the same twice"
    for a in actions:
        assert C.may_emit(module.name, a), f"{module.name} may not emit {type(a).__name__}"
        stored = C.to_dict(a)
        assert C.to_dict(C.from_dict(C.Action, stored)) == stored, f"{a} does not survive saving"
        for ref in _refs(a):
            missing = set(C.TEXT_VALUES[ref.key]) - set(ref.values)
            assert not missing, f"{ref.key}: no value for {sorted(missing)}"
    ids = [a.message.id for a in actions if isinstance(a, C.Send)]
    assert len(set(ids)) == len(ids) and all(ids), f"message ids are empty or not unique: {ids}"
    return actions


def _refs(x: Any) -> List[TextRef]:
    if isinstance(x, C.TextRef):
        return [x]
    if is_dataclass(x):
        return [r for f in fields(x) for r in _refs(getattr(x, f.name))]
    if isinstance(x, (list, tuple)):
        return [r for v in x for r in _refs(v)]
    return []


def get(obj: Any, name: str) -> Any:
    """A field of an action; of its message, gate, switch, ... when the action has no such field; "a__b" goes deeper."""
    for part in name.split("__"):
        if not hasattr(obj, part):
            inner = next((getattr(obj, n) for n in ("message", "gate", "switch", "watch", "discussion", "terminal",
                                                    "state", "golden", "text")
                          if hasattr(getattr(obj, n, None), part)), None)
            if inner is None:
                raise AttributeError(f"{type(obj).__name__} has no {part}")
            obj = inner
        obj = getattr(obj, part)
    return obj


def matches(action: Any, kind: type, want: Dict[str, Any]) -> bool:
    if not isinstance(action, kind):
        return False
    try:
        return all(get(action, k) == v for k, v in want.items())
    except AttributeError:
        return False


def find(actions: Iterable[Any], kind: type, **want: Any) -> List[Any]:
    """The actions of this class (or a subclass) whose fields have these values. A Send's message fields (recipient,
    sender, key, body, ...) can be given directly."""
    return [a for a in actions if matches(a, kind, want)]


def assert_has(test: Any, actions: Sequence[Any], kind: type, **want: Any) -> Any:
    """Exactly one such action; returned."""
    got = find(actions, kind, **want)
    test.assertEqual(len(got), 1, f"wanted one {kind.__name__} {want}, have:\n" + show(actions))
    return got[0]


def assert_none(test: Any, actions: Sequence[Any], kind: type = C.Action, **want: Any) -> None:
    test.assertEqual(find(actions, kind, **want), [], "unexpected:\n" + show(actions))


def assert_actions(test: Any, actions: Sequence[Any], expected: Sequence[Any]) -> None:
    """Exactly these, in this order: each a class or (class, {field: value})."""
    exp = [e if isinstance(e, tuple) else (e, {}) for e in expected]
    ok = len(actions) == len(exp) and all(type(a) is k and matches(a, k, w) for a, (k, w) in zip(actions, exp))
    test.assertTrue(ok, "wanted:\n" + "\n".join(f"  {k.__name__} {w}" for k, w in exp) + "\nhave:\n" + show(actions))


def sent_to(actions: Sequence[Any], recipient: str, snap: Optional[Snapshot] = None) -> List[Message]:
    """The messages that really go out to `recipient`: sends that no Hold, Drop or (with snap) agent hold stops."""
    stopped = {a.message_id for a in actions if isinstance(a, (C.Hold, C.Drop))}
    held = set(snap.agents[recipient].holds) if snap and recipient in snap.agents else set()
    held |= {a.reason for a in actions if isinstance(a, C.HoldAgent) and a.agent == recipient}
    return [a.message for a in actions if isinstance(a, C.Send) and a.message.recipient == recipient
            and a.message.id not in stopped and not a.message.holds and not held - set(a.bypass)]


def show(actions: Sequence[Any]) -> str:
    return "\n".join(f"  {a}" for a in actions) or "  (none)"


def mock_agents(clock: FakeClock, *ids: str, caps: Caps = ALL_CAPS) -> Dict[str, Tuple[MockCLI, MockAdapter]]:
    """Started mock CLIs: {id: (cli, adapter)}. After an "Orch restart" join one again with
    MockAdapter(clock, cli).reconnect(proc)."""
    out = {}
    for i in ids:
        cli = MockCLI(clock, i, caps)
        ad = MockAdapter(clock, cli)
        ad.start(C.AgentSpec(i, C.Role(i.split("-")[0]), CLAUDE, "/work"), None)
        out[i] = (cli, ad)
    return out
