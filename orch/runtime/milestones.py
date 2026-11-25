"""Milestones (docs/redesign.md 2.5): the list and its ids, the checklist file, the review trigger, the manager's
decision, approvals, changes and the end of a run. Pure: `decide(snapshot, event, prior)`. `state.json` (the snapshot's
milestone list) is the only store (MS-15); the checklist file is read by `read_checklist`, which the engine calls.

Text is never written here: messages carry text keys and values.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, List, Optional, Sequence, Tuple

from .. import boards as B
from . import contracts as C
from .contracts import (Checklist, ChecklistStep, Milestone, MilestoneStatus as S, Snapshot, TextKey as K, TextRef)

NAME = "milestones"
REOPENED = "@reopened"  # Milestone.notified of a reopened milestone: the checklist as it is counts as seen


# ------------------------------------------------------------------------------------------------ the checklist file


def read_checklist(path: str, text: Optional[str]) -> Checklist:
    """MS-1, 2: a step passes by itself when all its items are ticked; a missing, unreadable or empty file is not
    complete and says why in `error`."""
    if text is None:
        return Checklist(path=path, error="missing")
    parsed = B.parse_checklist(text)
    steps: List[ChecklistStep] = []
    if parsed["standing"]:  # items that hold for every step: shown as a step of their own
        steps.append(ChecklistStep("Standing rules", all(d for d, _ in parsed["standing"]), list(parsed["standing"])))
    for s in parsed["steps"]:
        steps.append(ChecklistStep(s["title"], B.step_done(s), list(s["items"])))
    if not steps:
        return Checklist(path=path, error="empty")
    # the digest follows the content that counts, not comments or spacing
    body = "\n".join(f"{s.title}|{int(s.done)}|" + "|".join(f"{int(d)}{t}" for d, t in s.items) for s in steps)
    return Checklist(path=path, digest=hashlib.sha1(body.encode("utf-8")).hexdigest()[:16], steps=steps,
                     complete=all(s.done for s in steps))


def checklist_lines(ck: Checklist) -> str:
    """The checklist as a value for a text: a heading per step, one line per item."""
    out: List[str] = []
    for s in ck.steps:
        out.append(f"{s.title}{'' if s.items else (' [x]' if s.done else ' [ ]')}")
        out += [f"- [{'x' if d else ' '}] {t}" for d, t in s.items]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------- reading a list

_LINE = re.compile(r"^\s*(?:\*\*)?(\d{1,3})[.)](?:\*\*)?\s+(\S.*)$")
_ID = re.compile(r"^[\[(]?\**(m\d+)\**[\])]?\s*[:—–-]?\s*(\S.*)$", re.I)


def parse_list(text: str) -> List[Tuple[str, str, str]]:
    """The first (longest) numbered list that starts at 1, as [(id or "", title, text)]. Each line may start with an
    id (`m3`, `[m3]`, `(m3)`, `**m3**`), as in the list a change request shows (MS-10)."""
    best: List[str] = []
    cur: List[Tuple[int, str]] = []

    def close() -> None:
        nonlocal best, cur
        if len(cur) > len(best):
            best = [t for _, t in cur]
        cur = []

    for ln in (text or "").split("\n"):
        m = _LINE.match(ln)
        if m:
            n = int(m.group(1))
            if n == 1:
                close()
                cur = [(1, m.group(2))]
            elif cur and n == cur[-1][0] + 1:
                cur.append((n, m.group(2)))
        elif cur and ln.strip():
            if ln.startswith((" ", "\t")):
                cur[-1] = (cur[-1][0], cur[-1][1] + " " + ln.strip())  # a wrapped line
            else:
                close()
    close()
    out = []
    for line in best:
        mid = _ID.match(line.strip())
        mid_, clean = (mid.group(1).lower(), mid.group(2).strip()) if mid else ("", line.strip())
        title = re.split(r"\s+[—–-]\s+|:\s+", clean, maxsplit=1)[0]
        out.append((mid_, re.sub(r"[*_`]", "", title).strip(), clean))
    return out


def _number(ms: Sequence[Milestone]) -> int:
    return max([int(m.id[1:]) for m in ms if m.id[1:].isdigit()] or [0]) + 1


def merge(old: Sequence[Milestone], listed: Sequence[Tuple[str, str, str]]) -> List[Milestone]:
    """MS-12: the list after a change. Finished milestones stay as they are; the current one is rewritten in place and
    stays first of the open ones; later ones follow the new order; new ones get the next ids; later ones left out are
    marked dropped (kept, ids never reused)."""
    by_id = {m.id: m for m in old}
    nxt, seen, listed_open = _number(old), set(), []
    for mid, title, text in listed:
        o = by_id.get(mid)
        if o is not None and o.id not in seen:
            seen.add(o.id)
            if o.status != S.DONE:
                listed_open.append(replace(o, title=title, text=text, status=S.PENDING if o.status == S.DROPPED else o.status))
        else:
            listed_open.append(Milestone(f"m{nxt}", title, text))
            nxt += 1
    done = [m for m in old if m.status == S.DONE]
    current = next((m for m in listed_open if m.status == S.CURRENT), None) \
        or next((m for m in old if m.status == S.CURRENT), None)
    rest = [m for m in listed_open if m is not current]
    dropped = [replace(m, status=S.DROPPED) for m in old if m.status in (S.PENDING, S.DROPPED) and m.id not in seen]
    return done + ([current] if current else []) + rest + dropped


# ----------------------------------------------------------------------------------------------------- the module


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: C.Event, prior: Sequence[C.Action] = ()) -> List[C.Action]:
        return _Run(snap, event, prior).go()


def module() -> _Module:
    return _Module()


class _Run:
    """One event. `ms` is the list as it becomes; `out` the actions; a changed list is written once, at the end."""

    def __init__(self, snap: Snapshot, event: C.Event, prior: Sequence[C.Action]):
        self.snap, self.ev, self.prior = snap, event, prior
        self.ms: List[Milestone] = list(snap.milestones)
        self.out: List[C.Action] = []
        self.dirty, self.finished, self.n = False, snap.finished, 0
        mgr = snap.manager
        self.manager = mgr.id if mgr else ""

    # ---- helpers
    def new_id(self) -> str:
        self.n += 1
        return C.new_id(NAME, self.ev, self.n - 1)

    def cur(self) -> Optional[Milestone]:
        return next((m for m in self.ms if m.status == S.CURRENT), None)

    def put(self, m: Milestone) -> None:
        self.ms = [m if x.id == m.id else x for x in self.ms]
        self.dirty = True

    def tell_manager(self, key: K, values: dict) -> None:
        msg = C.Message(self.new_id(), C.ORCH, self.manager, text=TextRef(key, values), trigger=C.Trigger.ORCH)
        self.out.append(C.Send(msg))

    def note(self, key: K, values: dict) -> None:
        self.out.append(C.ToUser(C.Message(self.new_id(), C.ORCH, C.USER, text=TextRef(key, values), needs_answer=False)))

    def checklist_path(self, mid: str) -> str:
        critic = self.snap.critic
        base = critic.state_file.rsplit("/", 1)[0] if critic and "/" in critic.state_file else self.snap.run.run_dir
        return f"{base}/checklist-{mid}.md"

    def open_gate(self, kind: C.GateKind, mid: str = "") -> List[C.Gate]:
        return [g for g in self.snap.gates.values() if g.is_open and g.kind == kind and (not mid or g.milestone == mid)]

    # ---- the run
    def go(self) -> List[C.Action]:
        ev = self.ev
        if not self.manager:
            return []
        if isinstance(ev, C.ChangeMilestones):
            self.change_request(ev)
        elif isinstance(ev, C.ReopenMilestone):
            self.reopen(ev)
        elif isinstance(ev, C.TurnEnded) and ev.agent == self.manager:
            self.manager_turn(ev)
        for a in self.prior:
            if isinstance(a, C.CloseGate) and not a.withdrawn and a.answer is not None:
                self.gate_closed(a)
        self.upkeep()
        if self.dirty:
            self.out.append(C.SetMilestones(tuple(self.ms)))
        if self.finished != self.snap.finished:
            self.out.append(C.SetFinished(self.finished))
        return self.out

    # ---- commands
    def refuse(self, why: C.Refusal) -> None:
        self.out.append(C.CommandReply(self.ev.id, False, TextRef(K.COMMAND_REFUSED, {"why": why.value})))

    def change_request(self, ev: C.ChangeMilestones) -> None:
        """MS-10: the user's words and the current list with ids, in one message to the manager."""
        if not ev.text.strip():
            return self.refuse(C.Refusal.EMPTY)
        if not self.ms:
            return self.refuse(C.Refusal.NOT_POSSIBLE)
        shown = [{"id": m.id, "title": m.title, "text": m.text, "status": m.status.value} for m in self.ms]
        msg = C.Message(self.new_id(), C.USER, self.manager, body=ev.text.strip(),
                        text=TextRef(K.MILESTONE_CHANGE_REQUEST, {"milestones": shown}), trigger=C.Trigger.USER_CHAT)
        self.out += [C.Send(msg), C.CommandReply(ev.id)]

    def reopen(self, ev: C.ReopenMilestone) -> None:
        """MS-12, 14: a finished milestone becomes the current one again; the checklist as it is counts as seen, so the
        manager hears of it when the critic changes it."""
        m = next((x for x in self.ms if x.id == ev.milestone_id), None)
        if m is None:
            return self.refuse(C.Refusal.UNKNOWN)
        if m.status != S.DONE:
            return self.refuse(C.Refusal.NOT_POSSIBLE)
        old = self.cur()
        if old:
            for g in self.open_gate(C.GateKind.MILESTONE, old.id):
                self.out.append(C.CloseGate(g.id, withdrawn=True))
            self.put(replace(old, status=S.PENDING, accepted=False))
        self.put(replace(m, status=S.CURRENT, ended=0.0, accepted=False, notified=REOPENED, asked_to_fix="",
                         refreshed=list(self.snap.agents), held_decision=None))
        self.finished = False
        self.out.append(C.CommandReply(ev.id))

    # ---- what the manager wrote
    def carried(self, turn: C.Turn) -> set:
        """Kinds of the gates whose answers the turn's messages carried."""
        kinds = set()
        for mid in turn.started_by + turn.joined:
            m = self.snap.messages.get(mid)
            for gid in (m.gates if m else []):
                if gid in self.snap.gates:
                    kinds.add(self.snap.gates[gid].kind)
        return kinds

    def manager_turn(self, ev: C.TurnEnded) -> None:
        snap, t = self.snap, self.snap.turns.get(self.manager)
        if t is None or t.reply is None or t.status != C.TurnStatus.OK or snap.drafting(self.manager):
            return
        r, keys, kinds = t.reply, set(t.keys), self.carried(t)
        in_change = (K.MILESTONE_CHANGE_REQUEST in keys or C.GateKind.MILESTONE_CHANGE in kinds or K.MILESTONE_CHANGE_UNREADABLE in keys
                     or (snap.discussions.get(self.manager) and snap.discussions[self.manager].milestone_change
                         and any(i == snap.discussions[self.manager].apply_message for i in t.started_by + t.joined)))
        if not self.ms:
            if keys & {K.PLAN_REQUEST, K.PLAN_LIST_MISSING} or C.GateKind.PLAN in kinds:
                self.plan_reply(t, r, keys)
            return
        if in_change:
            self.change_reply(t, r, keys)
        self.decision(t, r)

    def plan_reply(self, t: C.Turn, r: C.Reply, keys: set) -> None:
        """The manager's numbered list becomes the plan: a gate to approve, or at once by night (assumption 2)."""
        listed = parse_list(r.text)
        if not listed:
            if keys & {K.PLAN_REQUEST, K.PLAN_LIST_MISSING} and not r.questions:
                self.tell_manager(K.PLAN_LIST_MISSING, {})
            return
        plan = [Milestone(f"m{i + 1}", title, text) for i, (_, title, text) in enumerate(listed)]
        for g in self.open_gate(C.GateKind.PLAN):
            self.out.append(C.CloseGate(g.id, withdrawn=True))
        if self.snap.night or not self.snap.settings.approve_plan:
            if self.snap.night:
                self.out.append(C.RecordDecision("plan approved by night mode", self.manager, t.id))
            return self.start_plan(plan)
        self.out.append(C.OpenGate(C.Gate(
            self.new_id(), C.GateKind.PLAN, text=r.untagged or r.text, words=TextRef(K.GATE_PLAN, {"milestones": [m.text for m in plan]}),
            opened=self.snap.now, agent=self.manager, source_turn=t.id, summary=r.summary, new=plan)))

    def start_plan(self, plan: List[Milestone]) -> None:
        self.ms, self.dirty = plan, True
        self.start(plan[0])

    def change_reply(self, t: C.Turn, r: C.Reply, keys: set) -> None:
        """MS-10, 11: the reply to a change request; nothing changes before the gate is approved."""
        listed = parse_list(r.text)
        if not listed:
            if not r.questions and K.MILESTONE_CHANGE_UNREADABLE not in keys:
                self.tell_manager(K.MILESTONE_CHANGE_UNREADABLE, {"problem": C.Problem.EMPTY.value})
            return
        new = merge(self.snap.milestones, listed)
        old_cur = self.cur()
        for m in new:  # the rewritten current milestone: its rubric comes with the proposal (MS-12)
            if old_cur and m.id == old_cur.id and (m.title, m.text) != (old_cur.title, old_cur.text):
                new = [replace(m, rubric=list(r.rubric)) if x.id == m.id else x for x in new]
        for g in self.open_gate(C.GateKind.MILESTONE_CHANGE):
            self.out.append(C.CloseGate(g.id, withdrawn=True))
        self.out.append(C.OpenGate(C.Gate(
            self.new_id(), C.GateKind.MILESTONE_CHANGE, text=r.untagged or r.text, words=TextRef(K.GATE_MILESTONE_CHANGE, {}),
            opened=self.snap.now, agent=self.manager, source_turn=t.id, summary=r.summary,
            old=list(self.snap.milestones), new=new)))

    def decision(self, t: C.Turn, r: C.Reply) -> None:
        """MS-6, 8, 9: rubric and score are kept; ACCEPT opens the approval gate; REVISE changes nothing here (the
        critic unticks items, so the checklist changes); PAUSE lets the review run again after the pause."""
        m = self.cur()
        if m is None:
            return
        if r.rubric:
            m = replace(m, rubric=list(r.rubric))
            self.put(m)
        if r.decision is None:
            return
        if any(isinstance(a, C.HoldDecision) and a.milestone_id == m.id and a.decision is not None for a in self.prior):
            return
        m = replace(m, score=r.score if r.score is not None else m.score)
        self.put(m)
        if r.decision == C.Decision.PAUSE:
            self.put(replace(m, notified=""))
        elif r.decision == C.Decision.ACCEPT:
            self.accept(m, t, r)

    def accept(self, m: Milestone, t: C.Turn, r: C.Reply) -> None:
        s = self.snap.settings
        if not m.rubric:
            return self.tell_manager(K.REVIEW_RUBRIC_MISSING, {"milestone": m.id})
        if s.pass_score is not None and m.score is not None and m.score < s.pass_score:
            return  # below the agreed score: not accepted; the manager's own message to the critic goes on
        m = replace(m, accepted=True, summary=r.summary or r.untagged)
        self.put(m)
        if s.approve_milestones and not self.snap.night:
            self.out.append(C.OpenGate(C.Gate(
                self.new_id(), C.GateKind.MILESTONE, text=r.untagged or r.text,
                words=TextRef(K.GATE_MILESTONE, {"milestone": m.id, "title": m.title, "score": m.score}),
                opened=self.snap.now, agent=self.manager, source_turn=t.id, milestone=m.id, summary=r.summary)))
        else:
            if self.snap.night:
                self.out.append(C.RecordDecision(f"{m.id} {m.title} approved by night mode", self.manager, t.id))
            self.advance(m)

    # ---- gates closed (read from `prior`)
    def gate_closed(self, a: C.CloseGate) -> None:
        g = self.snap.gates.get(a.gate_id)
        if g is None or not g.is_open:
            return
        ok = bool(a.answer and a.answer.approve)
        if g.kind == C.GateKind.PLAN and ok and not self.ms and g.new:
            self.start_plan([replace(m) for m in g.new])
        elif g.kind == C.GateKind.MILESTONE:
            m = next((x for x in self.ms if x.id == g.milestone), None)
            if m is None or m.status != S.CURRENT:
                return
            if ok:
                self.advance(m)
            else:
                self.put(replace(m, accepted=False))
        elif g.kind == C.GateKind.MILESTONE_CHANGE and ok:
            self.apply_change(g)

    def apply_change(self, g: C.Gate) -> None:
        """MS-11 to 14: apply the approved list on what is there now."""
        old_cur = self.cur()
        listed = [(m.id, m.title, m.text) for m in g.new if m.status != S.DROPPED]
        self.ms, self.dirty = merge(self.ms, listed), True
        if old_cur:
            m = next(x for x in self.ms if x.id == old_cur.id)
            if (m.title, m.text) != (old_cur.title, old_cur.text):
                proposed = next((x for x in g.new if x.id == m.id), None)
                open_gates = self.open_gate(C.GateKind.MILESTONE, m.id)
                reviewing = bool(m.notified or m.accepted or open_gates)
                for og in open_gates:  # MS-13
                    self.out.append(C.CloseGate(og.id, withdrawn=True))
                if reviewing:
                    self.note(K.NOTE_REVIEW_WITHDRAWN, {"milestone": m.id, "title": m.title})
                self.put(replace(m, rubric=list(proposed.rubric) if proposed else [], score=None, accepted=False,
                                 summary="", notified="" if reviewing else m.notified))
        elif self.cur() is None:  # added after the last one (MS-14): starts like any other
            nxt = next((x for x in self.ms if x.status == S.PENDING), None)
            if nxt:
                self.start(nxt)

    # ---- starting and ending milestones
    def start(self, m: Milestone) -> None:
        """The milestone becomes the current one; the manager gets the usual first message (its reply carries the
        rubric)."""
        last = next((x for x in reversed(self.ms) if x.status == S.DONE), None)
        refreshed = [] if last else list(self.snap.agents)  # no compacting at the very first start
        self.put(replace(m, status=S.CURRENT, started=self.snap.now, ended=0.0, accepted=False, notified="",
                         asked_to_fix="", refreshed=refreshed, checklist_path=self.checklist_path(m.id)))
        self.finished = False
        self.tell_manager(K.MILESTONE_START, {"milestone": m.id, "title": m.title, "text": m.text,
                                              "checklist_path": self.checklist_path(m.id),
                                              "last_title": last.title if last else ""})

    def advance(self, m: Milestone) -> None:
        self.put(replace(m, status=S.DONE, ended=self.snap.now))
        nxt = next((x for x in self.ms if x.status == S.PENDING), None)
        if nxt:
            self.start(nxt)
        else:
            self.finished = True  # MS-14: the run stays open
            self.note(K.NOTE_FINISHED, {})

    # ---- what holds in any event
    def upkeep(self) -> None:
        snap, m = self.snap, self.cur()
        if m is None:
            if not self.ms and not self.open_gate(C.GateKind.PLAN) \
                    and not any(x.key == K.PLAN_REQUEST for x in snap.messages.values()):
                self.tell_manager(K.PLAN_REQUEST, {"goal": snap.run.goal})
            return
        if m.id not in {x.id for x in snap.milestones} or snap.milestone is None or snap.milestone.id != m.id:
            return  # started in this event: it is looked at in the next one
        self.refresh()
        m, ck = self.cur(), snap.checklist
        if ck.path != m.checklist_path:
            return
        if m.notified == REOPENED and ck.digest and not ck.error:
            return self.put(replace(m, notified=ck.digest))
        if ck.error:
            return self.unreadable(m, ck)
        if m.asked_to_fix:
            m = replace(m, asked_to_fix="")
            self.put(m)
        self.review(m, ck)

    def quiet(self) -> bool:
        """MS-4: no worker is writing, no message to a worker or the critic waits (the critic itself is not writing)."""
        snap = self.snap
        critic = snap.critic
        if critic and critic.status == C.AgentStatus.WRITING:
            return False
        if any(w.status == C.AgentStatus.WRITING for w in snap.by_role(C.Role.WORKER)):
            return False
        team = {a.id for a in snap.agents.values() if a.role in (C.Role.WORKER, C.Role.CRITIC)}
        return not any(x.recipient in team and x.status == C.MessageStatus.WAITING and x.drop is None
                       for x in snap.messages.values())

    def review(self, m: Milestone, ck: Checklist) -> None:
        snap = self.snap
        if not ck.complete or ck.digest == m.notified or m.accepted or snap.paused or self.finished:
            return
        if self.open_gate(C.GateKind.MILESTONE, m.id) or not self.quiet():
            return
        jobs = [f"{j.agent}: {j.label}" for j in snap.jobs.values() if j.state == C.JobState.RUNNING]
        msg = C.Message(self.new_id(), C.ORCH, self.manager, trigger=C.Trigger.ORCH, text=TextRef(
            K.CHECKLIST_COMPLETE, {"milestone": m.id, "title": m.title, "text": m.text, "rubric": list(m.rubric),
                                   "checklist": checklist_lines(ck), "jobs": jobs}))
        self.out.append(C.NotifyReviewReady(msg, milestone_id=m.id, digest=ck.digest))

    def unreadable(self, m: Milestone, ck: Checklist) -> None:
        """MS-2: not complete; the critic is asked to fix it, once for each problem, after it has had a turn."""
        snap, critic = self.snap, self.snap.critic
        t = snap.turns.get(critic.id) if critic else None
        if critic is None or ck.error == m.asked_to_fix or t is None or t.ended < m.started or not self.quiet():
            return
        self.put(replace(m, asked_to_fix=ck.error))
        msg = C.Message(self.new_id(), C.ORCH, critic.id, trigger=C.Trigger.ORCH,
                        text=TextRef(K.CHECKLIST_UNREADABLE, {"path": ck.path, "problem": ck.error}))
        self.out.append(C.Send(msg))

    def refresh(self) -> None:
        """Assumption 3: at a milestone start, each agent's session is compacted or renewed once it is settled."""
        snap, m, done = self.snap, self.cur(), []
        if not any(x.status == S.DONE and 0 < x.ended <= m.started for x in self.ms):
            return  # the first milestone starts on new sessions
        for a in snap.agents.values():
            if a.id in m.refreshed or a.role in (C.Role.MONITOR, C.Role.AUDITOR):
                continue
            mode = a.fresh_session or ("never" if a.role == C.Role.MANAGER else "compact")
            if mode == "never" or not a.is_open:
                done.append(a.id)
            elif snap.settled(a.id):
                self.out.append(C.RefreshSession(a.id, mode))
                done.append(a.id)
        if done:
            self.put(replace(m, refreshed=m.refreshed + done))
