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

import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
import contextlib
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import audit as _audit
from . import boards as B
from . import jobs as _jobs
from . import parallel as PW
from . import progress as P
from . import protocol as CP
from . import reminders as R
from . import routing as RT
from . import tags as T

from .backends import StopRequested, TurnRequest, TurnResult, get_backend
from .config import CHECK_EVERY, HOT_AGENT_FIELDS, MANAGER_CHECK_EVERY, PROMPTS_DIR, AgentConfig
from .controller import MonitorRunner, checklist_state, parse_options, relayed, resolve_choice, strip_checklist, truncate
from .report import CHECKBOX
from .templates import render
from .recorder import now_iso, write_json

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
PROGRESS_STATUS = "(orch, progress check)"  # the manager's light status look (B5); only the newest one waits for it
PLAN_STATUS = "(orch, from the critic's plan)"  # a check-in written by orch; only the newest one waits for the manager
QUESTION_TAG = re.compile(r"<question>.*?</question>", re.S | re.I)  # a question block, removed from a forwarded note
TERMINAL_HOLD = "its session is open in a terminal; delivered when the terminal closes"
# purposes whose <message> blocks orch delivers itself; in the others a phase of the run carries the manager's
# message to the critic (critic_message) and the critic's message to the worker (its feedback)
CARRIED = "carried by the manager's turn after my decision at the gate"  # a discussion closed at a gate
SELF_DELIVERED = ("incoming", "ack", "discussion", "review_file")  # turns whose messages orch delivers itself (route_reply)
# B6: phases that send the manager's words to the critic; they wait while I discuss with the manager
DISCUSSION_WAITS = ("kickoff", "answer", "pause", "mreview", "restart")
# B9: phases that go on while a blocking question waits for me (the gates are mine; a job is only watched)
QUESTION_GOES_ON = ("plan_gate", "gate", "wait", "job", "done", "workers_gate", "parallel")
CRITIC_TO_WORKER_PHASES = ("verify", "brief", "guidance", "amend")
BY_DESIGN = {"checklist": "the critic's own checklist; the worker never sees it",
             "plan": "the critic's own plan; the worker gets one step at a time",
             "verdict": "the critic's verdict is for orch's gate, not for the worker",
             "workers": "the critic's proposal of several workers, for its user to decide"}


def checklist_text(text: str) -> str:
    """The critic's checklist in a reply: its <checklist> block(s), or, when it wrote the list without the tag,
    its checkbox lines. Either way the list is the critic's own: the gate reads it, the worker never sees it."""
    tagged = T.bodies(text or "", "checklist")
    if tagged:
        return "\n".join(tagged)
    rest = T.without_markup(text or "", drop=("plan",))  # the plan's steps are not checklist items
    return "\n".join(ln for ln in rest.split("\n") if CHECKBOX.match(ln))


def checklist_of(text: str) -> Tuple[int, int]:
    """(done, open) items of the critic's checklist in a reply; (0, 0) without one."""
    return checklist_state(checklist_text(text))


def plan_text(text: str) -> str:
    """The critic's plan in a reply: its <plan> block (the last one), '' without one. Its steps are checkbox lines;
    the first open one is the step the worker is on."""
    blocks = T.bodies(text or "", "plan")
    return blocks[-1] if blocks else ""


def plan_steps(plan: str) -> List[Tuple[bool, str]]:
    """[(done, step)] of a plan."""
    out = []
    for ln in (plan or "").split("\n"):
        m = CHECKBOX.match(ln)
        if m:
            out.append((m.group(1) != " ", ln[m.end():].strip()))
    return out


def plan_status(plan: str) -> Tuple[int, int, str]:
    """(steps done, steps in all, the current step's text); (0, 0, '') without a plan."""
    steps = plan_steps(plan)
    cur = next((s for d, s in steps if not d), "")
    return sum(1 for d, _ in steps if d), len(steps), cur


def pid_alive(pid: int) -> bool:
    return _jobs.alive(pid)




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


RUBRIC_MIN, RUBRIC_MAX = 3, 8  # B6: criteria per milestone, set by the manager for that milestone's work
_RUBRIC_HEAD = re.compile(r"^(?:#|criteri(?:on|a)|rubric|what a 5 looks like|score)$", re.I)


