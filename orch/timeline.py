"""The board's timeline (B9b): every message of a run as one-line items, by milestone → step → block.

A block is one level of communication: ``user`` (me ↔ the manager), ``mc`` (manager ↔ critic), ``cw`` (critic ↔
worker; ``lane`` names the worker, so several workers get a lane each) and ``mon`` (monitor → critic). Besides the
messages (``kind`` "msg", "question", "gate"), a block holds small tagged lines: progress checks, jobs, reminders
sent, and system events (retries, model switches, session resets, warnings).

The state poll carries only these lines (``line`` is the first line of the text, at most LINE_CHARS) for the open
step; the full text of an item comes from ``GET /api/message?id=<item id>`` when it is unfolded, folded steps from
``GET /api/items?g=<group key>``, and ``GET /api/search?q=`` looks through every message's full text.

Item ids: ``t:<turn>`` a turn's reply, ``t:<turn>:<i>`` its i-th <message> block, ``c:<chat id>`` a board chat
message (``c:<id>:<i>`` a block it passed on), ``q:<id>`` a question, ``e:<n>`` line n of events.jsonl,
``i:<file>`` a note I left in inbox/, ``r:<turn>`` the reminders added to a turn's prompt.
"""

from __future__ import annotations

import bisect
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import tags as T
from .recorder import read_json

LINE_CHARS = 160     # an item's one line
ITEMS_MAX = 300      # items of the open step sent with the state; older ones come with /api/items
GROUP_MAX = 500      # items per /api/items reply unless all are asked for
SEARCH_MAX = 100     # hits per search
OTHER_KINDS = ("system", "warning", "job", "reminder", "check")  # the small tagged lines the filter hides

_LEVEL = {"manager": "user", "verifier": "mc", "worker": "cw", "monitor": "mon", "auditor": "user"}
_DEFAULT_TO = {"manager": "verifier", "verifier": "worker", "worker": "verifier", "monitor": "verifier",
               "auditor": "human"}
_ROUTE_STATUS = {"read": "delivered", "delivered": "delivered", "waiting": "sent", "held": "held",
                 "superseded": "superseded", "failed": "failed", "unseen": "unseen", "unknown": "unknown"}


def block_of(a: str, b: str) -> str:
    """The level of communication between two roles ("human" is me)."""
    roles = {a, b}
    if "monitor" in roles:
        return "mon"
    if "human" in roles:
        other = [r for r in roles if r != "human"]
        return _LEVEL.get(other[0], "user") if other else "user"
    if "worker" in roles:
        return "cw"
    return "mc"


