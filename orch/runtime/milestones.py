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
    """The first (longest) numbered list that starts at 1, as [(id or "", title, text)], text being what comes after "title -" (never the title again). Each line may start with an
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
        parts = re.split(r"\s+[—–-]\s+|:\s+", clean, maxsplit=1)
        out.append((mid_, re.sub(r"[*_`]", "", parts[0]).strip(), parts[1].strip() if len(parts) > 1 else ""))
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

    def checklist_path(self, m: Milestone) -> str:
        """In the run's folder, named by the milestone's position: the critic sees no id (MS-12)."""
        return f"{self.snap.run.run_dir}/checklist-{[x.id for x in self.ms].index(m.id) + 1}.md"

    def last_before(self, m: Milestone) -> str:
        """Title of the nearest finished milestone before `m`, or ""."""
        before = self.ms[:[x.id for x in self.ms].index(m.id)]
        return next((x.title for x in reversed(before) if x.status == S.DONE), "")

    def start_message(self, m: Milestone) -> None:
        self.tell_manager(K.MILESTONE_START, {"milestone": m.id, "title": m.title, "text": m.text,
                                              "checklist_path": m.checklist_path, "last_title": self.last_before(m)})

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
        self.gate_closed()
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
                         refreshed=list(self.snap.agents)))
        self.finished = False
        self.start_message(m)
        self.out.append(C.CommandReply(ev.id))

    # ---- what the manager wrote
    def took(self, t: C.Turn) -> List[C.Message]:
        return [self.snap.messages[i] for i in t.started_by + t.joined if i in self.snap.messages]

    def carried(self, msgs: List[C.Message]) -> List[C.Gate]:
        """The gates whose answers the turn's messages carried."""
        return [self.snap.gates[g] for m in msgs for g in m.gates if g in self.snap.gates]

    def in_change(self, t: C.Turn, msgs: List[C.Message]) -> Optional[str]:
        """Does the reply answer a milestone change request (MS-10)? "ask": the turn took the request itself or the
        apply message of a discussion that wants a change; "carried": it follows a gate answer; None: no."""
        snap, d = self.snap, self.snap.discussions.get(self.manager)
        if any(m.key in (K.MILESTONE_CHANGE_REQUEST,) for m in msgs) or (
                d and d.milestone_change and any(m.id == d.apply_message for m in msgs)):
            return "ask"
        if any(m.key == K.MILESTONE_CHANGE_UNREADABLE for m in msgs):
            return "carried"
        for g in self.carried(msgs):
            if g.kind == C.GateKind.MILESTONE_CHANGE:
                return "carried"
            if g.kind == C.GateKind.QUESTION and any(
                    x.key == K.MILESTONE_CHANGE_REQUEST and x.reply_turn == g.source_turn
                    for x in snap.messages.values()):
                return "carried"  # the manager asked first, the answer came back
        return None

    def manager_turn(self, ev: C.TurnEnded) -> None:
        snap, t = self.snap, self.snap.turns.get(self.manager)
        if t is None or t.reply is None or t.status != C.TurnStatus.OK or snap.drafting(self.manager):
            return
        r, msgs = t.reply, self.took(t)
        if not self.ms:
            return self.plan_reply(t, r, msgs)
        way = self.in_change(t, msgs)
        if way:
            return self.change_reply(t, r, msgs, way)
        self.decision(t, r, msgs)

    def plan_reply(self, t: C.Turn, r: C.Reply, msgs: List[C.Message]) -> None:
        """Until there is a plan, any reply with a numbered list (and no question) is read as one: a gate to approve,
        or at once by night (assumption 2)."""
        if r.questions:
            return
        listed = parse_list(r.untagged)
        if not listed:
            if any(m.key in (K.PLAN_REQUEST, K.PLAN_LIST_MISSING) for m in msgs):
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
            self.new_id(), C.GateKind.PLAN, text=r.untagged, opened=self.snap.now, agent=self.manager,
            words=TextRef(K.GATE_PLAN, {"milestones": [f"{m.title} \u2014 {m.text}" if m.text else m.title for m in plan]}),
            source_turn=t.id, summary=r.summary, new=plan)))

    def start_plan(self, plan: List[Milestone]) -> None:
        self.ms, self.dirty = plan, True
        self.start(plan[0])

    def change_reply(self, t: C.Turn, r: C.Reply, msgs: List[C.Message], way: str) -> None:
        """MS-10, 11: the reply to a change request; nothing changes before the gate is approved. The rubric of a
        rewritten current milestone lives in the gate until then."""
        if r.questions:
            return  # the answer comes back (carried) and its reply is read
        listed = parse_list(r.untagged)
        known = {m.id for m in self.snap.milestones}
        if not any(i in known for i, _, _ in listed):  # no id of ours: it would duplicate everything
            if way == "ask" and not any(m.key == K.MILESTONE_CHANGE_UNREADABLE for m in msgs):
                self.tell_manager(K.MILESTONE_CHANGE_UNREADABLE, {"problem": C.Problem.EMPTY.value})
            return
        new = merge(self.snap.milestones, listed)
        old_cur = self.cur()
        new = [replace(m, rubric=list(r.rubric)) if old_cur and m.id == old_cur.id
               and (m.title, m.text) != (old_cur.title, old_cur.text) else m for m in new]
        for g in self.open_gate(C.GateKind.MILESTONE_CHANGE):
            self.out.append(C.CloseGate(g.id, withdrawn=True))
        self.out.append(C.OpenGate(C.Gate(
            self.new_id(), C.GateKind.MILESTONE_CHANGE, text=r.untagged, words=TextRef(K.GATE_MILESTONE_CHANGE, {}),
            opened=self.snap.now, agent=self.manager, source_turn=t.id, summary=r.summary,
            old=list(self.snap.milestones), new=new)))

    def asked(self, m: Milestone, msgs: List[C.Message], keys: tuple) -> bool:
        """Did the turn take in a message of these keys about this milestone (or answers that decide it again)?"""
        for x in msgs:
            v = x.text.values if x.text else {}
            if x.key in keys and v.get("milestone") == m.id and (v.get("title"), v.get("text")) == (m.title, m.text):
                return True  # an old one of a rewritten milestone does not count (MS-13)
            if x.key == K.GATE_ANSWERS and K.GATE_ANSWERS in keys and v.get("decide_again") == m.title:
                return True
        return False

    def decision(self, t: C.Turn, r: C.Reply, msgs: List[C.Message]) -> None:
        """MS-6, 9: rubric and score are kept; ACCEPT opens the approval gate; REVISE changes nothing here (the
        critic unticks items, so the checklist changes). Only a turn that answers this milestone's review counts: a
        late answer never decides a later or rewritten milestone. A PAUSE is held by gates and decided again."""
        m = self.cur()
        if m is None:
            return
        if r.rubric and self.asked(m, msgs, (K.MILESTONE_START,)):
            m = replace(m, rubric=list(r.rubric))
            self.put(m)
        if r.decision is None or m.accepted or self.open_gate(C.GateKind.MILESTONE, m.id):
            return
        if m.notified in ("", REOPENED) or not self.asked(m, msgs, (K.CHECKLIST_COMPLETE, K.GATE_ANSWERS)):
            return
        if any(isinstance(a, C.HoldDecision) and a.milestone_id == m.id and a.decision is not None for a in self.prior):
            return
        if r.rubric:
            m = replace(m, rubric=list(r.rubric))
        m = replace(m, score=r.score if r.score is not None else m.score)
        self.put(m)
        if r.decision == C.Decision.ACCEPT:
            self.accept(m, t, r)

    def accept(self, m: Milestone, t: C.Turn, r: C.Reply) -> None:
        s = self.snap.settings
        if not m.rubric:
            return self.tell_manager(K.REVIEW_RUBRIC_MISSING, {"milestone": m.id})
        if s.pass_score is not None and m.score is not None and m.score < s.pass_score:
            return self.tell_manager(K.REVIEW_SCORE_LOW, {"milestone": m.id, "score": m.score, "pass_score": s.pass_score})
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
    def gate_closed(self) -> None:
        """An approved change takes effect with its CloseGate; plan and milestone answers with the message that carries
        them to the manager (gates sends it once all answer gates are closed), so nothing moves while another gate
        is still open."""
        snap = self.snap
        closing = {a.gate_id: a.answer for a in self.prior
                   if isinstance(a, C.CloseGate) and not a.withdrawn and a.answer is not None}
        for gid, ans in closing.items():
            g = snap.gates.get(gid)
            if g and g.is_open and g.kind == C.GateKind.MILESTONE_CHANGE and ans.approve:
                self.apply_change(g)
        for a in self.prior:
            if isinstance(a, C.Send) and a.message.key == K.GATE_ANSWERS:
                for gid in a.message.gates:
                    g = snap.gates.get(gid)
                    ans = closing.get(gid) or (g.answer if g else None)
                    if g and ans:
                        self.answered(g, bool(ans.approve))

    def answered(self, g: C.Gate, ok: bool) -> None:
        if g.kind == C.GateKind.PLAN and ok and not self.ms and g.new:
            self.start_plan([replace(m) for m in g.new])
        elif g.kind == C.GateKind.MILESTONE:
            m = self.cur()
            if m is None or m.id != g.milestone:
                return
            if ok and m.accepted:
                self.advance(m)
            elif not ok:
                self.put(replace(m, accepted=False))

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
                untaken = any(x.key == K.CHECKLIST_COMPLETE and x.drop is None and not x.answered_at
                              and x.text.values.get("milestone") == m.id for x in self.snap.messages.values())
                reviewing = bool(m.accepted or open_gates or untaken)
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
        refreshed = [] if any(x.status == S.DONE for x in self.ms) else list(self.snap.agents)  # none at the very first
        m = replace(m, status=S.CURRENT, started=self.snap.now, ended=0.0, accepted=False, notified="",
                    asked_to_fix="", refreshed=refreshed)
        self.put(replace(m, checklist_path=self.checklist_path(m)))
        self.finished = False
        self.start_message(self.cur())

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
        if not ck.complete or ck.digest == m.notified or m.accepted or snap.paused or self.finished \
                or self.manager in snap.discussions:
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
        if critic is None or ck.error == m.asked_to_fix or t is None or t.started < m.started or not self.quiet():
            return
        self.put(replace(m, asked_to_fix=ck.error))
        msg = C.Message(self.new_id(), C.ORCH, critic.id, trigger=C.Trigger.ORCH,
                        text=TextRef(K.CHECKLIST_UNREADABLE, {"path": ck.path, "problem": ck.error}))
        self.out.append(C.Send(msg))

    def refresh(self) -> None:
        """Assumption 3: at a milestone start, each agent's session is compacted or renewed once it is settled. An agent
        with no session, or one that began after the milestone, needs none; one that is busy with something else
        (held, switching, retrying, in a terminal or a discussion) is left for later."""
        snap, m, done = self.snap, self.cur(), []
        if not any(x.status == S.DONE and 0 < x.ended <= m.started for x in self.ms):
            return  # the first milestone starts on new sessions
        for a in snap.agents.values():
            if a.id in m.refreshed or a.role in (C.Role.MONITOR, C.Role.AUDITOR):
                continue
            mode = a.fresh_session or ("never" if a.role == C.Role.MANAGER else "compact")
            if mode == "never" or not a.is_open or a.session is None or a.session.started > m.started:
                done.append(a.id)
            elif a.holds or a.switch or a.retry or a.id in snap.terminals or a.id in snap.discussions:
                continue
            elif snap.settled(a.id):
                self.out.append(C.RefreshSession(a.id, mode))
                done.append(a.id)
        if done:
            self.put(replace(m, refreshed=m.refreshed + done))
