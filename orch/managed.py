"""Managed mode: three layers of review, so each layer above reads less.

    me (human)  ←→  manager  ←→  critic  ←→  worker(s)

* The **manager** sees my goal, its own system prompt, the critic's milestone reports and the critic's
  questions. It drafts the milestones and its scored rubric (I approve the plan), starts each milestone with
  a high-level note to the critic, scores the critic's report (``DECISION: ACCEPT`` / ``REVISE``) and
  answers the critic's questions, passing the ones only I can decide up to me.
* The **critic** turns the manager's note into concrete instructions and its own checklist, reviews every
  worker turn (``VERDICT: PASS/FAIL``, gated by the checklist as in pipeline mode), answers the worker's
  questions, asks the manager when unsure, and reports to the manager when a milestone is ready.
* The **worker(s)** do the work in the repository.

Questions never stop the run: whoever asks carries on with a stated assumption, the question is labelled
(Q1, Q2 …) and goes one level up; when it is answered — by the critic, the manager or finally by me — the
answer is relayed down and the critic turns it into a checklist item. The run only pauses for my OK on the
plan and after each accepted milestone (configurable), and those pauses can be answered from the board
(``orch run --serve``), the terminal, or ``orch approve``.

Nothing in the agents' messages mentions a workflow; relayed text reads like a normal conversation.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .backends import StopRequested
from .config import AgentConfig
from .controller import MonitorRunner, checklist_state, parse_options, resolve_choice, strip_checklist, \
    trailing_question, truncate
from .recorder import now_iso

_VERDICT_LINE = re.compile(r"\n*\s*VERDICT:\s*\**\s*\w+\**\s*$", re.I)
_SCORE = re.compile(r"(?:overall|total|final)[^\n\d]{0,40}?(\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+(?:\.\d+)?)", re.I)


# --------------------------------------------------------------------------- parsing helpers


def parse_milestones(text: str) -> List[Dict[str, str]]:
    """The first numbered list that starts at 1: [{title, done_when, text}]."""
    best: List[Tuple[int, str]] = []
    cur: List[Tuple[int, str]] = []
    for ln in (text or "").split("\n"):
        m = re.match(r"^\s*(?:\*\*)?(\d{1,2})[.)](?:\*\*)?\s+(\S.*)$", ln)
        if m:
            n = int(m.group(1))
            if n == 1:
                if len(cur) > len(best):
                    best = cur
                cur = [(1, m.group(2))]
            elif cur and n == cur[-1][0] + 1:
                cur.append((n, m.group(2)))
            continue
        if cur and ln.strip() and ln.startswith((" ", "\t")):
            cur[-1] = (cur[-1][0], cur[-1][1] + " " + ln.strip())  # wrapped line
        elif cur and ln.strip() and not ln.startswith((" ", "\t")):
            if len(cur) > len(best):
                best = cur
            cur = []
    if len(cur) > len(best):
        best = cur
    out = []
    for _, line in best:
        clean = line.strip()
        parts = re.split(r"\s+[—–-]\s+|:\s+", clean, maxsplit=1)
        title = re.sub(r"[*_`]", "", parts[0]).strip()
        out.append({"title": title, "done_when": parts[1].strip() if len(parts) > 1 else "", "text": clean})
    return out


def parse_score(text: str) -> Optional[float]:
    """'Overall score: 4.2/5' -> 4.2 (scaled to 5 when given out of 10/100)."""
    hits = list(_SCORE.finditer(text or ""))
    if not hits:
        return None
    v, top = float(hits[-1].group(1)), float(hits[-1].group(2))
    return round(v * 5.0 / top, 2) if top and top != 5 else v


def parse_criteria(text: str) -> List[List[Any]]:
    """[[criterion, score(0-5)]] from the first Markdown table that has a numeric score column."""
    rows: List[List[Any]] = []
    for ln in (text or "").split("\n"):
        if not ln.strip().startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= set("-: "):
            continue
        name = re.sub(r"[*_`]", "", cells[0]).strip()
        for c in cells[1:]:
            m = re.match(r"^\**\s*(\d+(?:\.\d+)?)\s*(?:/\s*(\d+))?\s*\**$", c)
            if m:
                v, top = float(m.group(1)), float(m.group(2) or 5)
                if name and not re.match(r"^(overall|total)\b", name, re.I):
                    rows.append([name, round(v * 5.0 / top, 2) if top != 5 else v])
                break
    return rows


_NOTE_STOP = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|#{1,6}\s|\W*(?:NOTES?|QUESTIONS?)\s+FOR\b|\W*(?:DECISION|VERDICT)\s*:|"
                        r"[*_]*\s*(?:review|feedback|instructions?|reply|message)?\s*(?:for|to)\s+(?:the\s+)?[\w-]+\s*:[*_]*\s*$)",
                        re.I)


def _clip(text: str, limit: int) -> str:
    """The start of a text, cut at a word, with a pointer to the rest."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + " … (in full in the decisions file)"


def note_blocks(text: str, pattern: str, to_end: bool = False) -> List[Tuple[str, str]]:
    """[(addressee, note)] for every 'NOTE FOR name: ...'. A note is its first paragraph plus a list it introduces
    ("...:" then bullets); ``to_end`` (a chat reply, where the note comes last) takes everything up to a divider,
    heading or the next label."""
    out = []
    for m in re.finditer(pattern, text or "", re.I | re.M):
        paras = text[m.end():].split("\n\n")
        keep = [paras[0]]
        for p in paras[1:]:
            if not p.strip() or _NOTE_STOP.match(p):
                break
            listy = bool(re.match(r"\s*([-*+]|\d+[.)])\s", p))
            prev = keep[-1].rstrip()
            if to_end or (listy and (prev.endswith(":") or len(keep) > 1)):  # in a run reply: only the list it introduces
                keep.append(p)
            else:
                break
        out.append((m.group(1).strip(" *_"), "\n\n".join(keep).strip()))
    return out


