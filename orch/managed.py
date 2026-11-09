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

import hashlib
import json
import random
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import audit as _audit
from . import jobs as _jobs
from . import tags as T

from .backends import StopRequested
from .config import AgentConfig
from .controller import MonitorRunner, checklist_state, parse_options, resolve_choice, strip_checklist, truncate
from .report import CHECKBOX
from .recorder import now_iso

def manager_pause(text: str) -> str:
    """A pause the manager calls: its <pause> block(s), or, with <decision>PAUSE</decision>, the reply outside the
    tags. Empty when there is none."""
    p = T.bodies(text or "", "pause")
    if p:
        return "\n\n".join(p)
    if T.value(text or "", "decision") == "PAUSE":
        return T.outside(text) or "Paused by the manager."
    return ""


# the head of an agent's state.md; the run id in it keeps one run's state out of the next
STATE_HEAD = ("<!-- orch: working state of {agent}, run {run_id}, written {at} by {by}. You can edit this file; "
              "the agent is told about your changes on its next turn. A new run moves it into this run's folder. -->")
_STATE_HEAD_RE = re.compile(r"\A\s*<!--\s*orch: working state of .*?run (\S+?), written (\S+) by .*?-->\s*", re.S)


# placeholders a reset leaves in an agent's queue; the handover notes are written when its next turn starts
HANDOVER = "(handover notes: written when this agent's next turn starts)"
HANDOVER_TOP_DOWN = "(handover notes for the top-down restart: written when this agent's next turn starts)"


def checklist_text(text: str) -> str:
    """The critic's checklist in a reply: its <checklist> block(s), or, when it wrote the list without the tag,
    its checkbox lines. Either way the list is the critic's own: the gate reads it, the worker never sees it."""
    tagged = T.bodies(text or "", "checklist")
    if tagged:
        return "\n".join(tagged)
    return "\n".join(ln for ln in (text or "").split("\n") if CHECKBOX.match(ln))


def checklist_of(text: str) -> Tuple[int, int]:
    """(done, open) items of the critic's checklist in a reply; (0, 0) without one."""
    return checklist_state(checklist_text(text))