def line_of(text: str) -> str:
    """The first line of prose of a message, without tags or markdown."""
    t = re.sub(r"<system-reminder>[\s\S]*?(</system-reminder>|$)", " ", text or "")
    t = re.sub(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?/?>", "\n", t)
    first = ""
    for ln in t.splitlines():
        s = ln.strip()
        if not s or re.match(r"^(\||---|```|>)", s) or not re.search(r"\w", s):
            continue
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^\s*([-+*]|\d+[.)])\s+", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"[*_`]+", "", s).strip()
        if not s:
            continue
        first = first or s
        if len(s) < 80 and s.endswith(":"):  # a lead-in like "Here is the instruction:"
            continue
        return s[:LINE_CHARS]
    return first[:LINE_CHARS]


def turn_path(rd: Path, tid: str) -> Path:
    """rounds/r001/t02-sam.json for turn id r001-t02-sam."""
    head, _, rest = tid.partition("-")
    return rd / "rounds" / head / f"{rest}.json"


def read_events(p: Path, cache: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any]]]:
    """events.jsonl as (line number, event), parsed again only when the file has grown or changed."""
    try:
        stt = p.stat()
    except OSError:
        return []
    key = (stt.st_mtime_ns, stt.st_size)
    if cache.get("key") == key:
        return cache["rows"]
    rows = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for n, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("event") in EVENT_KINDS:
            rows.append((n, ev))
    cache.update(key=key, rows=rows)
    return rows


def _f(x: Any) -> str:
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x or 0)


# event -> (kind, text of the line); `who` names the event's agent
EVENT_KINDS: Dict[str, Callable[[Dict[str, Any], Callable[[Any], str]], Tuple[str, str]]] = {
    "retry": lambda e, who: ("system", f"{e.get('model')} failed ({e.get('reason') or 'error'}); retry "
                                       f"{e.get('retry')}/{e.get('of')} in {_f(e.get('wait_s'))}s"),
    "fallback": lambda e, who: ("system", f"{e.get('old')} failed ({e.get('reason') or 'error'}); switched to backup "
                                          f"{e.get('new')}"),
    "model_note": lambda e, who: ("system", f"model changed ({e.get('old')} → {e.get('new')}); {who(e.get('to'))} "
                                            f"told in one line"),
    "session_note": lambda e, who: ("system", (
        {"compact": "session compacted", "model": "model changed"}.get(e.get("change"), "new session")
        + (f": {e.get('reason')}" if e.get("reason") else ""))),
    "backend_switched": lambda e, who: ("system", "backend switched" + (f": {e.get('reason')}" if e.get("reason") else "")),
    "compact_skipped": lambda e, who: ("system", "compact skipped" + (f": {e.get('reason')}" if e.get("reason") else "")),
    "billing_error": lambda e, who: ("warning", str(e.get("problem") or e.get("reason") or "billing or account problem")),
    "size_warning": lambda e, who: ("warning", str(e.get("problem") or "size out of range")),
    "route_problem": lambda e, who: ("warning", str(e.get("problem") or "a message could not be sent")),
    "tooling_mention": lambda e, who: ("warning", str(e.get("problem") or "mentions the tooling")),
    "board_warning": lambda e, who: ("warning", str(e.get("problem") or str(e.get("what") or "").replace("_", " "))),
    "check_flag": lambda e, who: ("warning", f"progress check: {e.get('problem') or 'flagged'}"),
    "check_error": lambda e, who: ("warning", f"progress check of {who(e.get('agent'))} did not finish: "
                                              f"{e.get('error') or 'error'}"),
    "checkin_error": lambda e, who: ("warning", f"check-in failed: {e.get('error') or 'error'}"),
    "safety_timeout": lambda e, who: ("warning", f"{who(e.get('agent'))}'s turn reached the safety timeout"),
    "urgent": lambda e, who: ("system", f"urgent message cut in ({e.get('how') or 'now'})"),
    "message_superseded": lambda e, who: ("system", f"a waiting message to {who(e.get('to'))} was replaced by a "
                                                    f"later one"),
    "held": lambda e, who: ("system", f"the manager's next step waits: {e.get('why') or ''}".rstrip(": ")),
    "held_released": lambda e, who: ("system", "the held step goes on"),
    "night": lambda e, who: ("system", f"night mode {'on' if e.get('on') else 'off'}"),
    "discussion": lambda e, who: ("system", f"discussion {'on' if e.get('on') else 'off'}"),
    "golden_changed": lambda e, who: ("system", f"golden board changed ({e.get('chars')} chars)"),
    "plan_step": lambda e, who: ("system", f"step {e.get('done')} of {e.get('total')} done"
                                           + (f"; next: {e.get('next')}" if e.get("next") else "")),
    "job_wait": lambda e, who: ("job", f"waiting for {who(e.get('agent'))}'s job: {e.get('jobs') or ''}".rstrip(": ")),
    "job_end": lambda e, who: ("job", f"job wait ended: {e.get('why') or ''}".rstrip(": ")),
    "review_requested": lambda e, who: ("system", "review file asked for"),
    "review_written": lambda e, who: ("system", "review file written"),
    "review_comments": lambda e, who: ("system", "review comments sent back"),
    "gate": lambda e, who: ("gate", f"waiting for your OK: {e.get('title') or e.get('gate_kind') or 'gate'}"),
    "gate_decided": lambda e, who: ("gate", f"gate decided: {e.get('decision') or ''}".rstrip(": ")),
}
_EVENT_BLOCK = {"held": "user", "held_released": "user", "night": "user", "discussion": "user", "golden_changed": "user",
                "gate": "user", "gate_decided": "user", "review_requested": "user", "review_written": "user",
                "review_comments": "user", "plan_step": "cw", "job_wait": "cw", "job_end": "cw"}


class Builder:
    """All items of a run, from the board's model (`Board._model`)."""

    def __init__(self, rd: Path, d: Dict[str, Any], chat_all: List[Dict[str, Any]], events: List[Tuple[int, Dict[str, Any]]]):
        self.rd, self.d, self.chat_all, self.events = rd, d, chat_all, events
        self.role = {a["name"]: a["role"] for a in d.get("agents") or []}
        self.called = {a["name"]: a["called"] for a in d.get("agents") or []}
        self.mgr = next((a["name"] for a in d.get("agents") or [] if a["role"] == "manager"), "manager")
        self.items: List[Dict[str, Any]] = []

    # names and roles
    def name_for(self, role: str) -> str:
        if role == "human":
            return "you"
        names = [n for n, r in self.role.items() if r == role]
        return names[0] if len(names) == 1 else (role if role != "verifier" else "critic")

    def role_of(self, name: Any) -> str:
        n = str(name or "")
        if n in ("you", "human", ""):
            return "human"
        return self.role.get(n, {"critic": "verifier"}.get(n, n))

    def lane(self, a: str, b: str) -> Optional[str]:
        for n in (a, b):
            if self.role_of(n) == "worker":
                return n
        return None

    def add(self, iid: str, ts: Any, frm: str, to: str, kind: str, line: str, status: str = "", **extra: Any) -> Dict[str, Any]:
        blk = extra.pop("block", None) or block_of(self.role_of(frm), self.role_of(to))
        it = {"id": iid, "ts": str(ts or ""), "block": blk, "kind": kind, "from": frm, "to": to,
              "status": status, "line": (line or "")[:LINE_CHARS]}
        lane = self.lane(frm, to) if blk == "cw" else None
        if lane:
            it["lane"] = lane
        it.update({k: v for k, v in extra.items() if not (v is None or v is False or v == "")})
        it["_o"] = len(self.items)  # the order within one second: as built (turns in order, then the rest)
        self.items.append(it)
        return it

    # sources
    def build(self) -> List[Dict[str, Any]]:
        self.turns()
        self.chats()
        self.notes()
        self.questions()
        self.system()
        return self.items

    def turns(self) -> None:
        flow, routes = self.d.get("flow") or {}, self.d.get("routes") or {}
        for t in self.d.get("turns") or []:
            tid, agent, purpose = t["turn_id"], t.get("agent") or "", t.get("purpose") or ""
            role = self.role_of(agent)
            f = flow.get(purpose)
            to_role = {"critic": "verifier"}.get(f[1], f[1]) if f else _DEFAULT_TO.get(role, "human")
            if purpose == "review" and t.get("revise"):
                to_role = "verifier"
            kind = "msg"
            if purpose == "check":
                to_role = "verifier"
                kind = "msg" if role == "monitor" else "check"
            elif purpose == "compact":
                kind = "system"
            to = self.name_for(to_role)
            if t.get("par_worker") and to_role == "worker":  # B11: a review of one of several workers: its lane
                to = str(t["par_worker"])
            rows = routes.get(tid) or []
            run = t.get("status") == "running"
            if run:
                status = "running"
            elif t.get("status") not in ("ok", None):
                status = str(t.get("status"))
            else:
                mine = [r for r in rows if self.role_of(r.get("to")) == to_role]
                status = _ROUTE_STATUS.get(str(mine[0].get("status")), "") if mine else ""
            line = "session compacted" if kind == "system" else ("writing…" if run else line_of(t.get("output") or ""))
            ts = t.get("started_at")
            self.add(f"t:{tid}", ts, agent, to, kind, line, status, tid=tid, purpose=purpose,
                     ms=t.get("milestone") if t.get("milestone") is not None else -1, _turn=True,
                     len=t.get("output_len"), verdict=t.get("verdict"))
            for r in rows:  # each <message> it passed on to another level: an item in that block too
                if r.get("status") == "you" or not r.get("to") or self.role_of(r.get("to")) == to_role:
                    continue
                self.add(f"t:{tid}:{r.get('i')}", ts, agent, str(r.get("to")), "msg",
                         r.get("preview") or "", _ROUTE_STATUS.get(str(r.get("status")), str(r.get("status") or "")),
                         tid=tid, ms=t.get("milestone") if t.get("milestone") is not None else -1, _turn=True,
                         urgent=r.get("urgent"))
            rem = list(t.get("reminders") or []) or (["tags"] if t.get("tag_reminder") else [])
            if rem:
                self.add(f"r:{tid}", ts, "orch", agent, "reminder", "reminder added: " + ", ".join(map(str, rem)),
                         block=block_of(role, to_role), ms=t.get("milestone") if t.get("milestone") is not None else -1,
                         _turn=True)

    def chats(self) -> None:
        relays = self.d.get("relays") or {}
        by_id = {c.get("id"): c for c in self.chat_all}
        for c in self.chat_all:
            cid = str(c.get("id") or "")
            with_ = c.get("with") or self.mgr
            blk = "user" if c.get("qid") else None
            if c.get("role") == "human":
                rep = by_id.get(cid[:-2]) if cid.endswith("-q") else None
                st = (rep or {}).get("status")
                status = "delivered" if st == "ok" else ("sent" if st == "running" or rep is None else str(st))
                self.add(f"c:{cid}", c.get("ts"), "you", with_, "msg", line_of(c.get("text") or ""), status,
                         block=blk, qid=c.get("qid"), via="chat")
                continue
            who = c.get("who") or with_
            run = c.get("status") == "running"
            status = "running" if run else ("" if c.get("status") in ("ok", None) else str(c.get("status")))
            self.add(f"c:{cid}", c.get("ts"), who, "you", "msg", "writing…" if run else line_of(c.get("text") or ""),
                     status, block=blk, qid=c.get("qid"), via="chat", tid=cid if run else None,
                     problems=len(c.get("problems") or []) or None)
            if run:
                continue
            bodies = T.blocks(c.get("text") or "", "message")
            for r in relays.get(cid) or []:
                i = r.get("i")
                body = bodies[i][1] if isinstance(i, int) and i < len(bodies) else ""
                self.add(f"c:{cid}:{i}", c.get("ts"), who, str(r.get("to") or ""), "msg", line_of(body),
                         _ROUTE_STATUS.get(str(r.get("status")), str(r.get("status") or "")), via="chat",
                         urgent=r.get("urgent"))

    def notes(self) -> None:
        """Notes I left for an agent (board notes) and my answers, from inbox/ (read or still waiting)."""
        for where, d in (("delivered", self.rd / "inbox" / "consumed"), ("sent", self.rd / "inbox")):
            for p in sorted(d.glob("*.json")) if d.is_dir() else []:
                x = read_json(p) or {}
                if not isinstance(x, dict) or x.get("from") or not x.get("message"):
                    continue  # an agent's relayed block is shown with the reply it came from
                k = x.get("kind")
                if k not in (None, "note", "answer", "gate"):
                    continue
                ts = (f"{p.name[0:4]}-{p.name[4:6]}-{p.name[6:8]}T{p.name[9:11]}:{p.name[11:13]}:{p.name[13:15]}"
                      if p.name[:8].isdigit() else "")
                if k in ("answer", "gate"):
                    if x.get("via") == "conversation":
                        continue  # the settled answer is the manager's chat reply, already shown
                    label = f"answer to {x.get('qid')}" if k == "answer" else f"gate: {x.get('decision')}"
                    self.add(f"i:{p.name}", ts, "you", self.mgr, "gate" if k == "gate" else "msg",
                             f"{label} — {line_of(x.get('message') or '')}", where)
                    continue
                for to in x.get("to") or [self.mgr]:
                    self.add(f"i:{p.name}", ts, "you", to, "msg", line_of(x.get("message") or ""), where,
                             urgent=x.get("urgent"))

    def questions(self) -> None:
        st = self.d.get("_questions") or self.d.get("questions") or []
        queued = self.d.get("queued_answers") or {}
        for q in st:
            to = q.get("to") or "human"
            to = "you" if to == "human" else to
            open_ = q.get("status") == "open" and not queued.get(q.get("id"))
            self.add(f"q:{q.get('id')}", q.get("asked_at"), q.get("from") or "", to, "question",
                     f"{q.get('id')}: {line_of(q.get('text') or '')}", "open" if open_ else str(q.get("status") or ""),
                     needs=open_ and to == "you", qid=q.get("id"))

    def system(self) -> None:
        gate = (self.d.get("managed") or {}).get("gate")
        last_gate = max((n for n, ev in self.events if ev.get("event") == "gate"), default=-1)
        for n, ev in self.events:
            k = ev.get("event")
            kind, text = EVENT_KINDS[k](ev, lambda x: self.called.get(str(x or ""), str(x or "")))
            agent = str(ev.get("agent") or "")
            blk = _EVENT_BLOCK.get(k) or (_LEVEL.get(self.role_of(agent), "user") if agent else "user")
            frm = agent or ("you" if k in ("gate_decided", "night", "discussion") else "orch")
            to = "you" if k == "gate" else (self.mgr if k == "gate_decided" else "")
            if k == "gate":
                frm = self.mgr
            self.add(f"e:{n}", ev.get("ts"), frm, to, kind, text, "open" if k == "gate" and gate and n == last_gate else "",
                     block=blk, needs=bool(k == "gate" and gate and n == last_gate))


def assign(items: List[Dict[str, Any]], turns: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    """Each item's milestone (`ms`, -1 before the milestones) and step (`step`: 1.. by the critic's plan, total+1 after
    its last step, 0 for a milestone without a plan), by time. Returns {milestone: step labels}."""
    seq = sorted(((t.get("started_at") or "", t.get("milestone")) for t in turns if t.get("started_at")),
                 key=lambda x: x[0])
    keys = [x[0] for x in seq]
    plans: Dict[int, List[Tuple[str, int, int]]] = {}
    labels: Dict[int, List[str]] = {}
    for t in turns:
        if t.get("plan") and t.get("milestone") is not None and t.get("status") != "running":
            plans.setdefault(t["milestone"], []).append((t.get("ended_at") or t.get("started_at") or "",
                                                          int(t["plan"][0]), int(t["plan"][1])))
            if t.get("plan_text"):
                from .report import checklist_items
                labels[t["milestone"]] = [s for _, s in checklist_items(t["plan_text"])]
    for ps in plans.values():
        ps.sort()
    last_ms = seq[-1][1] if seq and seq[-1][1] is not None else -1
    for it in items:
        ts = it["ts"]
        if it.pop("_turn", False):
            ms = it.get("ms", -1)
        elif ts:
            i = bisect.bisect_right(keys, ts) - 1
            ms = seq[i][1] if i >= 0 and seq[i][1] is not None else -1
        else:
            ms = last_ms
        it["ms"] = ms
        ps = plans.get(ms)
        if not ps:
            it["step"] = 0
            continue
        prev = None
        for p in ps:
            if ts and p[0] <= ts:
                prev = p
            elif ts:
                break
        if not ts:
            prev = ps[-1]
        done, total = (prev[1], prev[2]) if prev else (0, ps[0][2])
        it["step"] = min(done + 1, total + 1) if total else 0
    return labels


def groups(items: List[Dict[str, Any]], labels: Dict[int, List[str]]) -> Tuple[List[Dict[str, Any]], str]:
    """The (milestone, step) groups, oldest first, and the key of the open one: where the newest item is."""
    out: Dict[str, Dict[str, Any]] = {}
    newest: Tuple[str, str] = ("", "")
    for it in items:
        k = key_of(it)
        g = out.get(k)
        if g is None:
            ls = labels.get(it["ms"]) or []
            s = it["step"]
            label = (ls[s - 1] if 0 < s <= len(ls) else "after the last step" if ls and s > len(ls) else "")
            g = out[k] = {"key": k, "ms": it["ms"], "step": s, "label": label[:LINE_CHARS], "n": 0, "needs": 0,
                          "other": 0, "last": ""}
        g["n"] += 1
        g["needs"] += 1 if it.get("needs") else 0
        g["other"] += 1 if it["kind"] in OTHER_KINDS else 0
        g["last"] = max(g["last"], it["ts"])
        if it["ts"] and (it["ts"], k) >= newest:
            newest = (it["ts"], k)
    gs = sorted(out.values(), key=lambda g: (g["ms"], g["step"]))
    return gs, newest[1] or (gs[-1]["key"] if gs else "")


def key_of(it: Dict[str, Any]) -> str:
    return f"{it['ms']}|{it['step']}"


def order(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = sorted(items, key=lambda it: (it["ts"] or "~", it.get("_o", 0)))
    for it in out:
        it.pop("_o", None)
    return out


def snippet(text: str, q: str, width: int = 90) -> str:
    i = text.lower().find(q.lower())
    if i < 0:
        return ""
    a, b = max(0, i - width), min(len(text), i + len(q) + width)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")