def parse_rubric(text: str) -> List[str]:
    """The criteria of the last <rubric> block of a reply, one per line (list markers, numbers, checkboxes and table
    borders dropped; a table gives its first cell); [] without one."""
    blocks = T.bodies(text or "", "rubric")
    if not blocks:
        return []
    out: List[str] = []
    for ln in blocks[-1].split("\n"):
        s = ln.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or set("".join(cells)) <= set("-: "):
                continue
            s = next((c for c in cells if c and not re.match(r"^\d+(?:\.\d+)?$", c)), "")
        s = re.sub(r"^(?:[-*•+]|\d{1,2}[.)])\s+", "", s)
        s = re.sub(r"^\[[ xX]\]\s*", "", s).strip()
        s = re.sub(r"^\*\*(.+?)\*\*", r"\1", s).strip()
        if s and not _RUBRIC_HEAD.match(re.sub(r"[*_`]", "", s).strip()):
            out.append(s)
    return out


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
        """The configured workers (B11: the extra copies of a parallel section are not among them)."""
        return [a for a in self.mon.by_role("worker") if a.name not in self.clones]

    @property
    def all_workers(self) -> List[AgentConfig]:
        return self.mon.by_role("worker")

    @property
    def clones(self) -> Dict[str, str]:
        """B11: the copies of the worker (and its monitor) a parallel section added: name -> the agent copied."""
        return self.__dict__.setdefault("_clones", {})

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
        if "checklist_path" not in values:
            p = self.checklist_path()
            values["checklist_path"] = str(p) if p else "your checklist file"
        return self.tpl(name, **{**self.names(), **values})

    def has_session(self, a: AgentConfig) -> bool:
        return bool((self.st["sessions"].get(a.name) or {}).get("session_id"))

    def cur(self) -> Optional[Dict[str, Any]]:
        ms = self.mg.get("milestones") or []
        i = self.mg.get("ms", 0)
        return ms[i] if i < len(ms) else None

    def turn(self, agent: AgentConfig, prompt: str, rnd: int, purpose: str, seq: Optional[int] = None,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One turn; the reminders due are added at the end of its message (add_reminders). It waits while the
        agent's session is open in a terminal, and every <message> of its reply is routed (route_reply)."""
        self.hold_while_busy(agent)
        self.board_vars()
        self.rec.charge_chats()  # board-chat turns so far are entries of their own (B7 cost attribution)
        if agent.name in (self.mg.get("compact_due") or []) and agent.role != "monitor":
            self.compact(agent, rnd)
        before = (self.st.get("golden") or {}).get("hash")
        if agent.role == "verifier":
            self.check_worker_models()
        try:
            r = super().turn(agent, prompt, rnd, purpose, seq=seq,
                             extra={**(extra or {}), "communication_protocol": CP.VERSION})
        finally:
            if agent.role == "worker":
                self.note_worker_model(agent, self.last_model(agent))
        if r.get("status") == "ok" and agent.role == "monitor":
            self.rec.save()  # a check's report is handled by quick_check: it is never routed by tags
        elif r.get("status") == "ok":
            self.golden_after_turn(agent, r, before)
            self.check_addresses(agent, r.get("output") or "")
            self.check_tooling(agent, r)
            self.route_reply(agent, r)
            if agent.role == "manager":
                self.take_rubric(r)
            if agent.role in ("manager", "verifier"):
                self.take_workers(agent, r)
            self.rec.save()
        if (r.get("meta") or {}).get("compacted"):  # the CLI compacted the session during the turn
            self.after_compact(agent, "auto-compact")
        return r

    def after_compact(self, agent: AgentConfig, how: str) -> None:
        """A compacted session lost the golden board it was given: it gets it again with its next message (once)."""
        if (self.st.get("golden_seen") or {}).pop(agent.name, None) is not None:
            self.rec.event("golden_resend", agent=agent.name, why=how)
        (self.st.get("review_tpl_seen") or {}).pop(agent.name, None)
        (self.st.get("rubric_seen") or {}).pop(agent.name, None)  # the summary may drop the review instructions
        self.rec.save()

    # ---- B6: the manager's rubric for each milestone (3–8 criteria for that milestone's work)

    def take_rubric(self, r: Dict[str, Any]) -> None:
        """A <rubric> in a manager reply sets the current milestone's criteria; a count outside 3–8 is flagged on the
        board and the manager is asked once (in its next message) to set 3–8."""
        ms = self.cur() if self.mg.get("milestones") else None
        items = parse_rubric(r.get("output") or "")
        if ms is None or not items:
            return
        ms.update(rubric=items, rubric_turn=r.get("turn_id"))
        self.rec.event("rubric", milestone=self.mg.get("ms", 0), count=len(items), turn=r.get("turn_id"))
        if not RUBRIC_MIN <= len(items) <= RUBRIC_MAX:
            self.rubric_off(len(items))

    def rubric_off(self, count: int) -> None:
        n = self.mg.get("ms", 0) + 1
        self.rec.progress(f"⚠ the manager's rubric for milestone {n} has {count} criteria (3–8 expected); asked again")
        self.rec.event("board_warning", what="rubric_count", count=count,
                       problem=f"milestone {n}'s rubric has {count} criteria (3–8 expected)")
        self.forward(self.manager, self.mtpl("rubric_note", n=n, count=count).strip())

    def rubric_text(self, ms: Dict[str, Any]) -> str:
        items = ms.get("rubric") or []
        if not items:
            return self.mtpl("rubric_missing").strip()
        return self.mtpl("rubric_review", items="\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))).strip()

    # ---- B8: the worker's model changed: the critic is told once, in one neutral line, at its next turn

    def last_model(self, w: AgentConfig) -> str:
        """The model the worker's latest turn ran on (after any switch to a backup)."""
        return str((getattr(self, "turn_models", None) or {}).get(w.name) or "")

    def check_worker_models(self) -> None:
        """Before a critic turn: a worker whose settings now name another model (the user edited them, or its backup's
        cooldown ended) counts as a change, so the critic hears of it before it writes the next instruction."""
        for w in self.workers:
            if self.rec.agent_busy(w.name):
                continue  # its running turn reports the model it ends on
            try:
                self.refresh_agent(w)
            except Exception:
                continue
            chain = w.variants()
            if chain:
                self.note_worker_model(w, chain[self.start_variant(w, len(chain))].label)

    def note_worker_model(self, w: AgentConfig, model: str) -> None:
        if not model:
            return
        known = self.mg.setdefault("worker_models", {})
        old = known.get(w.name)
        known[w.name] = model
        if not old or old == model:
            self.rec.save()
            return
        who = "The person you instruct" if len(self.workers) == 1 else w.called
        text = self.mtpl("worker_model_changed", who=who).strip()
        if not text:
            return
        L, critic = self.ledger, self.critic
        q = self.st.setdefault("fwd", {}).setdefault(critic.name, [])
        for m in L.rows:  # one line per change: a newer change replaces a note still waiting
            if m.get("kind") == "model_change" and m["to"] == critic.name and m["status"] == "sent" \
                    and m.get("worker") == w.name:
                if m["text"] in q:
                    q.remove(m["text"])
                L.mark(m, "superseded", reason="replaced by a newer model change")
        if text in q:
            q.remove(text)  # the same line from another worker: one is enough
        L.add("orch", critic.name, text, text, via="note", purpose="model_change", kind="model_change",
              worker=w.name, old_model=old, new_model=model, carrier={"fwd": True})
        self.forward(critic, text)
        self.rec.event("model_note", agent=w.name, to=critic.name, old=old, new=model)
        self.rec.progress(f"🔀 {w.called}'s model changed ({old} → {model}); {critic.called} is told at its next turn")
        self.rec.save()

    def check_tooling(self, agent: AgentConfig, r: Dict[str, Any]) -> None:
        """A critic or worker reply that mentions orch, its run folder or `.orch/` paths is flagged on the board
        (a warning; the reply is passed on as usual). Those agents are told to ignore that tooling."""
        if agent.role not in ("verifier", "worker"):
            return
        found = RT.tooling_mentions(r.get("output") or "", agent.name, self.rec.run_dir)
        if found:
            what = "; ".join(found[:3])
            self.rec.progress(f"⚠ {agent.called} mentions the tooling that runs it: {what}")
            self.rec.event("tooling_mention", agent=agent.name, turn=r.get("turn_id"),
                           problem=f"{agent.called}'s reply mentions the tooling that runs it: {what}")

    def add_reminders(self, agent: AgentConfig, prompt: str, purpose: str) -> Tuple[str, List[str]]:
        """Reminders at the end of the message (orch/reminders.py): night mode while it is on (the manager needs it
        to act), the critic's reply routing when the message carries something from its user, and the registry's
        reminders when they are due for this agent. Returns the prompt and the ids sent."""
        role = R.ROLE_OF.get(agent.role)
        if not role:
            return prompt, []
        sent: List[str] = []

        def add(rid: str, text: str) -> None:
            nonlocal prompt
            if text.strip():
                prompt = R.append(prompt, text)
                sent.append(rid)

        seen_protocol = self.st.setdefault("communication_protocol_seen", {})
        if seen_protocol.get(agent.name) != CP.VERSION or not self.has_session(agent):
            add("communication_protocol", "Communication rule: untagged text is always for me (your upper level). "
                "Use explicit recipient tags for anyone else. A verdict alone assigns no worker turn.")
            seen_protocol[agent.name] = CP.VERSION
        if role == "manager" and self.st.get("night"):
            add("night_mode", self.mtpl("night_manager"))
        if role == "manager" and self.discussion_on():
            add("discussion", self.mtpl("discussion_manager"))
        if role == "critic" and (self.par or self.carries_from_user(agent)):
            add("reply_routing", self.mtpl("critic_reply_routing"))
        g = self.check_golden()
        if role == "manager" and g.get("prune_due"):  # once each time the board grows past its limit
            add(R.GOLDEN_PRUNE, self.mtpl("golden_prune", chars=f"{g.get('chars', 0):,}",
                                          limit=f"{g.get('limit', 0):,}", path=str(self.golden_path())))
            g["prune_due"] = False
        counters = self.st.setdefault("reminders", {}).setdefault(agent.name, {})
        for rem in self.mon.controller.reminders:
            text = R.text_for(rem, role)
            if rem["id"] == R.GOLDEN_BOARD:
                if R.applies(rem, purpose) and R.targets(rem, agent.name, role):
                    gb = self.golden_board_reminder(agent, role, text, R.due(counters, rem))
                    if gb:
                        add(R.GOLDEN_BOARD, gb)
                continue
            if not text or not R.applies(rem, purpose) or not R.targets(rem, agent.name, role):
                continue
            if rem["id"] == "protocol" and not self.mon.controller.protocol:  # no tag protocol to remind of
                continue
            if R.due(counters, rem):
                add(rem["id"], render(text, **self.names()))
        return prompt, sent

    # notes for the critic that are observations, not its user writing to it: no "answer me in <message to=you>"
    OBSERVATIONS = ("progress_check", "model_change")

    def carries_from_user(self, agent: AgentConfig) -> bool:
        """Whether the agent's waiting notes hold something from its user (a progress check report or a model-change
        line does not: nothing in it asks for an answer)."""
        waiting = [x for x in (self.st.get("fwd") or {}).get(agent.name) or [] if x]
        if not waiting:
            return False
        seen = {m.get("text") for m in self.ledger.rows
                if m.get("to") == agent.name and m.get("kind") in self.OBSERVATIONS}
        return any(x not in seen for x in waiting)

    # ------------------------------------------------------------------ the golden board (orch/boards.py)
    # One project file of rules, preferences and key decisions; the manager owns it and edits it in place. Every agent
    # gets it at the end of a message: once whenever it changed or the agent's session is new, and on the reminder's
    # schedule. The critic and the worker read it as their own user's standing rules; a board that names a layer above
    # one of them is held from it (and the manager is told) until it is rewritten.

    def golden_path(self) -> Path:
        c = self.mon.controller.golden_board
        return Path(c) if c else self.rec.run_dir.parent / "golden.md"

    def golden_body(self) -> str:
        return B.golden_body(B.read(self.golden_path()))

    def board_vars(self) -> None:
        """The paths the manager's protocol names (fixed for a project, so its system prompt stays the same)."""
        self.manager.prompt_vars["golden_board"] = str(self.golden_path())
        # B6: the critic's step checklist, one file per milestone (N), which the manager compares with its milestones
        self.manager.prompt_vars["checklist_files"] = str(self.agent_dir(self.critic) / "checklist-mN.md")

    def setup_boards(self) -> None:
        if B.ensure_golden(self.golden_path()):
            self.rec.event("golden_created", path=str(self.golden_path()))
        self.board_vars()
        self.check_golden()

    def golden_leaks(self, body: str) -> Dict[str, List[str]]:
        """{role: [words]}: what in the board names a layer above the critic or the worker (design principle 3)."""
        above = {"verifier": ("manager",), "worker": ("manager", "verifier")}
        words = {"verifier": ["manager", "orch", "real user"], "worker": ["manager", "orch", "real user", "critic"]}
        out: Dict[str, List[str]] = {}
        for role, roles in above.items():
            names = list(words[role])
            for a in self.mon.agents:
                if a.role in roles:
                    names += [n for n in (a.display_name, a.name) if n and len(n) > 2 and n.lower() != "the"]
            found = sorted({w for w in names if re.search(rf"(?i)(?<![\w.-]){re.escape(w)}(?![\w-])", body or "")})
            if found:
                out[role] = found
        return out

    def check_golden(self) -> Dict[str, Any]:
        """Read the board: note a change, a name it must not carry, and its size (a warning, and the manager asked
        once to prune; again only after it grows again). Never cut."""
        g = self.st.setdefault("golden", {})
        body = self.golden_body()
        h = B.digest(body)
        limit = int(self.mon.controller.golden_limit or 0)
        g.update(path=str(self.golden_path()), limit=limit)
        if g.get("hash") != h:
            first = "hash" not in g
            g.update(hash=h, chars=len(body), changed_at=now_iso())
            if not first:
                self.rec.event("golden_changed", chars=len(body))
                self.rec.progress(f"📋 the golden board changed ({len(body):,} characters); every agent gets it once")
            leaks = self.golden_leaks(body)
            g["leaks"] = leaks
            if leaks:
                what = ", ".join(sorted({w for ws in leaks.values() for w in ws}))
                self.rec.progress(f"⚠ the golden board names {what}; held from the agents it would reveal that to")
                self.rec.event("board_warning", what="golden_leak", problem=f"the golden board names {what}")
                self.forward(self.manager, self.mtpl("golden_leak", words=what).strip())
        n = len(body)
        g["over"] = bool(limit) and n > limit
        if g["over"]:
            if g.get("prune_at") is None or n > g["prune_at"]:
                g.update(prune_at=n, prune_due=True)
                self.rec.progress(f"⚠ the golden board is {n:,} characters, over its {limit:,}: the manager is asked "
                                  "to prune it")
                self.rec.event("board_warning", what="golden_size", chars=n, limit=limit,
                               problem=f"the golden board is {n:,} characters (limit {limit:,})")
            elif n < g["prune_at"]:
                g["prune_at"] = n  # pruned but still over: the next growth asks again
        else:
            g.pop("prune_at", None)
            g["prune_due"] = False
        return g

    def golden_board_reminder(self, agent: AgentConfig, role: str, intro: str, due: bool) -> str:
        """The board for this agent's message, or '': when it changed since the agent last got it, when the
        agent's session is new, or when the reminder is due. Nothing while the board is empty."""
        body = self.golden_body()
        g = self.st.get("golden") or {}
        if not body or (role != "manager" and agent.role in (g.get("leaks") or {})):
            return ""
        seen = (self.st.get("golden_seen") or {}).get(agent.name) or {}
        sid = (self.st["sessions"].get(agent.name) or {}).get("session_id")
        if not (due or not sid or seen.get("hash") != g.get("hash") or seen.get("session") != sid):
            return ""
        head = render(intro or "", **self.names(), golden_board=str(self.golden_path())).strip()
        return f"{head}\n\n{body}" if head else body

    def golden_after_turn(self, agent: AgentConfig, r: Dict[str, Any], before: Optional[str]) -> None:
        """After a turn: the agent has the board it was sent; a board the manager changed in its own turn counts as
        seen by it (it wrote it)."""
        g = self.check_golden() if agent.role == "manager" else (self.st.get("golden") or {})
        sent = R.GOLDEN_BOARD in (r.get("reminders") or [])
        if sent or (agent.role == "manager" and before is not None and g.get("hash") != before):
            self.st.setdefault("golden_seen", {})[agent.name] = {"hash": g.get("hash"), "session": r.get("session_id")}

    MAY_ADDRESS = RT.MAY_ADDRESS  # whom each role may write to with <message to="…"> (besides to="you", its own user)

    def bad_addresses(self, agent: AgentConfig, text: str) -> List[str]:
        """The <message to="…"> blocks of a reply that can't be delivered: no such recipient, the sender itself,
        a role this agent doesn't address, or an empty body. The critic's notes to its user (to="you", or the
        manager's names) are valid: they go up."""
        allowed = self.MAY_ADDRESS.get(agent.role, ())
        bad = []
        for attrs, body in T.blocks(text, "message"):
            to = (attrs.get("to") or "").strip()
            if T.addressed(attrs, "you"):
                continue
            target = T.recipient(to, self.mon.agents)
            if target is None or target.name == agent.name or target.role not in allowed or not (body or "").strip():
                bad.append(f'<message to="{to}">' + ("" if (body or "").strip() else " (empty)"))
        return bad

    def check_addresses(self, agent: AgentConfig, text: str) -> None:
        """An invalid address is reported on the board and corrected in one line at the sender's next turn."""
        self.correct(agent, self.bad_addresses(agent, text))

    def correct(self, agent: AgentConfig, bad: List[str]) -> None:
        """Report addresses that can't be delivered (a round turn, a board chat or the terminal) and queue the
        one-line correction for the sender's next turn."""
        if not bad:
            return
        role = {"manager": "manager", "verifier": "critic", "worker": "worker"}.get(agent.role)
        for b in bad:
            self.rec.progress(f"⚠ {agent.called}: {b} names no one it can write to; not sent")
            self.rec.event("route_problem", agent=agent.name, problem=f"{b} names no one it can write to; not sent")
        if role:
            note = self.mtpl(f"address_correction_{role}", bad=", ".join(bad)).strip()
            if note not in ((self.st.get("fwd") or {}).get(agent.name) or []):
                self.forward(agent, note)

    def say(self, agent: AgentConfig, prompt: str, purpose: str, rnd: Optional[int] = None,
            extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One turn of one agent, tagged with the milestone it belongs to."""
        mg = self.mg
        self.sync_state(agent)
        self.write_handover(agent)
        if agent.role == "manager":
            self.sync_chat_session(agent)
        r = self.turn(agent, prompt, int(self.st.get("round", 0)) if rnd is None else rnd, purpose,
                      extra={"milestone": mg.get("ms", 0) if mg.get("milestones") else None,
                             "milestone_round": mg.get("ms_round", 0), **(extra or {})})
        self.after_state_turn(agent, r)
        if r["status"] != "ok":
            if self.mon.controller.stop_on_error:
                raise _Failed(f"{agent.name} {purpose} turn ended with status {r['status']}")
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
        ck, plan = self.latest_checklist(), self.latest_plan()
        parts = []
        if mine:
            parts.append(self.mtpl("critic_state_instruction", state=mine).strip())
        p = self.checklist_path()
        if p is not None and p.exists():
            parts.append(f"## Your checklist file\n\n`{p}`: your steps and their items; edit it there.")
        if plan:
            parts.append("## Your plan\n\n" + plan)
        if ck:
            parts.append("## Your checklist (current step)\n\n" + ck)
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
            else:
                self.adopt_own_state(a)
            self.apply_preferences(a, out)
        elif a.role == "verifier":
            self.sync_checklist(r)
            self.note_plan(r)
            self.write_critic_state()
        elif a.role == "worker":
            self.write_state(a, T.outside(out), "orch, from the latest report")

    def adopt_own_state(self, a: AgentConfig) -> None:
        """The manager may edit its state file in place instead of writing `<state>`: a change made during its own
        turn is its own (taken in silently, and carried into the critic's standing instruction)."""
        try:
            text = self.state_path(a).read_text(encoding="utf-8")
        except OSError:
            return
        h = hashlib.sha1(text.encode()).hexdigest()
        known = (self.st.get("state_hash") or {}).get(a.name)
        if h != known:
            self.st.setdefault("state_hash", {})[a.name] = h
            self.rec.event("state_written", agent=a.name, by=a.called)
            self.write_critic_state()

    def apply_preferences(self, manager: AgentConfig, out: str) -> None:
        """The manager's <preference to="critic|worker">…</preference> tags: one line each in that agent's preference.md."""
        for attrs, body in T.blocks(out, "preference"):
            try:
                target = self.mon.agent(attrs.get("to", ""))
            except Exception:
                target = None
            if target is None or target.role not in ("verifier", "worker"):
                self.rec.progress(f"📝 {manager.called}'s preference note had no valid target (critic or worker); not saved")
                continue
            if self.write_preference(target, body, manager.name):
                self.rec.progress(f"📝 {manager.called} added a preference for {target.called}")

    def write_preference(self, a: AgentConfig, text: str, by: str) -> bool:
        """Append one line to the agent's preference.md (the file is read into its system prompt each turn)."""
        p = a.preference_path()
        line = " ".join((text or "").split())
        if p is None or not line:
            return False
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if line in old:
            return False  # already noted
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(old.rstrip() + ("\n" if old.strip() else "") + f"- {now_iso()[:10]}: {line}\n",
                     encoding="utf-8")
        self.rec.event("preference", agent=a.name, by=by)
        return True

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
        return [b for a, b in T.blocks(text, "message") if b and T.addressed(a, m.called, m.name, "manager", "you")]

    def sync_chat_session(self, agent: AgentConfig) -> None:
        """If my board chat moved the manager to a new session id, continue from there."""
        try:
            rows = [json.loads(x) for x in (self.rec.run_dir / "chat.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return
        from .board import manager_chat_session
        since = (self.st["sessions"].get(agent.name) or {}).get("reset_at") or ""
        sid = manager_chat_session(rows, since, self.st, agent.name)
        old = (self.st["sessions"].get(agent.name) or {}).get("session_id")
        if sid and sid != old:
            self.rec.update_session(agent.name, session_id=sid, cut_off=None)
            self.note_session(agent.name, "a board chat continued in a new session", previous=old, session_id=sid)

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

    def fresh_session_note(self, agent: AgentConfig) -> str:
        """A backup model on another provider starts a new session: with the agent's handover notes."""
        return self.handover(agent)

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

    # ------------------------------------------------------------------ message routing (orch/routing.py)
    # Every reply is scanned: round turns here (route_reply), board chats and the terminal through inbox/. Each
    # message gets a ledger entry; delivery is confirmed against the recipient's actual prompt (on_prompt).
    # Delivery is activation: a message for an idle manager or critic starts a short turn of its own (activate).

    @property
    def ledger(self) -> RT.Ledger:
        return RT.Ledger(self.st)

    def roles(self) -> Dict[str, str]:
        return {a.name: a.role for a in self.mon.agents}

    def route_reply(self, agent: AgentConfig, r: Dict[str, Any]) -> None:
        """Route untagged text upward and explicit messages to their recipients; log the
        blocks held by design and the addresses that can't be delivered."""
        out, tid, purpose = r.get("output") or "", r.get("turn_id") or "", r.get("purpose") or ""
        L = self.ledger
        hops = int(getattr(self, "_hops", 0) or 0) + 1 if purpose == "incoming" else 1
        mine = {"source": tid, "via": "round", "purpose": purpose}
        direct_up = agent.role == "verifier" and purpose in ("report", "checkin")
        if agent.role == "verifier":
            plain = T.outside(out).strip()
            if plain and direct_up:
                L.add(agent.name, self.manager.name, plain, carrier={"phase": purpose}, **mine)
            elif plain:
                self.deliver(agent, self.manager, plain, hops=hops, activate="wait", **mine)
        blocks = T.blocks(out, "message")
        sent, _bad = RT.split_reply(out, agent, self.mon.agents)
        by_i = {s["i"]: s for s in sent}
        for i, (attrs, body) in enumerate(blocks):
            to = (attrs.get("to") or "").strip()
            if T.addressed(attrs, "you") and agent.role != "verifier":
                continue  # the sender's own user reads it where it was written
            note_up = agent.role == "verifier" and (T.addressed(attrs, "you") or (i in by_i and by_i[i]["target"].role == "manager"))
            if note_up and (body or "").strip():
                if direct_up:
                    L.add(agent.name, self.manager.name, body, i=i, carrier={"phase": purpose}, **mine)
                    continue
                self.deliver(agent, self.manager, body, self.mtpl("relay_note_up", note=body).strip(), i=i, hops=hops,
                             activate=(True if purpose == "incoming" else "wait"), urgent=T.urgent(attrs), **mine)
                continue
            s = by_i.get(i)
            if s is None:
                L.add(agent.name, to, body or "", status="held", i=i, reason=f'<message to="{to}"> names no one '
                      f"{agent.called} can write to; not sent", **mine)
                continue
            t = s["target"]
            if agent.role == "manager" and t.role == "verifier" and purpose not in SELF_DELIVERED:
                self.supersede(L.add(agent.name, t.name, body, i=i, urgent=s["urgent"], additive=s["additive"],
                                     carrier={"phase": purpose}, **mine))
            elif agent.role == "verifier" and t.role == "worker" and (
                    (purpose in CRITIC_TO_WORKER_PHASES and not self.par) or
                    (purpose == "par_review" and t.name == r.get("par_worker"))):
                # B11: in one worker's review, its own message is carried by the review (par_review), not sent twice
                self.supersede(L.add(agent.name, t.name, body, self.for_worker(body), i=i, urgent=s["urgent"],
                                     additive=s["additive"], carrier={"phase": purpose}, **mine))
            else:  # a message turn's messages start their recipient's turn; a phase's wait for a pause in the run
                self.deliver(agent, t, body, i=i, hops=hops, urgent=s["urgent"], additive=s["additive"],
                             activate=True if purpose in ("incoming", "discussion") else "wait", **mine)
        if agent.role == "verifier" and not self.par:
            for section in PW.to_sections(out):
                target = T.recipient(section["worker"], self.mon.agents)
                if target is not None and target.role == "worker" and section["text"]:
                    if purpose in CRITIC_TO_WORKER_PHASES:
                        L.add(agent.name, target.name, section["text"], carrier={"phase": purpose},
                              step=section["step"], **mine)
                    else:
                        self.deliver(agent, target, section["text"], **mine)
        if agent.role == "verifier" and self.par and purpose not in ("par_review", "par_assign"):
            self.par_give(PW.to_sections(out), tid)
        if agent.role == "verifier" and purpose in CRITIC_TO_WORKER_PHASES:
            for w in self.workers:
                for name, why in BY_DESIGN.items():
                    for body in T.bodies(out, name):
                        L.add(agent.name, w.name, body, status="held", by_design=True, kind=name,
                              reason="held by design: " + why, **mine)

    def deliver(self, sender: AgentConfig, target: AgentConfig, body: str, text: str = "", source: str = "",
                i: Optional[int] = None, via: str = "round", purpose: str = "", hops: int = 1, urgent: bool = False,
                additive: bool = False, activate: Any = True, wake: Optional[bool] = None) -> Dict[str, Any]:
        """Send a message orch carries itself: to a worker as an update (live into its running turn where the
        backend can, else at the start of its next turn), urgent ones as a note that cuts into the running turn,
        the rest queued for the recipient's next turn, which starts now if it is idle (activate).
        A note up from the critic to the manager starts no turn unless it needs a decision (wakes_manager; `wake`
        overrides the check of the body): otherwise it is held for the manager's next regular turn."""
        L = self.ledger
        held_up = (sender.role == "verifier" and target.role == "manager" and not urgent
                   and not (wake if wake is not None else self.wakes_manager(body)))
        created_at = now_iso()
        if held_up:
            activate = False
            text = f"Progress records from the critic\n\n[{created_at}] {body}"
        if not text:
            text = self.for_worker(body) if target.role == "worker" and sender.role == "verifier" else \
                (self.mtpl("relay_note", note=body).strip() or body)
        if target.role == "worker":
            text = self.par_leaks(target, text, "message")
        if sender.role == "manager" and self.discussion_on():  # B6: nothing goes down while I discuss with it
            m = L.add(sender.name, target.name, body, text, source=source, i=i, via=via, purpose=purpose, hops=hops,
                      urgent=urgent, additive=additive, status="held", reason=RT.DISCUSSION,
                      carrier={"discussion": True})
            self.rec.progress(f"💬 {m['id']} from {sender.called} to {target.called} held for the discussion")
            return m
        m = L.add(sender.name, target.name, body, text, source=source, i=i, via=via, purpose=purpose, hops=hops,
                  urgent=urgent, additive=additive, created_at=created_at,
                  activate=(activate if hops <= RT.MAX_HOPS else False))
        if held_up:
            L.mark(m, "held", reason=RT.NEXT_TURN, next_turn=True)
        if urgent:
            name = self.note_file({"to": [target.name], "message": text, "from": sender.name, "urgent": True,
                                   "source": source, "msg": m["id"]})
            m["carrier"] = {"inbox": name}
        elif target.role == "worker":
            wm = self.rec.queue_worker_message(target.name, text, source or m["id"], sender.name)
            m["carrier"] = {"wm": wm["id"]}
            slot = (self.par.get("slots") or {}).get(target.name)
            if slot and slot.get("status") in (PW.IDLE, PW.DONE, PW.BLOCKED):
                slot.update(status=PW.READY, instr="")
        else:
            self.forward(target, text)
            m["carrier"] = {"fwd": True}
        self.supersede(m)
        return m

    @staticmethod
    def wakes_manager(text: str, urgent: bool = False) -> bool:
        """Does a note up need the manager now? Only with a decision to make: a <question>, a <pause>, or urgent.
        Anything else waits for its next regular turn (design: don't wake a level that has nothing to decide)."""
        return bool(urgent or T.bodies(text or "", "question") or T.bodies(text or "", "pause"))

    def note_file(self, data: Dict[str, Any]) -> str:
        inbox = self.rec.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() // 1000 % 1000000:06d}-orch-note.json"
        (inbox / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return name

    def supersede(self, new: Dict[str, Any]) -> None:
        """A newer message down replaces the sender's older waiting ones to the same recipient (orch/routing.py)."""
        L = self.ledger
        for old in L.older_to_replace(new, self.roles()):
            c = old.get("carrier") or {}
            if "inbox" in c:
                p = self.rec.run_dir / "inbox" / c["inbox"]
                if not p.exists():
                    continue  # already taken into a prompt
                (p.parent / "superseded").mkdir(exist_ok=True)
                os.replace(p, p.parent / "superseded" / p.name)
            elif "wm" in c:
                row = next((w for w in self.st.get("worker_messages") or [] if w["id"] == c["wm"]), None)
                if not row or row["status"] not in ("queued", "deferred"):
                    continue
                self.rec.worker_message_status(row["id"], "superseded", by=new["id"])
            elif "fwd" in c:
                q = (self.st.get("fwd") or {}).get(old["to"]) or []
                if old["text"] not in q:
                    continue
                q.remove(old["text"])
            who = next((a.called for a in self.mon.agents if a.name == new["from"]), new["from"])
            why = f"replaced by a newer message from {who} before it was delivered"
            L.mark(old, "superseded", by=new["id"], reason=why)
            self.rec.event("message_superseded", turn=old.get("source"), to=old["to"], by=new.get("source"),
                           msg=old["id"], by_msg=new["id"], reason=why)
            self.rec.progress(f"↷ {old['id']} to {old['to']} superseded by {new['id']}")

    def drop_phase_messages(self, turn: str, to: str, reason: str, by: str = "") -> None:
        """Messages a phase was going to carry that the run no longer passes on (e.g. at a gate)."""
        for m in self.ledger.rows:
            if m.get("source") == turn and m["to"] == to and m["status"] == "sent" and "phase" in (m.get("carrier") or {}):
                self.ledger.mark(m, "superseded", reason=reason, **({"by_turn": by} if by else {}))

    def drop_waiting(self, rows: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """Close messages still "sent" that nothing will carry any more as "dropped" (routing.py), with the reason;
        a queued update to a worker goes with them."""
        out = []
        for m in rows:
            if m.get("status") != "sent":
                continue
            self.ledger.drop(m, reason)
            wm = (m.get("carrier") or {}).get("wm")
            if wm and any(x["id"] == wm for x in self.rec.pending_worker_messages(m["to"])):
                self.rec.worker_message_status(wm, RT.DROPPED, reason=reason)
            out.append(m)
        if out:
            self.rec.event("message_dropped", msgs=[m["id"] for m in out], to=sorted({m["to"] for m in out}),
                           reason=reason)
            self.rec.progress(f"⊘ {', '.join(m['id'] for m in out)} closed undelivered: {reason}")
        return out

    def scan_inbox(self, only: Optional[str] = None) -> None:
        """Record the notes waiting in inbox/ (board chat replies, the terminal, my own notes). A note from the
        critic or the manager to a worker becomes an update for the worker, like a check-in's."""
        inbox = self.rec.run_dir / "inbox"
        try:
            files = sorted((p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")),
                           key=lambda p: (p.stat().st_mtime_ns, p.name))  # oldest first: newer ones supersede
        except OSError:
            return
        L = self.ledger
        known = {(m.get("carrier") or {}).get("inbox") for m in L.rows}
        changed = False
        for p in files:
            if p.name in known:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            data: Dict[str, Any] = {"message": text}
            if p.suffix == ".json":
                try:
                    data = json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    continue  # still being written
                if not isinstance(data, dict) or data.get("kind"):
                    continue
            msg = str(data.get("message", "")).strip()
            if not msg:
                continue
            to = data.get("to") or None
            to = [to] if isinstance(to, str) else to
            names = to or [self.manager.name]
            if only is not None and only not in names:
                continue
            frm = str(data.get("from") or "human")
            sender = next((a for a in self.mon.agents if a.name == frm), None)
            if sender is not None and sender.role == "manager" and self.discussion_on():
                (inbox / "held").mkdir(exist_ok=True)  # B6: out of the inbox, so no turn takes it in
                os.replace(p, inbox / "held" / p.name)
                for name in names:
                    L.add(frm, name, msg, source=str(data.get("source") or p.name), i=data.get("i"),
                          via=str(data.get("via") or "chat"), urgent=bool(data.get("urgent")),
                          additive=bool(data.get("additive")), status="held", reason=RT.DISCUSSION,
                          carrier={"discussion": p.name})
                self.rec.progress(f"💬 a message from {sender.called} held for the discussion")
                changed = True
                continue
            for name in names:
                tgt = next((a for a in self.mon.agents if a.name == name), None)
                if tgt is None:
                    continue
                created = data.get("created_at") or datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
                m = L.add(frm, name, msg, created_at=created, source=str(data.get("source") or p.name), i=data.get("i"),
                          via=str(data.get("via") or ("note" if sender is None else "chat")),
                          urgent=bool(data.get("urgent")), additive=bool(data.get("additive")),
                          hops=int(data.get("hops") or 1), activate=True, carrier={"inbox": p.name})
                if sender is not None and sender.role == "verifier" and tgt.role == "manager" \
                        and not self.wakes_manager(msg, m["urgent"]):
                    wrapper = f"Progress records from the critic\n\n[{created}] {msg}"
                    m["text"] = wrapper
                    if len(names) == 1:
                        data["message"] = wrapper
                        data["created_at"] = created
                        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    L.mark(m, "held", reason=RT.NEXT_TURN, next_turn=True, activate=False)
                if sender is not None and sender.role in ("verifier", "manager") and tgt.role == "worker" \
                        and not m["urgent"] and len(names) == 1:
                    text_w = self.par_leaks(tgt, self.for_worker(msg) if sender.role == "verifier" else msg, "message")
                    (inbox / "consumed").mkdir(exist_ok=True)
                    os.replace(p, inbox / "consumed" / p.name)
                    wm = self.rec.queue_worker_message(name, text_w, m["source"], frm)
                    m.update(text=text_w, carrier={"wm": wm["id"], "inbox": p.name})
                self.supersede(m)
                changed = True
        if changed:
            self.rec.save()

    def before_inbox(self, agent: AgentConfig) -> None:
        self.scan_inbox(only=agent.name)

    def on_prompt(self, agent: AgentConfig, turn_id: str, text: str, how: str) -> None:
        """What the agent was given: every waiting message it carries is delivered, in this turn."""
        self.ledger.seen(agent.name, text, turn_id, how)
        self.sync_live()
        self.rec.save()

    def sync_live(self) -> None:
        """An update sent into a worker's running turn counts once the backend accepted it."""
        rows = {w["id"]: w for w in self.st.get("worker_messages") or []}
        for m in self.ledger.rows:
            wm = rows.get((m.get("carrier") or {}).get("wm") or "")
            if m["status"] == "sent" and wm and wm.get("status") == "accepted" and wm.get("delivery_turn"):
                self.ledger.mark(m, "delivered", turn=wm["delivery_turn"], how="live" if wm.get("via") == "steer"
                                 else "prompt", delivered_at=wm.get("updated_at") or now_iso())

    # ---- delivery is activation

    def next_agent(self) -> Optional[str]:
        """The agent the next phase runs (None when it runs nobody, e.g. a gate)."""
        mg = self.mg
        ph = mg.get("phase", "plan")
        if ph in ("plan", "kickoff", "answer", "pause", "mreview", "restart", "next"):
            return self.manager.name
        if ph in ("brief", "review", "amend", "report", "guidance", "checkin") or (ph == "work" and mg.get("checkin_due")):
            return self.critic.name
        return None

    def activate(self, mode: str = "wait") -> None:
        """Start a turn for the manager or critic when a message waits for it and it is idle. `mode`: "between"
        (between two phases: only messages from a chat, the terminal, a note or a message turn; not when the next
        phase runs that agent anyway), "wait" (the run waits: a gate, a job, a held turn) or "stage" (a worker's
        turn is running)."""
        if self.should_stop() or getattr(self, "_activating", False):
            return
        self.take_messages()
        self.scan_inbox()
        self.sync_live()
        self.discussion_batch()  # B6: the one turn after I close a discussion
        self.review_file_turn()  # B9: a review file I asked for
        for _ in range(2 * RT.MAX_HOPS + 2):  # an answer can start the other agent's turn; hops bound the chain
            ran = False
            for a in (self.manager, self.critic):
                busy = self.terminal_busy(a)
                self.mark_terminal_hold(a, bool(busy))
                if busy or self.rec.agent_busy(a.name):
                    continue
                want = [m for m in self.ledger.waiting(a.name)
                        if m.get("activate") is True or (m.get("activate") == "wait" and mode != "between")]
                if not want or (mode == "between" and self.next_agent() == a.name):
                    continue
                self.message_turn(a, want, mode)
                ran = True
                if self.should_stop():
                    return
            if not ran:
                return

    def worker_working(self, mode: str = "") -> bool:
        """A worker is in a turn now (orch's, or its session open in a terminal)."""
        return mode == "stage" or any(self.rec.agent_busy(w.name) or self.terminal_busy(w) for w in self.workers)

    def worker_idle_why(self) -> str:
        """Why no worker is working now, in a few words for the critic's message turn."""
        mg = self.mg
        g = mg.get("gate") or {}
        if g.get("kind") in ("plan", "milestone", "workers"):
            return "the run is waiting for my decision at a gate"
        if g.get("kind") == "wait" or mg.get("phase") == "wait":
            return "the work is paused until a task of mine is done"
        if mg.get("phase") == "job" or mg.get("job"):
            return "its background job is still running, and your review waits for the job to end"
        if mg.get("held"):
            return "the work is held until I answer a question or close a discussion"
        return "it is between two turns"

    def critic_turn_prompt(self, mode: str) -> str:
        """The critic's message turn. The worker in a turn: the update goes to it now. Its next turn comes before the
        critic's (the step is given): an update goes with that step. Otherwise the critic's next step carries the
        change, so no separate update is written to wait in the queue and stack with that step."""
        if self.worker_working(mode):
            return self.mtpl("critic_message_turn")
        if self.mg.get("phase") in ("work", "parallel", "workers_gate"):
            return self.mtpl("critic_message_turn_next")
        return self.mtpl("critic_message_turn_idle", why=self.worker_idle_why())

    def message_turn(self, a: AgentConfig, want: List[Dict[str, Any]], mode: str = "wait") -> None:
        self._activating = True
        self._hops = max(int(m.get("hops") or 1) for m in want)
        frm = sorted({m["from"] for m in want})
        self.rec.progress(f"✉ {a.called}: {len(want)} message(s) from {', '.join(frm)} arrived; it answers now")
        self.rec.event("activation", agent=a.name, msgs=[m["id"] for m in want])
        checks = a.role == "verifier" and all(m.get("kind") == "progress_check" for m in want)
        try:  # woken by a progress check that found a problem: it messages the worker if needed, nothing goes up
            prompt = (self.mtpl("manager_message_turn") if a.role == "manager" else
                      self.mtpl("critic_check_turn") if checks else self.critic_turn_prompt(mode))
            gate = self.mg.get("gate") or {}
            if a.role == "manager" and gate.get("kind") == "milestone":
                prompt = prompt.rstrip() + "\n\n" + self.mtpl("manager_message_at_gate",
                                                              n=int(self.mg.get("ms", 0)) + 1).strip() + "\n"
            r = self.say(a, prompt, "incoming")
            if r.get("status") == "ok" and a.role == "manager":
                self.after_manager(r, relay=False)
            elif r.get("status") == "ok" and not checks:  # its words for its user, like a check-in update; a question goes up whole
                update = "\n\n".join(x for x in [self.ask_block(r["output"], "verifier")] if x)
                if update:
                    self.deliver(a, self.manager, update, self.mtpl("relay_note_up", note=update).strip(),
                                 source=r["turn_id"], purpose="incoming", hops=self._hops + 1,
                                 wake=self.wakes_manager(r["output"]))
        except _Failed as e:
            self.rec.progress(f"⚠ {a.called}'s message turn did not finish: {e}")
        finally:
            self._activating = False
            self._hops = 0
        for m in want:  # never loop on a message: what this turn didn't carry waits for the next regular turn
            if m["status"] == "sent":
                m["activate"] = False
                self.rec.progress(f"⚠ {m['id']} for {a.called} was not found in its prompt; it waits for its next turn")
        self.rec.save()

    # board controls that act at once, also while a turn runs or the run waits (the others wait for check_control)
    AT_ONCE = ("reply", "correction", "terminal", "discussion", "milestone", "review", "review_comments",
               "chat_question")

    def take_messages(self) -> None:
        """Replies from a terminal, address corrections from board chats, and the switches that act at once."""
        for d in self.rec.take_control(self.AT_ONCE):
            self.message_control(d)

    def message_control(self, d: Dict[str, Any]) -> None:
        kind = d.get("kind")
        if kind == "discussion":
            return self.set_discussion(bool(d.get("on")))
        if kind == "milestone":
            if d.get("action") == "reopen":
                self.reopen_milestone(int(d.get("ms", -1)))
            return
        if kind == "review":
            return self.request_review()
        if kind == "review_comments":
            return self.review_comments(str(d.get("path") or ""))
        a = next((x for x in self.mon.agents if x.name == d.get("agent")), None)
        if a is None:
            return
        if kind == "chat_question":  # a <question> the manager wrote in a board chat reply: like a round question's
            if a.role == "manager":
                qs = self.manager_questions(str(d.get("text") or ""), str(d.get("source") or ""))
                if qs:
                    self.rec.event("chat_question", agent=a.name, source=d.get("source"), qids=[q["id"] for q in qs])
                    self.rec.save()
            return
        if kind == "correction":
            self.correct(a, [str(b) for b in d.get("bad") or []])
        elif kind == "terminal":
            if d.get("event") == "start":
                self.rec.progress(f"⌨ {a.called}'s session is open in a terminal; orch holds its turns and routes "
                                  "its messages")
            elif d.get("event") == "end":
                sid = d.get("session_id")
                old = (self.st["sessions"].get(a.name) or {}).get("session_id")
                if sid and sid != old:
                    self.rec.update_session(a.name, session_id=sid, cut_off=None)
                    self.note_session(a.name, "the terminal session moved to a new session", previous=old,
                                      session_id=sid)
                # its turns there report no cost to orch; the next orch turn's increase includes them
                self.rec.update_session(a.name, terminal_use=now_iso())
                self.rec.progress(f"⌨ {a.called}'s terminal session closed; orch continues it")
            self.rec.event("terminal", agent=a.name, what=d.get("event"), session_id=d.get("session_id"))
        elif kind == "reply":
            self.route_text(a, str(d.get("text") or ""), str(d.get("source") or d.get("file") or ""),
                            str(d.get("via") or "terminal"))

    def route_text(self, a: AgentConfig, text: str, source: str, via: str) -> None:
        """A reply written outside orch's turns (the terminal): its messages go out now, bad addresses are corrected
        at the sender's next turn, and a pause the manager calls stops the work."""
        if source and any(m.get("source") == source for m in self.ledger.rows):
            return  # handed over twice
        sent, bad = RT.split_reply(text, a, self.mon.agents, include_upward=True)
        for s in sent:
            t = s["target"]
            body = self.mtpl("relay_note_up", note=s["body"]).strip() if a.role == "verifier" and t.role == "manager" else ""
            self.deliver(a, t, s["body"], body, source=source, i=s["i"], via=via, purpose=via, urgent=s["urgent"],
                         additive=s["additive"])
        for b in bad:
            self.ledger.add(a.name, "", b, status="held", reason=f"{b} names no one {a.called} can write to; not sent",
                            source=source, via=via)
        self.correct(a, bad)
        if a.role == "manager":
            self.manager_questions(text, source)  # its questions for me count as a round turn's do
            pause = manager_pause(text)
            if pause:
                self.mg["pause_req"] = {"text": pause, "turn": source}
        self.rec.event("reply_routed", agent=a.name, via=via, source=source, sent=len(sent), bad=len(bad))
        if sent:
            self.rec.progress(f"✉ {a.called} ({via}): {len(sent)} message(s) passed on")
        self.rec.save()

    # ---- B6 discussion mode: a board switch. While it is on, the manager's messages to the other agents are held in
    # the ledger (reason "discussion") and the phases that would send its words down wait; the manager is reminded
    # that it is in a discussion. When I close it, the manager gets one turn to send one batch, which supersedes
    # the held messages; the critic decides whether the change interrupts the worker (urgent).

    def pending_switch(self, kind: str) -> Optional[bool]:
        """The newest switch of this kind still waiting in inbox/ (read only), None when there is none."""
        val: Optional[bool] = None
        try:
            files = sorted((self.rec.run_dir / "inbox").glob("*.json"))
        except OSError:
            return None
        for p in files:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get("kind") == kind:
                val = bool(d.get("on"))
        return val

    def discussion_on(self) -> bool:
        """On as set, or switched on on the board and not applied yet (a message is never sent past the switch)."""
        return bool(self.st.get("discussion")) or self.pending_switch("discussion") is True

    def set_discussion(self, on: bool) -> None:
        if on == bool(self.st.get("discussion")):
            return
        self.st["discussion"] = on
        self.rec.event("discussion", on=on)
        if on:
            self.st["discussion_since"] = now_iso()
            self.rec.progress(f"💬 discussion on: nothing from {self.manager.called} goes to the others until it closes")
        else:
            gate = self.discussion_gate()
            self.mg["discussion_batch"] = {"at": now_iso(), "since": self.st.pop("discussion_since", ""),
                                           **({"gate": gate} if gate else {})}
            self.rec.progress(f"💬 discussion closed at the gate: approve or send back; {self.manager.called} then "
                              "sends one message with the discussion and my decision" if gate else
                              f"💬 discussion closed: {self.manager.called} sends one batch with the new directions")
        self.rec.save()

    # gates where a discussion I close waits for my decision, and the phases that wait at them
    DISCUSSION_GATES = ("plan", "milestone")
    DISCUSSION_GATE_PHASES = ("plan_gate", "gate")

    def discussion_gate(self) -> str:
        """The key of an open plan or milestone gate ('' when none): a discussion I close there is not sent at once;
        it goes into the manager's turn after my decision, so the critic gets one message, not two."""
        g = self.mg.get("gate") or {}
        return str(g.get("key") or "") if g.get("kind") in self.DISCUSSION_GATES else ""

    def discussion_held(self) -> List[Dict[str, Any]]:
        return [m for m in self.ledger.rows if m["status"] == "held" and m.get("reason") == RT.DISCUSSION]

    def discussion_held_text(self) -> str:
        held = self.discussion_held()
        if not held:
            return self.mtpl("discussion_held_none").strip()
        names = {x.name: x.called for x in self.mon.agents}
        msgs = "\n\n".join(f"To {names.get(m['to'], m['to'])}:\n<<<\n{m['body']}\n>>>" for m in held)
        return self.mtpl("discussion_held", messages=msgs).strip()

    def discussion_done(self, r: Dict[str, Any], why_none: str = "the discussion closed with nothing new to send",
                        at_gate: bool = False) -> None:
        """After the manager's turn that carried the discussion: what was held is replaced by what it sent."""
        a = self.manager
        held = self.discussion_held()
        new = [m for m in self.ledger.rows if r.get("turn_id") and m.get("source") == r.get("turn_id")
               and m["from"] == a.name and m["status"] in ("sent", "delivered")]
        for m in held:  # not superseded by a message to the same agent: replaced by the batch
            self.ledger.mark(m, "superseded", by=new[0]["id"] if new else "",
                             reason="replaced by the batch sent when the discussion closed" if new else why_none)
        self.rec.event("discussion_batch", turn=r.get("turn_id"), held=[m["id"] for m in held],
                       sent=[m["id"] for m in new], **({"at_gate": True} if at_gate else {}))
        self.rec.progress(f"💬 after the discussion: {len(new)} message(s) sent, {len(held)} held one(s) replaced")
        self.rec.save()

    def gate_discussion(self) -> str:
        """Right after my decision at a gate: the discussion I closed while it was open, as text for the manager's
        next turn ('' when there is none). It is taken off the queue, so it goes once, with my decision."""
        b = self.mg.get("discussion_batch") or {}
        if not b.get("gate") or self.discussion_on():
            return ""
        self.mg.pop("discussion_batch")
        return self.mtpl("manager_discussion_at_gate", held=self.discussion_held_text()).strip()

    def discussion_carried(self, r: Dict[str, Any]) -> None:
        """The turn that carried a discussion closed at the plan gate (the plan or the kickoff) ran: close it out."""
        if self.mg.pop("discussion_carry", None):
            self.discussion_done(r, why_none=CARRIED, at_gate=True)

    def discussion_batch(self) -> None:
        """The manager's one turn after I close the discussion: one message for the critic, which supersedes what
        was held (routing supersede); anything still held after it is dropped as replaced by the batch."""
        a, mg = self.manager, self.mg
        if not mg.get("discussion_batch") or self.discussion_on():
            return
        if mg["discussion_batch"].get("gate") and mg.get("phase") in self.DISCUSSION_GATE_PHASES:
            return  # closed at a gate: it goes with my decision there (gate_discussion), in one turn
        if self.terminal_busy(a) or self.rec.agent_busy(a.name):
            return  # the manager is busy (a board chat, the terminal): the batch comes when it is free
        mg.pop("discussion_batch")
        held_text = self.discussion_held_text()
        self._activating = True
        try:
            r = self.say(a, self.mtpl("manager_discussion_end", held=held_text), "discussion")
            if r.get("status") == "ok":
                self.after_manager(r, relay=False)
        except _Failed as e:
            self.rec.progress(f"⚠ {a.called}'s turn after the discussion did not finish: {e}")
            r = {}
        finally:
            self._activating = False
        self.discussion_done(r)

    # ---- B9: work that waits for me (a blocking question) or for a discussion to close

    def held_by(self, ph: str) -> str:
        """Why the next phase must not start now ('' when it may): a blocking question of the manager's for me
        (outside night mode) holds every phase but the gates; a discussion holds the phases that send the manager's
        words down."""
        if ph in QUESTION_GOES_ON:
            return ""
        if not self.st.get("night"):
            qs = [q["id"] for q in self.human_open() if q.get("blocking")]
            if qs:
                return "question " + ", ".join(qs)
        if ph in DISCUSSION_WAITS and self.discussion_on():
            return "discussion"
        return ""

    def wait_held(self, why: str) -> None:
        """One short wait while the work is held: messages are still answered and controls read."""
        mg = self.mg
        if (mg.get("held") or {}).get("why") != why:
            mg["held"] = {"why": why, "since": now_iso()}
            self.rec.event("held", why=why, phase=mg.get("phase"))
            self.rec.progress(f"⏸ the work waits for my answer to {why[len('question '):]}" if why.startswith("question")
                              else f"⏸ {self.manager.called}'s next step waits for the discussion to close")
            self.rec.save()
        self.activate("wait")
        if self.should_stop():
            raise StopRequested()
        time.sleep(0.2)

    def release_held(self) -> None:
        h = self.mg.pop("held", None)
        if h:
            self.rec.event("held_released", why=h.get("why"))
            self.rec.progress("▶ the work goes on")
            self.rec.save()

    # ---- B6 milestone fallback: the board moves a finished milestone back to open (no prompt to the manager)

    def reopen_milestone(self, i: int) -> None:
        mss = self.mg.get("milestones") or []
        if not 0 <= i < len(mss) or mss[i].get("status") != "done":
            self.rec.progress(f"ⓘ milestone {i + 1} is not a finished one; nothing reopened")
            return
        ms = mss[i]
        ms.update(status="open", reopened_at=now_iso())
        ms.setdefault("reopens", []).append(now_iso())
        p = self.agent_dir(self.critic) / f"checklist-m{i + 1}.md"
        text = B.read(p) or B.CHECKLIST_HEAD
        n = len(B.parse_checklist(text).get("steps") or []) + 1
        item = self.mtpl("checklist_reopened").strip()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text.rstrip("\n") + f"\n\n## Step {n}: Reopened\n\n- [ ] {item}\n", encoding="utf-8")
        rec = (self.st.get("checklists") or {}).get(f"m{i + 1}")
        if rec is not None:
            rec["hash"] = B.digest(B.read(p))
        self.rec.event("milestone_reopened", milestone=i, checklist=str(p))
        self.rec.progress(f"↺ milestone {i + 1} ({ms.get('title', '')}) moved back to open")
        self.rec.save()

    # ---- B9 review file: the manager writes a review for me to comment on; my comments go back to it

    def reviews_dir(self) -> Path:
        return self.rec.run_dir.parent / "reviews"

    def request_review(self) -> None:
        if self.mg.get("review_req"):
            return
        path = self.reviews_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
        self.mg["review_req"] = {"at": now_iso(), "path": str(path)}
        self.rec.event("review_requested", path=str(path))
        self.rec.progress(f"📝 review file asked for; {self.manager.called} writes it when free")
        self.rec.save()

    def review_file_turn(self) -> None:
        a, mg = self.manager, self.mg
        req = mg.get("review_req")
        if not req or self.terminal_busy(a) or self.rec.agent_busy(a.name):
            return
        mg.pop("review_req")
        p = Path(req["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        self._activating = True
        try:
            r = self.say(a, self.mtpl("review_file_request", path=str(p)), "review_file")
            if r.get("status") == "ok":
                self.after_manager(r, relay=False)
        except _Failed as e:
            self.rec.progress(f"⚠ {a.called}'s review file turn did not finish: {e}")
            r = {}
        finally:
            self._activating = False
        how = "written"
        if not p.is_file() and self.for_me(r.get("output") or "").strip():
            p.write_text(self.for_me(r["output"]).strip() + "\n", encoding="utf-8")  # written in the reply instead
            how = "from its reply"
        if not p.is_file():
            self.rec.progress(f"⚠ {a.called} wrote no review file")
            self.st.setdefault("review_files", []).append({"path": str(p), "requested_at": req["at"],
                                                           "turn": r.get("turn_id"), "status": "missing"})
            self.rec.save()
            return
        orig = p.parent / ".orig" / p.name  # what the manager wrote: my comments are what I change in the file
        orig.parent.mkdir(exist_ok=True)
        orig.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        self.st.setdefault("review_files", []).append({"path": str(p), "requested_at": req["at"], "written_at": now_iso(),
                                                       "turn": r.get("turn_id"), "status": "written", "how": how})
        self.rec.event("review_written", path=str(p), turn=r.get("turn_id"))
        self.rec.progress(f"📝 review file written: {p}")
        self.rec.save()

    def review_comments(self, path: str) -> None:
        """My comments in a review file (what I changed in it, as a diff) go to the manager as a message from me."""
        import difflib
        e = next((x for x in self.st.get("review_files") or [] if x["path"] == path), None)
        if e is None:
            self.rec.progress(f"⚠ no review file {path}")
            return
        p = Path(path)
        now, before = B.read(p), B.read(p.parent / ".orig" / p.name)
        diff = "".join(difflib.unified_diff(before.splitlines(True), now.splitlines(True), "as written",
                                            "with my comments", n=2))
        if not diff.strip():
            self.rec.progress(f"ⓘ no comments found in {p.name}; nothing sent")
            return
        text = self.mtpl("review_comments", path=path, diff="```diff\n" + diff.rstrip() + "\n```").strip()
        self.note_file({"to": [self.manager.name], "message": text, "from": "human", "via": "review"})
        (p.parent / ".orig" / p.name).write_text(now, encoding="utf-8")  # the next round of comments starts here
        e.update(status="comments sent", comments_at=now_iso())
        e["rounds"] = int(e.get("rounds") or 0) + 1
        self.rec.event("review_comments", path=path)
        self.rec.progress(f"📝 my comments on {p.name} sent to {self.manager.called}")
        self.rec.save()

    # ---- an agent's session open in a terminal (orch/hooks/terminal_relay.py): one writer at a time

    def terminal_busy(self, a: AgentConfig) -> Optional[Dict[str, Any]]:
        """The busy mark the terminal hook keeps for this agent, unless it has gone stale (a crashed terminal)."""
        p = self.rec.run_dir / "busy" / f"{a.name}.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        touched = float(d.get("touched") or 0)
        if time.time() - touched > float(self.mon.controller.terminal_busy_timeout or 0):
            stale = self.st.setdefault("terminal_stale", {})
            if stale.get(a.name) != touched:
                stale[a.name] = touched
                self.rec.progress(f"⌨ {a.called}'s terminal mark is stale (no activity for "
                                  f"{self.mon.controller.terminal_busy_timeout:g}s); orch continues its session")
                self.rec.event("terminal_stale", agent=a.name)
            return None
        return d

    def mark_terminal_hold(self, a: AgentConfig, busy: bool) -> None:
        L = self.ledger
        for m in L.rows:
            if m["to"] != a.name:
                continue
            if busy and m["status"] == "sent":
                L.mark(m, "held", reason=TERMINAL_HOLD)
            elif not busy and m["status"] == "held" and m.get("reason") == TERMINAL_HOLD:
                L.mark(m, "sent", reason="")

    def hold_while_busy(self, a: AgentConfig) -> None:
        """While the agent's session is open in a terminal, its turn waits (two writers would split the history);
        meanwhile the terminal's messages are routed and the other agents answer theirs."""
        if not self.terminal_busy(a):
            if threading.get_ident() == getattr(self, "_main_ident", None):
                # a terminal that just closed: read its "end" note first, so this turn continues the session the
                # terminal ended in (the mark is gone before orch reads the note)
                for d in self.rec.take_control(("terminal",)):
                    self.message_control(d)
            return
        self.rec.progress(f"⏸ {a.called}'s session is open in a terminal; its turn waits until the terminal closes")
        self.rec.event("terminal_hold", agent=a.name)
        main = threading.get_ident() == getattr(self, "_main_ident", None)
        while self.terminal_busy(a):
            if self.should_stop():
                raise StopRequested()
            if main:
                self.mark_terminal_hold(a, True)
                self.activate("wait")
            time.sleep(0.3)
        self.take_messages()
        self.mark_terminal_hold(a, False)
        self.rec.save()

    # ------------------------------------------------------------------ questions

    def ask_block(self, text: str, role: str = "") -> str:
        """The <question> blocks of a reply as one text (empty if it asks nothing of the level above)."""
        return self.join_questions(T.bodies(text, "question"))

    @staticmethod
    def join_questions(qs: List[str]) -> str:
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
                 answer=answer if status == "answered" else q.get("answer", ""),  # whole: an answer is an instruction
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
        # the critic hears about its own question, never the manager's wording for me
        own = next((self.q(pid) for pid in q.get("parent") or [] if (self.q(pid) or {}).get("from") == self.critic.name), None)
        short_c = truncate(self.question_line(own["text"]), 300) if own else short
        if via == "conversation":  # the manager was in the conversation; only the critic needs telling
            q["via"] = via
            for_c = self.for_critic(ans, fallback=False) or T.outside(ans)  # only what it wrote for the critic
            self.forward(self.critic, self.mtpl("relay_settled_critic", question=short_c, answer=for_c).strip())
        else:
            self.forward(self.manager, self.mtpl("relay_answer_manager", question=short, answer=ans).strip())
            self.forward(self.critic, self.mtpl("relay_answer_critic", question=short_c, answer=ans).strip())
        self.decide(ans if via == "conversation" else f"{self.short_question(q['text'])}: {ans}", "you", q["id"])
        self.rec.event("human_answer", qid=q["id"])
        self.rec.progress(f"✅ **{q['id']}** answered by me: {truncate(ans, 300)}")
        self.log(f"  ✓ {q['id']} answered; the answer reaches {self.manager.name} and {self.critic.name} next")
        self.rec.save()

    # ------------------------------------------------------------------ decisions register

    def decide(self, text: str, by: str, source: str, at: str = "") -> None:
        """Record something settled: my answers and notes, gate messages, the manager's answers to the critic.
        Entries are kept whole; nothing is cut from the register."""
        text = (text or "").strip()
        regs = self.st.setdefault("decisions", [])
        if not text or any(d["text"] == text for d in regs):
            return
        d = {"id": f"S{len(regs) + 1}", "text": text, "by": by, "source": source, "at": at or now_iso()}
        regs.append(d)
        self.rec.event("decision", id=d["id"], by=by, source=source)
        self.write_decisions()

    def write_decisions(self) -> None:
        regs = self.st.get("decisions") or []
        lines = ["# Decisions", "", "Binding for the whole run; where two say different things, the later one holds.", ""]
        for d in regs:
            src = "note" if str(d["source"]).startswith("note to ") else d["source"]  # agents read it: no names
            lines.append(f"## {d['id']} · {d['at'][:16].replace('T', ' ')} · {src}")
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
        if role in ("verifier", "worker"):  # below the manager: no night-mode choices (its text for me), no notes to others
            kept = [d for d in kept if "night mode" not in str(d.get("by", ""))
                    and not (str(d.get("source", "")).startswith("note to ")
                             and a.name not in str(d["source"])[len("note to "):].split(", "))]
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

    # ------------------------------------------------------------------ the step checklist (orch/boards.py)
    # One file per milestone in the critic's folder, holding its steps and their items; the critic edits it in place.
    # PASS and the plan's progress are read from it. A reply that still carries a <plan> or <checklist> block (the
    # older protocol) is written into the file, unless the critic edited the file in that turn: then the file holds.

    def agent_dir(self, a: AgentConfig) -> Path:
        return Path(a.state_file).parent if a.state_file else self.rec.run_dir.parent / "agents" / a.name

    def checklist_key(self) -> Optional[str]:
        return f"m{self.mg.get('ms', 0) + 1}" if self.mg.get("milestones") else None

    def checklist_path(self) -> Optional[Path]:
        key = self.checklist_key()
        return self.agent_dir(self.critic) / f"checklist-{key}.md" if key else None

    def ck_rec(self, start: bool = False) -> Optional[Dict[str, Any]]:
        """This milestone's checklist record in the run state ({hash, path}); made the first time it is needed. A
        file already there from an earlier run (any file, when the milestone starts) is moved aside; a run from
        before checklist files is seeded from the critic's latest blocks."""
        key, p = self.checklist_key(), self.checklist_path()
        if key is None or p is None:
            return None
        recs = self.st.setdefault("checklists", {})
        r = recs.get(key)
        if r is not None:
            return r
        if p.exists() and (start or self._older_than_milestone(p)):
            old = p.with_name(f"{p.stem}.earlier-{time.strftime('%Y%m%d-%H%M%S')}.md")
            p.replace(old)
            self.rec.event("checklist_moved", path=str(p), to=str(old))
            self.log(f"an earlier checklist file moved to {old}")
        if not p.exists():
            seed = None if start else B.merge_blocks(None, self._legacy_plan(), self._legacy_checklist())
            text = B.render_checklist(seed) if seed and seed.get("steps") else (B.CHECKLIST_HEAD if start else "")
            if text:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
        r = recs[key] = {"path": str(p), "hash": B.digest(B.read(p))}
        self.rec.save()
        return r

    def _older_than_milestone(self, p: Path) -> bool:
        started = (self.cur() or {}).get("started_at")
        if not started:
            return False
        try:
            import datetime as _dt
            return p.stat().st_mtime < _dt.datetime.fromisoformat(started).timestamp()
        except (OSError, ValueError):
            return False

    def checklist_board(self) -> Dict[str, Any]:
        """The parsed checklist file of the current milestone (empty without one)."""
        if self.ck_rec() is None:
            return {"steps": [], "standing": []}
        return B.parse_checklist(B.read(self.checklist_path()))

    def latest_checklist(self) -> str:
        """The checklist that gates the current step (standing rules and the step's items), from the file."""
        return B.current_list(self.checklist_board())

    def latest_plan(self) -> str:
        """The critic's steps for the current milestone as plan lines, from the file ('' without steps)."""
        return B.plan_lines(self.checklist_board())

    def sync_checklist(self, r: Dict[str, Any]) -> None:
        """After a critic turn: take in an older-protocol block (unless the file was edited in this turn), note the
        file's state in the turn record (for the board), and ask once to merge when the list grows past the cap."""
        rec = self.ck_rec()
        if rec is None:
            return
        p, out = self.checklist_path(), r.get("output") or ""
        text = B.read(p)
        plan, ck = plan_text(out), "\n".join(T.bodies(out, "checklist"))
        if plan.strip() or ck.strip():
            if B.digest(text) != rec.get("hash"):
                self.rec.event("checklist_block_ignored", turn=r.get("turn_id"))
                self.rec.progress(f"ⓘ {self.critic.called} edited its checklist file; the list in its reply is not used")
            else:
                text = B.render_checklist(B.merge_blocks(B.parse_checklist(text) if text.strip() else None, plan, ck))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                self.rec.event("checklist_from_reply", turn=r.get("turn_id"))
        rec["hash"] = B.digest(text)
        board = B.parse_checklist(text)
        cur = B.current_list(board)
        r["checklist_file"] = {"path": str(p), "checklist": cur, "plan": B.plan_lines(board)}
        if r.get("turn_id") and r.get("round") is not None and r.get("seq") is not None:
            with self.rec.lock:
                write_json(self.rec.round_dir(r["round"]) / f"t{r['seq']:02d}-{r['agent']}.json", r)
        n = sum(checklist_state(cur))
        if n > B.CHECKLIST_CAP:
            if rec.get("cap_at") is None or n > rec["cap_at"]:
                rec["cap_at"] = n
                self.forward(self.critic, self.mtpl("checklist_cap", n=n, cap=B.CHECKLIST_CAP).strip())
                self.rec.progress(f"⚠ {self.critic.called}'s checklist for the current step has {n} items (about "
                                  f"{B.CHECKLIST_CAP} at most); asked to merge")
                self.rec.event("board_warning", what="checklist_size", items=n,
                               problem=f"the current step's checklist has {n} items")
        else:
            rec.pop("cap_at", None)
        self.rec.save()

    def _legacy_checklist(self) -> str:
        """The critic's newest <checklist> block in this milestone's turns (runs from before checklist files)."""
        ms = self.mg.get("ms", 0) if self.mg.get("milestones") else None
        for p in sorted((self.rec.run_dir / "rounds").glob("r*/t*.json"), reverse=True):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if t.get("agent") != self.critic.name:
                continue
            if ms is not None and t.get("milestone") is not None and t.get("milestone") != ms:
                return ""
            if t.get("purpose") in ("verify", "amend", "guidance", "brief"):
                ck = "\n".join(T.bodies(t.get("output") or "", "checklist"))
                if ck:
                    return ck
        return ""

    def _legacy_plan(self) -> str:
        """The critic's newest <plan> block in this milestone's turns (runs from before checklist files)."""
        ms = self.mg.get("ms", 0) if self.mg.get("milestones") else None
        for p in sorted((self.rec.run_dir / "rounds").glob("r*/t*.json"), reverse=True):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if t.get("agent") != self.critic.name:
                continue
            if ms is not None and t.get("milestone") is not None and t.get("milestone") != ms:
                return ""  # older turns belong to an earlier milestone
            plan = plan_text(t.get("output") or "")
            if plan:
                return plan
        return ""

    def stalled(self) -> int:
        """Rounds since the last step of the critic's plan finished (or since this work started); runs from before
        plans count every round of the milestone."""
        mg = self.mg
        return int(mg["stall"] if mg.get("stall") is not None else mg.get("ms_round", 0) or 0)

    def plan_line(self) -> str:
        """One status line from the critic's plan ('' without a plan)."""
        done, total, cur = plan_status(self.latest_plan())
        if not total:
            return ""
        mg, n = self.mg, self.stalled()
        if done >= total:
            return f"all {total} steps of the plan are done; the final review comes next"
        line = f"step {done + 1} of {total} ({_clip(cur, 200)}), {n} round{'s' if n != 1 else ''} on it"
        if mg.get("step_round"):
            line += f"; the last step finished in round {mg['step_round']}"
        return line

    def limit_note(self) -> str:
        mg, n = self.mg, self.stalled()
        if self.latest_plan():
            return f", since {n} rounds have passed without a step of your plan finishing"
        return f", since we have spent {mg.get('ms_round', 0)} rounds on it"

    def limit_title(self) -> str:
        mg, n = self.mg, self.stalled()
        if self.latest_plan():
            return f"no step finished in {n} rounds"
        return f"round limit reached ({mg.get('ms_round')} rounds)"

    def note_plan(self, r: Dict[str, Any]) -> None:
        """After a critic turn: a step finished in its checklist file is progress, so the stall count starts again."""
        plan = self.latest_plan()
        if not plan:
            return
        mg = self.mg
        done, total, cur = plan_status(plan)
        before = mg.get("plan_done")
        mg["plan_done"], mg["plan_total"] = done, total
        if before is not None and done > before:
            mg["stall"] = 0
            mg["step_round"] = int(self.st.get("round", 0))
            # B7: between steps (never mid-task), agents set to compact_between_steps compact before their next turn
            due = [a.name for a in self.mon.agents if a.compact_between_steps and self.has_session(a)]
            if due:
                mg["compact_due"] = sorted(set(mg.get("compact_due") or []) | set(due))
            self.rec.event("plan_step", turn=r.get("turn_id"), done=done, total=total, next=cur)
            self.rec.progress(f"✓ step {done} of {total} done" + (f"; next: {_clip(cur, 160)}" if cur else ""))

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
          questions (what holds is on the golden board, sent with the first message of the new session).
        - critic: its state (my standing instruction and its checklist) and its latest review in short; in a
          top-down restart only its checklist, since my new instruction comes next.
        - worker: that this is a fresh conversation, plus its state (its latest report) outside a top-down restart;
          the critic's instruction comes with the next message."""
        mg, ms = self.mg, (self.cur() or {})
        if a.role == "worker":
            body, _at = self.read_state(a)
            work = body or ((mg.get("work") or {}).get(a.name) or {}).get("output") or ""
            state = "" if top_down or not work else "Your latest report:\n\n" + work
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
        if a.role == "manager":
            parts.append("The goal:\n\n" + (self.mon.goal or "").strip())
            if mg.get("plan_text"):
                parts.append("Your plan and rubric, as I approved them:\n\n" + mg["plan_text"].strip())
            rows = [f"| {i + 1} | {m.get('title', '')} | {m.get('status', '')} | {m.get('score') if m.get('score') is not None else ''} |"
                    for i, m in enumerate(mg.get("milestones") or [])]
            if rows:
                parts.append("| # | milestone | status | score |\n|---|---|---|---|\n" + "\n".join(rows))
            if mine:
                parts.append(f"Your working state (`{self.state_path(a)}`, written {mine_at[5:16].replace('T', ' ')}):\n\n"
                             + mine)
            t = self.last_turn(self.critic.name, ("report", "checkin", "verify", "amend", "guidance", "brief"))
            if t:  # the critic's newest message of any kind: a report or check-in can be many rounds old
                text = strip_checklist(self.clean_report(t.get("output") or ""))
                ck = self.latest_checklist()
                done, open_ = checklist_state(ck) if ck else (0, 0)
                pd, pt, _cur = plan_status(self.latest_plan())
                parts.append(_cap(f"{self.critic.called}'s latest message ({t.get('purpose')}, "
                                  f"{(t.get('started_at') or '')[5:16].replace('T', ' ')}"
                                  + (f"; its plan: step {min(pd + 1, pt)} of {pt}" if pt else "")
                                  + (f"; its checklist: {done} of {done + open_} done" if ck else "") + "):\n\n")
                             + text)
        elif a.role == "verifier":
            body, _at = self.read_state(a)
            ck, plan = self.latest_checklist(), self.latest_plan()
            p = self.checklist_path()
            if p is not None and p.exists():
                parts.append(f"Your steps and checklist are in `{p}`; edit them there.")
            if top_down:  # where the work stands carries over; the checklist is rebuilt from the new instruction
                if plan:
                    parts.append("Your plan so far (steps marked [x] are done; revise it to fit my new instruction):"
                                 "\n\n" + plan)
                if ck:
                    parts.append("Your last checklist, for reference: write a new one for the step you hand out next, "
                                 "carrying over only the standing rules that still hold:\n\n" + ck)
            elif body:
                parts.append(f"Your working state (`{self.state_path(a)}`):\n\n"
                             + body)
            else:  # a run from before state files
                when = lambda k: f" ({mg[k][5:16].replace('T', ' ')})" if mg.get(k) else ""
                if mg.get("note"):
                    parts.append(f"My instruction when this part of the work started{when('note_at')}; newer decisions "
                                 "below override it:\n\n" + mg["note"].strip())
                if mg.get("guidance"):
                    parts.append(f"My latest guidance{when('guidance_at')}:\n\n" + mg["guidance"].strip())
                if plan:
                    parts.append("Your plan (keep it; tick steps as they finish):\n\n" + plan)
                if ck:
                    parts.append("Your checklist for the current step (keep the items as they are):\n\n" + ck)
            last = "" if top_down else self.last_output(a.name, ("verify", "guidance", "brief", "amend"))
            if last:  # the prose briefly; the checklist is above
                prose = strip_checklist(T.without_markup(last, drop=("plan", "checklist", "verdict")))
                parts.append("Your latest review:\n\n" + prose)
        qs = [q for q in self.st.get("questions") or [] if q.get("status") == "open" and q.get("to") == "human"]
        if qs and a.role == "manager":  # the manager's questions to me: never shown below it
            parts.append("Questions still open with me: " + "; ".join(f"{q['id']} {self.short_question(q['text'])}" for q in qs))
        # what holds (rules, preferences, key decisions) is on the golden board, which a new session gets with this
        # message; the decisions register stays a record and is not handed over
        return self.mtpl("session_reset", state="\n\n".join(parts)).strip()

    def report_text(self, r: Optional[Dict[str, Any]]) -> str:
        if r and r.get("status") == "ok":
            output = CP.upward(r.get("output") or "")
            questions = T.bodies(r.get("output") or "", "question")
            output = "\n\n".join([output] + [f"<question>{q}</question>" for q in questions])
            r = {**r, "output": output}
        return super().report_text(r)

    def clean_report(self, text: str) -> str:
        return T.without_markup(text, drop=("plan", "checklist", "verdict"))

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
            prev = {k: v for k, v in sess.items() if k not in ("previous", "resets", "cut_off")}
            old = sess.get("session_id")
            self.rec.update_session(a.name, session_id=None, turns=0, reset_at=now_iso(), cut_off=None,
                                    previous=(sess.get("previous") or []) + ([prev] if prev.get("session_id") else []))
            fwd = self.session_notes_dropped(a)
            self.st["fwd"][a.name] = [HANDOVER_TOP_DOWN if top_down else HANDOVER] + fwd  # written at its next turn
            self.rec.event("session_reset", agent=a.name, session_id=old)
            if old:
                self.note_session(a.name, "sessions reset (refresh)", previous=old)
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
        self.after_manager(r, relay=False)
        msg = self.critic_message(r, "restart")
        mg.update(guidance=msg, guidance_at=now_iso())
        self.decide(msg, self.manager.called, "restart")
        c = self.say(self.critic, self.mtpl("critic_restart", instruction=msg, human_messages="",
                                            cwd=self.critic.working_dir), "brief")
        self.worker_feedback_for(c["output"])
        mg.pop("restart", None)
        mg["phase"] = "work"
        self.critic_asks(c, after="work")
        return None

    # notes tied to an agent's old session (handovers, "you were cut off", a fresh-start line, a progress check of
    # earlier work): a new session drops them
    SESSION_NOTES = ("Your last turn was cut off", "We are starting a new part", "You are taking over",
                     "This is a fresh conversation", "Your last turn ran past", "I ended your last turn early",
                     "Progress check, ")

    def session_notes_dropped(self, a: AgentConfig) -> List[str]:
        q = self.st.setdefault("fwd", {}).get(a.name) or []
        return [x for x in q if x not in (HANDOVER, HANDOVER_TOP_DOWN) and not x.startswith(self.SESSION_NOTES)]

    def fresh_sessions(self) -> None:
        """B7. At a milestone start the critic and the workers have their sessions compacted in place (`fresh_session:
        compact`, the default) and get a short note that a new part starts; when the backend can't compact (or the
        compact fails), or with `fresh_session: milestone`, they start new sessions instead: their system prompt and a
        short natural instruction, nothing pasted from earlier rounds. "never" keeps the session as it is. What holds
        is on the golden board (its reminder comes with a new or compacted session's next message) and in the critic's
        checklist file, which they read. The manager keeps its session (unless set otherwise)."""
        mg = self.mg
        if mg.get("fresh_for") == mg.get("ms"):
            return
        n = mg.get("ms", 0) + 1
        for a in self.mon.agents:
            if a.fresh_mode in ("milestone", "compact") and self.has_session(a):
                if a.fresh_mode == "compact" and self.compact(a, int(self.st.get("round", 0)), f"milestone {n} starts"):
                    note = {"worker": "milestone_compact", "verifier": "milestone_compact_critic"}.get(a.role)
                    if note:  # the critic's brief, or the manager's kickoff, follows
                        fwd = self.st.setdefault("fwd", {})
                        fwd[a.name] = [self.mtpl(note, cwd=a.working_dir).strip()] + list(fwd.get(a.name) or [])
                    self.log(f"{a.name}: session compacted for milestone {n}")
                    continue
                sess = self.st["sessions"].get(a.name) or {}
                prev = {k: v for k, v in sess.items() if k not in ("previous", "resets", "cut_off")}
                old = sess.get("session_id")
                self.rec.update_session(a.name, session_id=None, turns=0, cut_off=None,
                                        previous=(sess.get("previous") or []) + [prev])
                note = {"worker": "milestone_fresh", "verifier": "milestone_fresh_critic"}.get(a.role)
                self.st["fwd"][a.name] = ([self.mtpl(note, cwd=a.working_dir).strip()] if note else [HANDOVER]) \
                    + self.session_notes_dropped(a)  # the critic's brief, or the manager's kickoff, follows
                self.rec.event("fresh_session", agent=a.name, milestone=mg.get("ms"), session_id=old)
                self.note_session(a.name, f"milestone {mg.get('ms', 0) + 1} starts", previous=old)
                self.log(f"{a.name}: fresh session for milestone {mg.get('ms', 0) + 1}")
        mg["fresh_for"] = mg.get("ms")
        self.rec.save()

    def compact(self, agent: AgentConfig, rnd: int, why: str = "between steps") -> bool:
        """B7: compact the agent's session before its next turn, between steps of the critic's plan (never mid-task:
        the agent is idle and its lock is held). Claude Code: `/compact` in its resumed session; a backend that can't
        compact on request (Codex) is skipped with one note. The compact is recorded as an entry of its own (its cost
        too), and the next turn records the compact as its session reset. Also at a milestone start (fresh_session:
        compact; `why` names it). True when the session was compacted."""
        mg = self.mg
        mg["compact_due"] = [x for x in mg.get("compact_due") or [] if x != agent.name]
        self.rec.save()
        sess = self.st["sessions"].get(agent.name) or {}
        sid = sess.get("session_id")
        if not sid:
            return False
        chain = agent.variants()
        v = next((x for x in chain if x.session_key == sess.get("session_key")), chain[0])
        backend = get_backend(v.backend)
        req = TurnRequest(agent=agent.name, role=agent.role, prompt="", system_prompt=agent.current_system_prompt(),
                          session_id=sid, cwd=agent.working_dir, timeout=agent.timeout, options=v.options,
                          env={**v.env, _jobs.TAG_VAR: _jobs.tag_for(self.rec.run_dir.name, agent.name)},
                          context={"round": rnd, "purpose": "compact", "monitor": self.mon.name})
        seq = self.rec.next_seq(rnd)
        record: Dict[str, Any] = {
            "turn_id": self.rec.turn_id(rnd, seq, agent.name), "round": rnd, "seq": seq, "agent": agent.name,
            "role": agent.role, "purpose": "compact", "backend": v.backend, "model": v.label, "cwd": agent.working_dir,
            "session_id": sid, "resumed_session": True, "status": "running", "started_at": now_iso(),
            "input": "/compact", "output": "", "milestone": mg.get("ms", 0) if mg.get("milestones") else None,
            "compact_reason": why}
        t0 = time.monotonic()
        with self.unlocked(), self.rec.agent_lock(agent.name):
            try:
                res = backend.compact(req, self.should_stop)
            except Exception as e:
                res = TurnResult("error", "", sid, None, f"{type(e).__name__}: {e}", [], {})
        if res is None:
            if not (self.st.get("compact_skipped") or {}).get(v.backend):
                self.st.setdefault("compact_skipped", {})[v.backend] = True
                self.rec.progress(f"🗜 {agent.called}: compacting on request is not available for `{v.backend}`; "
                                  "its own context handling applies (at a milestone: a new session)")
                self.rec.event("compact_skipped", agent=agent.name, backend=v.backend)
                self.rec.save()
            return False
        record.update(status=res.status, output=res.output or "", meta=res.meta, command=res.command,
                      stderr=(res.stderr or "")[-2000:], exit_code=res.exit_code, ended_at=now_iso(),
                      duration_s=round(time.monotonic() - t0, 2), session_id=res.session_id or sid)
        self.rec.write_turn(record)
        if res.status == "ok":
            if res.session_id and res.session_id != sid:
                self.rec.update_session(agent.name, session_id=res.session_id)
            self.note_session(agent.name, f"compacted {why}" if why == "between steps" else f"compacted: {why}",
                              change="compact", turn=record["turn_id"])
            self.after_compact(agent, f"compact ({why})")
            return True
        self.rec.progress(f"🗜 {agent.called}: compact ({why}) failed ({res.status}); "
                          + ("a new session instead" if why != "between steps" else "the session goes on"))
        return False

    @staticmethod
    def for_me(text: str) -> str:
        """The part of a manager reply that is for me: everything outside the tags."""
        return T.outside(text)

    def after_manager(self, r: Dict[str, Any], parent: Optional[List[str]] = None, relay: bool = True) -> str:
        """Record the manager's questions for me (one per <question>, non-blocking), pass on its messages (unless the
        caller takes the message for the critic itself) and note a pause it calls; return its whole reply."""
        text = r.get("output") or ""
        self.manager_questions(text, r["turn_id"], parent)
        if relay:  # its message to the critic as a note; a message to the worker is routed after the turn (route_reply)
            for i, (attrs, body) in enumerate(T.blocks(text, "message")):
                a = self.find_agent(attrs.get("to", ""))
                if a and a.role == "verifier" and body:
                    note = self.mtpl("relay_note", note=body).strip()
                    if self.discussion_on():  # B6: held until the discussion closes; its batch replaces it
                        for m in self.ledger.rows:
                            if m.get("source") == r.get("turn_id") and m["to"] == a.name and m.get("i") == i \
                                    and m["status"] == "sent":
                                self.ledger.mark(m, "held", reason=RT.DISCUSSION, text=note,
                                                 carrier={"discussion": True})
                        continue
                    self.forward(a, note)
                    # now a waiting note: a newer message to the critic (e.g. the kickoff's instruction after one
                    # written with the plan) replaces it instead of both being delivered (routing.py)
                    for m in self.ledger.rows:
                        if m.get("source") == r.get("turn_id") and m["to"] == a.name and m.get("i") == i \
                                and m["status"] == "sent":
                            m.update(text=note, carrier={**(m.get("carrier") or {}), "fwd": True})
        pause = manager_pause(text) if r.get("purpose") != "pause" else ""  # it stops the run itself
        if pause:
            self.mg["pause_req"] = {"text": pause, "turn": r["turn_id"]}
        return text

    def manager_questions(self, text: str, turn: str, parent: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Each <question> of a manager reply is a question for me (a round turn, a board chat reply, the terminal):
        blocking unless it says blocking="no"; in night mode its own recommendation stands and is recorded."""
        if turn and any(q.get("turn") == turn and q.get("from") == self.manager.name
                        for q in self.st.get("questions") or []):
            return []  # handed over twice
        mine = truncate(self.for_me(text), 1500)
        out = []
        for attrs, q in T.blocks(text, "question"):
            if not (q or "").strip():
                continue
            nq = self.new_question(self.manager, "human", q, turn, parent, assumption=mine)
            # B9: the work that depends on it waits for my answer, unless the manager says it can go on meanwhile
            nq["blocking"] = T._norm(attrs.get("blocking", "")) not in ("no", "false", "0")
            if self.st.get("night"):  # nobody is watching: the manager's choice stands until I say otherwise
                nq.update(status="night", answered_by=self.manager.name, answer=mine, answer_turn=turn,
                          answered_at=now_iso())
                self.decide(f"{self.short_question(q)}: {mine or '(see the manager turn)'}", "manager (night mode)",
                            nq["id"])
            out.append(nq)
        return out

    def find_agent(self, who: str) -> Optional[AgentConfig]:
        w = re.sub(r"^the\s+", "", who.lower().strip())
        for a in self.mon.agents:
            if w in (a.name.lower(), re.sub(r"^the\s+", "", a.called.lower())):
                return a
        return None

    def critic_asks(self, r: Dict[str, Any], after: str) -> bool:
        """If the critic asked the manager something, record it and answer it right away (next phase). A question
        inside a <message to="you"> note counts too: the parser keeps nested tags in the note's body."""
        text = r.get("output") or ""
        qs = T.bodies(text, "question") + [q for n in self.notes_up(text) for q in T.bodies(n, "question")]
        block = self.join_questions(qs)
        wq = [x["id"] for w in self.workers for x in self.open_qs(w.name)]
        if not block:
            for qid in wq:
                self.settle(self.q(qid), self.critic.name, "", r["turn_id"])  # answered in the critic's reply
            return False
        for qid in wq:
            self.settle(self.q(qid), "", "", r["turn_id"], status="passed_up")
        q = self.new_question(self.critic, self.manager.name, block, r["turn_id"], wq)
        self.unnest_questions(r, block)
        self.mg.update(ask=q["id"], after=after, phase="answer")
        return True

    def unnest_questions(self, r: Dict[str, Any], block: str) -> None:
        """A question nested in the critic's note to the manager is asked at once (the next phase), so the note
        that waits for the manager goes without it. Only here: a question is never cut from a note otherwise."""
        q = (self.st.get("fwd") or {}).get(self.manager.name) or []
        for m in RT.Ledger(self.st).rows:
            if m.get("source") != r.get("turn_id") or m.get("to") != self.manager.name or not QUESTION_TAG.search(m["body"]):
                continue
            rest = QUESTION_TAG.sub("", m["body"]).strip()
            new = (f"Progress records from the critic\n\n[{m.get('created_at') or now_iso()}] {rest}"
                   if rest else "")
            if m["text"] in q:
                i = q.index(m["text"])
                q[i:i + 1] = [new] if new else []
            m.update(text=new or block, carrier={"phase": "answer"} if not new else m.get("carrier"))
            if new and m["status"] == "sent" and not m.get("urgent"):  # the rest has nothing to decide: no turn of its own
                RT.Ledger(self.st).mark(m, "held", reason=RT.NEXT_TURN, next_turn=True, activate=False)

    def amend_feedback(self, r: Dict[str, Any]) -> None:
        """Amend only the explicitly addressed workers; other queued assignments stay intact."""
        out = r.get("output") or ""
        fb = self.mg.setdefault("feedback", {})
        replaced = []
        why = "replaced by the critic's amended instruction for the same step before it was delivered"
        for w in self.workers:
            text = self.clean_for(out, w)
            if not text:
                continue
            names = [w.called, w.name] + (["worker"] if w.name not in self.clones else [])
            add = any(RT.additive(attrs) for attrs, body in T.blocks(out, "message")
                      if body and T.addressed(attrs, *names))
            old = fb.get(w.name, "")
            fb[w.name] = "\n\n".join(x for x in [old if add else "", text] if x)
            if add or not old:
                continue
            replaced.append(w.name)
            for m in self.ledger.rows:
                if (m["from"] == self.critic.name and m["to"] == w.name and m["status"] == "sent"
                        and "phase" in (m.get("carrier") or {}) and m.get("source") != r.get("turn_id")):
                    self.ledger.mark(m, "superseded", reason=why, by_turn=r.get("turn_id"))
        if replaced:
            self.rec.event("feedback_superseded", turn=r.get("turn_id"), workers=replaced)
            self.rec.progress(f"↷ {self.critic.called}'s amended instruction replaces its queued review for "
                              + ", ".join(replaced))

    def worker_feedback_for(self, output: str, append: bool = False) -> None:
        fb = self.mg.setdefault("feedback", {})
        for w in self.workers:
            text = self.clean_for(output, w)
            fb[w.name] = "\n\n".join(x for x in [fb.get(w.name, "") if append else "", text] if x)

    def upward(self, text: str) -> str:
        return CP.upward(text, (self.manager.name, self.manager.called, "manager"))

    def for_critic(self, text: str, fallback: bool = True) -> str:
        """Only explicitly addressed manager messages reach the critic."""
        c = self.critic
        return "\n\n".join(b for a, b in T.blocks(text, "message")
                            if b and T.addressed(a, c.called, c.name, "critic"))

    def critic_message(self, r: Dict[str, Any], purpose: str = "") -> str:
        """The manager's message for the critic in a reply where the work needs one. Without a
        <message to="critic">, the manager is asked once more; if that fails too, the critic is told to carry on
        (never handed the manager's text for me)."""
        msg = self.for_critic(r.get("output") or "")
        if msg or not self.mon.controller.protocol or r.get("status") != "ok":
            return msg
        self.rec.progress(f"⚠ {self.manager.called}: no message for {self.critic.called} in its reply; asked again")
        r2 = self.say(self.manager, self.mtpl("manager_no_message").strip(), "message")
        self.after_manager(r2, relay=False)
        msg = self.for_critic(r2.get("output") or "")
        if not msg:
            self.rec.progress(f"⚠ {self.manager.called}: still no message for {self.critic.called}; it carries on")
            msg = self.mtpl("critic_no_message").strip()
        return msg

    def clean_critic(self, text: str) -> str:
        """Explicit worker messages only; untagged text always goes upward."""
        names = [n for w in self.workers for n in (w.called, w.name)] + ["worker"]
        extra = [b for a, b in T.blocks(text, "message") if b and T.addressed(a, *names)]
        extra += [sec["text"] for sec in PW.to_sections(text)
                  if T.addressed({"to": sec["worker"]}, *names)]
        return self.for_worker("\n\n".join(extra))

    def clean_for(self, text: str, w: AgentConfig) -> str:
        """Only messages explicitly addressed to this worker, independent of turn type."""
        names = [w.called, w.name] + (["worker"] if w.name not in self.clones else [])
        extra = [b for a, b in T.blocks(text, "message") if b and T.addressed(a, *names)]
        if not self.par:
            extra += [sec["text"] for sec in PW.to_sections(text)
                      if T.addressed({"to": sec["worker"]}, *names)]
        return self.for_worker("\n\n".join(extra))

    def for_worker(self, text: str) -> str:
        """Preserve explicitly addressed message content; dispatch never interprets its prose."""
        return (text or "").strip()

    def file_verdict(self) -> str:
        """PASS when the critic's checklist file has items and none is open."""
        done, open_ = checklist_state(self.latest_checklist())
        return "PASS" if done and not open_ else "FAIL"

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
        night: Optional[bool] = None
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
            elif d.get("kind") == "night":
                night = bool(d.get("on"))  # applied after the answers below: one may close the open question
            elif d.get("kind") == "job":
                self.mg["job_release"] = True
            elif d.get("kind") == "pause" and d.get("message"):  # from a chat with the manager on the board
                self.mg["pause_req"] = {"text": str(d["message"]), "turn": d.get("turn", "")}
            elif d.get("kind") in self.AT_ONCE:  # the terminal; corrections from a board chat; board switches
                self.message_control(d)
        for qid, msgs in answers.items():
            if msgs and qid not in talked:
                self.human_answer(qid, "\n\n".join(msgs))
        for qid, text in talked.items():
            if text:
                self.human_answer(qid, text, via="conversation")
        self.switch_night(night)
        self.rec.save()

    def human_open(self) -> List[Dict[str, Any]]:
        """Questions for me that are still open."""
        return [q for q in self.st.get("questions") or [] if q.get("to") == "human" and q.get("status") == "open"]

    def switch_night(self, want: Optional[bool]) -> None:
        """B9: night mode switches only while no question for me is open; a switch asked meanwhile waits (queued) and
        is applied once the question closes."""
        if want is not None:
            self.st["night_queued"] = want
        want = self.st.get("night_queued")
        if want is None:
            return
        if want == bool(self.st.get("night")):
            self.st.pop("night_queued", None)
            return
        open_ = self.human_open()
        if open_:
            ids = ", ".join(q["id"] for q in open_)
            if self.st.get("night_queued_for") != ids:
                self.st["night_queued_for"] = ids
                self.rec.progress(f"🌙 night mode switches {'on' if want else 'off'} once {ids} is closed")
                self.rec.event("night_queued", on=want, questions=ids)
            return
        self.st.pop("night_queued", None)
        self.st.pop("night_queued_for", None)
        self.st["night"] = want
        self.rec.event("night", on=want)
        self.rec.progress("🌙 night mode on: the manager decides alone" if want else "☀ night mode off")

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
                self.activate("wait")  # a message for the manager or critic is answered while the run waits
                queue_ = self.st.get("gate_queue") or []
                while queue_ and decision is None:
                    d = queue_.pop(0)
                    if not d.get("gate") or d["gate"] == gate["key"]:  # stale clicks for an older gate are dropped
                        decision = d
                if decision is None and self.st.get("night") and gate["kind"] == "workers":
                    decision = {"decision": "approve", "message": "", "night": True}  # clipped to the hard cap
                if decision is None and self.st.get("night") and gate["kind"] in ("plan", "milestone"):
                    ok = gate["kind"] == "plan" or self.cur() is None or self.cur().get("accepted", True)
                    decision = {"decision": "approve" if ok else "revise", "message": "", "night": True}
                    self.decide(f"{gate['title']}: {'approved' if ok else 'kept going with the manager’s guidance'}"
                                " (night mode, nobody at the gate)", "manager (night mode)", "gate")
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
                    if gate["kind"] == "wait":  # typed text is my note with "done" (send back: `orch approve --revise`)
                        decision = {"decision": "approve", "message": "" if ok else line}
                    else:
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
        self._main_ident = threading.get_ident()
        if self.st.pop("status_lines", None):  # lines left by a run that stopped mid-turn: nothing is watched yet
            self.rec.save()
        self.adopt_state_files()
        self.setup_boards()
        try:
            self.par_restore()  # B11: a parallel section a restart cut off gets its workers back
        except _Failed as f:
            self.rec.set(error=str(f))
            return "failed"
        while True:
            self.check_control()
            self.audit_collect()  # an audit that finished while the rounds went on
            if mg.get("phase", "plan") != "done":
                try:
                    self.activate("between")  # messages from a chat, the terminal or a note start their turn now
                except _Failed as f:
                    self.rec.set(error=str(f))
                    return "failed"
            ph = mg.get("phase", "plan")
            if ph == "done":
                self.audit_due(final=True)
                return "completed"
            if ph == "work" and not mg.get("in_work"):  # between rounds
                self.audit_due()
            if ph == "work" and mg.get("pause_req") and not mg.get("in_work"):  # the manager called a pause
                req = mg.pop("pause_req")
                mg["wait"] = {"who": "the manager", "text": req.get("text", ""), "proposal": "",
                              "turn": req.get("turn", "")}
                mg["phase"] = ph = "wait"
                self.rec.progress("⏸ the manager paused the work until a task of mine is done")
            why = "" if mg.get("in_work") else self.held_by(ph)  # a round cut off by a restart is redone first
            if why:
                self.wait_held(why)
                continue
            self.release_held()
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

    # ---- the auditor: every `audit_every` rounds (and once when the run ends; never with 0) it reads the digest of how the agents
    # talked and updates its findings file. It is outside the team: nothing it writes reaches the agents.
    @property
    def auditor(self) -> Optional[AgentConfig]:
        a = self.mon.by_role("auditor")
        return a[0] if a else None

    def audit_due(self, final: bool = False) -> None:
        """Start an audit when `audit_every` rounds have passed since the last one. It runs in a thread of its own
        while the rounds go on (it only reads the run's files and writes its findings file); its result is taken in
        on this thread at a later call. The final audit, when the run ends, is waited for, after any still running."""
        self.audit_collect(wait=final)
        if getattr(self, "_audit_thread", None) is not None:
            return  # one audit at a time: the next starts once this one is taken in
        a, c = self.auditor, self.mon.controller
        if a is None or self.should_stop() or c.audit_every <= 0:  # 0: no automatic audits, the final one included
            return
        au = self.mg.setdefault("audit", {})
        done = int(self.st.get("round", 0)) - (1 if self.mg.get("in_work") else 0)
        through = int(au.get("through", 0))
        span = c.audit_every or 10
        if done <= through or (not final and done - through < c.audit_every):
            return
        lo = max(through + 1, done - span + 1)  # at most one span: a long run audited for the first time
        self.rec.progress(f"🔎 the auditor reads rounds {lo}–{done}")
        self.log(f"  🔎 audit of rounds {lo}–{done}")
        # shown on the board while the auditor works (it is not a round's turn, so it has no row in the turn list)
        self.st["auditing"] = {"lo": lo, "hi": done, "since": now_iso(), "agent": a.name}
        self.rec.event("audit_started", rounds=[lo, done], agent=a.name)
        self.rec.save()
        box: Dict[str, Any] = {"lo": lo, "hi": done}
        prompts = au.get("prompts")

        def work() -> None:
            try:
                box["res"] = _audit.run_audit(self.mon, a, self.rec.run_dir, lo, done, prompts,
                                              should_stop=self.should_stop, log=self.log)
            except Exception as e:  # an audit never stops the run
                box["res"] = {"status": "error", "error": f"{type(e).__name__}: {e}", "points": ""}

        th = threading.Thread(target=work, name="audit", daemon=True)
        self._audit_thread, self._audit_box = th, box
        th.start()
        if final:
            self.audit_collect(wait=True)

    def audit_collect(self, wait: bool = False) -> None:
        """Take in a finished audit (or, with `wait`, the running one once it ends): its record, cost and progress."""
        th = getattr(self, "_audit_thread", None)
        if th is None:
            return
        if wait:
            with self.unlocked():
                th.join()
        if th.is_alive():
            return
        box = self._audit_box
        self._audit_thread = self._audit_box = None
        self.st.pop("auditing", None)
        lo, done = box["lo"], box["hi"]
        res = box.get("res") or {"status": "error", "error": "the audit ended without a result", "points": ""}
        if res.get("status") == "stopped":
            self.rec.save()
            return  # audited again after the restart
        au = self.mg.setdefault("audit", {})
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
        if mg.get("discussion_carry"):  # a discussion I closed at the plan gate before sending the plan back
            prompt = prompt.rstrip() + "\n\n" + mg["discussion_carry"] + "\n"
        r = self.say(self.manager, prompt, "plan")
        self.discussion_carried(r)
        ms = parse_milestones(r["output"])
        if not ms:
            r = self.say(self.manager, "I could not find the milestone list in your reply. Please give it again as a "
                                       "numbered list, one milestone per line: \"1. Short title — what has to be true "
                                       "when it is done\".", "plan")
            ms = parse_milestones(r["output"])
        if not ms:
            raise _Failed("the manager's plan has no numbered milestone list")
        text = self.after_manager(r)
        summ = T.bodies(text, "summary")
        mg.update(plan_text=text, plan_turn=r["turn_id"], comment="", plan_summary=summ[-1] if summ else "",
                  milestones=[{**x, "status": "pending", "rounds": 0, "score": None} for x in ms])
        self.rec.progress("**Plan:**\n" + "\n".join(f"{i + 1}. {x['text']}" for i, x in enumerate(ms)))
        mg["phase"] = "plan_gate" if self.mon.controller.approve_plan else "kickoff"
        return None

    def ph_plan_gate(self, mg: Dict[str, Any]) -> Optional[str]:
        d = self.wait_gate({"kind": "plan", "title": "Milestone plan", "text": mg.get("plan_text", ""),
                            "summary": mg.get("plan_summary", ""), "turn": mg.get("plan_turn")})
        disc = self.gate_discussion()
        if disc:  # the manager's next turn (the reworked plan, or the first instruction) carries the discussion
            mg["discussion_carry"] = disc
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
        self.ck_rec(start=True)  # the critic's checklist file for this milestone
        self.fresh_sessions()
        ms.pop("rubric", None)  # set afresh for this milestone (take_rubric, in the turn)
        r = self.say(self.manager, self.mtpl("manager_kickoff", n=mg["ms"] + 1, total=len(mg["milestones"]),
                                             milestone=ms["text"], human_messages=mg.get("discussion_carry") or ""),
                     "kickoff")
        self.after_manager(r, relay=False)
        self.discussion_carried(r)
        if not ms.get("rubric"):
            self.rubric_off(0)
        mg.update(note=self.critic_message(r, "kickoff"), note_at=now_iso(), ms_round=0, stall=0,
                  limit=False, phase="brief", plan_done=None, plan_total=None, step_round=None)
        return None

    def ph_brief(self, mg: Dict[str, Any]) -> Optional[str]:
        v = self.critic
        prompt = self.mtpl("critic_brief", note=mg.get("note", ""), human_messages="", cwd=v.working_dir,
                           instructions=self.mon.controller.instructions if not self.has_session(v) else "")
        req = mg.get("par_req") or {}
        if req.get("status") == "approved" and not self.par:  # B11: approved before the brief (e.g. at kickoff)
            prompt = prompt.rstrip() + "\n\n" + self.mtpl("critic_brief_workers", n=int(req.get("n") or 2)).strip() + "\n"
        r = self.say(v, prompt, "brief")
        self.worker_feedback_for(r["output"])
        mg["phase"] = "work"
        self.critic_asks(r, after="work")
        return None

    def ph_work(self, mg: Dict[str, Any]) -> Optional[str]:
        m, c = self.mon, self.mon.controller
        if int(self.st.get("round", 0)) >= m.max_rounds:
            return "max_rounds_reached"
        redo = bool(mg.get("in_work"))  # a round cut off by a restart is redone before any limit or check-in
        req = mg.get("par_req") or {}
        if not redo and req.get("status") == "gate":  # B11: more workers than the manager may approve: I decide
            mg["phase"] = "workers_gate"
            return None
        if not redo and req.get("status") == "approved":  # B11: the approved workers take the independent steps
            self.par_start(req)
            mg["phase"] = "parallel"
            return None
        if self.par and self.par.get("active"):
            mg["phase"] = "parallel"
            return None
        selected = [w for w in self.workers if
                    (mg.get("feedback") or {}).get(w.name) or self.rec.pending_worker_messages(w.name)]
        if redo:
            selected = [w for w in self.workers if w.name in mg.get("in_work_agents", [a.name for a in self.workers])]
        elif not selected:
            if req.get("status") == "declined" and not req.get("single_assignment_requested"):
                # Declining parallelism does not assign work. Give the critic one turn
                # to explicitly assign the remaining worker before escalating an empty reply.
                req["single_assignment_requested"] = True
                notice = self.mtpl("workers_declined").strip()
                queued = self.st.get("fwd", {}).get(self.critic.name) or []
                mg.update(guidance="" if notice in queued else notice, phase="guidance")
                return None
            self.missing_instructions([w.called for w in self.workers])
            return None
        else:
            mg.pop("instruction_retry", None)
        # milestone_rounds counts rounds without progress: a step of the critic's plan finishing starts it again
        # (without a plan, every round of the milestone counts, as before)
        if not redo and self.stalled() >= c.milestone_rounds:
            mg.update(limit=True, phase="report")
            return None
        if not redo and mg.pop("checkin_due", False):
            mg["phase"] = "checkin"
            return None
        if mg.get("in_work"):  # this round was cut off by a restart: redo it, don't count it twice
            rnd = int(mg["in_work"])
        else:
            rnd = int(self.st.get("round", 0)) + 1
            mg["stall"] = self.stalled() + 1
            mg["ms_round"] = mg.get("ms_round", 0) + 1
        mg["in_work"] = rnd
        mg["in_work_agents"] = [w.name for w in selected]
        summary = self.begin_round(rnd)
        self.cur()["rounds"] = mg["ms_round"]
        items = []
        for w in selected:
            fb = self.for_worker((mg.get("feedback") or {}).get(w.name) or "")
            if not self.has_session(w):
                prompt = self.mtpl("worker_start", task=w.task or "", feedback=fb, human_messages="", cwd=w.working_dir,
                                   decisions="")  # what the work must keep to comes in the critic's instruction
            else:
                prompt = self.mtpl("worker_next", feedback=fb, human_messages="", round=rnd, cwd=w.working_dir)
            items.append((w, prompt, "work"))
        extra = {"milestone": mg.get("ms", 0), "milestone_round": mg["ms_round"]}
        if c.job_wait > 0:  # to find jobs the worker leaves running; a redone round compares with the first snapshot
            if redo and mg.get("procs_before") is not None:
                self._procs = {int(k): tuple(v) for k, v in mg["procs_before"].items()}
            else:
                self._procs = _jobs.snapshot()
                mg["procs_before"] = {str(k): list(v) for k, v in self._procs.items()}
        else:
            self._procs = {}
        self.rec.save()
        # a redone round keeps the workers' turns that already finished; only the unfinished ones run again
        done = self.rec.finished_turns(rnd, "work") if redo else {}
        for w, _p, _pur in items:
            if w.name in done:
                self.after_state_turn(w, done[w.name])  # idempotent: writes the same state again
                self.rec.progress(f"↻ {w.name}'s turn `{done[w.name]['turn_id']}` had finished before the restart; kept")
        ran = {r["agent"]: r for r in self._stage([it for it in items if it[0].name not in done], rnd, extra)}
        recs = [done.get(w.name) or ran[w.name] for w, _p, _pur in items]
        if self.should_stop() or any(r["status"] == "stopped" for r in recs):
            raise StopRequested()  # keep the round open; `orch run --resume` redoes it in the same session
        mg.pop("in_work", None)
        mg.pop("in_work_agents", None)
        mg.pop("procs_before", None)
        for r in recs:
            block = self.ask_block(r.get("output") or "", "worker")
            if block:
                self.new_question(m.agent(r["agent"]), self.critic.name, block, r["turn_id"])
        return self.after_work(recs, summary)

    def missing_instructions(self, missing: List[str]) -> None:
        """Ask the manager once for direction; another empty instruction waits for the human."""
        mg = self.mg
        retried = bool(mg.get("instruction_retry"))
        text = (f"No next-step instruction was supplied for {', '.join(missing)}. "
                "No further worker turn has been started for them. An unfinished plan or FAIL verdict alone "
                "does not assign work. Confirm whether execution should remain paused or give the critic "
                "a concrete authorized next step, resolving any closure, scope or deadline conflict.")
        if retried:
            text += " The manager's continuation was followed by another empty instruction; your direction is needed."
        mg.update(instruction_retry=True,
                  wait={"who": "missing worker instructions", "text": text, "proposal": text},
                  phase="wait" if retried else "pause")
        self.rec.event("missing_worker_instruction", workers=missing, awaiting_human=retried)
        self.rec.progress("No worker instruction: " + ("waiting for your direction" if retried
                                                       else "asking the manager whether to pause"))

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
                a = m.agent(r["agent"])
                self.rec.progress(f"{r['agent']} ran past its safety limit ({a.turn_limit(self.watched(a)):.0f}s); "
                                  "the turn was stopped; continuing")
                self.rec.event("safety_timeout", agent=r["agent"], turn=r.get("turn_id"),
                               limit=a.turn_limit(self.watched(a)))
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
            self._carry_jobs = {}
        elif c.job_wait > 0:
            outs = "\n".join(r.get("output") or "" for r in recs)
            skip = {int(a["pid"]) for a, _b in T.blocks(outs, "no-wait") if str(a.get("pid", "")).isdigit()}
            found = [j for j in _jobs.detect(getattr(self, "_procs", {}) or {},
                                             {self.mon.agent(r["agent"]).working_dir or self.mon.working_dir for r in recs},
                                             c.job_ignore,
                                             tags=[_jobs.tag_for(self.rec.run_dir.name, r["agent"]) for r in recs])
                     if j["pid"] not in skip]  # orch finds them itself
            self._procs = {}
            for name, j in (getattr(self, "_carry_jobs", None) or {}).items():  # still running from the last wait
                for pid in j["pids"]:
                    cmd = (j.get("cmds") or {}).get(str(pid), "")
                    if pid not in skip and _jobs.alive(pid, cmd) and not any(pid == x["pid"] for x in found):
                        found.append({"pid": pid, "command": cmd, "agent": name})
            self._carry_jobs = {}
            for j in found:
                if any(j["pid"] in x["pids"] for x in jobs.values()):
                    continue
                for r in recs[:1] if len(recs) == 1 else [x for x in recs if x["agent"] == j.get("agent", x["agent"])]:
                    x = jobs.setdefault(r["agent"], {"pids": [], "lines": [], "cmds": {}})
                    x["pids"].append(j["pid"])
                    x["lines"].append(f"pid {j['pid']}: {_clip(str(j['command']), 160)}")
                    x["cmds"][str(j["pid"])] = j["command"]
        carried = getattr(self, "_carry_since", None)
        self._carry_since = None
        mg.pop("running_jobs", None)
        if jobs:  # never waited on by itself: the critic is told in its review and decides (<wait-job/>)
            mg["running_jobs"] = {"agents": jobs, "since": (carried or {}).get("since") or now_iso(),
                                  "since_ts": (carried or {}).get("since_ts") or time.time()}
            pids = ", ".join(str(x) for j in jobs.values() for x in j["pids"])
            self.rec.progress(f"⚙ still running after the worker's turn: pid {pids}; {self.critic.name} decides "
                              "whether the review waits for it")
            for a, j in jobs.items():  # a small job line on the board
                self.rec.event("job_running", agent=a, jobs="; ".join(j["lines"])[:300])
        if any(self.rec.pending_worker_messages(w.name) for w in self.workers):
            # A check-in can finish after the worker's backend has already closed its turn. Deliver
            # those instructions before reviewing the old result.
            # ph_work selects only the recipients with queued instructions.
            mg["phase"] = "work"
            self.end_round(summary, "additional instructions pending → worker")
            return None
        mg["phase"] = "review"
        return None

    def ph_job(self, mg: Dict[str, Any]) -> Optional[str]:
        """The worker finished its turn but is waiting on its own background job: don't spend a critic review on
        work that isn't done, and no rounds on asking whether it still runs. While it runs, progress checks watch it
        (B5); when it ends, the worker picks up again. A message for the worker gives it a turn now, and the job is
        watched again afterwards. `job_wait` caps the wait only when checks are off. I can skip ahead."""
        c, job = self.mon.controller, mg.get("job") or {}
        agents = job.get("agents") or {}
        for name in list(agents):  # a wait saved by an older orch may hold processes that were never jobs
            j = agents[name]
            ws = [self.mon.agent(name).working_dir or self.mon.working_dir]
            keep = {x for x in _jobs.inside(j["pids"], ws)
                    if not _jobs.is_tool((j.get("cmds") or {}).get(str(x), ""))}  # a shell or editor helper is never a job
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
        since = float(job.get("since_ts") or time.time())
        watched = []
        for name, j in agents.items():
            w = self.mon.agent(name)
            if self.start_watch(w, "job", jobs=j["lines"], since=since):
                watched.append(w)
        nap = min([2.0] + [max(0.05, x["every"] / 4) for x in (getattr(self, "_watches", None) or {}).values()])
        try:
            while True:
                if self.should_stop():
                    raise StopRequested()
                self.check_control()
                self.urgent_checkin()
                self.progress_tick()  # B5: the monitor looks at the job every few minutes; no rounds spent
                self.activate("wait")
                if any(self.message_for_job_turn(n) for n in agents):
                    why = "message"  # a message for the worker: its turn comes now, the job may keep running
                    break
                if mg.pop("job_release", False):
                    why = "released"  # explicit "Review now" on the board
                    break
                if not any(_jobs.alive(x, (j.get("cmds") or {}).get(str(x), "")) for j in agents.values()
                           for x in j["pids"]):
                    break
                if not watched and time.time() - since > c.job_wait:  # with checks off, job_wait caps the wait
                    why = "timeout"
                    break
                time.sleep(nap)
        finally:
            for w in watched:
                self.end_watch(w)
            self.rec.set(status="running")
        mg.pop("job", None)
        self.rec.event("job_end", agent=next(iter(agents), ""), why=why)
        if why == "released":
            mg.pop("work_earlier", None)
            self.rec.progress(f"▶ review now, without waiting for the job")
            mg["phase"] = "review"
            return None
        rnd = int(self.st.get("round", 0))
        summary = mg.get("summary") or {"round": rnd, "turns": []}
        hours = (time.time() - float(job.get("since_ts") or time.time())) / 3600
        items = []
        self._carry_jobs, self._carry_since = {}, None
        if why == "message":  # the job runs on: after the worker's turn it is watched again, not reviewed half done
            self._carry_jobs = {n: {"pids": list(j["pids"]), "cmds": dict(j.get("cmds") or {})} for n, j in agents.items()}
            self._carry_since = {"since": job.get("since"), "since_ts": job.get("since_ts")}
        for name, j in agents.items():
            w = self.mon.agent(name)
            if why == "message":
                items.append((w, self.mtpl("worker_job_message", jobs="; ".join(j["lines"]), human_messages=""), "work"))
                continue
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
            # Keep draining the worker's backend while the main thread handles a critic check-in.
            # Finish both conversations before advancing the phase or starting another worker turn.
            watch = purpose == "work" and a.role == "worker"
            if watch:
                self.start_watch(a, "turn")
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(self.turn, a, p, rnd, purpose, extra=extra)
                    while not wait([future], timeout=0.2).done:
                        self.urgent_checkin()
                        self.progress_tick()  # B5: quick checks while the worker works; no rounds spent on them
                        self.activate("stage")  # the manager and critic answer their messages while the worker works
                    out.append(future.result())
            finally:
                if watch:
                    self.end_watch(a)
            self.after_state_turn(a, out[-1])
        return out

    def urgent_checkin(self) -> None:
        """Answer an urgent critic note alongside work, without changing its phase or job wait."""
        if self.should_stop() or not any(n["to"] and self.critic.name in n["to"]
                                         for n in self.rec.urgent_notes()):
            return
        self.rec.progress(f"⚡ {self.critic.name}: urgent check-in; the worker continues")
        self.rec.event("urgent", agent=self.critic.name, how="checkin")
        try:
            r = self.say(self.critic, self.mtpl("critic_urgent_checkin"), "checkin")
            if r["status"] != "ok":
                raise _Failed(f"{self.critic.name} check-in ended with status {r['status']}")
            # say() already queues <message to="you"> for the manager. Plain text and questions
            # from this dedicated check-in also go up, never into worker feedback or verdicts.
            update = "\n\n".join(x for x in [self.upward(r["output"]), self.ask_block(r["output"], "verifier")] if x)
            a = self.say(self.manager, self.mtpl("manager_checkin", update=update, human_messages=""), "ack")
            if a["status"] == "ok":
                self.after_manager(a, relay=False)  # its messages are routed after the turn (route_reply)
        except _Failed as e:
            self.rec.event("checkin_error", error=str(e))
            self.rec.progress(f"Urgent check-in did not finish: {e}; the worker continues")
        self.rec.save()

    # ------------------------------------------------------------------ B5: progress checks
    # While a worker's turn or its background job runs, a quick check every `check_every` minutes (the worker's
    # agent.yaml, else the monitor's; default 20; 0 = off). The monitor (role: monitor, a small cheap model) looks at
    # logs and status, with the current step's items from the critic's checklist file, and reports evidence only.
    # Without a monitor, the critic's side session does it (check_session: own runs it in the critic's own session).
    # The report goes to the critic: held for its next turn when all is fine, waking it now when the run is quiet,
    # off track or something it relies on has stopped. The manager gets a status line at its own, longer interval
    # (no turn of its own). Each check updates the worker's status line on the board.

    def clock(self) -> float:
        return time.time()

    @property
    def monitor_agent(self) -> Optional[AgentConfig]:
        m = self.mon.by_role("monitor")
        return m[0] if m else None

    def check_every(self, w: AgentConfig) -> float:
        """Minutes between checks of this worker's turns and jobs (0 = off)."""
        for a in (w, self.monitor_agent):
            if a is not None and a.check_every is not None:
                return float(a.check_every)
        return CHECK_EVERY

    def watched(self, agent: AgentConfig) -> bool:
        return self.mon.controller.mode == "managed" and agent.role == "worker" and self.check_every(agent) > 0

    def refresh_agent(self, agent: AgentConfig) -> None:
        if agent.role == "monitor" and agent.name not in {a.name for a in self.mon.agents}:
            return  # the critic's side session for checks: built from the critic's settings at each check
        base = self.clones.get(agent.name)
        if base:  # B11: a copy takes the hot settings of the agent it copies, re-read from the config
            orig = next((a for a in self.mon.agents if a.name == base), None)
            if orig is not None:
                super().refresh_agent(orig)
                for f in HOT_AGENT_FIELDS:
                    setattr(agent, f, getattr(orig, f))
            return
        super().refresh_agent(agent)

    def checker(self, w: Optional[AgentConfig] = None) -> Tuple[AgentConfig, str]:
        """Who runs a check and where: (agent, "own" | "side"). The monitor (own session unless its check_session
        says side); without one, the critic: a side session by default (its own conversation is left alone).
        B11: each extra worker of a parallel section has its own copy of the monitor (or its own side session)."""
        slot = self.par_slot(w.name) if w is not None else None
        m = self.monitor_agent
        if m is not None:
            mine = next((a for a in self.mon.agents if slot and a.name == slot.get("monitor")), m)
            return mine, mine.check_session or "own"
        c = self.critic
        if (c.check_session or "side") == "own" and not (slot and w.name in self.clones):
            return c, "own"
        side = dataclasses.replace(
            c, name=f"{c.name}-check" + (f"-{w.name}" if slot and w.name in self.clones else ""), role="monitor", display_name="", description="", task="", system_prompt="",
            system_prompt_path=str(PROMPTS_DIR / "monitor.md"), memory_files=[], state_file="", fresh_session="",
            protocol_path=str(PROMPTS_DIR / "protocol" / "monitor.md") if self.mon.controller.protocol else "")
        return side, "side"

    def start_watch(self, w: AgentConfig, kind: str, jobs: Optional[List[str]] = None,
                    since: Optional[float] = None) -> bool:
        every = self.check_every(w) * 60.0
        if every <= 0:
            return False
        now = self.clock()
        watches = self.__dict__.setdefault("_watches", {})
        watches[w.name] = {"agent": w.name, "kind": kind, "jobs": list(jobs or []), "since": since or now,
                           "every": every, "next": now + every, "checks": 0}
        if getattr(self, "_mgr_next", None) is None:
            me = self.manager.check_every
            me = MANAGER_CHECK_EVERY if me is None else float(me)
            self._mgr_next = now + me * 60.0 if me > 0 else None
        self.status_line(w, watches[w.name], None)
        return True

    def end_watch(self, w: AgentConfig) -> None:
        (getattr(self, "_watches", None) or {}).pop(w.name, None)
        if not getattr(self, "_watches", None):
            self._mgr_next = None
        lines = self.st.get("status_lines") or {}
        if lines.pop(w.name, None) is not None:
            self.rec.save()

    def progress_tick(self, run: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
        """Run the checks that are due (called from the loops that wait on a worker's turn or job). `run` starts one
        check (default: here and now; B11: on the checks' own threads, par_checks)."""
        if self.should_stop():
            return
        now = self.clock()
        for name, watch in list((getattr(self, "_watches", None) or {}).items()):
            if now >= watch["next"]:
                watch["next"] = now + watch["every"]
                (run or self.quick_check)(watch)
        if getattr(self, "_watches", None) and getattr(self, "_mgr_next", None) and self.clock() >= self._mgr_next:
            me = self.manager.check_every
            self._mgr_next = self.clock() + (MANAGER_CHECK_EVERY if me is None else float(me)) * 60.0
            self.manager_status()

    def what_runs(self, w: AgentConfig, watch: Dict[str, Any]) -> str:
        if watch["kind"] == "job":
            return f"{w.called}'s background job ({'; '.join(watch['jobs']) or 'a job'})"
        return f"{w.called}'s current turn"

    def step_items(self, w: Optional[AgentConfig] = None) -> Tuple[str, str]:
        """(label for the status line, e.g. "step 3"; the current step's items for the check prompt). B11: in a
        parallel section, the step that worker was given."""
        try:
            ck = self.checklist_board()
        except Exception:
            return "", ""
        slot = self.par_slot(w.name) if w is not None else None
        if slot and slot.get("step"):
            s, title = PW.step_title(ck, slot["step"])
            items = "\n".join(f"- [{'x' if d else ' '}] {t}"
                              for d, t in list(ck.get("standing") or []) + list((s or {}).get("items") or []))
            return f"step {slot['step']}", (f"What to look for, {title}:\n{items}" if items else "")
        steps = ck.get("steps") or []
        i = B.current_index(ck)
        if i < 0 or not steps:
            return "", ""
        s = steps[i]
        label = "" if s.get("implicit") else f"step {i + 1}"
        items = B.current_list(ck)
        return label, (f"What to look for, {s['title']}:\n{items}" if items else "")

    def activity_text(self, w: AgentConfig, watch: Dict[str, Any], now: float) -> Tuple[str, str, Optional[float]]:
        """(the activity paragraph of the check prompt, the short phrase for the status line, seconds quiet)."""
        if watch["kind"] == "job":
            alive = []
            for ln in watch["jobs"]:
                m = re.match(r"pid (\d+)", ln)
                if m:
                    alive.append(f"pid {m.group(1)} {'running' if _jobs.alive(int(m.group(1))) else 'gone'}")
            state = ", ".join(alive)
            return (f"Process check: {state}." if state else ""), ("job " + (state or "running")), None
        quiet, tail = P.activity(self.rec.run_dir, w.name, watch["since"], now)
        if quiet is None:
            q = now - watch["since"]
            return (f"Nothing written in the turn's output since it started {P.minutes(q)} min ago.",
                    f"no output for {P.minutes(q)} min", q)
        lines = "\n".join(f"- {x}" for x in tail)
        head = f"Latest activity in the turn's output, {P.minutes(quiet)} min ago:"
        phrase = "log active" if quiet < watch["every"] else f"log quiet for {P.minutes(quiet)} min"
        return f"{head}\n{lines}", phrase, quiet

    def status_line(self, w: AgentConfig, watch: Dict[str, Any], check: Optional[Dict[str, Any]]) -> None:
        """The board's live line for a working agent: "Sam: step 3, running 14 min — log active"."""
        now = self.clock()
        label, _items = self.step_items(w)
        mins = P.minutes(now - watch["since"])
        summary = (check or {}).get("summary") or "no check yet"
        doing = "job running" if watch["kind"] == "job" else "running"
        text = f"{w.called}: " + (f"{label}, " if label else "") + f"{doing} {mins} min — {summary}"
        self.st.setdefault("status_lines", {})[w.name] = {
            "agent": w.name, "kind": watch["kind"], "step": label, "since": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(watch["since"])), "since_ts": watch["since"], "summary": summary,
            "status": (check or {}).get("status") or "", "checked_at": now_iso() if check else "", "text": text,
            "checks": watch.get("checks", 0)}
        self.rec.save()

    def quick_check(self, watch: Dict[str, Any]) -> None:
        """One quick check of a running worker turn or job: the checker looks and reports evidence; the report goes
        to the critic (problem: now; all fine: at its next turn); the status line is updated."""
        w = self.mon.agent(watch["agent"])
        checker, where = self.checker(w)
        if self.rec.agent_busy(checker.name):  # e.g. a board chat with it: try again shortly
            watch["next"] = self.clock() + min(60.0, watch["every"])
            return
        now = self.clock()
        mins = P.minutes(now - watch["since"])
        what = self.what_runs(w, watch)
        act, phrase, quiet = self.activity_text(w, watch, now)
        _label, step = self.step_items(w)
        own_critic = checker.role == "verifier"
        tpl = "critic_quick_check" if own_critic else "monitor_check"
        prompt = self.mtpl(tpl, what=what, minutes=mins, cwd=w.working_dir or self.mon.working_dir, activity=act,
                           step=step or "(No step items to look for yet.)").strip()
        if where == "side":  # a fresh session for each check
            self.st["sessions"].pop(checker.name, None)
        watch["checks"] = watch.get("checks", 0) + 1
        self.rec.event("progress_check", agent=w.name, checker=checker.name, where=where, watch=watch["kind"],
                       minutes=mins)
        try:
            r = self.turn(checker, prompt, int(self.st.get("round", 0)), "check",
                          extra={"watching": w.name, "watch_kind": watch["kind"]})
        except StopRequested:
            raise
        except Exception as e:  # a check that fails never stops the work
            self.rec.event("check_error", agent=w.name, checker=checker.name, error=str(e))
            self.rec.progress(f"⚠ progress check of {w.called} did not finish: {e}")
            return
        if r.get("status") != "ok":
            self.rec.event("check_error", agent=w.name, checker=checker.name, error=r.get("status"))
            self.rec.progress(f"⚠ progress check of {w.called} ended with {r.get('status')}; the work goes on")
            return
        out = r.get("output") or ""
        status, tagged = P.parse_status(out)
        if own_critic and not tagged:  # the critic's own session: a message to the worker marks a problem
            status = P.QUIET if T.bodies(out, "message") else P.OK
        if quiet is not None and quiet >= watch["every"] and status == P.OK:
            status = P.QUIET  # orch's own evidence: nothing written in the turn for a whole interval
        report, flags = P.clean_report(T.outside(out) if own_critic else out, w.name, self.rec.run_dir)
        for f in flags:
            self.rec.event("check_flag", agent=checker.name, turn=r.get("turn_id"), problem=f)
        if flags:
            self.rec.progress(f"⚠ progress check `{r.get('turn_id')}`: removed from the report before the critic "
                              f"reads it: {'; '.join(flags[:3])}")
        problem = P.is_problem(status)
        summary = phrase + ("" if status == P.OK else f"; check: {status}")
        if (getattr(self, "_watches", None) or {}).get(w.name) is watch:  # not a turn or job that ended meanwhile
            self.status_line(w, watch, {"status": status, "summary": summary})
        slot = self.par_slot(w.name)
        if slot is not None:  # B11: the lane and the work board show each worker's latest check
            first = next((ln.strip() for ln in report.splitlines() if ln.strip()), "")
            slot["check"] = f"{status}: {_clip(first, 120)}" if first else status
            self.write_work_board()
        self.rec.progress(f"🔎 progress check of {w.called} ({mins} min): {status}"
                          + (" → the critic is told now" if problem and not own_critic else ""))
        if not own_critic and report:
            self.report_check(checker, w, watch, report, status, problem, r.get("turn_id") or "", mins)
        self.rec.save()

    def report_check(self, checker: AgentConfig, w: AgentConfig, watch: Dict[str, Any], report: str, status: str,
                     problem: bool, turn_id: str, mins: int) -> None:
        """The report to the critic, as an observation about the work. A newer report replaces one still waiting.
        All fine: held for the critic's next turn. A problem: it starts a short critic turn now (activate)."""
        L, critic = self.ledger, self.critic
        text = self.mtpl("progress_check_report", minutes=mins, what=self.what_runs(w, watch), report=report).strip()
        for old in L.rows:  # B11: only an older report on the same worker
            if old.get("kind") == "progress_check" and old["to"] == critic.name and old["status"] in ("sent", "held") \
                    and old.get("worker", w.name) == w.name:
                q = (self.st.get("fwd") or {}).get(critic.name) or []
                if old["text"] in q:
                    q.remove(old["text"])
                L.mark(old, "superseded", reason="replaced by a newer progress check", by=turn_id)
        m = L.add(checker.name, critic.name, report, text, source=turn_id, via="check", purpose="check",
                  kind="progress_check", check_status=status, activate=bool(problem), carrier={"fwd": True},
                  worker=w.name)
        if not problem:
            L.mark(m, "held", reason=RT.NEXT_TURN, next_turn=True)
        self.forward(critic, text)
        self.rec.event("check_report", agent=w.name, checker=checker.name, status=status, msg=m["id"],
                       wakes=bool(problem))

    def manager_status(self) -> None:
        """The manager's light status look: one line from the latest checks and the plan, waiting for its next turn
        (it starts no turn; only the newest one waits)."""
        lines = [x.get("text") or "" for x in (self.st.get("status_lines") or {}).values() if x.get("text")]
        plan = self.plan_line()
        line = "; ".join(lines + ([f"plan: {plan}"] if plan else []))
        if not line:
            return
        fwd = self.st.setdefault("fwd", {}).setdefault(self.manager.name, [])
        fwd[:] = [x for x in fwd if not x.startswith(PROGRESS_STATUS)]
        fwd.append(f"{PROGRESS_STATUS} " + self.mtpl("manager_progress_status", line=line).strip())
        self.rec.event("manager_check", text=line)
        self.rec.progress(f"ⓘ status for the manager: {line}")
        self.rec.save()

    # ------------------------------------------------------------------ B11: parallel workers (orch/parallel.py)
    # The critic (or the manager) proposes several workers for independent steps with <workers n=… steps=…>; the
    # manager approves within the budget (max_workers, rounds left, parallel_turns); above it I decide at a gate (in
    # night mode the manager's number stands up to workers_hard_cap). Approved, the worker gets copies (worker-2..N:
    # same settings, own session, lane, job tag, state file and monitor). The critic hands out steps with
    # <to worker="ID" step="N" out="folder">; their turns run at the same time; each finished step is reviewed on its
    # own, one critic turn at a time. orch keeps the work board (work-m<N>.md in the critic's folder) up to date.
    #
    # Concurrency: the main thread holds the recorder's lock for the whole section and lets it go only while it
    # waits; a worker's thread holds it for everything in its turn except the backend's run itself (run_backend).
    # So all run state is changed by one thread at a time, while the agents themselves work at the same time.

    @contextlib.contextmanager
    def unlocked(self) -> Any:
        """Let go of the run state for a wait (all levels this thread holds), and take it back afterwards."""
        lock, n = self.rec.lock, 0
        while True:
            try:
                lock.release()
            except RuntimeError:
                break
            n += 1
        try:
            yield
        finally:
            for _ in range(n):
                lock.acquire()

    def run_backend(self, backend: Any, req: TurnRequest, cancel: Any) -> TurnResult:
        with self.unlocked():
            return backend.run(req, cancel)

    def enter_agent_lock(self, lock: Any) -> None:
        with self.unlocked():
            lock.__enter__()

    def pause(self, secs: float) -> None:
        with self.unlocked():
            super().pause(secs)

    @property
    def par(self) -> Dict[str, Any]:
        """The running parallel section ({} when none)."""
        p = self.mg.get("par") or {}
        return p if p.get("active") else {}

    def par_slot(self, name: str) -> Optional[Dict[str, Any]]:
        return (self.par.get("slots") or {}).get(name)

    def work_board_path(self) -> Path:
        return self.agent_dir(self.critic) / f"work-m{self.mg.get('ms', 0) or 1}.md"

    def write_work_board(self) -> None:
        par = self.mg.get("par") or {}
        if not par.get("slots"):
            return
        p = self.work_board_path()
        try:
            ck = self.checklist_board()
        except Exception:
            ck = {}
        try:
            old = p.read_text(encoding="utf-8") if p.exists() else ""
            called = {n: self.mon.agent(n).called for n in par["slots"] if self.has_agent(n)}
            body = PW.render_board(par, called, ck, old)
            if body != old:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")
        except OSError as e:
            self.rec.event("work_board_error", error=str(e))

    def has_agent(self, name: str) -> bool:
        return any(a.name == name for a in self.mon.agents)

    def take_workers(self, agent: AgentConfig, r: Dict[str, Any]) -> None:
        """A <workers> tag: the critic's proposal goes to the manager as a note that wakes it; the manager's is its
        decision (or its own proposal), checked against the budget."""
        if r.get("status") != "ok":
            return
        tag = PW.workers_tag(r.get("output") or "")
        if not tag:
            return
        mg = self.mg
        if self.par:
            self.rec.progress(f"ⓘ {agent.called} asked for {tag['n']} people while several already work: not acted on")
            return
        if len(self.workers) != 1:
            self.rec.progress(f"ⓘ {agent.called} proposed several workers; this run has {len(self.workers)} "
                              "configured workers, so parallel copies are not made")
            return
        req = mg.get("par_req") or {}
        if agent.role == "verifier":
            mg["par_req"] = {"status": "proposed", "n": tag["n"], "steps": tag["steps"], "reason": tag["reason"],
                             "by": agent.name, "turn": r.get("turn_id")}
            self.rec.event("workers_proposed", agent=agent.name, n=tag["n"], steps=tag["steps"])
            self.rec.progress(f"👥 {agent.called} proposes {tag['n']} workers at once"
                              + (f" for steps {', '.join(map(str, tag['steps']))}" if tag["steps"] else ""))
            steps = ", ".join(map(str, tag["steps"])) or "(not named)"
            self.deliver(agent, self.manager, tag["reason"] or f"{tag['n']} people at once",
                         self.mtpl("workers_proposal", n=tag["n"], steps=steps, reason=tag["reason"] or "-",
                               max=self.mon.controller.max_workers).strip(),
                         source=r.get("turn_id") or "", purpose=r.get("purpose") or "", wake=True, activate=True)
            return
        proposed = req.get("status") == "proposed"
        steps = (req.get("steps") or tag["steps"]) if proposed else tag["steps"]
        self.decide_workers(tag["n"], steps, by=agent.name, reason=tag["reason"] or req.get("reason", ""),
                            turn=r.get("turn_id") or "", proposed_by=req.get("by") if proposed else agent.name)

    def decide_workers(self, n: int, steps: List[int], by: str, reason: str, turn: str, proposed_by: str) -> str:
        c = self.mon.controller
        left = self.mon.max_rounds - int(self.st.get("round", 0))
        status, k, why = PW.decide(n, steps, c.max_workers, c.workers_hard_cap, left, c.parallel_turns,
                                   bool(self.st.get("night")))
        self.mg["par_req"] = {"status": status, "n": k, "asked": n, "steps": list(steps), "reason": reason,
                              "by": proposed_by, "decided_by": by, "turn": turn, "why": why}
        self.rec.event("workers_decision", status=status, n=k, asked=n, by=by, why=why)
        if status == "declined":
            self.rec.progress(f"👥 one worker stays ({why})")
            if proposed_by == self.critic.name:
                self.forward(self.critic, self.mtpl("workers_declined").strip())
        elif status == "approved":
            self.rec.progress(f"👥 {k} workers approved ({why}); they start with the next round")
        else:
            self.rec.progress(f"👥 {n} workers asked for: {why}; I decide at a gate")
        self.rec.save()
        return status

    def ph_workers_gate(self, mg: Dict[str, Any]) -> Optional[str]:
        req = mg.get("par_req") or {}
        n, c = int(req.get("n") or 2), self.mon.controller
        steps = ", ".join(map(str, req.get("steps") or [])) or "not named"
        text = (f"{n} workers at the same time on steps {steps} (the manager may approve up to {c.max_workers} "
                f"alone; night mode allows up to {c.workers_hard_cap}).\n\nWhy: {req.get('reason') or '-'}\n\n"
                "Approve to start them; send it back to keep one worker.")
        d = self.wait_gate({"kind": "workers", "title": f"{n} workers at once", "text": text,
                            "milestone": mg.get("ms", 0)})
        if d["decision"] == "approve":
            k = min(n, c.workers_hard_cap) if d.get("night") else n
            req.update(status="approved", n=k, decided_by="night mode" if d.get("night") else "me")
            self.rec.progress(f"👥 {k} workers approved at the gate")
        else:
            req.update(status="declined", n=1, decided_by="me")
            note = d.get("message") or ""
            self.forward(self.manager, f"About {n} workers at once: I keep one worker." + (f" {note}" if note else ""))
            if req.get("by") == self.critic.name:
                self.forward(self.critic, self.mtpl("workers_declined").strip())
        mg["par_req"] = req
        mg["phase"] = "work"
        return None

    # ---- the copies

    def par_clone(self, base: AgentConfig, k: int, wt: Optional[Dict[str, str]]) -> AgentConfig:
        name = f"{base.name}-{k}"
        sf = ""
        if base.state_file:
            p = Path(base.state_file)
            sf = str(p.parent.parent / name / p.name)
        # without a name of its own a copy is called by its id ("writer-2"), not "the writer-2" (AgentConfig.called)
        a = dataclasses.replace(base, name=name, display_name=f"{base.display_name}-{k}" if base.display_name else name,
                                state_file=sf, prompt_vars=dict(base.prompt_vars or {}),
                                working_dir=wt["cwd"] if wt else base.working_dir)
        self.mon.agents.append(a)
        self.clones[name] = base.name
        return a

    def par_monitor(self, w: AgentConfig, k: int) -> str:
        m = self.monitor_agent
        if m is None:
            return ""
        name = f"{m.name}-{k}"
        if not self.has_agent(name):
            sf = str(Path(m.state_file).parent.parent / name / Path(m.state_file).name) if m.state_file else ""
            a = dataclasses.replace(m, name=name, display_name=f"{m.display_name}-{k}" if m.display_name else name,
                                    state_file=sf, prompt_vars={**(m.prompt_vars or {}), "worker": w.called})
            self.mon.agents.append(a)
            self.clones[name] = m.name
        return name

    def par_agents_extra(self) -> None:
        """The copies, for the board (kept after the section ends: its turns stay in their lanes)."""
        old = [x for x in self.st.get("agents_extra") or [] if x.get("name") not in self.clones]
        self.st["agents_extra"] = old + [{"name": a.name, "role": a.role, "display_name": a.display_name,
                                    "copy_of": self.clones[a.name], "backend": a.backend,
                                    "model": (a.backend_options or {}).get("model")}
                                   for a in self.mon.agents if a.name in self.clones]

    def par_start(self, req: Dict[str, Any]) -> None:
        c, mg = self.mon.controller, self.mg
        base = self.workers[0]
        n = max(2, int(req.get("n") or 2))
        steps = [int(x) for x in req.get("steps") or []]
        req["status"] = "started"
        par = {"active": True, "n": n, "steps": steps, "queue": list(steps), "reason": req.get("reason", ""),
               "slots": {}, "reviews": [], "turns": 0, "worktrees": False, "ms": mg.get("ms", 0),
               "assign_due": True, "started_at": now_iso()}
        mg["par"] = par
        warned = False
        for k in range(1, n + 1):
            wt = None
            if k > 1 and c.worker_worktrees:
                wt = PW.worktree_add(base.working_dir or self.mon.working_dir, f"{base.name}-{k}", self.rec.run_dir.name)
                if wt is None and not warned:
                    warned = True
                    self.rec.progress("⚠ worker_worktrees is on but the workspace is not a git repository (or git "
                                      "refused): every worker works in the same folder")
            w = base if k == 1 else self.par_clone(base, k, wt)
            mon = (self.monitor_agent.name if self.monitor_agent else "") if k == 1 else self.par_monitor(w, k)
            par["slots"][w.name] = {"status": PW.IDLE, "step": None, "out": "", "turns": 0, "step_turns": 0,
                                    "done_steps": [], "workspace": w.working_dir or self.mon.working_dir,
                                    "monitor": mon, "k": k}
            if wt:
                par["worktrees"] = True
                par["slots"][w.name].update(branch=wt["branch"], worktree=wt)
        mg["feedback"] = {}  # the critic hands out the steps afresh
        for m in self.ledger.waiting(base.name):  # what the phase was going to carry to the one worker is dropped
            if "phase" in (m.get("carrier") or {}):
                self.ledger.mark(m, "superseded", reason="the steps are handed out afresh to several people")
        self.par_agents_extra()
        self.rec.event("parallel_start", n=n, steps=steps, workers=list(par["slots"]))
        self.rec.progress(f"👥 **{n} workers at once:** {', '.join(par['slots'])}"
                          + (f" on steps {', '.join(map(str, steps))}" if steps else ""))
        self.write_work_board()
        self.rec.save()

    def par_restore(self) -> None:
        """After a restart in the middle of a parallel section: its copies come back; a turn that was cut off runs
        again (with the prompt it had)."""
        par = self.mg.get("par") or {}
        if self.mg.get("phase") == "parallel" and not par:
            # Older failure cleanup archived the section without changing its phase. Only an untouched
            # assignment can be recovered from that archive: ended worktrees or completed work cannot be replayed.
            last = self.mg.get("par_last") or {}
            slots = last.get("slots") or {}
            if (slots and last.get("ms") == self.mg.get("ms") and last.get("turns") == 0
                    and not last.get("worktrees") and not last.get("reviews")
                    and all(s.get("status") == PW.IDLE and s.get("turns") == 0 and s.get("step") is None
                            and not s.get("worktree") and not s.get("done_steps") for s in slots.values())):
                import copy
                par = copy.deepcopy(last)
                par.update(active=True, assign_due=True)
                par.pop("ended_at", None)
                self.mg["par"] = par
                self.rec.event("parallel_recovered", reason="unassigned section archived by older failure cleanup")
                self.rec.save()
            else:
                raise _Failed("Cannot resume parallel work: active section state is missing and its archive "
                              "cannot safely restore an unstarted assignment; inspect the saved run state.")
        if not par.get("active"):
            return
        base = self.workers[0]
        for name, s in par.get("slots", {}).items():
            if name == base.name or self.has_agent(name):
                continue
            w = self.par_clone(base, int(s.get("k") or 2), s.get("worktree"))
            if s.get("monitor"):
                self.par_monitor(w, int(s.get("k") or 2))
            if s.get("status") == PW.WORKING:
                s["status"] = PW.READY
        if base.name in par.get("slots", {}) and par["slots"][base.name].get("status") == PW.WORKING:
            par["slots"][base.name]["status"] = PW.READY
        for s in par.get("slots", {}).values():
            if s.get("status") == PW.JOB:  # its job is looked at again by the worker
                s.update(status=PW.READY, instr=s.get("instr") or "Please check where your background job stands.")
        self.par_agents_extra()
        self.rec.progress("↻ the parallel section goes on after the restart")

    def par_drop_clones(self) -> None:
        par = self.mg.get("par") or {}
        for s in par.get("slots", {}).values():
            wt = s.get("worktree")
            if wt:
                ok, why = PW.worktree_remove(wt)
                s["worktree_kept"] = not ok
                self.rec.progress(f"🌿 worktree {wt['path']}: " + ("removed" if ok else f"kept ({why})"))
                self.rec.event("worktree_end", path=wt["path"], removed=ok, why=why)
        for name in list(self.clones):
            self.end_watch(self.mon.agent(name)) if self.has_agent(name) else None
        self.mon.agents[:] = [a for a in self.mon.agents if a.name not in self.clones]
        self.clones.clear()

    # ---- the section

    def ph_parallel(self, mg: Dict[str, Any]) -> Optional[str]:
        with self.rec.lock:  # see the note at the top of this section
            return self._par_run(mg)

    def par_can_turn(self) -> bool:
        par, c = self.par, self.mon.controller
        if int(self.st.get("round", 0)) >= self.mon.max_rounds:
            par["max_rounds"] = True
            return False
        if c.parallel_turns and par.get("turns", 0) >= c.parallel_turns:
            par["cap"] = True
            return False
        return True

    def _par_run(self, mg: Dict[str, Any]) -> Optional[str]:
        par = mg["par"]
        futs: Dict[Any, str] = {}
        self.__dict__.setdefault("_par_rounds", {})
        self.__dict__.setdefault("_par_procs", {})
        ex = ThreadPoolExecutor(max_workers=max(1, len(par["slots"])), thread_name_prefix="worker")
        checks = self.par_checks(len(par["slots"]))  # B5: each worker's monitor checks its turn or job, on its own
        ok = False
        try:
            while True:
                if self.should_stop():
                    raise StopRequested()
                self.check_control()
                self.scan_inbox()
                for name, slot in par["slots"].items():
                    if slot["status"] in (PW.IDLE, PW.DONE, PW.BLOCKED) and self.rec.pending_worker_messages(name):
                        slot.update(status=PW.READY, instr="")
                for f in [f for f in list(futs) if f.done()]:
                    self.par_after_turn(futs.pop(f), f.result())
                self.par_poll_jobs()
                if par["reviews"]:  # one review at a time, in the order the steps finished
                    self.par_review(par["reviews"][0])
                    continue
                slots = par["slots"]
                idle = [n for n, s in slots.items() if s["status"] == PW.IDLE]
                if idle and par.get("assign_due"):
                    self.par_assign(idle)
                    continue
                for n in idle:  # not given a step: it stops (no idle turns)
                    slots[n]["status"] = PW.DONE
                running = set(futs.values())
                for n, s in slots.items():
                    if s["status"] == PW.READY and n not in running and self.par_can_turn():
                        if (not s.get("prompt") and not self.for_worker(s.get("instr") or "")
                                and not self.rec.pending_worker_messages(n)):
                            s["status"] = PW.BLOCKED
                            self.rec.progress(f"No next-step instruction for {n}: no further turn starts; "
                                              "the manager decides when the other assigned work has finished")
                            continue
                        futs[self.par_dispatch(ex, n)] = n
                busy = futs or any(s["status"] in (PW.WORKING, PW.JOB, PW.REVIEW) for s in slots.values())
                ready = any(s["status"] == PW.READY for s in slots.values()) and self.par_can_turn()
                if not busy and not ready:
                    break
                self.urgent_checkin()
                self.activate("stage")  # the manager and critic answer their messages meanwhile
                self.rec.save()
                with self.unlocked():
                    if futs:
                        wait(list(futs), timeout=0.2, return_when=FIRST_COMPLETED)
                    else:
                        time.sleep(0.05)
            ok = True
        finally:
            with self.unlocked():
                checks()
                ex.shutdown(wait=True)
            if not ok and not self.should_stop():
                # A failed critic turn must not destroy resumable slots, queues, or worktrees. Workers may
                # have completed while the critic failed: collect them after shutdown so resume reviews their
                # results instead of replaying their work. Failed workers retain their saved session/prompt.
                for future, name in futs.items():
                    try:
                        self.par_after_turn(name, future.result())
                    except Exception as error:  # keep the original failure and collect the other workers too
                        self.rec.event("parallel_worker_failed", agent=name, error=str(error))
                self.rec.save()
        return self.par_finish(mg)

    def par_checks(self, n: int) -> Callable[[], None]:
        """The progress checks of a parallel section, on threads of their own: a long critic turn on the main thread
        (a review of one worker's step) never holds up the checks of the others. A check needs none of the critic's
        turns; its report waits in the critic's queue (report_check) and a problem starts a critic turn once the
        critic is free. Returns the function that stops them (it waits for a check still running)."""
        stop = threading.Event()
        pool = ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="check")
        running: Dict[str, Any] = {}

        def one(watch: Dict[str, Any]) -> None:
            with self.rec.lock:
                try:
                    self.quick_check(watch)
                except StopRequested:
                    pass
                except Exception as e:  # a check never stops the work
                    self.rec.event("check_error", agent=watch.get("agent"), error=f"{type(e).__name__}: {e}")

        def start(watch: Dict[str, Any]) -> None:
            f = running.get(watch["agent"])
            if f is None or f.done():  # one check at a time per worker
                running[watch["agent"]] = pool.submit(one, watch)

        def loop() -> None:
            while not stop.is_set() and not self.should_stop():
                with self.rec.lock:
                    self.progress_tick(start)
                stop.wait(0.05)

        th = threading.Thread(target=loop, name="checks", daemon=True)
        th.start()

        def end() -> None:
            stop.set()
            th.join()
            pool.shutdown(wait=True)
        return end

    def par_dispatch(self, ex: ThreadPoolExecutor, name: str) -> Any:
        mg, par, c = self.mg, self.par, self.mon.controller
        s, w = par["slots"][name], self.mon.agent(name)
        rnd = int(self.st.get("round", 0)) + 1
        summary = self.begin_round(rnd)
        summary["worker"] = name
        self._par_rounds[name] = summary
        mg["ms_round"] = mg.get("ms_round", 0) + 1
        self.cur()["rounds"] = mg["ms_round"]
        prompt = s.get("prompt")
        if not prompt:
            fb = self.for_worker(s.pop("instr", "") or "")
            if not self.has_session(w):
                prompt = self.mtpl("worker_start", task=w.task or "", feedback=fb, human_messages="",
                                   cwd=w.working_dir, decisions="")
            else:
                prompt = self.mtpl("worker_next", feedback=fb, human_messages="", round=rnd, cwd=w.working_dir)
            s["prompt"] = prompt  # kept until the turn ends: a restart runs it again
        mg.pop("instruction_retry", None)
        prompt = s["prompt"] = self.par_leaks(w, prompt)
        for m in self.ledger.waiting(name):  # as it will be found in the prompt
            m["text"] = self.scrub_others(w, m.get("text") or "")[0]
        if c.job_wait > 0:
            self._par_procs[name] = _jobs.snapshot()
        s.update(status=PW.WORKING, round=rnd, turns=s.get("turns", 0) + 1, step_turns=s.get("step_turns", 0) + 1)
        par["turns"] = par.get("turns", 0) + 1
        extra = {"milestone": mg.get("ms", 0), "milestone_round": mg["ms_round"], "par_worker": name,
                 "par_step": s.get("step")}
        self.rec.progress(f"▶ {w.called} (`{name}`) works on step {s.get('step') or '?'} (round {rnd})")
        self.write_work_board()
        self.rec.save()
        return ex.submit(self._par_turn, w, prompt, rnd, extra)

    def _par_turn(self, w: AgentConfig, prompt: str, rnd: int, extra: Dict[str, Any]) -> Dict[str, Any]:
        """One worker's turn, on its own thread (the backend runs without the run-state lock)."""
        with self.rec.lock:
            watched = self.start_watch(w, "turn")
            try:
                r = self.say(w, prompt, "work", rnd=rnd, extra=extra)
            finally:
                if watched:
                    self.end_watch(w)
            self.after_state_turn(w, r)
            return r

    OTHER_TASK = "another task"  # what a worker reads where the critic named another worker

    def scrub_others(self, w: AgentConfig, text: str) -> Tuple[str, List[str]]:
        """B11: the ids and names of the other workers in text for worker `w` (they must not know about each other):
        (the text with each copy's id or name replaced by "another task", every name found). A copy's id ("writer-3",
        "Sam-3") can't be an ordinary word, so it is replaced; the first worker's own name ("writer", "Sam") may be
        one, so it is replaced only in backticks and otherwise just reported."""
        if not text or not self.clones:  # only a parallel section has copies that must not know each other
            return text, []
        hit: List[str] = []
        for a in self.all_workers:
            if a.name == w.name:
                continue
            for x in {a.name, a.display_name} - {""}:
                pat = re.compile(rf"(?<![\w-]){re.escape(x)}(?![\w-])", re.I)
                if not pat.search(text):
                    continue
                hit.append(x)
                safe = a.name in self.clones
                sub = (rf"(?i)(?:\bthe\s+)?`?(?<![\w-]){re.escape(x)}(?![\w-])`?" if safe
                       else rf"`{re.escape(x)}`")
                text = re.sub(sub, self.OTHER_TASK, text)
        return text, sorted(set(hit))

    def par_leaks(self, w: AgentConfig, text: str, what: str = "prompt") -> str:
        """Text for a worker without the other workers' ids (scrub_others); a hit is flagged on the board."""
        out, hit = self.scrub_others(w, text)
        if hit:
            cut = out != text
            self.rec.event("role_leak", agent=w.name, names=hit, what=what, replaced=cut)
            self.rec.progress(f"⚠ the {what} for `{w.name}` named another worker ({', '.join(hit)}); "
                              + (f"replaced with \"{self.OTHER_TASK}\"" if cut else "sent as written"))
        return out

    def par_after_turn(self, name: str, r: Dict[str, Any]) -> None:
        par, c, m = self.par, self.mon.controller, self.mon
        s, w = par["slots"][name], m.agent(name)
        summary = self._par_rounds.get(name) or {"round": s.get("round"), "turns": []}
        summary["turns"] = (summary.get("turns") or []) + [r["turn_id"]]
        if r["status"] == "stopped" or self.should_stop():
            s["status"] = PW.READY  # its prompt is kept: `orch run --resume` runs it again
            raise StopRequested()
        s.pop("prompt", None)
        s["turn_id"] = r["turn_id"]
        out = r.get("output") or ""
        block = self.ask_block(out, "worker")
        if block:
            self.new_question(w, self.critic.name, block, r["turn_id"])
        if r["status"] == "timeout":
            self.rec.progress(f"{name} ran past its safety limit; the turn was stopped; continuing")
            self.rec.event("safety_timeout", agent=name, turn=r.get("turn_id"), limit=w.turn_limit(self.watched(w)))
            self.forward(w, self.mtpl("worker_timeout").strip())
        elif r["status"] != "ok" and c.stop_on_error:
            self.end_round(summary, "worker error → stop", errors=[r["turn_id"]])
            raise _Failed(f"{name}'s turn ended with status {r['status']}")
        if r.get("cut_short"):
            self.forward(w, self.mtpl("worker_after_cut").strip())
        procs = self._par_procs.pop(name, None)
        running: Dict[str, Any] = {"pids": [], "lines": [], "cmds": {}}
        if c.job_wait > 0 and procs and not r.get("cut_short"):
            skip = {int(a["pid"]) for a, _b in T.blocks(out, "no-wait") if str(a.get("pid", "")).isdigit()}
            for j in _jobs.detect(procs, {w.working_dir or m.working_dir}, c.job_ignore,
                                  tags=[_jobs.tag_for(self.rec.run_dir.name, name)]):
                if j["pid"] not in skip:
                    running["pids"].append(j["pid"])
                    running["lines"].append(f"pid {j['pid']}: {_clip(str(j['command']), 160)}")
                    running["cmds"][str(j["pid"])] = j["command"]
        j = s.pop("job", None) or {}
        if j and not r.get("cut_short"):  # a message turn while its job ran: still running, the critic decides again
            for p, l in zip(j.get("pids") or [], j.get("lines") or []):
                if p not in running["pids"]:
                    running["pids"].append(p)
                    running["lines"].append(l)
                    running["cmds"][str(p)] = (j.get("cmds") or {}).get(str(p), "")
        s.pop("running_jobs", None)
        alive = self.alive_jobs({name: running}).get(name)
        if alive:  # never waited on by itself: the critic is told in its review and decides (<wait-job/>)
            s["running_jobs"] = {**alive, "since_ts": j.get("since_ts") or time.time()}
            self.rec.event("job_running", agent=name, jobs="; ".join(alive["lines"])[:300])
            self.rec.progress(f"⚙ still running after {name}'s turn: {', '.join(alive['lines'])}; "
                              f"{self.critic.name} decides whether its review waits for it")
        if self.rec.pending_worker_messages(name):
            s.update(status=PW.READY, instr="Please continue with the additional instructions and check the "
                                            "resulting work.")
            self.end_round(summary, f"additional instructions pending → {name}")
            return
        earlier = s.pop("earlier", "")
        s["report"] = (earlier + "\n\n**Later, after the job it was waiting on:**\n\n" + out) if earlier else out
        s["report_status"] = r["status"]
        s["status"] = PW.REVIEW
        par["reviews"].append(name)
        self.write_work_board()

    @staticmethod
    def alive_jobs(jobs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """{agent: {pids, lines, cmds}} with only the processes still running (lines follow their pids)."""
        out = {}
        for name, j in (jobs or {}).items():
            cmds = j.get("cmds") or {}
            keep = [(p, l) for p, l in zip(j.get("pids") or [], j.get("lines") or [])
                    if _jobs.alive(p, cmds.get(str(p), ""))]
            if keep:
                out[name] = {"pids": [p for p, _l in keep], "lines": [l for _p, l in keep],
                             "cmds": {str(p): cmds.get(str(p), "") for p, _l in keep}}
        return out

    @staticmethod
    def wait_job_choice(text: str, running: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """The running jobs a critic's `<wait-job/>` waits for: all of them, or those its pid attributes name
        (a pid that is not one of them is ignored). Empty: no tag, or nothing it names still runs."""
        tags = T.blocks(text or "", "wait-job")
        if not tags or not running:
            return {}
        named = {int(x) for a, _b in tags for x in re.split(r"[\s,]+", str(a.get("pid") or "")) if x.isdigit()}
        if not named:
            return {n: dict(j) for n, j in running.items()}
        out = {}
        for name, j in running.items():
            keep = [i for i, p in enumerate(j["pids"]) if p in named]
            if keep:
                out[name] = {"pids": [j["pids"][i] for i in keep], "lines": [j["lines"][i] for i in keep],
                             "cmds": {str(j["pids"][i]): j["cmds"].get(str(j["pids"][i]), "") for i in keep}}
        return out

    def message_for_job_turn(self, name: str) -> bool:
        """A message a worker's turn would carry while its job runs. A message a phase carries (in the critic's
        review or brief) is not one: the job turn's prompt never holds it, so waking the worker for it would repeat
        the turn for as long as the job runs."""
        return bool(self.rec.pending_worker_messages(name)
                    or any("phase" not in (m.get("carrier") or {}) for m in self.ledger.waiting(name)))

    def par_poll_jobs(self) -> None:
        par, mg = self.par, self.mg
        release = mg.pop("job_release", False)
        for name, s in par["slots"].items():
            if s["status"] != PW.JOB:
                continue
            j, w = s.get("job") or {}, self.mon.agent(name)
            alive = any(_jobs.alive(x, (j.get("cmds") or {}).get(str(x), "")) for x in j.get("pids") or [])
            msg = self.message_for_job_turn(name)
            if alive and not release and not msg:
                continue
            self.end_watch(w)
            self.rec.event("job_end", agent=name, why="released" if release else "message" if alive else "finished")
            if release:
                s.pop("job", None)
                s["report"], s["status"] = s.pop("earlier", ""), PW.REVIEW
                par["reviews"].append(name)
            elif alive:
                s.update(status=PW.READY, prompt=self.mtpl("worker_job_message", jobs="; ".join(j["lines"]),
                                                          human_messages=""))
            else:
                s.pop("job", None)
                s.update(status=PW.READY, prompt=self.mtpl("worker_job_done", jobs="; ".join(j["lines"]),
                                                          state="has finished", human_messages=""))
            self.write_work_board()

    def find_slot(self, who: str) -> Optional[str]:
        w = re.sub(r"^the\s+", "", (who or "").strip().lower())
        for name in self.par.get("slots", {}):
            a = self.mon.agent(name)
            if w in (name.lower(), re.sub(r"^the\s+", "", a.called.lower()), (a.display_name or "").lower()):
                return name
        return None

    def par_give(self, sections: List[Dict[str, Any]], turn: str, reviewed: str = "") -> List[str]:
        """Apply the <to> sections of a critic reply: a free worker gets the step; a busy one gets the text as a
        message. Returns the workers given a step."""
        par, given = self.par, []
        for sec in sections:
            name = self.find_slot(sec["worker"])
            if not name:
                self.rec.progress(f"⚠ <to worker=\"{sec['worker']}\"> names no worker of this section; not sent")
                self.ledger.add(self.critic.name, sec["worker"], sec["text"], status="held", source=turn,
                                reason="names no worker of the parallel section")
                continue
            s = par["slots"][name]
            if s["status"] in (PW.IDLE, PW.DONE, PW.BLOCKED) or name == reviewed:
                step = sec["step"] or (par["queue"][0] if par["queue"] else None)
                if step in par["queue"]:
                    par["queue"].remove(step)
                s.update(status=PW.READY, step=step, out=sec["out"] or s.get("out", ""), instr=sec["text"],
                         step_turns=0 if step != s.get("step") or name != reviewed else s.get("step_turns", 0))
                s.pop("prompt", None)
                self.ledger.add(self.critic.name, name, sec["text"], self.for_worker(sec["text"]), source=turn,
                                status="sent", carrier={"phase": "par_assign"}, via="parallel", purpose="par_assign", step=step)
                given.append(name)
            else:
                self.deliver(self.critic, self.mon.agent(name), sec["text"], source=turn, purpose="par_message",
                             activate=False)
        return given

    def par_assign(self, idle: List[str]) -> None:
        """The critic hands out steps to the workers without one (one turn, one <to> section each)."""
        par, c = self.par, self.critic
        ids = ", ".join(f"`{n}`" + (f" ({self.mon.agent(n).called})" if self.mon.agent(n).called != n else "")
                        for n in par["slots"])
        base = self.workers[0]
        try:
            ck = self.checklist_board()
        except Exception:
            ck = {}
        steps = ", ".join(PW.step_title(ck, k)[1] for k in par["queue"]) or "the next steps of your plan"
        wt = "; each also has its own separate copy of the repository (a git worktree, shown on the board)" \
            if par.get("worktrees") else ""
        nudge = (self.st.get("nudge") or {}).get(c.name, "")
        prompt = self.mtpl("critic_par_assign", human_messages=nudge, n=len(par["slots"]), ids=ids,
                           first=f"`{base.name}`", fresh="" if base.name in idle and len(idle) == len(par["slots"])
                           else "", steps=steps, who=", ".join(f"`{n}`" for n in idle), worktrees=wt,
                           board=str(self.work_board_path()))
        rnd = int(self.st.get("round", 0))
        r = self.say(c, prompt, "par_assign", rnd=rnd)
        if r["status"] != "ok":
            raise _Failed(f"{c.name}'s assignment turn ended with status {r['status']}")
        self.rec.set(nudge=None)
        given = self.par_give(PW.to_sections(r.get("output") or ""), r["turn_id"])
        par["assign_due"] = False
        self.par_questions_up(r)
        left = [n for n in idle if n not in given]
        if left:
            self.rec.progress(f"ⓘ no step for {', '.join(left)}: they stop here")
        self.write_work_board()

    def par_questions_up(self, r: Dict[str, Any]) -> None:
        """A question the critic asks in a parallel section goes up as a note that wakes the manager; the work goes
        on (no phase change)."""
        block = self.ask_block(r.get("output") or "", "verifier")
        if block:
            self.new_question(self.critic, self.manager.name, block, r["turn_id"])
            self.deliver(self.critic, self.manager, block, self.mtpl("relay_note_up", note=block).strip(),
                         source=r["turn_id"], purpose=r.get("purpose") or "", wake=True, activate=True)

    def par_review(self, name: str) -> None:
        """One review of one worker's step (main thread; one at a time)."""
        par, c, v = self.par, self.mon.controller, self.critic
        s, w = par["slots"][name], self.mon.agent(name)
        summary = self._par_rounds.get(name) or {"round": s.get("round") or self.st.get("round", 0),
                                                       "turns": [x for x in [s.get("turn_id")] if x]}
        try:
            ck = self.checklist_board()
        except Exception:
            ck = {}
        _st, title = PW.step_title(ck, s.get("step"))
        title = title or "their step"
        nxt = par["queue"][0] if par["queue"] else None
        tail = (self.mtpl("critic_par_next", next_step=PW.step_title(ck, nxt)[1], who=f"`{name}`") if nxt
                else self.mtpl("critic_par_last", who=f"`{name}`"))
        nudge = (self.st.get("nudge") or {}).get(v.name, "")
        raw_report = s.get("report", "")
        report = self.report_text({"output": raw_report, "status": s.get("report_status", "ok")})
        prompt = self.mtpl("critic_par_review", human_messages=nudge, who=w.called, id=name, step=title,
                           report=report, next=tail.strip() and " " + tail.strip(), board=str(self.work_board_path()))
        held = s.get("running_jobs") or {}
        running = self.alive_jobs({name: held}) if held else {}
        if running:  # processes it left running: the critic decides whether its step waits for them
            prompt = prompt.rstrip() + "\n\n" + self.mtpl(
                "critic_jobs_running", who=w.called, jobs="; ".join(running[name]["lines"])).strip()
        r = self.say(v, prompt, "par_review", rnd=int(summary.get("round") or 0), extra={"par_worker": name})
        par["reviews"].remove(name)  # consume only after the critic returns, before saving its decisions
        self._par_rounds.pop(name, None)
        s.pop("report", None)
        s.pop("running_jobs", None)
        summary["turns"] = (summary.get("turns") or []) + [r["turn_id"]]
        chosen = self.wait_job_choice(r.get("output") or "", running).get(name)
        if chosen:  # the step waits for the job, watched by the progress checks (par_poll_jobs)
            s.update(status=PW.JOB, job={**chosen, "since_ts": held.get("since_ts") or time.time()},
                     earlier=raw_report)
            feedback = self.clean_for(r.get("output") or "", w)
            if feedback:  # reaches the worker at its turn after the job
                self.forward(w, feedback)
            others = [x for x in PW.to_sections(r.get("output") or "") if self.find_slot(x["worker"]) != name]
            if others:
                self.par_give(others, r["turn_id"])
            self.start_watch(w, "job", jobs=chosen["lines"])
            self.rec.event("job_wait", agent=name, jobs="; ".join(chosen["lines"])[:300])
            self.rec.progress(f"⏳ {v.called} waits for {name}'s job ({', '.join(chosen['lines'])}) before reviewing")
            self.par_questions_up(r)
            self.end_round(summary, f"{name}: {v.name} waits for its job")
            self.write_work_board()
            return
        if T.blocks(r.get("output") or "", "wait-job"):
            self.rec.progress(f"{v.called} asked to wait for {name}'s job, but none of its processes is still "
                              "running; the review counts as it is")
        vd = self.verdict(r)
        if vd in ("ERROR", "UNKNOWN") and c.stop_on_error and r["status"] != "ok":
            self.end_round(summary, "critic error → stop", verdicts={v.name: vd})
            raise _Failed(f"{v.name}'s review of {name} ended with status {r['status']}")
        self.rec.set(nudge=None)
        out = r.get("output") or ""
        if vd == "PASS" and c.checklist:  # the step's items in the checklist file decide, never the reply
            try:
                st_, _t = PW.step_title(self.checklist_board(), s.get("step"))
            except Exception:
                st_ = None
            items = (st_ or {}).get("items") or []
            if st_ is not None and (not items or not all(d for d, _x in items)):
                vd = "FAIL"
                summary["checklist_overruled"] = [v.name]
                self.rec.progress(f"{v.name} passed {title} for {name} but its items are not all ticked → FAIL")
                self.rec.set(nudge={v.name: self.mtpl("par_checklist_nudge", step=title, who=f"`{name}`").strip()})
        for q in self.open_qs(name):  # the critic saw the worker's question in the report
            self.settle(q, v.name, "", r["turn_id"])
        sections = PW.to_sections(out)
        mine = [x for x in sections if self.find_slot(x["worker"]) == name]
        others = [x for x in sections if self.find_slot(x["worker"]) != name]
        feedback = self.clean_for(out, w)
        s.pop("instr", None)
        s.pop("prompt", None)
        if vd == "PASS":
            if s.get("step") is not None:
                s.setdefault("done_steps", []).append(s["step"])
            s.update(step=None, step_turns=0)
        if mine:
            self.par_give(mine, r["turn_id"], reviewed=name)
            if feedback:
                s["instr"] = "\n\n".join(x for x in [feedback, s.get("instr", "")] if x)
        elif feedback or self.rec.pending_worker_messages(name):
            s.update(status=PW.READY, instr=feedback)
        elif vd == "PASS":
            s["status"] = PW.IDLE if par["queue"] else PW.DONE
            if par["queue"]:
                par["assign_due"] = True
        else:
            s["status"] = PW.BLOCKED
        if s["status"] == PW.READY and s.get("step_turns", 0) >= c.milestone_rounds:
            s["status"] = PW.STALLED
        decision = f"{name}: " + ("explicit instruction" if s["status"] == PW.READY else s["status"])
        summary["verdicts"] = {v.name: vd}
        if not s.get("instr"):  # its message rode on the review, and no further turn carries it
            self.drop_waiting([m for m in self.ledger.waiting(name) if m.get("source") == r.get("turn_id")
                               and "phase" in (m.get("carrier") or {})],
                              "the step passed; nothing more goes to them for it")
        if others:
            self.par_give(others, r["turn_id"])
        self.par_questions_up(r)
        self.end_round(summary, decision)
        self.write_work_board()

    def par_end(self, mg: Dict[str, Any]) -> None:
        par = mg.get("par") or {}
        for n in list(par.get("slots", {})):
            if self.has_agent(n):
                self.end_watch(self.mon.agent(n))
        self.write_work_board()
        why = "the parallel section ended: they work no more"
        for n in [n for n in par.get("slots", {}) if n in self.clones]:  # nothing reaches a copy that has stopped
            self.drop_waiting(self.ledger.waiting(n), why)
            for wm in self.rec.pending_worker_messages(n):
                self.rec.worker_message_status(wm["id"], RT.DROPPED, reason=why)
        self.par_drop_clones()
        par["active"] = False
        par["ended_at"] = now_iso()
        mg["par_last"] = par
        mg.pop("par", None)
        mg.pop("par_req", None)
        self._par_rounds, self._par_procs = {}, {}

    def par_finish(self, mg: Dict[str, Any]) -> Optional[str]:
        """The section is over: tidy up, then the critic takes stock in a review of the whole (as for one worker)."""
        par = mg.get("par") or mg.get("par_last") or {}
        missing = [n for n, s in par.get("slots", {}).items() if s.get("status") == PW.BLOCKED]
        if missing and par.get("active"):
            self.write_work_board()
            self.missing_instructions(missing)
            return None
        if par.get("active"):
            self.par_end(mg)
        try:
            ck = self.checklist_board()
        except Exception:
            ck = {}
        lines = []
        for n, s in par.get("slots", {}).items():
            done = ", ".join(PW.step_title(ck, k)[1] for k in s.get("done_steps") or []) or "no step passed"
            state = {PW.STALLED: f"; stopped on {PW.step_title(ck, s.get('step'))[1]} after too many turns"}.get(
                s.get("status"), "")
            kept = f"; its worktree is kept at {s['worktree']['path']} (branch {s['branch']})" \
                if s.get("worktree_kept") else ""
            lines.append(f"- `{n}`: {done}{state}{kept} (output: {s.get('out') or 'not named'})")
        if par.get("queue"):
            lines.append("- Not handed out: " + ", ".join(PW.step_title(ck, k)[1] for k in par["queue"]))
        if par.get("cap"):
            lines.append(f"- The turn budget for this section ({self.mon.controller.parallel_turns}) is used up.")
        summary = "\n".join(lines)
        self.rec.event("parallel_end", turns=par.get("turns", 0), max_rounds=bool(par.get("max_rounds")))
        self.rec.progress(f"👥 parallel section ends after {par.get('turns', 0)} worker turns")
        missing = [n for n, s in par.get("slots", {}).items() if s.get("status") == PW.BLOCKED]
        if missing:
            self.missing_instructions(missing)
            return None
        if par.get("max_rounds"):
            return "max_rounds_reached"
        if any(s.get("status") == PW.STALLED for s in par.get("slots", {}).values()):
            mg.update(limit=True, phase="report")
            return None
        mg["review_prompt"] = self.mtpl("critic_par_done", human_messages="", summary=summary,
                                        board=str(self.work_board_path()))
        base = self.workers[0].name
        mg["work"] = {base: {"turn_id": "", "status": "ok", "output": summary, "stderr": ""}}
        mg["summary"] = self.begin_round(int(self.st.get("round", 0)) + 1)
        mg["phase"] = "review"
        return None

    def ph_review(self, mg: Dict[str, Any]) -> Optional[str]:
        c, v = self.mon.controller, self.critic
        mg.pop("review_report", None)
        work = mg.get("work") or {}
        reports = "\n\n".join((f"From {self.mon.agent(k).called}:\n" if len(work) > 1 else "") + self.report_text(x)
                              for k, x in work.items())
        nudge = (self.st.get("nudge") or {}).get(v.name, "")
        prompt = mg.pop("review_prompt", "")  # B11: after a parallel section, the critic takes stock of it
        if not prompt:  # the full instructions once per session (and after a compaction); a short line after
            seen = self.st.setdefault("review_tpl_seen", {})
            sid = (self.st["sessions"].get(v.name) or {}).get("session_id")
            tpl = "critic_review_short" if sid and seen.get(v.name) == sid else "critic_review"
            prompt = self.mtpl(tpl, worker_report=reports, human_messages=nudge)
        running = self.alive_jobs((mg.get("running_jobs") or {}).get("agents") or {})
        if running:  # processes the worker left running: the critic decides whether the step waits for them
            prompt = prompt.rstrip() + "\n\n" + self.mtpl(
                "critic_jobs_running", who=" and ".join(self.mon.agent(a).called for a in running),
                jobs="; ".join(l for j in running.values() for l in j["lines"])).strip()
        r = self.say(v, prompt, "verify")
        sid = (self.st["sessions"].get(v.name) or {}).get("session_id")
        if sid and r.get("status") == "ok":
            self.st.setdefault("review_tpl_seen", {})[v.name] = sid
        summary = mg.get("summary") or {"round": self.st.get("round", 0), "turns": []}
        summary["turns"] = (summary.get("turns") or []) + [r["turn_id"]]
        held = mg.pop("running_jobs", None) or {}
        chosen = self.wait_job_choice(r.get("output") or "", running)
        if chosen:  # the critic waits for the job: the step waits, watched by the progress checks (ph_job)
            mg["job"] = {"agents": chosen, "since": held.get("since") or now_iso(),
                         "since_ts": held.get("since_ts") or time.time()}
            mg["work_earlier"] = {a: x.get("output") or "" for a, x in work.items()}
            text = self.clean_critic(r.get("output") or "")
            for name in chosen:  # what the critic wrote to the worker reaches it at its turn after the job
                if text:
                    self.forward(self.mon.agent(name), text)
            pids = ", ".join(str(x) for j in chosen.values() for x in j["pids"])
            self.rec.progress(f"⏳ {v.called} waits for the job (pid {pids}) before reviewing")
            for a, j in chosen.items():
                self.rec.event("job_wait", agent=a, jobs="; ".join(j["lines"])[:300])
            self.log(f"  ⏳ waiting for job pid {pids} (watched by progress checks; 'Review now' on the board skips)")
            mg["phase"] = "job"
            asked = self.critic_asks(r, after="job")
            self.end_round(summary, f"{v.name} waits for the job" + (" · question for the manager" if asked else ""))
            mg["asks"] = 0
            return None
        if T.blocks(r.get("output") or "", "wait-job"):
            self.rec.progress(f"{v.called} asked to wait for a job, but none of the processes is still running; "
                              "the review counts as it is")
        vd = self.verdict(r)
        if vd == "UNKNOWN" and c.checklist:  # no tag needed: the checklist file decides (an explicit FAIL still holds)
            vd = self.file_verdict()
        if vd == "UNKNOWN":  # a review without its verdict tag: asked once for it before the run stops
            self.rec.progress(f"⚠ {v.called}'s review has no verdict; asked for it")
            r2 = self.say(v, self.mtpl("critic_verdict_missing").strip(), "verdict")
            summary["turns"].append(r2["turn_id"])
            vd = self.verdict(r2)
        if vd in ("ERROR", "UNKNOWN") and c.stop_on_error:
            self.end_round(summary, "critic error / no verdict → stop", verdicts={v.name: vd})
            self.rec.set(error=f"{v.name}'s review ended with " + ("an error" if vd == "ERROR" else "no verdict"))
            return "failed"
        if vd == "PASS" and c.checklist:  # read from the checklist file, never from a list in the reply
            done, open_ = checklist_state(self.latest_checklist())
            if open_ or not done:
                vd = "FAIL"
                summary["checklist_overruled"] = [v.name]
                self.rec.progress(f"{v.name} said PASS but its checklist file has {open_} open / {done} done items "
                                  "→ FAIL")
                self.rec.set(nudge={v.name: self.checklist_nudge(open_)})
            else:
                self.rec.set(nudge=None)
        elif c.checklist:
            self.rec.set(nudge=None)
        if vd == "PASS":
            pd, pt, cur = plan_status(self.latest_plan())
            if pt and pd < pt:  # a finished step is not the finished work: the next step goes to the worker
                vd = "FAIL"
                summary["plan_open"] = pt - pd
                self.rec.progress(f"{v.name} said PASS with {pt - pd} plan step(s) still open → the work goes on")
                self.rec.set(nudge={v.name: self.mtpl("plan_nudge", step=cur or "the next step").strip()})
        summary["verdicts"] = {v.name: vd}
        passed = vd == "PASS"
        self.worker_feedback_for(r["output"])
        has_work = any((mg.get("feedback") or {}).values())
        wait = T.bodies(r.get("output") or "", "pause")
        nxt = "pause" if wait else ("work" if has_work or not passed else "report")
        if wait:
            mg["wait"] = {"who": self.critic.called, "text": "\n\n".join(wait), "turn": r["turn_id"]}
        if nxt == "report":  # the final review was asked to double as the report: no second turn for the same news
            text = self.upward(r.get("output") or "").strip()
            if text:
                mg["review_report"] = {"turn_id": r["turn_id"], "text": text}
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

    def checklist_nudge(self, open_: int) -> str:
        """Said to the critic when its PASS was overruled: about the file (a nudge set in the config wins)."""
        c = self.mon.controller
        if c.checklist_nudge != type(c).checklist_nudge:
            return c.checklist_nudge
        return self.mtpl("checklist_file_nudge", open=open_).strip()

    def ph_pause(self, mg: Dict[str, Any]) -> Optional[str]:
        """The critic proposes stopping the whole run until a person acts; the manager decides."""
        w = mg.get("wait") or {}
        r = self.say(self.manager, self.mtpl("manager_pause", proposal=w.get("text", ""), human_messages=""), "pause")
        return self.pause_decision(r, w)

    def pause_decision(self, r: Dict[str, Any], w: Dict[str, Any]) -> Optional[str]:
        """The manager's PAUSE (the run waits for me) or CONTINUE (the critic gets work meanwhile)."""
        mg = self.mg
        out = r.get("output") or ""
        decision = T.value(out, "decision")
        if decision != "CONTINUE":
            # An absent or malformed decision must not implicitly authorize more worker turns.
            self.after_manager(r)
            msg = "\n\n".join(T.bodies(out, "pause")) or self.for_me(out)
            who = "the manager agrees" if decision == "PAUSE" or T.bodies(out, "pause") else "manager decision unclear"
            mg["wait"] = {**w, "who": who, "text": msg or w.get("text", ""), "proposal": w.get("text", ""),
                          "turn": r["turn_id"]}
            mg["phase"] = "wait"
            return None
        self.after_manager(r, relay=False)
        mg.pop("pause_req", None)  # it decided to continue
        guidance = self.critic_message(r, "pause")
        mg.pop("wait", None)
        c = self.say(self.critic, self.mtpl("critic_continue", guidance=guidance, human_messages=""), "guidance")
        self.worker_feedback_for(c["output"])
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
        if d["decision"] == "revise":  # B9: sent back with my notes: the manager decides again
            msg = msg or "Not done; please decide again."
            self.decide(f"Pause sent back: {msg}", "you", "wait")
            r = self.say(self.manager, self.mtpl("manager_wait_revise", message=msg,
                                                 waited=truncate(w.get("proposal") or w.get("text", ""), 600)), "pause")
            return self.pause_decision(r, {**w, "proposal": w.get("proposal") or w.get("text", "")})
        if msg:
            self.decide(msg, "you", "wait")
            self.forward(self.manager, self.mtpl("manager_resumed", message=msg,
                                                 waited=truncate(w.get("proposal") or w.get("text", ""), 600)).strip())
        r = self.say(self.critic, self.mtpl("critic_resume", message=msg or "(no message)",
                                            waited=w.get("proposal") or "a task outside this work",
                                            human_messages=""), "guidance")
        self.worker_feedback_for(r["output"])
        mg.pop("wait", None)
        mg["phase"] = "work"
        self.critic_asks(r, after="work")
        return None

    def ph_checkin(self, mg: Dict[str, Any]) -> Optional[str]:
        """A short update for the manager. With a plan, orch writes it from the plan (no model turn: the manager
        hears it at its next turn, and the stall limit or a question brings it in when it matters); without one,
        the critic writes it and the manager acknowledges or steers (no full report, no scoring)."""
        line = self.plan_line()
        if line:
            fwd = self.st.setdefault("fwd", {}).setdefault(self.manager.name, [])
            fwd[:] = [x for x in fwd if not x.startswith(PLAN_STATUS)]  # only the newest status waits for it
            fwd.append(f"{PLAN_STATUS} {line}")
            self.rec.event("checkin", how="plan", text=line)
            self.rec.progress(f"ⓘ check-in from the plan: {line}")
            mg["phase"] = "work"
            return None
        r = self.say(self.critic, self.mtpl("critic_checkin", human_messages=""), "checkin")
        update = "\n\n".join(x for x in [self.upward(r["output"]), self.ask_block(r["output"], "verifier")] if x)
        a = self.say(self.manager, self.mtpl("manager_checkin", update=update, human_messages=""), "ack")
        self.after_manager(a, relay=False)  # its messages are routed after the turn (route_reply)
        mg["phase"] = "work"
        return None

    def ph_answer(self, mg: Dict[str, Any]) -> Optional[str]:
        q = self.q(mg.get("ask", ""))
        if not q:
            mg["phase"] = mg.get("after") or "work"
            return None
        prompt = self.mtpl("manager_question", question=q["text"], human_messages="")
        if mg.get("after") == "report":  # the critic passed the milestone and asked: the review is the next turn
            prompt = prompt.rstrip() + "\n\n" + self.mtpl("manager_question_before_report").strip()
        r = self.say(self.manager, prompt, "answer")
        self.after_manager(r, parent=[q["id"]], relay=False)
        answer = self.critic_message(r, "answer")
        if self.open_qs(self.manager.name, "human") and any(q["id"] in x["parent"] for x in self.open_qs(self.manager.name)):
            self.settle(q, "", "", r["turn_id"], status="passed_up")  # gone up to me; the manager's guess is its message
        else:
            self.settle(q, self.manager.name, answer, r["turn_id"])
            self.decide(f"{self.short_question(q['text'])}: {answer}", self.manager.called, q["id"])
        mg.update(answer_text=answer, phase="amend")
        return None

    def ph_amend(self, mg: Dict[str, Any]) -> Optional[str]:
        """The critic takes the manager's answer in. When its review has not reached the worker yet, the worker gets
        one instruction for the step: the amended one replaces the queued review (unless it is marked additive, or
        holds nothing for the worker, in which case the review goes out as it was)."""
        pending = any((mg.get("feedback") or {}).get(w.name) for w in self.workers)
        prompt = self.mtpl("critic_answer", answer=mg.get("answer_text", ""))
        if pending:
            prompt = prompt.rstrip() + "\n\n" + self.mtpl("critic_answer_pending").strip()
        r = self.say(self.critic, prompt, "amend", extra={"after_phase": mg.get("after")})
        after = mg.get("after") or "work"
        vd = self.verdict(r) if T.value(r.get("output") or "", "verdict") else None
        if vd is None and self.mon.controller.checklist and r.get("status") == "ok":
            vd = self.file_verdict()  # no tag needed: the checklist file decides
        if after == "report" and (vd == "FAIL" or self.clean_critic(r["output"])):
            after = "work"
        elif after == "work" and vd == "PASS" and not self.clean_critic(r["output"]):
            done, open_ = checklist_state(self.latest_checklist())
            pd, pt, _cur = plan_status(self.latest_plan())
            if (not self.mon.controller.checklist or (done and not open_)) and not (pt and pd < pt):
                after = "report"
        if after == "work":
            self.amend_feedback(r)
        mg["phase"] = after
        mg["asks"] = mg.get("asks", 0) + 1
        if mg["asks"] < 2:
            self.critic_asks(r, after=after)
        return None

    def ph_report(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        note = self.limit_note() if mg.get("limit") else ""
        rr = mg.pop("review_report", None)
        if rr and not note:  # the final review is the report; its progress note would only repeat it
            for m in self.ledger.rows:
                if m.get("source") == rr["turn_id"] and m.get("to") == self.manager.name and m.get("status") == "held":
                    self.ledger.mark(m, "superseded", reason="carried by the milestone report", by=rr["turn_id"])
                    fwd = (self.st.get("fwd") or {}).get(self.manager.name) or []
                    if m.get("text") in fwd:
                        fwd.remove(m["text"])
                        if not fwd:
                            self.st["fwd"].pop(self.manager.name, None)
            r = {"turn_id": rr["turn_id"]}
            report = rr["text"]
            self.rec.progress(f"📝 {self.critic.called}'s final review is its report")
        else:
            r = self.say(self.critic, self.mtpl("critic_report", limit_note=note, human_messages=""), "report")
            report = self.upward(r["output"])
        plan = self.latest_plan()
        if plan:  # where the work stands, step by step; one line once every step is done
            pd, pt, _cur = plan_status(plan)
            report += "\n\n" + self.mtpl("report_plan_done" if pt and pd == pt else "report_plan",
                                           done=pd, total=pt, plan=plan).strip()
        ck = self.latest_checklist()
        if ck:  # the manager judges the milestone with the critic's own list in view; the worker never gets it
            done, open_ = checklist_state(ck)
            report += "\n\n" + self.mtpl("report_checklist", done=done, total=done + open_, checklist=ck).strip()
        pending_work = any(self.rec.pending_worker_messages(w.name) for w in self.workers)
        mg.update(report=report, report_turn=r["turn_id"], phase="work" if pending_work else "mreview")
        return None

    def ph_mreview(self, mg: Dict[str, Any]) -> Optional[str]:
        c, ms = self.mon.controller, self.cur()
        m = self.manager
        sid = (self.st["sessions"].get(m.name) or {}).get("session_id")
        key = [mg["ms"], ms.get("rubric_turn") or ""]  # a re-review in the same session: the rubric is already there
        seen = (self.st.get("rubric_seen") or {}).get(m.name)
        rubric = (self.mtpl("rubric_unchanged").strip() if sid and ms.get("rubric") and seen == [sid] + key
                  else self.rubric_text(ms))
        r = self.say(m, self.mtpl("manager_review", n=mg["ms"] + 1, title=ms["title"],
                                  report=mg.get("report", ""), human_messages="", rubric=rubric), "review")
        sid = (self.st["sessions"].get(m.name) or {}).get("session_id")
        if sid and r.get("status") == "ok" and ms.get("rubric"):
            self.st.setdefault("rubric_seen", {})[m.name] = [sid] + key
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
        summ = [b for x in (out, r.get("output") or "") for b in T.bodies(x, "summary")]
        self.st.setdefault("reviews", []).append(
            {"milestone": mg["ms"], "turn": r["turn_id"], "score": score, "criteria": crit,
             "decision": "ACCEPT" if accept else "REVISE", "round": self.st.get("round", 0), "at": now_iso()})
        ms["score"], ms["criteria"] = score, crit
        if accept:
            self.drop_phase_messages(r["turn_id"], self.critic.name, "the milestone was accepted; the next one starts "
                                                                     "with a new instruction")
        if accept or mg.get("limit"):
            ms.update(status="awaiting_ok", summary=self.for_me(text), summary_turn=r["turn_id"], accepted=accept,
                      gate_summary=summ[-1] if summ else "")
            gate = c.approve_milestones or not accept
            mg.update(phase="gate" if gate else "next", guidance="" if accept else self.critic_message(r, "review"),
                      guidance_at=now_iso())
        else:
            mg.update(guidance=self.critic_message(r, "review"), guidance_at=now_iso(), phase="guidance")
        return None

    def ph_guidance(self, mg: Dict[str, Any]) -> Optional[str]:
        r = self.say(self.critic, self.mtpl("critic_guidance", guidance=mg.get("guidance", ""), human_messages=""),
                     "guidance")
        self.worker_feedback_for(r["output"])
        mg.update(phase="work", limit=False)
        self.critic_asks(r, after="work")
        return None

    def ph_gate(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        why = "" if ms.get("accepted", True) else f" — {self.limit_title()}, the manager has not accepted it"
        d = self.wait_gate({"kind": "milestone", "milestone": mg["ms"], "title": f"Milestone {mg['ms'] + 1}: "
                            f"{ms['title']}{why}", "text": ms.get("summary", ""), "summary": ms.get("gate_summary", ""),
                            "turn": ms.get("summary_turn")})
        if d.get("message"):
            self.decide(f"Milestone {mg['ms'] + 1} ({ms['title']}): {d['decision']}. {d['message']}", "you", "gate")
        held = (mg.get("guidance") or "").strip()  # the manager's message for the critic, held while I decide
        disc = self.gate_discussion()  # a discussion I closed while the gate was open: one turn carries both
        if d["decision"] == "approve":
            if d.get("message"):
                self.forward(self.manager, d["message"])
            if held:
                self.rec.event("message_superseded", turn=ms.get("summary_turn"), to=self.critic.name,
                               reason="milestone approved at the gate")
                self.drop_phase_messages(ms.get("summary_turn") or "", self.critic.name, "milestone approved at the gate")
            if disc and mg["ms"] + 1 < len(mg.get("milestones") or []):
                mg["discussion_carry"] = disc  # the next milestone's kickoff carries it
            elif disc:
                self.discussion_done({}, why_none="the last milestone was approved; nothing more goes to the critic",
                                     at_gate=True)
            mg.update(phase="next", guidance="")
            return None
        comment = d.get("message") or held or "Please keep going on this milestone."
        # with my comment in its place, the held message would vanish unseen: show it to the manager to carry over
        unsent = self.mtpl("unsent_message", message=held).strip() if held and d.get("message") else ""
        prompt = self.mtpl("manager_feedback", n=mg["ms"] + 1, title=ms["title"], comment=comment, unsent=unsent)
        if disc:
            prompt = prompt.rstrip() + "\n\n" + disc + "\n"
        r = self.say(self.manager, prompt, "feedback")
        if held:
            why = ("replaced by the manager's message after the gate"
                   + ("" if unsent or comment == held else "; not shown to the manager"))
            self.rec.event("message_superseded", turn=ms.get("summary_turn"), to=self.critic.name, by=r["turn_id"],
                           reason=why)
            self.drop_phase_messages(ms.get("summary_turn") or "", self.critic.name, why, by=r["turn_id"])
        ms.update(status="active")
        self.after_manager(r, relay=False)
        if disc:
            self.discussion_done(r, why_none=CARRIED, at_gate=True)
        mg.update(guidance=self.critic_message(r, "feedback"), guidance_at=now_iso(), ms_round=0, stall=0,
                  limit=False, phase="guidance")
        return None

    def ph_next(self, mg: Dict[str, Any]) -> Optional[str]:
        ms = self.cur()
        ms.update(status="done", ended_at=now_iso(), ended_round=int(self.st.get("round", 0)))
        self.rec.progress(f"🏁 **Milestone {mg['ms'] + 1} done:** {ms['title']}")
        roles = self.roles()
        self.drop_waiting([m for m in self.ledger.rows if roles.get(m["to"]) == "worker"  # the phases that carried them are over
                           and "phase" in (m.get("carrier") or {})], f"milestone {mg['ms'] + 1} ended before it reached them")
        mg["ms"] += 1
        mg["phase"] = "kickoff" if mg["ms"] < len(mg["milestones"]) else "done"
        if mg["phase"] == "done":
            self.drop_waiting(list(self.ledger.rows), "the run ended before it was delivered")
            self.st["final_report"] = "\n\n".join(x for x in (ms.get("gate_summary"), ms.get("summary")) if x)
        return None


class _Failed(Exception):
    pass