# --------------------------------------------------------------------------- runner


class ManagedRunner(MonitorRunner):
    questions_pause = False

    # ------------------------------------------------------------------ basics

    @property
    def manager(self) -> AgentConfig:
        return self.mon.by_role("manager")[0]

    @property
    def critic(self) -> AgentConfig:
        return self.mon.by_role("verifier")[0]

    @property
    def workers(self) -> List[AgentConfig]:
        return self.mon.by_role("worker")

    def inbox_broadcast(self, agent: AgentConfig) -> bool:
        return agent.role == "manager"  # notes to nobody in particular are for the manager

    @property
    def mg(self) -> Dict[str, Any]:
        return self.st.setdefault("managed", {"phase": "plan", "ms": 0, "ms_round": 0, "milestones": [],
                                              "feedback": {}, "asks": 0})

    def names(self) -> Dict[str, str]:
        return {"manager": self.manager.called, "critic": self.critic.called,
                "worker": " and ".join(w.called for w in self.workers)}

    def mtpl(self, name: str, **values: Any) -> str:
        values.setdefault("instructions", self.mon.controller.instructions or "")
        values.setdefault("decisions", "")
        return self.tpl(name, **{**self.names(), **values})

    def has_session(self, a: AgentConfig) -> bool:
        return bool((self.st["sessions"].get(a.name) or {}).get("session_id"))

    def cur(self) -> Optional[Dict[str, Any]]:
        ms = self.mg.get("milestones") or []
        i = self.mg.get("ms", 0)
        return ms[i] if i < len(ms) else None

    def say(self, agent: AgentConfig, prompt: str, purpose: str) -> Dict[str, Any]:
        """One turn of one agent, tagged with the milestone it belongs to."""
        mg = self.mg
        if agent.role == "manager":
            self.sync_chat_session(agent)
        r = self.turn(agent, prompt, int(self.st.get("round", 0)), purpose,
                      extra={"milestone": mg.get("ms", 0) if mg.get("milestones") else None,
                             "milestone_round": mg.get("ms_round", 0)})
        if r["status"] != "ok":
            if self.mon.controller.stop_on_error:
                raise _Failed(f"{agent.name} {purpose} turn ended with status {r['status']}")
        if agent.role == "verifier":
            for note in self.notes_up(r.get("output") or ""):
                self.forward(self.manager, self.mtpl("relay_note_up", note=note).strip())
        return r

    def notes_up(self, text: str) -> List[str]:
        """'NOTE FOR YOU: ...' (or 'NOTE FOR <manager>: ...') paragraphs in a critic reply: FYIs for the manager."""
        mgr = re.sub(r"^the\s+", "", self.manager.called.lower())
        return [body for who, body in note_blocks(text, self.mon.controller.note_pattern)
                if re.sub(r"^the\s+", "", who.lower()) in ("you", "me", mgr, self.manager.name.lower()) and body]

    def sync_chat_session(self, agent: AgentConfig) -> None:
        """If my board chat moved the manager to a new session id, continue from there."""
        try:
            rows = [json.loads(x) for x in (self.rec.run_dir / "chat.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return
        sid = next((r.get("session_id") for r in reversed(rows) if r.get("session_id")), None)
        if sid and sid != (self.st["sessions"].get(agent.name) or {}).get("session_id"):
            self.rec.update_session(agent.name, session_id=sid)

    def recover_interrupted(self) -> List[Dict[str, Any]]:
        self.backfill_decisions()
        out = super().recover_interrupted()
        mg = self.mg
        for t in out:  # runs started before `in_work` existed
            if t.get("purpose") == "work" and mg.get("phase") == "work" and not mg.get("in_work"):
                mg["in_work"] = t["round"]
        self.rec.save()
        return out

    def forward(self, agent: AgentConfig, text: str) -> None:
        if text:
            self.st.setdefault("fwd", {}).setdefault(agent.name, []).append(text)

    # ------------------------------------------------------------------ questions

    def ask_block(self, text: str, role: str) -> str:
        """The question block of a reply (empty if it asks nothing of the level above)."""
        c = self.mon.controller
        text = text or ""
        m = re.search(c.question_pattern, text, re.I | re.M) or re.search(c.human_pattern, text, re.I | re.M)
        if m:
            start = text.rfind("\n", 0, m.start()) + 1
            paras = text[start:].split("\n\n")
            block = paras[0]
            if len(paras) > 1 and parse_options(paras[1]) and not parse_options(paras[0]):
                block += "\n" + paras[1]
            return block.strip()
        if role == "worker":
            return trailing_question(text)
        return ""

    @staticmethod
    def without(text: str, block: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", (text or "").replace(block, "")).strip() if block else (text or "").strip()

    def new_question(self, frm: AgentConfig, to: str, text: str, turn: str, parent: Optional[List[str]] = None,
                     assumption: str = "") -> Dict[str, Any]:
        qs = self.st.setdefault("questions", [])
        q = {"id": f"Q{len(qs) + 1}", "from": frm.name, "to": to, "text": text,
             "options": parse_options(text), "status": "open", "parent": parent or [],
             "milestone": self.mg.get("ms", 0), "round": self.st.get("round", 0), "turn": turn,
             "asked_at": now_iso(), "answer": "", "answered_by": "", "answer_turn": "", "assumption": assumption}
        qs.append(q)
        self.rec.event("question", qid=q["id"], frm=frm.name, to=to)
        self.rec.progress(f"❓ **{q['id']}** {frm.name} → {to}: {truncate(text, 300)}")
        if to == "human":
            self.log("\n" + "?" * 60 + f"\n{q['id']} for you from {frm.name} (the run keeps going):\n\n{text}\n\n"
                     f"Answer any time: type `{q['id']}: your answer` here, or use the board.\n" + "?" * 60)
        return q

    def q(self, qid: str) -> Optional[Dict[str, Any]]:
        return next((x for x in self.st.get("questions") or [] if x["id"] == qid), None)

    def open_qs(self, frm: str, to: Optional[str] = None) -> List[Dict[str, Any]]:
        return [x for x in self.st.get("questions") or []
                if x["from"] == frm and x["status"] == "open" and (to is None or x["to"] == to)]

    def settle(self, q: Dict[str, Any], by: str, answer: str, turn: str = "", status: str = "answered") -> None:
        q.update(status=status, answered_by=by if status == "answered" else q.get("answered_by", ""),
                 answer=truncate(answer, 4000) if status == "answered" else q.get("answer", ""),
                 answer_turn=turn, answered_at=now_iso())
        if status == "answered":
            for pid in q.get("parent") or []:
                p = self.q(pid)
                if p and p["status"] in ("open", "passed_up"):
                    self.settle(p, by, answer, turn)

    def question_line(self, text: str) -> str:
        """The question itself, without the 'QUESTIONS FOR ...' header or the options."""
        lines = []
        for ln in (text or "").split("\n"):
            ln = re.sub(r"^\W*(QUESTIONS?|NEEDS[_ ]HUMAN)\b[^:\n]*:?\s*", "", ln.strip(), flags=re.I).strip()
            if ln and not parse_options(ln + "\n" + ln):
                lines.append(ln)
        return next((ln for ln in lines if ln.endswith("?")), lines[0] if lines else text)

    def human_answer(self, qid: str, answer: str, via: str = "") -> None:
        q = self.q(str(qid).upper())
        if not q:
            self.log(f"no question {qid}")
            return
        ans = resolve_choice(answer, [tuple(x) for x in q.get("options") or []])
        self.settle(q, "human", ans)
        short = truncate(self.question_line(q["text"]), 300)
        if via == "conversation":  # the manager was in the conversation; only the critic needs telling
            q["via"] = via
            self.forward(self.critic, self.mtpl("relay_settled_critic", question=short, answer=ans).strip())
        else:
            self.forward(self.manager, self.mtpl("relay_answer_manager", question=short, answer=ans).strip())
            self.forward(self.critic, self.mtpl("relay_answer_critic", question=short, answer=ans).strip())
        self.decide(ans if via == "conversation" else f"{self.short_question(q['text'])}: {ans}", "you", q["id"])
        self.rec.event("human_answer", qid=q["id"])
        self.rec.progress(f"✅ **{q['id']}** answered by me: {truncate(ans, 300)}")
        self.log(f"  ✓ {q['id']} answered; the answer reaches {self.manager.name} and {self.critic.name} next")
        self.rec.save()

    # ------------------------------------------------------------------ decisions register

    def decide(self, text: str, by: str, source: str, at: str = "") -> None:
        """Record something settled: my answers and notes, gate messages, the manager's answers to the critic."""
        text = (text or "").strip()
        regs = self.st.setdefault("decisions", [])
        if not text or any(d["text"] == text for d in regs):
            return
        d = {"id": f"S{len(regs) + 1}", "text": truncate(text, 1500), "by": by, "source": source, "at": at or now_iso()}
        regs.append(d)
        self.rec.event("decision", id=d["id"], by=by, source=source)
        self.write_decisions()

    def write_decisions(self) -> None:
        regs = self.st.get("decisions") or []
        lines = ["# Decisions", "", "Binding for the whole run; where two say different things, the later one holds.", ""]
        for d in regs:
            lines.append(f"## {d['id']} · {d['by']} · {d['at'][:16].replace('T', ' ')} · {d['source']}")
            lines += ["", d["text"], ""]
        (self.rec.run_dir / "decisions.md").write_text("\n".join(lines), encoding="utf-8")
        self.rec.save()

    @staticmethod
    def short_question(text: str) -> str:
        """A question as one plain line for the register: no header, no options, no numbering of a single question."""
        lines = [ln.strip() for ln in (text or "").split("\n")]
        lines = [ln for ln in lines if ln and not re.match(r"^\W*(QUESTIONS?|NEEDS[_ ]HUMAN)\b", ln, re.I)
                 and not re.match(r"^[A-H][).]\s", ln)]
        if sum(1 for ln in lines if re.match(r"^\d+[.)]\s", ln)) <= 1:
            lines = [re.sub(r"^\d+[.)]\s+", "", ln) for ln in lines]
        return truncate(re.sub(r"\*\*", "", " ".join(lines)), 400)

    def decisions_text(self) -> str:
        regs = self.st.get("decisions") or []
        if not regs:
            return ""
        head = self.mtpl("decisions_intro", path=str(self.rec.run_dir / "decisions.md")).strip()
        body = "\n".join(f"- **{d['id']}** ({d['by']}, {d['at'][:16].replace('T', ' ')}): "
                         + _clip(d["text"], 500).replace("\n", "\n  ") for d in regs)  # in full in the file
        return f"{head}\n\n{body}"

    def backfill_decisions(self) -> None:
        """A run started before the register existed: rebuild it from answers, my notes and gate messages."""
        if "decisions" in self.st:
            return
        self.st["decisions"] = []
        found = []
        for q in self.st.get("questions") or []:
            who = q.get("answered_by")  # only answers from the one asked: an answer to a passed-up question is copied down
            if q.get("status") == "answered" and q.get("answer") and (
                    (who == "human" and q.get("to") == "human") or (who == self.manager.name and q.get("to") == self.manager.name)):
                by = "you" if q["answered_by"] == "human" else self.manager.called
                text = q["answer"] if q.get("via") == "conversation" else f"{self.short_question(q['text'])}: {q['answer']}"
                found.append((q.get("answered_at") or q.get("asked_at") or "", text, by, q["id"]))
        for p in sorted((self.rec.run_dir / "inbox" / "consumed").glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and not data.get("kind") and str(data.get("message", "")).strip():
                ts = p.name[:15]
                at = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}" if len(ts) == 15 else ""
                found.append((at, data["message"], "you", "note to " + ", ".join(data.get("to") or ["all"])))
        for g in (self.mg.get("gates") or []):
            if g.get("message"):
                found.append((g.get("decided_at") or "", f"{g.get('title', 'Gate')}: {g['decision']}. {g['message']}",
                              "you", "gate"))
        for at, text, by, src in sorted(found, key=lambda x: x[0]):
            self.decide(text, by, src, at=at)
        self.write_decisions()

    def human_text(self, agent: Optional[str], msgs: List[Dict[str, Any]], annotate: bool = False) -> str:
        for x in msgs:  # my notes are decisions too
            if agent is None or x["to"] is None or agent in x["to"]:
                self.decide(x["message"], "you", "note to " + ", ".join(x["to"] or ["all"]))
        return MonitorRunner.human_text(agent, msgs, annotate)

    def fresh_sessions(self) -> None:
        """Agents set to `fresh_session: milestone` start each milestone in a new session (once per milestone)."""
        mg = self.mg
        if mg.get("fresh_for") == mg.get("ms"):
            return
        for a in self.mon.agents:
            if a.fresh_session == "milestone" and self.has_session(a):
                sess = self.st["sessions"].get(a.name) or {}
                prev = {k: v for k, v in sess.items() if k != "previous"}
                self.rec.update_session(a.name, session_id=None, turns=0, previous=(sess.get("previous") or []) + [prev])
                self.st.setdefault("fwd", {}).setdefault(a.name, []).insert(
                    0, self.mtpl("milestone_fresh", cwd=a.working_dir).strip())
                self.rec.event("fresh_session", agent=a.name, milestone=mg.get("ms"), session_id=sess.get("session_id"))
                self.log(f"{a.name}: fresh session for milestone {mg.get('ms', 0) + 1}")
        mg["fresh_for"] = mg.get("ms")
        self.rec.save()

    def for_me(self, rest: str) -> str:
        """What follows the manager's question that is meant for me (its recommendation, context), up to the
        first divider or the part addressed to the critic, which goes to the critic and not onto my question."""
        name = re.escape(re.sub(r"^the\s+", "", self.critic.called, flags=re.I))
        stop = re.search(rf"^\s*(?:-{{3,}}|\*{{3,}}|_{{3,}}|#{{1,6}}\s|[*_]*\s*(?:reply|message|answer|notes?|instructions?)?\s*"
                         rf"(?:to|for)\s+(?:the\s+)?(?:{name}|critic)\b|NOTES?\s+FOR\b)", rest or "", re.I | re.M)
        return (rest[:stop.start()] if stop else rest or "").strip()

    def after_manager(self, r: Dict[str, Any], parent: Optional[List[str]] = None) -> str:
        """Record the manager's questions for me (non-blocking), pass on its notes; return the rest."""
        text = r.get("output") or ""
        block = self.ask_block(text, "manager")
        rest = self.without(text, block)
        if block:
            self.new_question(self.manager, "human", block, r["turn_id"], parent,
                              assumption=truncate(self.for_me(rest), 1500))
        for who, note in note_blocks(rest, self.mon.controller.note_pattern):
            a = self.find_agent(who)
            if a and a.name != self.manager.name:
                self.forward(a, self.mtpl("relay_note", note=note).strip())
        return rest

    def find_agent(self, who: str) -> Optional[AgentConfig]:
        w = re.sub(r"^the\s+", "", who.lower().strip())
        for a in self.mon.agents:
            if w in (a.name.lower(), re.sub(r"^the\s+", "", a.called.lower())):
                return a
        return None

    def critic_asks(self, r: Dict[str, Any], after: str) -> bool:
        """If the critic asked the manager something, record it and answer it right away (next phase)."""
        block = self.ask_block(r.get("output") or "", "verifier")
        wq = [x["id"] for w in self.workers for x in self.open_qs(w.name)]
        if not block:
            for qid in wq:
                self.settle(self.q(qid), self.critic.name, "", r["turn_id"])  # answered in the critic's reply
            return False
        for qid in wq:
            self.settle(self.q(qid), "", "", r["turn_id"], status="passed_up")
        q = self.new_question(self.critic, self.manager.name, block, r["turn_id"], wq)
        self.mg.update(ask=q["id"], after=after, phase="answer")
        return True

    def worker_feedback(self, text: str, append: bool = False) -> None:
        fb = self.mg.setdefault("feedback", {})
        for w in self.workers:
            fb[w.name] = (fb.get(w.name, "") + "\n\n" + text).strip() if append and fb.get(w.name) else text

    def for_critic(self, text: str) -> str:
        """The part of a manager reply meant for the critic (after a 'For <critic>:' line), else all of it
        without the score table and score lines."""
        name = re.escape(re.sub(r"^the\s+", "", self.critic.called, flags=re.I))
        hits = list(re.finditer(rf"^\W*(?:message\s+|note\s+)?(?:for|to)\s+(?:the\s+)?{name}\s*:\**[ \t]*", text or "",
                                re.I | re.M))
        if hits:
            return text[hits[-1].end():].strip()
        keep = [ln for ln in (text or "").split("\n")
                if not ln.strip().startswith("|") and not re.search(r"\b(overall|total)\b.*\d\s*/\s*\d", ln, re.I)]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()

    def clean_critic(self, text: str) -> str:
        """What the worker gets from a critic reply: no verdict line, no checklist, no questions upward."""
        t = _VERDICT_LINE.sub("", text or "").strip()
        t = self.without(t, self.ask_block(t, "verifier"))
        for note in self.notes_up(t):
            t = re.sub(r"^\W*NOTES? FOR[^:\n]*:\s*" + re.escape(note) + r"\s*", "", t, flags=re.I | re.M).strip()
        if self.mon.controller.checklist:
            t = strip_checklist(t) or t
        # a reply with its own "Review for <worker>" / "For <worker>:" section: only that section goes down;
        # what comes before it is the critic talking to the manager (e.g. about a relayed answer)
        names = {re.sub(r"^the\s+", "", n, flags=re.I) for w in self.workers for n in (w.called, w.name)} | {"worker"}
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        m = re.search(rf"^[#*_\s]*(?:review|feedback|notes?|message|instructions?)?\s*(?:for|to)\s+(?:the\s+)?(?:{alt})[\s:*_]*\n", t, re.I | re.M)
        if m and t[m.end():].strip():
            t = t[m.end():].strip()
        return re.sub(r"^(?:\s*(?:-{3,}|\*{3,}|_{3,})\s*\n)+", "", t).strip()

    # ------------------------------------------------------------------ live terminal input & control files

    def pump_line(self, line: str) -> bool:
        m = re.match(r"^(Q\d+)\s*[:：]\s*(.+)$", line, re.I)
        if not m:
            return False
        self.control({"kind": "answer", "qid": m.group(1).upper(), "message": m.group(2)})
        self.log(f"  ✓ answer to {m.group(1).upper()} queued")
        return True

    def control(self, data: Dict[str, Any]) -> None:
        inbox = self.rec.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 100000:05d}-{data['kind']}.json") \
            .write_text(json.dumps(data), encoding="utf-8")

    def check_control(self) -> None:
        answers: Dict[str, List[str]] = {}  # several answers to one question become one relay
        talked: Dict[str, str] = {}  # settled in conversation with the manager: its summary is the answer
        for d in self.rec.take_control():
            if d.get("kind") == "answer" and d.get("qid") and d.get("via") == "conversation":
                talked[str(d["qid"]).upper()] = str(d.get("message", "")).strip()
            elif d.get("kind") == "answer" and d.get("qid"):
                msgs = answers.setdefault(str(d["qid"]).upper(), [])
                m = str(d.get("message", "")).strip()
                if m and m.lower() not in (x.lower() for x in msgs):
                    msgs.append(m)
            elif d.get("kind") == "gate":
                self.st.setdefault("gate_queue", []).append(d)
        for qid, msgs in answers.items():
            if msgs and qid not in talked:
                self.human_answer(qid, "\n\n".join(msgs))
        for qid, text in talked.items():
            if text:
                self.human_answer(qid, text, via="conversation")
        self.rec.save()

    def wait_gate(self, gate: Dict[str, Any]) -> Dict[str, Any]:
        """Pause until I approve or send it back (board, terminal, or `orch approve`)."""
        mg = self.mg
        gate["key"] = f"{gate['kind']}-{gate.get('milestone') or 0}-{len(mg.get('gates') or [])}"
        mg["gate"] = {**gate, "since": now_iso()}
        self.rec.set(status="awaiting_approval")
        self.rec.event("gate", gate_kind=gate.get("kind"), milestone=gate.get("milestone"), title=gate.get("title"))
        self.rec.progress(f"⏸ **Waiting for my OK:** {gate['title']}")
        (self.rec.run_dir / "AWAITING_APPROVAL.md").write_text(
            f"# Waiting for your OK: {gate['title']}\n\n{gate.get('text', '')}\n\n"
            "Approve on the board, or `orch approve <run>` (add `--revise \"what still needs work\"`).\n",
            encoding="utf-8")
        tty = sys.stdin.isatty()
        self.log("\n" + "=" * 60 + f"\nWAITING FOR YOUR OK — {gate['title']}\n\n{truncate(gate.get('text', ''), 2500)}\n"
                 + ("\nPress Enter to approve, or type what still needs work (Q3: … answers a question)."
                    if tty else "\nApprove on the board or with `orch approve`.") + "\n" + "=" * 60)
        if tty:
            self.start_input_pump()
            self._awaiting = True
        decision: Optional[Dict[str, Any]] = None
        try:
            while decision is None:
                if self.should_stop():
                    raise StopRequested()
                self.check_control()
                queue_ = self.st.get("gate_queue") or []
                while queue_ and decision is None:
                    d = queue_.pop(0)
                    if not d.get("gate") or d["gate"] == gate["key"]:  # stale clicks for an older gate are dropped
                        decision = d
                if decision is not None:
                    break
                if tty:
                    import queue as _q
                    try:
                        line = self._answers.get(timeout=0.5).strip()
                    except _q.Empty:
                        continue
                    if self.pump_line(line):
                        continue
                    ok = line.lower() in ("", "y", "yes", "ok", "approve", "approved", "lgtm")
                    decision = {"decision": "approve" if ok else "revise", "message": "" if ok else line}
                else:
                    time.sleep(0.5)
        finally:
            if tty:
                self._awaiting = False
        (self.rec.run_dir / "AWAITING_APPROVAL.md").unlink(missing_ok=True)
        decision["decision"] = "approve" if str(decision.get("decision", "approve")).startswith("approve") else "revise"
        mg["gate"] = None
        mg.setdefault("gates", []).append({**gate, "text": None, "decided_at": now_iso(),
                                           "decision": decision["decision"], "message": decision.get("message", "")})
        self.rec.set(status="running")
        self.rec.event("gate_decided", decision=decision["decision"])
        self.rec.progress(f"▶ **{gate['title']}:** {decision['decision']}"
                          + (f" — {decision.get('message')}" if decision.get("message") else ""))
        return decision

    # ------------------------------------------------------------------ the loop

    def main_loop(self) -> str:
        mg = self.mg
        while True:
            self.check_control()
            ph = mg.get("phase", "plan")
            if ph == "done":
                return "completed"
            try:
                out = getattr(self, "ph_" + ph)(mg)
            except _Failed as f:
                self.rec.set(error=str(f))
                return "failed"
            self.rec.save()
            if out:
                return out

    # ---- planning
    def ph_plan(self, mg: Dict[str, Any]) -> Optional[str]:
        if mg.get("comment"):
            prompt = self.mtpl("manager_plan_revise", comment=mg["comment"])
        else:
            prompt = self.mtpl("manager_plan", human_messages="")
        r = self.say(self.manager, prompt, "plan")
        ms = parse_milestones(r["output"])
        if not ms:
            r = self.say(self.manager, "I could not find the milestone list in your reply. Please give it again as a "
                                       "numbered list, one milestone per line: \"1. Short title — what has to be true "
                                       "when it is done\".", "plan")
            ms = parse_milestones(r["output"])
        if not ms:
            raise _Failed("the manager's plan has no numbered milestone list")
        text = self.after_manager(r)
        mg.update(plan_text=text, plan_turn=r["turn_id"], comment="",
                  milestones=[{**x, "status": "pending", "rounds": 0, "score": None} for x in ms])
        self.rec.progress("**Plan:**\n" + "\n".join(f"{i + 1}. {x['text']}" for i, x in enumerate(ms)))
        mg["phase"] = "plan_gate" if self.mon.controller.approve_plan else "kickoff"
        return None

    def ph_plan_gate(self, mg: Dict[str, Any]) -> Optional[str]:
        d = self.wait_gate({"kind": "plan", "title": "Milestone plan", "text": mg.get("plan_text", ""),
                            "turn": mg.get("plan_turn")})
        if d["decision"] == "approve":
            if d.get("message"):
                self.forward(self.manager, d["message"])
            mg.update(phase="kickoff", ms=0)
        else:
            mg.update(phase="plan", comment=d.get("message") or "Please rework the plan.")
        return None

    # ---- one milestone
    def ph_kickoff(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        ms.update(status="active", started_at=now_iso(), started_round=int(self.st.get("round", 0)) + 1)
        self.fresh_sessions()
        r = self.say(self.manager, self.mtpl("manager_kickoff", n=mg["ms"] + 1, total=len(mg["milestones"]),
                                             milestone=ms["text"], human_messages="",
                                             decisions=self.decisions_text()), "kickoff")
        mg.update(note=self.for_critic(self.after_manager(r)), ms_round=0, limit=False, phase="brief")
        return None

    def ph_brief(self, mg: Dict[str, Any]) -> Optional[str]:
        v = self.critic
        r = self.say(v, self.mtpl("critic_brief", note=mg.get("note", ""), human_messages="", cwd=v.working_dir,
                                  decisions=self.decisions_text(),
                                  instructions=self.mon.controller.instructions if not self.has_session(v) else ""), "brief")
        self.worker_feedback(self.clean_critic(r["output"]))
        mg["phase"] = "work"
        self.critic_asks(r, after="work")
        return None

    def ph_work(self, mg: Dict[str, Any]) -> Optional[str]:
        m, c = self.mon, self.mon.controller
        if int(self.st.get("round", 0)) >= m.max_rounds:
            return "max_rounds_reached"
        redo = bool(mg.get("in_work"))  # a round cut off by a restart is redone before any limit or check-in
        if not redo and mg.get("ms_round", 0) >= c.milestone_rounds:
            mg.update(limit=True, phase="report")
            return None
        if not redo and mg.pop("checkin_due", False):
            mg["phase"] = "checkin"
            return None
        if mg.get("in_work"):  # this round was cut off by a restart: redo it, don't count it twice
            rnd = int(mg["in_work"])
        else:
            rnd = int(self.st.get("round", 0)) + 1
            mg["ms_round"] = mg.get("ms_round", 0) + 1
        mg["in_work"] = rnd
        summary = self.begin_round(rnd)
        self.cur()["rounds"] = mg["ms_round"]
        items = []
        for w in self.workers:
            fb = (mg.get("feedback") or {}).get(w.name) or "Please carry on."
            if not self.has_session(w):
                prompt = self.mtpl("worker_start", task=w.task or "", feedback=fb, human_messages="", cwd=w.working_dir,
                                   decisions=self.decisions_text())
            else:
                prompt = self.mtpl("worker_next", feedback=fb, human_messages="", round=rnd, cwd=w.working_dir)
            items.append((w, prompt, "work"))
        extra = {"milestone": mg.get("ms", 0), "milestone_round": mg["ms_round"]}
        recs = self._stage(items, rnd, extra)
        if self.should_stop() or any(r["status"] == "stopped" for r in recs):
            raise StopRequested()  # keep the round open; `orch run --resume` redoes it in the same session
        mg.pop("in_work", None)
        for r in recs:
            block = self.ask_block(r.get("output") or "", "worker")
            if block:
                self.new_question(m.agent(r["agent"]), self.critic.name, block, r["turn_id"])
        mg["work"] = {r["agent"]: {"turn_id": r["turn_id"], "status": r["status"], "output": r.get("output"),
                                   "stderr": r.get("stderr")} for r in recs}
        mg["feedback"] = {}
        summary["turns"] = [r["turn_id"] for r in recs]
        mg["summary"] = summary
        for r in recs:  # past the time limit: not fatal; the worker picks up from where things stand
            if r["status"] == "timeout":
                self.rec.progress(f"{r['agent']} ran past its time limit ({m.agent(r['agent']).timeout:.0f}s); continuing")
                self.forward(m.agent(r["agent"]), self.mtpl("worker_timeout").strip())
        errors = [r for r in recs if r["status"] not in ("ok", "timeout")]
        if errors and c.stop_on_error:
            self.end_round(summary, "worker error → stop", errors=[r["turn_id"] for r in errors])
            return "failed"
        mg["phase"] = "review"
        return None

    def _stage(self, items: List[Tuple[AgentConfig, str, str]], rnd: int, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for a, p, purpose in items:  # workers of one milestone run one after another
            out.append(self.turn(a, p, rnd, purpose, extra=extra))
        return out

    def ph_review(self, mg: Dict[str, Any]) -> Optional[str]:
        c, v = self.mon.controller, self.critic
        work = mg.get("work") or {}
        reports = "\n\n".join((f"From {self.mon.agent(k).called}:\n" if len(work) > 1 else "") + self.report_text(x)
                              for k, x in work.items())
        nudge = (self.st.get("nudge") or {}).get(v.name, "")
        r = self.say(v, self.mtpl("critic_review", worker_report=reports, human_messages=nudge), "verify")
        vd = self.verdict(r)
        summary = mg.get("summary") or {"round": self.st.get("round", 0), "turns": []}
        summary["turns"] = (summary.get("turns") or []) + [r["turn_id"]]
        if vd in ("ERROR", "UNKNOWN") and c.stop_on_error:
            self.end_round(summary, "critic error / no verdict → stop", verdicts={v.name: vd})
            return "failed"
        if vd == "PASS" and c.checklist:
            done, open_ = checklist_state(r.get("output") or "")
            if open_ or not done:
                vd = "FAIL"
                summary["checklist_overruled"] = [v.name]
                self.rec.progress(f"{v.name} said PASS but its checklist has {open_} open / {done} done items → FAIL")
                self.rec.set(nudge={v.name: c.checklist_nudge})
            else:
                self.rec.set(nudge=None)
        elif c.checklist:
            self.rec.set(nudge=None)
        summary["verdicts"] = {v.name: vd}
        passed = vd == "PASS"
        if not passed:
            self.worker_feedback(self.clean_critic(r["output"]))
        mg["phase"] = "report" if passed else "work"
        n = c.checkin_every
        if not passed and n and mg.get("ms_round", 0) % n == 0:
            mg["checkin_due"] = True  # after any question to the manager has been dealt with
        asked = self.critic_asks(r, after="report" if passed else "work")
        self.end_round(summary, ("passed → report" if passed else "feedback → " + ", ".join(w.name for w in self.workers))
                       + (" · question for the manager" if asked else ""))
        mg["asks"] = 0
        return None

    def ph_checkin(self, mg: Dict[str, Any]) -> Optional[str]:
        """A short update from the critic; the manager acknowledges or steers (no full report, no scoring)."""
        r = self.say(self.critic, self.mtpl("critic_checkin", human_messages=""), "checkin")
        update = self.clean_critic(r["output"])
        a = self.say(self.manager, self.mtpl("manager_checkin", update=update, human_messages=""), "ack")
        self.after_manager(a)
        mg["phase"] = "work"
        return None

    def ph_answer(self, mg: Dict[str, Any]) -> Optional[str]:
        q = self.q(mg.get("ask", ""))
        if not q:
            mg["phase"] = mg.get("after") or "work"
            return None
        r = self.say(self.manager, self.mtpl("manager_question", question=q["text"], human_messages=""), "answer")
        text = self.after_manager(r, parent=[q["id"]])
        if self.open_qs(self.manager.name, "human") and any(q["id"] in x["parent"] for x in self.open_qs(self.manager.name)):
            self.settle(q, "", "", r["turn_id"], status="passed_up")  # gone up to me; the manager's guess is in `text`
        else:
            self.settle(q, self.manager.name, text, r["turn_id"])
            self.decide(f"{self.short_question(q['text'])}: {truncate(self.for_critic(text), 1200)}", self.manager.called, q["id"])
        mg.update(answer_text=text, phase="amend")
        return None

    def ph_amend(self, mg: Dict[str, Any]) -> Optional[str]:
        r = self.say(self.critic, self.mtpl("critic_answer", answer=mg.get("answer_text", "")), "amend")
        after = mg.get("after") or "work"
        vd = self.verdict(r) if re.search(r"VERDICT:", r.get("output") or "", re.I) else None
        if after == "report" and vd == "FAIL":
            after = "work"
        elif after == "work" and vd == "PASS":
            done, open_ = checklist_state(r.get("output") or "")
            if not self.mon.controller.checklist or (done and not open_):
                after = "report"
        if after == "work":
            self.worker_feedback(self.clean_critic(r["output"]), append=True)
        mg["phase"] = after
        mg["asks"] = mg.get("asks", 0) + 1
        if mg["asks"] < 2:
            self.critic_asks(r, after=after)
        return None

    def ph_report(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        note = f", since we have spent {mg.get('ms_round', 0)} rounds on it" if mg.get("limit") else ""
        r = self.say(self.critic, self.mtpl("critic_report", limit_note=note, human_messages=""), "report")
        mg.update(report=r["output"], report_turn=r["turn_id"], phase="mreview")
        return None

    def ph_mreview(self, mg: Dict[str, Any]) -> Optional[str]:
        c, ms = self.mon.controller, self.cur()
        r = self.say(self.manager, self.mtpl("manager_review", n=mg["ms"] + 1, title=ms["title"],
                                             report=mg.get("report", ""), human_messages=""), "review")
        out = r.get("output") or ""
        acc = [x.end() for x in re.finditer(c.accept_pattern, out, re.I)]
        rev = [x.end() for x in re.finditer(c.revise_pattern, out, re.I)]
        if not acc and not rev:
            r = self.say(self.manager, "Please end your reply with one line: `DECISION: ACCEPT` or `DECISION: REVISE`.",
                         "review")
            out = r.get("output") or ""
            acc = [x.end() for x in re.finditer(c.accept_pattern, out, re.I)]
            rev = [x.end() for x in re.finditer(c.revise_pattern, out, re.I)]
        accept = bool(acc) and (max(acc) > (max(rev) if rev else -1))
        score, crit = parse_score(out), parse_criteria(out)
        if accept and c.min_score is not None and score is not None and score < c.min_score:
            accept = False
            self.rec.progress(f"manager accepted at {score}/5, below min_score {c.min_score} → REVISE")
            r = self.say(self.manager, f"Your overall score of {score}/5 is below the {c.min_score}/5 we agreed on. "
                                       f"What should {self.critic.called} do next?", "review")
        text = re.sub(r"\n*\s*DECISION:\s*\**\s*\w+\**\s*$", "", self.after_manager(r), flags=re.I).strip()
        self.st.setdefault("reviews", []).append(
            {"milestone": mg["ms"], "turn": r["turn_id"], "score": score, "criteria": crit,
             "decision": "ACCEPT" if accept else "REVISE", "round": self.st.get("round", 0), "at": now_iso()})
        ms["score"], ms["criteria"] = score, crit
        if accept or mg.get("limit"):
            ms.update(status="awaiting_ok", summary=text, summary_turn=r["turn_id"], accepted=accept)
            gate = c.approve_milestones or not accept
            mg.update(phase="gate" if gate else "next", guidance="" if accept else self.for_critic(text))
        else:
            mg.update(guidance=self.for_critic(text), phase="guidance")
        return None

    def ph_guidance(self, mg: Dict[str, Any]) -> Optional[str]:
        r = self.say(self.critic, self.mtpl("critic_guidance", guidance=mg.get("guidance", ""), human_messages=""),
                     "guidance")
        self.worker_feedback(self.clean_critic(r["output"]))
        mg.update(phase="work", limit=False)
        self.critic_asks(r, after="work")
        return None

    def ph_gate(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        why = "" if ms.get("accepted", True) else \
            f" — round limit reached ({mg.get('ms_round')} rounds), the manager has not accepted it"
        d = self.wait_gate({"kind": "milestone", "milestone": mg["ms"], "title": f"Milestone {mg['ms'] + 1}: "
                            f"{ms['title']}{why}", "text": ms.get("summary", ""), "turn": ms.get("summary_turn")})
        if d.get("message"):
            self.decide(f"Milestone {mg['ms'] + 1} ({ms['title']}): {d['decision']}. {d['message']}", "you", "gate")
        if d["decision"] == "approve":
            if d.get("message"):
                self.forward(self.manager, d["message"])
            mg["phase"] = "next"
            return None
        comment = d.get("message") or mg.get("guidance") or "Please keep going on this milestone."
        r = self.say(self.manager, self.mtpl("manager_feedback", n=mg["ms"] + 1, title=ms["title"], comment=comment),
                     "feedback")
        ms.update(status="active")
        mg.update(guidance=self.for_critic(self.after_manager(r)), ms_round=0, limit=False, phase="guidance")
        return None

    def ph_next(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        ms.update(status="done", ended_at=now_iso(), ended_round=int(self.st.get("round", 0)))
        self.rec.progress(f"🏁 **Milestone {mg['ms'] + 1} done:** {ms['title']}")
        mg["ms"] += 1
        mg["phase"] = "kickoff" if mg["ms"] < len(mg["milestones"]) else "done"
        if mg["phase"] == "done":
            self.st["final_report"] = ms.get("summary", "")
        return None


class _Failed(Exception):
    pass