def pid_alive(pid: int) -> bool:
    try:  # a finished child of ours stays a zombie until reaped
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True




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
    """<score>4.2</score> (or 4.2/5, 8.4/10) -> 4.2 out of 5."""
    vals = T.bodies(text or "", "score")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|out of)?\s*(\d+(?:\.\d+)?)?", vals[-1]) if vals else None
    if not m:
        return None
    v, top = float(m.group(1)), float(m.group(2) or 5)
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


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _clip(text: str, limit: int) -> str:
    """The start of a text, cut at a word."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"


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

    def yields_to(self, agent: AgentConfig, to: List[str]) -> bool:
        return agent.role == "worker" and self.critic.name in to  # the critic redirects the work now, not after it

    def cut_short_note(self, agent: AgentConfig) -> str:
        return self.mtpl("worker_cut_short").strip()

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

    def turn(self, agent: AgentConfig, prompt: str, rnd: int, purpose: str, seq: Optional[int] = None,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Now and then (`tag_reminder`, a probability per turn) a short reminder of the tags is added to the
        message. It only reminds; nothing is required of the agent because of it."""
        role = {"manager": "manager", "verifier": "critic", "worker": "worker"}.get(agent.role)
        p = self.mon.controller.tag_reminder
        if role and p > 0 and random.random() < p:
            note = self.mtpl(f"tag_reminder_{role}").strip()
            if note:
                prompt = f"{prompt.rstrip()}\n\n<system-reminder>\n{note}\n</system-reminder>\n"
                extra = {**(extra or {}), "tag_reminder": True}
        return super().turn(agent, prompt, rnd, purpose, seq=seq, extra=extra)

    def say(self, agent: AgentConfig, prompt: str, purpose: str) -> Dict[str, Any]:
        """One turn of one agent, tagged with the milestone it belongs to."""
        mg = self.mg
        self.sync_state(agent)
        self.write_handover(agent)
        if agent.role == "manager":
            self.sync_chat_session(agent)
        r = self.turn(agent, prompt, int(self.st.get("round", 0)), purpose,
                      extra={"milestone": mg.get("ms", 0) if mg.get("milestones") else None,
                             "milestone_round": mg.get("ms_round", 0)})
        self.after_state_turn(agent, r)
        if r["status"] != "ok":
            if self.mon.controller.stop_on_error:
                raise _Failed(f"{agent.name} {purpose} turn ended with status {r['status']}")
        if agent.role == "verifier":
            for note in self.notes_up(r.get("output") or ""):
                self.forward(self.manager, self.mtpl("relay_note_up", note=note).strip())
        return r

    # ------------------------------------------------------------------ working state (agents/<name>/state.md)
    # One owner and one moment per piece: the manager rewrites its state (standing instruction for the critic and the
    # decisions that still hold) in a <state> tag whenever it writes to the critic; the critic's file is that
    # instruction plus the critic's own checklist, rewritten at every review; the worker's file is its latest report.
    # I can edit any of them; the agent gets my changes on its next turn. Fresh sessions start from these files.

    def state_path(self, a: AgentConfig) -> Path:
        return Path(a.state_file) if a.state_file else self.rec.run_dir / "state" / f"{a.name}.md"

    def read_state(self, a: AgentConfig) -> Tuple[str, str]:
        """(body, written_at) of the agent's state file for this run; ('', '') if there is none."""
        try:
            text = self.state_path(a).read_text(encoding="utf-8")
        except OSError:
            return "", ""
        m = _STATE_HEAD_RE.match(text)
        if m and m.group(1) != self.rec.run_dir.name:
            return "", ""  # another run's state
        return (text[m.end():] if m else text).strip(), (m.group(2) if m else "")

    def write_state(self, a: AgentConfig, body: str, by: str) -> None:
        body = (body or "").strip()
        if not body:
            return
        p = self.state_path(a)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = STATE_HEAD.format(agent=a.called, run_id=self.rec.run_dir.name, at=now_iso(), by=by) + "\n\n" + body + "\n"
        p.write_text(text, encoding="utf-8")
        self.st.setdefault("state_hash", {})[a.name] = hashlib.sha1(text.encode()).hexdigest()
        self.rec.event("state_written", agent=a.name, by=by)

    def adopt_state_files(self) -> None:
        """A state file from another run is moved into that run's folder (or kept next to it, renamed)."""
        for a in self.mon.agents:
            p = self.state_path(a)
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            m = _STATE_HEAD_RE.match(text)
            old = m.group(1) if m else ""
            if old == self.rec.run_dir.name:
                continue
            target = self.rec.run_dir.parent / old / "agent_state" / p.name if old else None
            if target is not None and target.parent.parent.is_dir():
                target.parent.mkdir(exist_ok=True)
                target = target.with_name(f"{a.name}.md")
            else:
                target = p.with_name(f"state.{old or 'earlier'}.md")
            p.replace(target)
            self.log(f"{a.name}: an earlier state file moved to {target}")

    def sync_state(self, a: AgentConfig) -> None:
        """Before the agent's turn: if I edited its state file, tell it (and carry the manager's into the critic's)."""
        p = self.state_path(a)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return
        h = hashlib.sha1(text.encode()).hexdigest()
        known = (self.st.get("state_hash") or {}).get(a.name)
        if known is None or h == known:
            return  # written by orch (or never written by it): nothing new from me
        self.st.setdefault("state_hash", {})[a.name] = h
        body, _at = self.read_state(a)
        if body:
            self.forward(a, self.mtpl("state_edited", state=body).strip())
            if a.role == "manager":
                self.write_critic_state()
        self.rec.save()

    def write_critic_state(self) -> None:
        mine, _at = self.read_state(self.manager)
        ck = self.latest_checklist()
        parts = []
        if mine:
            parts.append(self.mtpl("critic_state_instruction", state=mine).strip())
        if ck:
            parts.append("## Your checklist\n\n" + ck)
        if parts:
            self.write_state(self.critic, "\n\n".join(parts), "orch")

    def after_state_turn(self, a: AgentConfig, r: Dict[str, Any]) -> None:
        """After a turn: save what it changed in the working state."""
        out = r.get("output") or ""
        if r.get("status") != "ok" or not out:
            return
        if a.role == "manager":
            bodies = T.bodies(out, "state")
            if bodies:
                self.write_state(a, bodies[-1], a.called)
                self.write_critic_state()
        elif a.role == "verifier" and checklist_text(out):
            self.write_critic_state()
        elif a.role == "worker":
            self.write_state(a, T.outside(out), "orch, from the latest report")

    def write_handover(self, agent: AgentConfig) -> None:
        """A reset leaves a placeholder in the agent's queue; the notes are written now, from where the run is at
        this moment, so a reset some time before the resume doesn't hand over an old picture."""
        q = (self.st.get("fwd") or {}).get(agent.name) or []
        for i, x in enumerate(q):
            if x in (HANDOVER, HANDOVER_TOP_DOWN):
                q[i] = self.handover(agent, top_down=x == HANDOVER_TOP_DOWN)
        self.rec.save()

    def notes_up(self, text: str) -> List[str]:
        """<message to="manager"> blocks in a critic reply: things the manager should know."""
        m = self.manager
        return [b for a, b in T.blocks(text, "message") if b and T.addressed(a, m.called, m.name, "manager")]

    def sync_chat_session(self, agent: AgentConfig) -> None:
        """If my board chat moved the manager to a new session id, continue from there."""
        try:
            rows = [json.loads(x) for x in (self.rec.run_dir / "chat.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return
        since = (self.st["sessions"].get(agent.name) or {}).get("reset_at") or ""
        sid = next((r.get("session_id") for r in reversed(rows) if r.get("session_id") and (r.get("ts") or "") > since),
                   None)
        if sid and sid != (self.st["sessions"].get(agent.name) or {}).get("session_id"):
            self.rec.update_session(agent.name, session_id=sid)

    def switch_backends(self) -> None:
        """An agent moved to another backend starts a new session: with its handover notes (state file and what
        happened since), like after a reset, not just "pick up from the repository"."""
        super().switch_backends()
        for a in self.mon.agents:
            note = self.tpl("backend_switched", agent=a.called, cwd=a.working_dir).strip()
            q = (self.st.get("fwd") or {}).get(a.name) or []
            if note and note in q:
                q[q.index(note)] = HANDOVER
        self.rec.save()

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

    def ask_block(self, text: str, role: str = "") -> str:
        """The <question> blocks of a reply as one text (empty if it asks nothing of the level above)."""
        qs = T.bodies(text, "question")
        if len(qs) == 1:
            return qs[0]
        return "\n\n".join(f"{n}. {q}" for n, q in enumerate(qs, 1))

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

    @staticmethod
    def topic(text: str, limit: int = 140) -> str:
        """What a question is about, in one short line: its first sentence that asks something."""
        flat = re.sub(r"\*\*|`", "", " ".join(ln.strip() for ln in (text or "").split("\n")
                                            if ln.strip() and not re.match(r"^\W*(QUESTIONS?|NEEDS[_ ]HUMAN)\b", ln, re.I)
                                            and not re.match(r"^\W*[A-H][).]\s", ln)))
        flat = re.sub(r"^\s*(\d+[.)]\s+)?(What's being decided|Question|Decision)\s*:\s*", "", flat, flags=re.I)
        ask = re.search(r"[^.?!]*\?", flat)
        line = (ask.group(0) if ask else re.split(r"(?<=[.!])\s", flat, maxsplit=1)[0]).strip()
        return truncate(line, limit)

    def decision_body(self, d: Dict[str, Any]) -> Tuple[str, str]:
        """(topic, decision) of a register entry. Entries from a question were saved as "question: answer", and a
        long question pushed the answer out of a clipped entry, so the answer is taken from the question itself."""
        src = str(d.get("source") or "")
        q = next((x for x in self.st.get("questions") or [] if x.get("id") == src), None) if src.startswith("Q") else None
        if q:
            ans = (q.get("answer") or "").strip()
            if not ans:
                pre = self.short_question(q.get("text", "")) + ": "
                ans = d["text"][len(pre):] if d["text"].startswith(pre) else d["text"].rsplit("?: ", 1)[-1]
            return self.topic(q.get("text", "")), ans.strip()
        return "", d["text"]

    def decisions_text(self, a: Optional[AgentConfig] = None, after: str = "") -> str:
        """The register as one agent should read it.
        - Each entry is the decision itself, with a one-line topic when it answered a question.
        - Who decided is named from the reader's side: the manager reads me as "me" and itself as "you"; the critic
          and the worker get no attribution (to them a decision simply holds).
        - The worker and critic skip my milestone gate messages (milestones are between the manager and me); the
          worker also skips the pause messages and the pointer to the file, which names who decided."""
        regs = self.st.get("decisions") or []
        role = a.role if a is not None else "manager"
        skip = {"worker": ("gate", "wait"), "verifier": ("gate",)}.get(role, ())
        rows = []
        kept = [d for d in regs if str(d.get("source", "")) not in skip and (d.get("at") or "") >= after]  # same second: keep
        recent = {d["id"] for d in kept[-12:]}  # older decisions in one short line; the file has them in full
        for d in kept:
            topic, body = self.decision_body(d)
            lim = 400 if role == "worker" else (500 if d["id"] in recent else 160)
            body = body if len(body) <= lim else body[:lim].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"  # the file has it in full
            who = ""
            if role == "manager":
                who = " (me" if d.get("by") == "you" else (" (you" if d.get("by") == self.manager.called else f" ({d.get('by')}")
                who += f", {d['at'][5:16].replace('T', ' ')})"
            head = f"- **{d['id']}**{who}" + (f" — {topic}" if topic else "") + ": "
            rows.append(head + body.replace("\n", "\n  "))
        if not rows:
            return ""
        intro = (self.mtpl("decisions_intro_worker") if role == "worker" else self.mtpl("decisions_since") if after
                 else self.mtpl("decisions_intro", path=str(self.rec.run_dir / "decisions.md"))).strip()
        return intro + "\n\n" + "\n".join(rows)

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

    def latest_checklist(self) -> str:
        """The critic's newest checklist (from a review, or an amended review), '' if it has none yet."""
        for p in sorted((self.rec.run_dir / "rounds").glob("r*/t*.json"), reverse=True):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if t.get("agent") == self.critic.name and t.get("purpose") in ("verify", "amend", "guidance"):
                ck = checklist_text(t.get("output") or "")
                if ck:
                    return ck
        return ""

    def last_turn(self, agent: str, purposes: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
        """The newest finished turn of an agent in one of these kinds of turn (None if none)."""
        best = None
        for p in (self.rec.run_dir / "rounds").glob("r*/t*.json"):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if t.get("agent") == agent and t.get("purpose") in purposes and t.get("status") == "ok" and t.get("output"):
                key = (t.get("started_at") or "", p.parent.name, p.name)  # ties within a second: the later turn
                if best is None or key > best[0]:
                    best = (key, t)
        return best[1] if best else None

    def last_output(self, agent: str, purposes: Tuple[str, ...]) -> str:
        """The newest reply of an agent in one of these kinds of turn ('' if none)."""
        for p in sorted((self.rec.run_dir / "rounds").glob("r*/t*.json"), reverse=True):
            if p.name.endswith(".input.json"):
                continue
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if t.get("agent") == agent and t.get("purpose") in purposes and t.get("output"):
                return t["output"]
        return ""

    def handover(self, a: AgentConfig, top_down: bool = False) -> str:
        """What an agent needs to carry on in a fresh session, built from its state file (agents/<name>/state.md)
        and what happened since it was written. Without a state file yet (a run from before them), from the run's
        records instead.
        - manager: the goal, its plan and rubric, the milestone table, its state, the critic's newest message, open
          questions, and the decisions made since its state was written.
        - critic: its state (my standing instruction and its checklist) and its latest review in short; in a
          top-down restart only its checklist, since my new instruction comes next.
        - worker: that this is a fresh conversation, plus its state (its latest report) outside a top-down restart;
          the critic's instruction comes with the next message."""
        mg, ms = self.mg, (self.cur() or {})
        if a.role == "worker":
            body, _at = self.read_state(a)
            work = body or ((mg.get("work") or {}).get(a.name) or {}).get("output") or ""
            state = "" if top_down or not work else "Your latest report, in short:\n\n" + _clip(work, 2500)
            return self.mtpl("worker_fresh", cwd=a.working_dir, state=state).strip()
        parts = []
        if ms:
            parts.append(f"Where we are: milestone {mg.get('ms', 0) + 1} of {len(mg.get('milestones') or [])}"
                         f" (\"{ms.get('title', '')}\"), round {mg.get('ms_round', 0)} of it."
                         if a.role == "manager" else f"Where we are: round {mg.get('ms_round', 0)} of the current work.")
        w = mg.get("wait") or {}
        if mg.get("phase") == "wait" or (mg.get("gate") or {}).get("kind") == "wait":
            parts.append("The whole run is paused until I act on this:\n\n" + (w.get("text") or ""))
        mine, mine_at = self.read_state(self.manager)  # the standing instruction and what holds, and since when
        since = mine_at
        if a.role == "manager":
            parts.append("The goal:\n\n" + (self.mon.goal or "").strip())
            if mg.get("plan_text"):
                parts.append("Your plan and rubric, as I approved them:\n\n" + mg["plan_text"].strip())
            rows = [f"| {i + 1} | {m.get('title', '')} | {m.get('status', '')} | {m.get('score') if m.get('score') is not None else ''} |"
                    for i, m in enumerate(mg.get("milestones") or [])]
            if rows:
                parts.append("| # | milestone | status | score |\n|---|---|---|---|\n" + "\n".join(rows))
            if mine:
                parts.append(f"Your working state (`{self.state_path(a)}`, written {mine_at[5:16].replace('T', ' ')}):\n\n{mine}")
            t = self.last_turn(self.critic.name, ("report", "checkin", "verify", "amend", "guidance", "brief"))
            if t:  # the critic's newest message of any kind: a report or check-in can be many rounds old
                text = strip_checklist(self.clean_report(t.get("output") or ""))
                ck = self.latest_checklist()
                done, open_ = checklist_state(ck) if ck else (0, 0)
                parts.append(_cap(f"{self.critic.called}'s latest message ({t.get('purpose')}, "
                                  f"{(t.get('started_at') or '')[5:16].replace('T', ' ')}"
                                  + (f"; its checklist: {done} of {done + open_} done" if ck else "") + "):\n\n")
                             + _clip(text, 4000))
        elif a.role == "verifier":
            body, _at = self.read_state(a)
            ck = self.latest_checklist()
            if top_down:
                if ck:
                    parts.append("Your checklist (keep the items as they are):\n\n" + ck)
            elif body:
                parts.append(f"Your working state (`{self.state_path(a)}`):\n\n{body}")
            else:  # a run from before state files
                when = lambda k: f" ({mg[k][5:16].replace('T', ' ')})" if mg.get(k) else ""
                if mg.get("note"):
                    parts.append(f"My instruction when this part of the work started{when('note_at')}; newer decisions "
                                 "below override it:\n\n" + mg["note"].strip())
                if mg.get("guidance"):
                    parts.append(f"My latest guidance{when('guidance_at')}:\n\n" + mg["guidance"].strip())
                if ck:
                    parts.append("Your checklist (keep the items as they are):\n\n" + ck)
            last = "" if top_down else self.last_output(a.name, ("verify", "guidance", "brief", "amend"))
            if last:  # the prose briefly; the checklist is above
                prose = strip_checklist(T.without_markup(last, drop=("checklist", "verdict")))
                parts.append("Your latest review, in short:\n\n" + _clip(prose, 2500))
        qs = [q for q in self.st.get("questions") or [] if q.get("status") == "open" and q.get("to") == "human"]
        if qs:
            parts.append("Questions still open with me: " + "; ".join(f"{q['id']} {self.short_question(q['text'])}" for q in qs))
        if top_down and a.role == "verifier":  # the manager's new instruction carries what matters now
            parts.append(self.mtpl("decisions_pointer", path=str(self.rec.run_dir / "decisions.md")).strip())
        elif since:  # the state holds what was decided before it; only what came after
            dec = self.decisions_text(a, after=since)
            if dec:
                parts.append(dec)
            parts.append(self.mtpl("decisions_pointer", path=str(self.rec.run_dir / "decisions.md")).strip())
        else:
            dec = self.decisions_text(a)
            if dec:
                parts.append(dec)
        return self.mtpl("session_reset", state="\n\n".join(parts)).strip()

    def clean_report(self, text: str) -> str:
        return T.without_markup(text, drop=("checklist", "verdict"))

    def reset_sessions(self, names: Optional[List[str]] = None) -> List[str]:
        """Clear these agents' conversations (all by default): the run keeps its place, records and decisions, and
        each agent starts a new session with a handover of what it needs."""
        done = []
        mg = self.mg
        top_down = (not names or self.manager.name in names) and mg.get("phase") in self.RESTARTABLE + ("restart",)
        if top_down:  # the manager restarts the work: a short instruction to the critic, a detailed one to the worker
            if mg.get("phase") != "restart":  # a second reset before the restart ran keeps the first one's origin
                mg["restart"] = {"from": mg.get("phase"), "at": now_iso()}
            mg["phase"] = "restart"
            mg.pop("in_work", None)
        for a in self.mon.agents:
            if names and a.name not in names and not (top_down and a.role in ("verifier", "worker")):
                continue
            if a.role not in ("manager", "verifier", "worker"):
                continue
            sess = self.st["sessions"].get(a.name) or {}
            prev = {k: v for k, v in sess.items() if k != "previous"}
            self.rec.update_session(a.name, session_id=None, turns=0, reset_at=now_iso(),
                                    previous=(sess.get("previous") or []) + ([prev] if prev.get("session_id") else []))
            fwd = [x for x in (self.st.setdefault("fwd", {}).get(a.name) or [])
                   if x not in (HANDOVER, HANDOVER_TOP_DOWN)
                   and not x.startswith(("Your last turn was cut off", "We are starting a new part", "You are taking over",
                                         "This is a fresh conversation"))]
            self.st["fwd"][a.name] = [HANDOVER_TOP_DOWN if top_down else HANDOVER] + fwd  # written at its next turn
            self.rec.event("session_reset", agent=a.name, session_id=sess.get("session_id"))
            done.append(a.name)
        if self.mg.get("fresh_for") is None:
            self.mg["fresh_for"] = self.mg.get("ms")  # no second fresh start for this milestone
        self.rec.save()
        return done

    # a top-down restart replaces these steps of the worker/critic loop; plans, gates, pauses and reports go on as they are
    RESTARTABLE = ("work", "review", "checkin", "answer", "amend", "guidance")

    def ph_restart(self, mg: Dict[str, Any]) -> Optional[str]:
        """After a reset of everyone's sessions: the manager, with the whole picture, gives the critic a short
        instruction on what to continue with; the critic turns it into concrete steps for the worker."""
        for a in [self.manager, self.critic] + list(self.workers):  # a cut-off turn is replaced, not sent again
            q = (self.st.get("fwd") or {}).get(a.name)
            if q:
                self.st["fwd"][a.name] = [x for x in q if not x.startswith("Your last turn was cut off")]
        r = self.say(self.manager, self.mtpl("manager_restart", human_messages=""), "restart")
        text = self.after_manager(r, relay=False)
        msg = self.for_critic(text)
        mg.update(guidance=msg, guidance_at=now_iso())
        self.decide(msg, self.manager.called, "restart")
        c = self.say(self.critic, self.mtpl("critic_restart", instruction=msg, human_messages="",
                                            cwd=self.critic.working_dir), "brief")
        self.worker_feedback(self.clean_critic(c["output"]))
        mg.pop("restart", None)
        mg["phase"] = "work"
        self.critic_asks(c, after="work")
        return None

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
                self.st.setdefault("fwd", {}).setdefault(a.name, []).insert(  # the worker: the critic's brief follows
                    0, self.mtpl("milestone_fresh", cwd=a.working_dir).strip() if a.role == "worker" else HANDOVER)
                self.rec.event("fresh_session", agent=a.name, milestone=mg.get("ms"), session_id=sess.get("session_id"))
                self.log(f"{a.name}: fresh session for milestone {mg.get('ms', 0) + 1}")
        mg["fresh_for"] = mg.get("ms")
        self.rec.save()

    @staticmethod
    def for_me(text: str) -> str:
        """The part of a manager reply that is for me: everything outside the tags."""
        return T.outside(text)

    def after_manager(self, r: Dict[str, Any], parent: Optional[List[str]] = None, relay: bool = True) -> str:
        """Record the manager's questions for me (one per <question>, non-blocking), pass on its messages (unless the
        caller takes the message for the critic itself) and note a pause it calls; return its whole reply."""
        text = r.get("output") or ""
        mine = truncate(self.for_me(text), 1500)
        for q in T.bodies(text, "question"):
            self.new_question(self.manager, "human", q, r["turn_id"], parent, assumption=mine)
        if relay:
            for attrs, body in T.blocks(text, "message"):
                a = self.find_agent(attrs.get("to", ""))
                if a and a.name != self.manager.name and body:
                    self.forward(a, self.mtpl("relay_note", note=body).strip())
        pause = manager_pause(text) if r.get("purpose") != "pause" else ""  # it stops the run itself
        if pause:
            self.mg["pause_req"] = {"text": pause, "turn": r["turn_id"]}
        return text

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

    def for_critic(self, text: str, fallback: bool = True) -> str:
        """What a manager reply passes to the critic: its <message to="critic"> blocks; without one (and with
        `fallback`), the reply outside the tags, minus score tables."""
        c = self.critic
        msgs = [b for a, b in T.blocks(text, "message") if b and T.addressed(a, c.called, c.name, "critic")]
        if msgs:
            return "\n\n".join(msgs)
        if not fallback:
            return ""
        keep = [ln for ln in T.outside(text).split("\n") if not ln.strip().startswith("|")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()

    def clean_critic(self, text: str) -> str:
        """What the worker gets from a critic reply: the text outside the tags, plus any <message to="worker">."""
        names = [n for w in self.workers for n in (w.called, w.name)] + ["worker"]
        extra = [b for a, b in T.blocks(text, "message") if b and T.addressed(a, *names)]
        return self.for_worker("\n\n".join([x for x in [T.outside(text)] + extra if x]))

    def for_worker(self, text: str) -> str:
        """The critic's words as the worker should get them: without the checklist (also when the critic forgot the
        tag) and without headings that name the worker or anyone else ("## Updated instruction for the worker")."""
        names = "|".join(re.escape(n) for w in self.workers for n in {w.called, w.name, "worker"} if n)
        others = "|".join(re.escape(n) for n in {self.manager.called, self.manager.name, "manager", "lead"} if n)
        role_head = re.compile(r"^\s*(?:#+\s*|\*\*)?\s*(?:(?:updated|revised|new|next|my)\s+)*"
                               r"(?:instructions?|feedback|notes?|steps|tasks?|message|review)\s+(?:for|to)\s+(?:the\s+)?"
                               rf"(?:{names}|{others})\b[^\n]*$", re.I | re.M)
        text = role_head.sub("", strip_checklist(text or ""))
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def verdict(self, r: Dict[str, Any]) -> str:
        if r.get("status") != "ok":
            return "ERROR"
        v = T.value(r.get("output") or "", "verdict")
        return v if v in ("PASS", "FAIL") else "UNKNOWN"

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
            elif d.get("kind") == "job":
                self.mg["job_release"] = True
            elif d.get("kind") == "pause" and d.get("message"):  # from a chat with the manager on the board
                self.mg["pause_req"] = {"text": str(d["message"]), "turn": d.get("turn", "")}
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
        self.adopt_state_files()
        while True:
            self.check_control()
            ph = mg.get("phase", "plan")
            if ph == "done":
                self.audit_due(final=True)
                return "completed"
            if ph == "work" and not mg.get("in_work"):  # between rounds
                self.audit_due()
            if ph == "work" and mg.get("pause_req") and not mg.get("in_work"):  # the manager called a pause
                req = mg.pop("pause_req")
                mg["wait"] = {"who": "the manager", "text": req.get("text", ""), "proposal": req.get("text", ""),
                              "turn": req.get("turn", "")}
                mg["phase"] = ph = "wait"
                self.rec.progress("⏸ the manager paused the work until a task of mine is done")
            try:
                out = getattr(self, "ph_" + ph)(mg)
            except _Failed as f:
                self.rec.set(error=str(f))
                return "failed"
            self.rec.save()
            if out:
                if out in ("completed", "max_rounds_reached"):
                    self.audit_due(final=True)
                return out

    # ---- the auditor: every `audit_every` rounds (and once when the run ends) it reads the digest of how the agents
    # talked and updates its findings file. It is outside the team: nothing it writes reaches the agents.
    @property
    def auditor(self) -> Optional[AgentConfig]:
        a = self.mon.by_role("auditor")
        return a[0] if a else None

    def audit_due(self, final: bool = False) -> None:
        a, c = self.auditor, self.mon.controller
        if a is None or self.should_stop():
            return
        au = self.mg.setdefault("audit", {})
        done = int(self.st.get("round", 0)) - (1 if self.mg.get("in_work") else 0)
        through = int(au.get("through", 0))
        span = c.audit_every or 10
        if done <= through or (not final and (c.audit_every <= 0 or done - through < c.audit_every)):
            return
        lo = max(through + 1, done - span + 1)  # at most one span: a long run audited for the first time
        self.rec.progress(f"🔎 the auditor reads rounds {lo}–{done}")
        self.log(f"  🔎 audit of rounds {lo}–{done}")
        try:
            res = _audit.run_audit(self.mon, a, self.rec.run_dir, lo, done, au.get("prompts"),
                                   should_stop=self.should_stop, log=self.log)
        except Exception as e:  # an audit never stops the run
            res = {"status": "error", "error": f"{type(e).__name__}: {e}", "points": ""}
        if res.get("status") == "stopped":
            return  # audited again after the restart
        au.update(through=done, prompts=res.get("prompts") or au.get("prompts") or {}, last=now_iso(),
                  status=res.get("status"))
        if res.get("cost_usd"):
            self.st["cost_usd"] = float(self.st.get("cost_usd") or 0) + float(res["cost_usd"])
        self.rec.event("audit", rounds=[lo, done], status=res.get("status"), points=res.get("count", 0),
                       file=res.get("file", ""), cost_usd=res.get("cost_usd", 0))
        if res.get("status") == "ok":
            self.rec.progress(f"🔎 audit of rounds {lo}–{done}: {res.get('count', 0)} point(s) → {res.get('file')}")
        else:
            self.rec.progress(f"🔎 audit of rounds {lo}–{done} did not finish ({res.get('status')}"
                              f"{': ' + res['error'] if res.get('error') else ''})")
        self.rec.save()

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
        mg.update(note=self.for_critic(self.after_manager(r, relay=False)), note_at=now_iso(), ms_round=0, limit=False, phase="brief")
        return None

    def ph_brief(self, mg: Dict[str, Any]) -> Optional[str]:
        v = self.critic
        r = self.say(v, self.mtpl("critic_brief", note=mg.get("note", ""), human_messages="", cwd=v.working_dir,
                                  decisions=self.decisions_text(v),
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
            fb = self.for_worker((mg.get("feedback") or {}).get(w.name) or "") or "Please carry on."  # also if saved before
            if not self.has_session(w):
                prompt = self.mtpl("worker_start", task=w.task or "", feedback=fb, human_messages="", cwd=w.working_dir,
                                   decisions="")  # what the work must keep to comes in the critic's instruction
            else:
                prompt = self.mtpl("worker_next", feedback=fb, human_messages="", round=rnd, cwd=w.working_dir)
            items.append((w, prompt, "work"))
        extra = {"milestone": mg.get("ms", 0), "milestone_round": mg["ms_round"]}
        self._procs = _jobs.snapshot() if c.job_wait > 0 else {}  # to find jobs the worker leaves running
        recs = self._stage(items, rnd, extra)
        if self.should_stop() or any(r["status"] == "stopped" for r in recs):
            raise StopRequested()  # keep the round open; `orch run --resume` redoes it in the same session
        mg.pop("in_work", None)
        for r in recs:
            block = self.ask_block(r.get("output") or "", "worker")
            if block:
                self.new_question(m.agent(r["agent"]), self.critic.name, block, r["turn_id"])
        return self.after_work(recs, summary)

    def after_work(self, recs: List[Dict[str, Any]], summary: Dict[str, Any]) -> Optional[str]:
        """After the workers' turns: hand the work to the critic, or first wait for a job the worker is waiting on."""
        m, c, mg = self.mon, self.mon.controller, self.mg
        earlier = mg.pop("work_earlier", None) or {}
        mg["work"] = {r["agent"]: {"turn_id": r["turn_id"], "status": r["status"], "stderr": r.get("stderr"),
                                   "output": (earlier[r["agent"]] + "\n\n**Later, after the job it was waiting on:**\n\n"
                                              + (r.get("output") or "")) if earlier.get(r["agent"]) else r.get("output")}
                      for r in recs}
        for a, out in earlier.items():  # a worker that had no job of its own keeps its earlier report
            mg["work"].setdefault(a, {"turn_id": "", "status": "ok", "output": out, "stderr": ""})
        mg["feedback"] = {}
        summary["turns"] = (summary.get("turns") or []) + [r["turn_id"] for r in recs]
        mg["summary"] = summary
        for r in recs:  # past the time limit: not fatal; the worker picks up from where things stand
            if r["status"] == "timeout":
                self.rec.progress(f"{r['agent']} ran past its time limit ({m.agent(r['agent']).timeout:.0f}s); continuing")
                self.forward(m.agent(r["agent"]), self.mtpl("worker_timeout").strip())
        errors = [r for r in recs if r["status"] not in ("ok", "timeout")]
        if errors and c.stop_on_error:
            self.end_round(summary, "worker error → stop", errors=[r["turn_id"] for r in errors])
            return "failed"
        for r in recs:  # ended early for an urgent message: the worker hears why at its next turn
            if r.get("cut_short"):
                self.forward(m.agent(r["agent"]), self.mtpl("worker_after_cut").strip())
        cut = any(r.get("cut_short") for r in recs)
        jobs: Dict[str, Dict[str, Any]] = {}
        if c.job_wait > 0 and cut:
            self._procs = {}  # the critic acts now; it doesn't wait for a job the worker left running
        elif c.job_wait > 0:
            outs = "\n".join(r.get("output") or "" for r in recs)
            skip = {int(a["pid"]) for a, _b in T.blocks(outs, "no-wait") if str(a.get("pid", "")).isdigit()}
            found = [j for j in _jobs.detect(getattr(self, "_procs", {}) or {},
                                             {self.mon.agent(r["agent"]).working_dir or self.mon.working_dir for r in recs},
                                             c.job_ignore)
                     if j["pid"] not in skip]  # orch finds them itself
            self._procs = {}
            for j in found:
                if any(j["pid"] in x["pids"] for x in jobs.values()):
                    continue
                for r in recs[:1] if len(recs) == 1 else recs:
                    x = jobs.setdefault(r["agent"], {"pids": [], "lines": [], "cmds": {}})
                    x["pids"].append(j["pid"])
                    x["lines"].append(f"pid {j['pid']}: {_clip(str(j['command']), 160)}")
                    x["cmds"][str(j["pid"])] = j["command"]
        if jobs:
            mg["job"] = {"agents": jobs, "since": now_iso(), "since_ts": time.time()}
            mg["work_earlier"] = {a: x.get("output") or "" for a, x in mg["work"].items()}
            mg["phase"] = "job"
            pids = ", ".join(str(x) for j in jobs.values() for x in j["pids"])
            self.rec.progress(f"⏳ waiting for the worker's job (pid {pids}) before {self.critic.name} reviews")
            self.log(f"  ⏳ waiting for job pid {pids} (up to {c.job_wait / 3600:.1f} h; 'Review now' on the board skips)")
            return None
        mg["phase"] = "review"
        return None

    def ph_job(self, mg: Dict[str, Any]) -> Optional[str]:
        """The worker finished its turn but is waiting on its own background job: don't spend a critic review on
        work that isn't done. When the job ends (or after job_wait), the worker picks up again; I can skip ahead."""
        c, job = self.mon.controller, mg.get("job") or {}
        agents = job.get("agents") or {}
        for name in list(agents):  # a wait saved by an older orch may hold processes that were never jobs
            j = agents[name]
            ws = [self.mon.agent(name).working_dir or self.mon.working_dir]
            keep = set(_jobs.inside(j["pids"], ws))
            if keep != set(j["pids"]):
                j["lines"] = [l for x, l in zip(j["pids"], j["lines"]) if x in keep]
                j["cmds"] = {k: v for k, v in (j.get("cmds") or {}).items() if int(k) in keep}
                j["pids"] = [x for x in j["pids"] if x in keep]
            if not j["pids"]:
                del agents[name]
        if not agents:
            mg.pop("job", None)
            self.rec.progress("▶ none of the processes I was waiting for runs in the workspace: review now")
            mg["phase"] = "review"
            return None
        self.rec.set(status="waiting_job")
        why = "finished"
        try:
            while True:
                if self.should_stop():
                    raise StopRequested()
                self.check_control()
                if mg.pop("job_release", False) or any(n["to"] and self.critic.name in n["to"]
                                                        for n in self.rec.urgent_notes()):
                    why = "released"  # "Review now" on the board, or an urgent message for the critic
                    break
                if not any(_jobs.alive(x, (j.get("cmds") or {}).get(str(x), "")) for j in agents.values()
                           for x in j["pids"]):
                    break
                if time.time() - float(job.get("since_ts") or time.time()) > c.job_wait:
                    why = "timeout"
                    break
                time.sleep(2)
        finally:
            self.rec.set(status="running")
        mg.pop("job", None)
        if why == "released":
            mg.pop("work_earlier", None)
            self.rec.progress(f"▶ review now, without waiting for the job")
            mg["phase"] = "review"
            return None
        rnd = int(self.st.get("round", 0))
        summary = mg.get("summary") or {"round": rnd, "turns": []}
        hours = (time.time() - float(job.get("since_ts") or time.time())) / 3600
        items = []
        for name, j in agents.items():
            w = self.mon.agent(name)
            state = (f"has finished" if why == "finished" else f"is still running after {hours:.1f} h")
            items.append((w, self.mtpl("worker_job_done", jobs="; ".join(j["lines"]), state=state, human_messages=""),
                          "work"))
        self._procs = _jobs.snapshot() if c.job_wait > 0 else {}
        recs = self._stage(items, rnd, {"milestone": mg.get("ms", 0), "milestone_round": mg.get("ms_round", 0)})
        if self.should_stop() or any(r["status"] == "stopped" for r in recs):
            mg["job"] = {**job, "agents": agents}  # wait again (or go straight on) after the restart
            raise StopRequested()
        for r in recs:
            block = self.ask_block(r.get("output") or "", "worker")
            if block:
                self.new_question(self.mon.agent(r["agent"]), self.critic.name, block, r["turn_id"])
        return self.after_work(recs, summary)

    def _stage(self, items: List[Tuple[AgentConfig, str, str]], rnd: int, extra: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for a, p, purpose in items:  # workers of one milestone run one after another
            self.sync_state(a)
            self.write_handover(a)
            out.append(self.turn(a, p, rnd, purpose, extra=extra))
            self.after_state_turn(a, out[-1])
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
            done, open_ = checklist_of(r.get("output") or "")
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
        wait = [] if passed else T.bodies(r.get("output") or "", "pause")
        nxt = "report" if passed else ("pause" if wait else "work")  # a proposed pause goes to the manager
        if wait:
            mg["wait"] = {"who": self.critic.called, "text": "\n\n".join(wait), "turn": r["turn_id"]}
        mg["phase"] = nxt
        n = c.checkin_every
        if nxt == "work" and n and mg.get("ms_round", 0) % n == 0:
            mg["checkin_due"] = True  # after any question to the manager has been dealt with
        asked = self.critic_asks(r, after=nxt)
        self.end_round(summary, ("passed → report" if passed else "critic proposes a pause → manager" if wait
                                 else "feedback → " + ", ".join(w.name for w in self.workers))
                       + (" · question for the manager" if asked else ""))
        mg["asks"] = 0
        return None

    def ph_pause(self, mg: Dict[str, Any]) -> Optional[str]:
        """The critic proposes stopping the whole run until a person acts; the manager decides."""
        w = mg.get("wait") or {}
        r = self.say(self.manager, self.mtpl("manager_pause", proposal=w.get("text", ""), human_messages=""), "pause")
        out = r.get("output") or ""
        if T.value(out, "decision") == "PAUSE" or (T.bodies(out, "pause") and T.value(out, "decision") != "CONTINUE"):
            self.after_manager(r)
            msg = "\n\n".join(T.bodies(out, "pause")) or self.for_me(out)
            mg["wait"] = {**w, "who": "the manager agrees", "text": msg or w.get("text", ""), "proposal": w.get("text", ""),
                          "turn": r["turn_id"]}
            mg["phase"] = "wait"
            return None
        self.after_manager(r, relay=False)
        mg.pop("pause_req", None)  # it decided to continue
        guidance = self.for_critic(out)
        mg.pop("wait", None)
        c = self.say(self.critic, self.mtpl("critic_continue", guidance=guidance, human_messages=""), "guidance")
        self.worker_feedback(self.clean_critic(c["output"]))
        mg["phase"] = "work"
        self.critic_asks(c, after="work")
        return None

    def ph_wait(self, mg: Dict[str, Any]) -> Optional[str]:
        """Nothing for the worker to do until a human task is done: pause (no rounds spent) until I say carry on."""
        w = mg.get("wait") or {}
        mg.pop("pause_req", None)
        d = self.wait_gate({"kind": "wait", "milestone": mg.get("ms", 0), "turn": w.get("turn"),
                            "title": f"Waiting for you ({w.get('who') or 'a human task'})", "text": w.get("text", "")})
        msg = (d.get("message") or "").strip()
        if msg:
            self.decide(msg, "you", "wait")
            self.forward(self.manager, self.mtpl("manager_resumed", message=msg,
                                                 waited=truncate(w.get("proposal") or w.get("text", ""), 600)).strip())
        r = self.say(self.critic, self.mtpl("critic_resume", message=msg or "(no message)",
                                            waited=w.get("proposal") or w.get("text", ""),
                                            human_messages=""), "guidance")
        self.worker_feedback(self.clean_critic(r["output"]))
        mg.pop("wait", None)
        mg["phase"] = "work"
        self.critic_asks(r, after="work")
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
        text = self.after_manager(r, parent=[q["id"]], relay=False)
        if self.open_qs(self.manager.name, "human") and any(q["id"] in x["parent"] for x in self.open_qs(self.manager.name)):
            self.settle(q, "", "", r["turn_id"], status="passed_up")  # gone up to me; the manager's guess is in `text`
        else:
            self.settle(q, self.manager.name, self.for_critic(text), r["turn_id"])
            self.decide(f"{self.short_question(q['text'])}: {truncate(self.for_critic(text), 1200)}", self.manager.called, q["id"])
        mg.update(answer_text=self.for_critic(text), phase="amend")
        return None

    def ph_amend(self, mg: Dict[str, Any]) -> Optional[str]:
        r = self.say(self.critic, self.mtpl("critic_answer", answer=mg.get("answer_text", "")), "amend")
        after = mg.get("after") or "work"
        vd = self.verdict(r) if T.value(r.get("output") or "", "verdict") else None
        if after == "report" and vd == "FAIL":
            after = "work"
        elif after == "work" and vd == "PASS":
            done, open_ = checklist_of(r.get("output") or "")
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
        report = strip_checklist(T.without_markup(r["output"] or "", drop=("checklist", "verdict")))
        ck = self.latest_checklist()
        if ck:  # the manager judges the milestone with the critic's own list in view; the worker never gets it
            done, open_ = checklist_state(ck)
            report += "\n\n" + self.mtpl("report_checklist", done=done, total=done + open_, checklist=ck).strip()
        mg.update(report=report, report_turn=r["turn_id"], phase="mreview")
        return None

    def ph_mreview(self, mg: Dict[str, Any]) -> Optional[str]:
        c, ms = self.mon.controller, self.cur()
        r = self.say(self.manager, self.mtpl("manager_review", n=mg["ms"] + 1, title=ms["title"],
                                             report=mg.get("report", ""), human_messages=""), "review")
        out = r.get("output") or ""
        dec = T.value(out, "decision")
        if dec not in ("ACCEPT", "REVISE"):
            r = self.say(self.manager, "Please end your reply with `<decision>ACCEPT</decision>` or "
                                       "`<decision>REVISE</decision>`.", "review")
            out = r.get("output") or ""
            dec = T.value(out, "decision")
        accept = dec == "ACCEPT"
        score, crit = parse_score(out), parse_criteria(out)
        if accept and c.min_score is not None and score is not None and score < c.min_score:
            accept = False
            self.rec.progress(f"manager accepted at {score}/5, below min_score {c.min_score} → REVISE")
            r = self.say(self.manager, f"Your overall score of {score}/5 is below the {c.min_score}/5 we agreed on. "
                                       f"What should {self.critic.called} do next?", "review")
        text = self.after_manager(r, relay=False)
        self.st.setdefault("reviews", []).append(
            {"milestone": mg["ms"], "turn": r["turn_id"], "score": score, "criteria": crit,
             "decision": "ACCEPT" if accept else "REVISE", "round": self.st.get("round", 0), "at": now_iso()})
        ms["score"], ms["criteria"] = score, crit
        if accept or mg.get("limit"):
            ms.update(status="awaiting_ok", summary=self.for_me(text), summary_turn=r["turn_id"], accepted=accept)
            gate = c.approve_milestones or not accept
            mg.update(phase="gate" if gate else "next", guidance="" if accept else self.for_critic(text), guidance_at=now_iso())
        else:
            mg.update(guidance=self.for_critic(text), guidance_at=now_iso(), phase="guidance")
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
        mg.update(guidance=self.for_critic(self.after_manager(r, relay=False)), guidance_at=now_iso(), ms_round=0, limit=False, phase="guidance")
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
