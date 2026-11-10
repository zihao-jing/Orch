"""Monitor runner: one controller coordinating one team of agents.

Two controller modes:

pipeline  Deterministic loop. Each round: workers get their task (round 1) or the
          verifiers' feedback (later rounds); then every verifier reviews the workspace
          and answers PASS/FAIL. Failing feedback is routed to the workers named in
          ``feedback_to`` (default: the workers it ``reviews``, default: all workers).
          Stops when the verifiers pass (``require: all|any``) or at ``max_rounds``.

llm       An LLM controller agent reads the goal, instructions and roster, and each
          round replies with a JSON directive saying which agent gets which message.
          The orchestrator runs those turns and feeds the results back to the
          controller, until it declares ``done`` or ``max_rounds`` is reached.

Every agent keeps one session for the whole run, so later turns continue earlier context.
"""

from __future__ import annotations

import sys
import dataclasses
import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .backends import StopRequested, TurnRequest, TurnResult, get_backend
from .config import AgentConfig, MonitorConfig
from .recorder import Recorder, now_iso
from .report import CHECKBOX
from .templates import render

TERMINAL = ("completed", "failed", "max_rounds_reached", "needs_human")


class NeedsHuman(Exception):
    """An agent asked a question, needs a permission/allowance decision, or produced an unusable reply
    that only a human can fix. The run pauses (status ``needs_human``) and shows the raw text."""

    def __init__(self, record: Dict[str, Any], reason: str):
        super().__init__(reason)
        self.record, self.reason = record, reason


_OPT = re.compile(r"^\s*(?:[-*]\s*)?\(?(\d{1,2}|[A-Za-z])[.)]\s+(\S.*)$")


def parse_options(block: str) -> List[Tuple[str, str]]:
    """Lettered / numbered choices in a question block: [(key, label), ...] (needs at least two)."""
    opts = [(m.group(1), m.group(2).strip()) for m in map(_OPT.match, (block or "").splitlines()) if m]
    return opts if len(opts) >= 2 else []


def resolve_choice(ans: str, opts: List[Tuple[str, str]]) -> str:
    """Turn '2' / 'b' / '1,3' into the chosen option text(s); anything else is a free-text answer."""
    keys = {k.lower(): lab for k, lab in opts}
    if opts and not opts[0][0].isdigit():  # lettered choices: also accept 1, 2, 3
        keys.update({str(i + 1): lab for i, (_, lab) in enumerate(opts)})
    parts = [x.strip().lower() for x in re.split(r"[,\s]+(?:and\s+)?", ans.strip()) if x.strip()]
    if opts and parts and all(x in keys for x in parts):
        return "; ".join(f"{x.upper() if not x.isdigit() else x}) {keys[x]}" for x in parts)
    return ans


def trailing_question(text: str) -> str:
    """The closing paragraph of a reply, if it is a question to the reader (ignoring a final VERDICT line)."""
    body = re.sub(r"\n*\s*VERDICT:\s*\**\s*\w+\**\s*$", "", (text or "").strip(), flags=re.I).strip()
    paras = [x.strip() for x in body.split("\n\n")] if body else [""]
    last = paras[-1]
    if parse_options(last) and len(paras) > 1 and paras[-2].endswith("?"):
        return paras[-2] + "\n" + last  # a question followed by its choices
    if len(paras) > 1 and last.endswith("?") is False and \
            re.search(r"\?\s*\n\s*(?:[-*]\s*)?\(?(\d{1,2}|[A-Za-z])[.)]\s", last):
        return last  # question and choices in one paragraph
    return last if last.endswith("?") and not last.startswith(("-", "*", "|", "#")) else ""


_BOX = CHECKBOX


def checklist_state(text: str) -> Tuple[int, int]:
    """(done, open) counts of Markdown checkboxes ("- [x] ..." / "- [ ] ...") in a reply."""
    marks = _BOX.findall(text or "")
    return sum(1 for m in marks if m != " "), sum(1 for m in marks if m == " ")


def strip_checklist(text: str) -> str:
    """Drop checkbox lines (and a short heading right above a checklist) so the reviewer's list stays its own."""
    lines = (text or "").split("\n")
    keep = []
    for i, ln in enumerate(lines):
        if _BOX.match(ln):
            continue
        nxt = next((x for x in lines[i + 1:] if x.strip()), "")
        if _BOX.match(nxt) and len(ln.strip()) < 60 and re.match(r"^\s*(#+\s|\*\*|[^.!?]*:\s*\**\s*$)", ln):
            continue  # e.g. "**Checklist**" or "Checklist:"
        keep.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def classify(c: Any, res: TurnResult) -> Tuple[str, str]:
    """Return (status, reason). Catches failures a backend reports as success (exit 0, empty reply)."""
    if res.status != "ok":
        return res.status, ""
    text, err = res.output or "", res.stderr or ""
    if re.search(c.human_pattern, text, re.I | re.M):
        return "needs_human", "agent asked the human a question"
    if re.search(c.permission_pattern, err + "\n" + text[:2000], re.I):
        return "needs_human", "agent needs a permission / allowance decision"
    if trailing_question(text):
        return "needs_human", "agent asked the human a question"
    if not text.strip():
        return "error", "empty reply (exit code 0 but no output)"
    return "ok", ""


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.3)
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-tail:]}"


class MonitorRunner:
    #: managed mode: questions in a reply do not pause the run (they go one level up instead)
    questions_pause = True

    def __new__(cls, mon: MonitorConfig, *a: Any, **kw: Any):
        if cls is MonitorRunner and mon.controller.mode == "managed":
            from .managed import ManagedRunner
            return super().__new__(ManagedRunner)
        return super().__new__(cls)

    def __init__(self, mon: MonitorConfig, global_stop: Optional[threading.Event] = None,
                 resume: bool = False, log: Optional[Callable[[str], None]] = None):
        self.mon = mon
        self.global_stop = global_stop or threading.Event()
        self.resume = resume
        self._log = log or (lambda s: print(s, flush=True))
        self.rec: Recorder  # set in run()

    # ------------------------------------------------------------------ plumbing

    def log(self, msg: str) -> None:
        self._log(f"[{self.mon.name}] {msg}")

    def should_stop(self) -> bool:
        return self.global_stop.is_set() or self.rec.stop_requested()

    @property
    def st(self) -> Dict[str, Any]:
        return self.rec.state

    def tpl(self, name: str, **values: Any) -> str:
        m = self.mon
        base = dict(goal=m.goal, instructions=m.controller.instructions or "(none)",
                    cwd=m.working_dir, run_dir=str(self.rec.run_dir), max_rounds=m.max_rounds)
        base.update(values)
        return render(m.controller.templates[name], **base)

    def run(self) -> str:
        m = self.mon
        snap = m.snapshot()
        rec = Recorder.latest(m.output_dir, m.name) if self.resume else None
        if rec is not None and rec.state.get("status") == "completed":
            self.rec = rec
            self.log(f"latest run {rec.run_dir.name} already completed; nothing to resume")
            return "completed"
        if rec is None:
            rec = Recorder.create(m.output_dir, m.name, snap)
            self.rec = rec
            self.log(f"new run → {rec.run_dir}")
        else:
            self.rec = rec
            (rec.run_dir / "STOP").unlink(missing_ok=True)
            rec.event("run_resumed", from_round=rec.state.get("completed_round", 0) + 1)
            rec.progress(f"---\n\n**Resumed** {now_iso()} after round {rec.state.get('completed_round', 0)}")
            self.log(f"resuming {rec.run_dir} after round {rec.state.get('completed_round', 0)}")
            self.switch_backends()
            self.recover_interrupted()
        rec.set(status="running", ended_at=None, error=None)
        answered = False
        try:
            self.snapshot(None)
        except Exception:
            self.log("snapshot failed:\n" + traceback.format_exc())
        self.start_input_pump()
        self.ask_unanswered()
        try:
            status = self.main_loop()
        except StopRequested:
            status = "stopped"
        except NeedsHuman as nh:
            status = "needs_human"
            answered = self.escalate(nh)
        except Exception:
            status = "failed"
            tb = traceback.format_exc()
            rec.set(error=tb)
            rec.event("run_error", error=tb)
            self.log("crashed:\n" + tb)
        rec.set(status=status, ended_at=now_iso())
        rec.event("run_finished", status=status)
        rec.progress(f"**Run finished: {status}** at {now_iso()}")
        rec.refresh_reports()
        self.log(f"finished: {status}  ({rec.run_dir})")
        if status == "needs_human" and answered:
            self.resume = True
            return self.run()
        return status

    def switch_backends(self) -> None:
        """On resume: an agent whose backend changed in its config can't resume its old session (a Claude
        session means nothing to Codex), so it starts a new one, told to pick up from the workspace."""
        for a in self.mon.agents:
            sess = self.st["sessions"].get(a.name) or {}
            was = sess.get("backend")
            if not sess.get("session_id") or not was or was == a.backend:
                continue
            prev = {k: v for k, v in sess.items() if k != "previous"}
            self.rec.update_session(a.name, session_id=None, turns=0, backend=a.backend,
                                    previous=(sess.get("previous") or []) + [prev])
            note = self.tpl("backend_switched", agent=a.called, cwd=a.working_dir).strip()
            q = self.st.setdefault("fwd", {}).setdefault(a.name, [])
            cut = render(self.mon.controller.templates.get("turn_interrupted", "")).strip()
            q[:] = [x for x in q if x != cut]  # "same session, pick up where you were" no longer holds
            if note:
                q.insert(0, note)
            # a turn cut off on the old backend is re-sent in full on the new one, not resumed
            self.rec.event("backend_switched", agent=a.name, old=was, new=a.backend, session_id=sess.get("session_id"))
            self.rec.progress(f"🔁 **{a.name}** moved from `{was}` to `{a.backend}`; it starts a new session")
            self.log(f"{a.name}: backend {was} → {a.backend}, starting a new session")
        self.rec.save()

    def main_loop(self) -> str:
        return self._pipeline() if self.mon.controller.mode == "pipeline" else self._llm()

    def pump_line(self, line: str) -> bool:
        """Hook for live terminal input; return True when the line was handled."""
        return False

    def inbox_broadcast(self, agent: AgentConfig) -> bool:
        """Whether this agent picks up human notes that are not addressed to anyone."""
        return True

    @staticmethod
    def short_questions(text: str, limit: int = 1200) -> str:
        """Just the question part of an agent reply: from the QUESTIONS line to the first blank line."""
        text = (text or "").strip()
        m = re.search(r"^.*(questions? for (the )?(human|me|you))\b.*$", text, re.I | re.M)
        if m:
            tail = text[m.start():]
            paras = tail.split("\n\n")
            text = paras[0]
            if len(paras) > 1 and parse_options(paras[1]) and not parse_options(paras[0]):
                text += "\n" + paras[1]
        else:
            text = trailing_question(text) or text
        return text if len(text) <= limit else text[:limit].rstrip() + " …"

    # ---- live human input: one reader thread owns stdin for the whole run
    def start_input_pump(self) -> None:
        if getattr(self, "_pump", None) or not sys.stdin.isatty():
            return
        import queue
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._awaiting = False
        names = [a.name for a in self.mon.agents]

        def loop() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                except Exception:
                    return
                if not line:
                    self._answers.put("")
                    return
                line = line.strip()
                if self._awaiting:
                    self._answers.put(line)
                    continue
                if not line:
                    continue
                if self.pump_line(line):
                    continue
                to, msg = None, line
                m = re.match(r"@(\w+)\s+(.*)", line)
                if m and m.group(1) in names:
                    to, msg = [m.group(1)], m.group(2)
                inbox = self.rec.run_dir / "inbox"
                inbox.mkdir(exist_ok=True)
                (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 1000:03d}-live.json").write_text(
                    json.dumps({"to": to, "message": msg}), encoding="utf-8")
                self.rec.event("human_live_message", to=to)
                self.log(f"  ✓ queued for {', '.join(to) if to else 'the next agent'}")

        self._pump = threading.Thread(target=loop, daemon=True)
        self._pump.start()
        self.log("Type a message any time and press Enter: it is added to the next agent's input "
                 f"(start with @{names[0]} or @{names[-1]} to address one agent).")

    @staticmethod
    def answer_prompt(q: str, empty: str) -> str:
        if parse_options(q):
            return f"\nPick an option (e.g. 1 or b; several: 1,3) or type your own answer (empty = {empty}): "
        return f"\nYour answer (one line; empty = {empty}): "

    def ask_line(self, prompt: str) -> str:
        """Read one answer line through the pump (stdin is shared with live messages)."""
        self.start_input_pump()
        print(prompt, end="", flush=True)
        self._awaiting = True
        try:
            return self._answers.get().strip()
        finally:
            self._awaiting = False

    # ---- version history of the watched files (for auditing; agents are never told about it)
    def snapshot(self, record: Optional[Dict[str, Any]] = None) -> None:
        c = self.mon.controller
        if not c.snapshot:
            return
        import difflib, shutil
        base = Path(self.mon.working_dir)
        files: Dict[str, Path] = {}
        for item in c.snapshot:
            src = (base / item)
            for f in ([src] if src.is_file() else sorted(src.rglob("*")) if src.is_dir() else []):
                if f.is_file() and not any(x.startswith(".") for x in f.relative_to(base).parts):
                    files[str(f.relative_to(base))] = f
        cur = {k: f.read_bytes() for k, f in files.items()}
        prev = getattr(self, "_snap_prev", None)
        if prev is not None and cur == prev:
            return
        out = self.rec.run_dir / "snapshots"  # inside the run's log folder
        n = len(list(out.glob("v*"))) if out.exists() else 0
        tag = record["turn_id"] if record else "start"
        d = out / f"v{n:03d}-{tag}"
        (d / "files").mkdir(parents=True, exist_ok=True)
        changed = sorted(k for k in cur if prev is None or prev.get(k) != cur[k]) + \
            sorted(k for k in (prev or {}) if k not in cur)
        for k, data in cur.items():
            dst = d / "files" / k
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        diff = []
        for k in changed:
            a = (prev or {}).get(k, b"").decode("utf-8", "replace").splitlines(keepends=True)
            b = cur.get(k, b"").decode("utf-8", "replace").splitlines(keepends=True)
            diff += difflib.unified_diff(a, b, f"before/{k}", f"after/{k}")
        if prev is not None:
            (d / "changes.diff").write_text("".join(diff), encoding="utf-8")
        with (out / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(), "version": d.name, "turn": tag,
                                 "agent": record["agent"] if record else None, "changed": changed}) + "\n")
        self._snap_prev = cur

    def recover_interrupted(self) -> List[Dict[str, Any]]:
        """On resume: turns left 'running' were cut off by the restart. Keep each agent's session (found in
        the live log, or in Claude's own transcript for older runs) and tell the agent to pick up from there."""
        rec, out = self.rec, []
        for p in sorted((rec.run_dir / "rounds").glob("r*/t*.json")):
            if p.name.endswith(".input.json"):
                continue
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(t, dict) or t.get("status") not in ("running", "stopped"):
                continue
            try:
                agent = self.mon.agent(t["agent"])
            except Exception:
                continue
            sid = t.get("session_id") or self._live_session(t["turn_id"]) or self._claude_session(agent, t)
            t.update(status="interrupted", ended_at=now_iso(), session_id=sid)
            rec.write_turn(t)
            if t.get("backend", agent.backend) != agent.backend:
                sid = None  # the agent moved to another backend: its old session can't be resumed
            reset_at = (self.st["sessions"].get(agent.name) or {}).get("reset_at") or ""
            if sid and reset_at and reset_at >= (t.get("started_at") or ""):
                sid = None  # the agent was reset after this turn: start fresh, as the reset asked
            if sid:
                sess = self.st["sessions"].get(agent.name) or {}
                rec.update_session(agent.name, session_id=sid, backend=agent.backend, role=agent.role,
                                   turns=int(sess.get("turns", 0)) + 1, last_turn=t["turn_id"], last_status="interrupted")
                note = self.mon.controller.templates.get("turn_interrupted", "")
                if note:
                    self.st.setdefault("fwd", {}).setdefault(agent.name, []).insert(0, render(note).strip())
            rec.event("turn_recovered", turn=t["turn_id"], agent=agent.name, session_id=sid)
            rec.progress(f"↻ {agent.name}'s turn `{t['turn_id']}` was cut off by the restart; "
                         + ("it continues in the same session" if sid else "its session was lost, so it starts fresh"))
            self.log(f"recovered {t['turn_id']} ({'session ' + sid[:8] if sid else 'no session found'})")
            out.append(t)
        rec.save()
        return out

    def turn_cancel(self, agent: AgentConfig, hit: List[str]) -> Callable[[], bool]:
        """What the backend polls during a turn: a stop, or an urgent message that should cut in now. Sets
        hit[0] to "resume" (the message is for this agent) or "cut" (for the agent that directs it)."""
        last = [0.0]

        def cancel() -> bool:
            if self.should_stop():
                return True
            now = time.monotonic()
            if now - last[0] < 1.0:
                return False
            last[0] = now
            how = self.urgent_hit(agent)
            if how:
                hit[:] = [how]
                return True
            return False
        return cancel

    def urgent_hit(self, agent: AgentConfig) -> Optional[str]:
        notes = self.rec.urgent_notes()
        if any((n["to"] and agent.name in n["to"]) or (not n["to"] and self.inbox_broadcast(agent)) for n in notes):
            return "resume"
        if any(n["to"] and self.yields_to(agent, n["to"]) for n in notes):
            return "cut"
        return None

    def yields_to(self, agent: AgentConfig, to: List[str]) -> bool:
        """Does an urgent message for one of `to` end this agent's running turn?"""
        return False

    def cut_short_note(self, agent: AgentConfig) -> str:
        return self.tpl("urgent_cut_short").strip()

    def _live_session(self, turn_id: str) -> Optional[str]:
        try:
            for ln in (self.rec.run_dir / "live" / f"{turn_id}.jsonl").read_text(encoding="utf-8").splitlines():
                ev = json.loads(ln)
                if ev.get("k") == "session" and ev.get("s"):
                    return str(ev["s"])
        except (OSError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def _claude_session(agent: AgentConfig, t: Dict[str, Any]) -> Optional[str]:
        """Find the Claude transcript whose first message is this turn's prompt."""
        if agent.backend != "claude" or not t.get("input"):
            return None
        import datetime as _dt
        import os as _os
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(agent.working_dir).resolve()))
        d = Path(_os.path.expanduser("~/.claude/projects")) / slug
        try:
            since = _dt.datetime.fromisoformat(t["started_at"]).timestamp() - 5
        except (KeyError, ValueError, TypeError):
            since = 0
        want = str(t["input"]).strip()[:200]
        for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.stat().st_mtime < since:
                break
            try:
                with open(p, encoding="utf-8") as f:
                    for i, ln in enumerate(f):
                        if i > 60:
                            break
                        e = json.loads(ln)
                        if e.get("type") != "user":
                            continue
                        c = (e.get("message") or {}).get("content")
                        c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                        if c.strip().startswith(want):
                            return p.stem
                        break
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def ask_unanswered(self) -> None:
        """On resume: if an agent's question is still open (nothing queued in the inbox), ask it now."""
        asked = self.st.get("asked") or {}
        agent = asked.get("agent")
        if not agent or not sys.stdin.isatty() or list((self.rec.run_dir / "inbox").glob("*.json")):
            return
        turn = (self.st["sessions"].get(agent) or {}).get("last_turn")
        f = self.rec.run_dir / "rounds" / f"r{int(asked['round']):03d}" / f"{turn}.json"
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return
        self.log("\n" + "!" * 60 + f"\n{agent} asks:\n\n{self.short_questions(rec.get('output') or '')}\n" + "!" * 60)
        try:
            q = self.short_questions(rec.get("output") or "")
            ans = resolve_choice(self.ask_line(self.answer_prompt(q, "skip")), parse_options(q))
        except EOFError:
            return
        if ans:
            inbox = self.rec.run_dir / "inbox"
            inbox.mkdir(exist_ok=True)
            (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-answer.json").write_text(
                json.dumps({"to": [agent], "message": ans}), encoding="utf-8")
            self.rec.event("human_answer", turn=rec.get("turn_id"))

    def escalate(self, nh: NeedsHuman) -> bool:
        """Show the (short) question, collect the answer in the terminal, queue it in the inbox.
        Returns True when the human answered and the run can continue straight away."""
        r = nh.record
        q = self.short_questions(r.get("output") or "")
        tail = (r.get("stderr") or "")[-600:].strip()
        body = (f"# Human needed: {r['agent']} ({nh.reason})\n\n{q or '(empty)'}\n\n"
                + (f"stderr tail:\n```\n{tail}\n```\n\n" if tail else "")
                + f"Full reply: `{r['turn_id']}` in report.html. Answer with `orch inject` or just resume.\n")
        (self.rec.run_dir / "NEEDS_HUMAN.md").write_text(body, encoding="utf-8")
        self.rec.event("needs_human", turn=r["turn_id"], reason=nh.reason)
        self.rec.set(error=f"{r['agent']}: {nh.reason}")
        self.log("\n" + "!" * 60 + f"\nHUMAN NEEDED — {r['agent']}: {nh.reason}\n\n{q}\n" + "!" * 60)
        if not sys.stdin.isatty():
            return False
        try:
            ans = resolve_choice(self.ask_line(self.answer_prompt(q, "stop here")), parse_options(q))
        except EOFError:
            return False
        if not ans:
            return False
        inbox = self.rec.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-answer.json").write_text(
            json.dumps({"to": [r["agent"]], "message": ans}),
            encoding="utf-8")
        self.rec.event("human_answer", turn=r["turn_id"])
        return True

    # ------------------------------------------------------------------ turns

    def turn(self, agent: AgentConfig, prompt: str, rnd: int, purpose: str,
             seq: Optional[int] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.should_stop():
            raise StopRequested()
        rec = self.rec
        human = self.human_text(agent.name, rec.take_inbox(agent.name, broadcast=self.inbox_broadcast(agent)))
        if human:  # queued with the relayed messages, so a restart mid-turn re-sends it instead of losing it
            self.st.setdefault("fwd", {}).setdefault(agent.name, []).append(human)
            rec.save()
        fwd = list((self.st.get("fwd") or {}).get(agent.name) or [])  # answers / notes relayed, and my messages
        late = "\n\n".join(x for x in fwd if x)
        if late:
            prompt = f"{late}\n\n{prompt}" if prompt.strip() else late
        seq = seq or rec.next_seq(rnd)
        sess = self.st["sessions"].get(agent.name) or {}
        sid = sess.get("session_id")
        record: Dict[str, Any] = {
            "turn_id": rec.turn_id(rnd, seq, agent.name),
            "round": rnd, "seq": seq, "agent": agent.name, "role": agent.role, "purpose": purpose,
            "backend": agent.backend, "cwd": agent.working_dir,
            "session_id": sid, "resumed_session": bool(sid), "session_turn": int(sess.get("turns", 0)) + 1,
            "status": "running", "started_at": now_iso(), "ended_at": None, "duration_s": None,
            "system_prompt": agent.current_system_prompt(), "input": prompt, "output": "",
            **(extra or {}),
        }
        rec.write_turn(record)  # relayed messages stay queued until the turn ends, so a restart re-sends them
        self.log(f"r{rnd} ▶ {agent.name} ({purpose}{', resume ' + sid[:8] if sid else ', new session'})")
        backend = get_backend(agent.backend)
        t0 = time.monotonic()
        res: TurnResult = TurnResult("error", "", sid)
        attempts: List[Dict[str, Any]] = []
        reason = ""
        lock = rec.agent_lock(agent.name)
        lock.__enter__()
        hit: List[str] = []
        cancel = self.turn_cancel(agent, hit)
        attempt, urgent_send = 0, ""
        while attempt <= agent.retries:
            send = prompt
            if urgent_send:  # an urgent message cut in: the same session carries on with it
                send, urgent_send = urgent_send, ""
            elif attempt and res.session_id:  # correction: tell the agent what went wrong in its own session
                send = (f"Your previous reply was unusable ({reason or res.status}). "
                        f"Please redo the last request and reply with text.\n\n{prompt}")
            hit.clear()
            req = TurnRequest(
                agent=agent.name, role=agent.role, prompt=send, system_prompt=record["system_prompt"],
                session_id=sid, cwd=agent.working_dir, timeout=agent.timeout,
                options=agent.backend_options, env=agent.env,
                context={"round": rnd, "purpose": purpose, "monitor": self.mon.name,
                         "roster": [a.name for a in self.mon.agents], **(extra or {})},
                on_event=lambda ev, tid=record["turn_id"]: rec.live(tid, ev),
            )
            try:
                res = backend.run(req, cancel)
            except Exception:
                res = TurnResult("error", "", sid, None, traceback.format_exc(), [], {})
            if res.status == "stopped" and hit and not self.should_stop():
                if hit[0] == "resume":  # the message is for this agent: it continues, with the message first
                    note = self.human_text(agent.name, rec.take_inbox(agent.name, broadcast=self.inbox_broadcast(agent)))
                    sid = res.session_id or self._live_session(record["turn_id"]) or sid
                    if note:  # queued like any message until the turn ends, so a restart re-sends it
                        self.st.setdefault("fwd", {}).setdefault(agent.name, []).append(note)
                        fwd.append(note)
                        prompt = f"{note}\n\n{prompt}"  # what a correction or a fresh session re-sends
                    urgent_send = self.tpl("urgent_resume", note=note or "(no text)") if sid else prompt
                    record.setdefault("interruptions", []).append({"at": now_iso(), "message": note})
                    record["session_id"] = sid
                    rec.write_turn(record)
                    rec.event("urgent", turn=record["turn_id"], how="resume")
                    rec.progress(f"⚡ `{record['turn_id']}` **{agent.name}**: an urgent message cut in; it carries on with it")
                    self.log(f"r{rnd} ⚡ {agent.name}: urgent message, continuing in the same turn")
                    continue
                # the message is for the agent that directs this one: end this turn so that agent acts now
                cut = self.cut_short_note(agent)
                res = dataclasses.replace(res, status="ok", output=((res.output or "").rstrip() + "\n\n" + cut).strip())
                record["cut_short"] = True
                rec.event("urgent", turn=record["turn_id"], how="cut_short")
                rec.progress(f"⚡ `{record['turn_id']}` **{agent.name}**: ended early for an urgent message")
                self.log(f"r{rnd} ⚡ {agent.name}: ended early for an urgent message")
            st_, reason = classify(self.mon.controller, res)
            if st_ != res.status:
                res = dataclasses.replace(res, status=st_)
            attempts.append({"attempt": attempt + 1, "status": res.status, "exit_code": res.exit_code,
                             "reason": reason})
            if res.status == "needs_human" and not self.questions_pause:
                res = dataclasses.replace(res, status="ok")  # handled by the caller (goes one level up)
                record["asked"] = reason
            if res.status in ("ok", "stopped", "needs_human", "timeout") or self.should_stop():
                break  # a timed-out turn is not re-run from scratch (it may have been running a long job)
            self.log(f"r{rnd} ✗ {agent.name} attempt {attempt + 1} → {res.status}")
            attempt += 1
        lock.__exit__(None, None, None)
        if fwd and res.status != "stopped" and not self.should_stop():  # a stopped turn is re-sent on resume
            left = (self.st.get("fwd") or {}).get(agent.name) or []
            if left[:len(fwd)] == fwd:
                del left[:len(fwd)]
            if not left:
                (self.st.get("fwd") or {}).pop(agent.name, None)
        dur = round(time.monotonic() - t0, 2)
        if not res.session_id:  # e.g. stopped before the final JSON: the stream already named the session
            res = dataclasses.replace(res, session_id=self._live_session(record["turn_id"]) or sid)
        record.update(
            status=res.status, output=res.output, session_id=res.session_id or sid,
            exit_code=res.exit_code, stderr=res.stderr[-6000:], command=res.command, meta=res.meta,
            attempts=attempts, ended_at=now_iso(), duration_s=dur,
        )
        if res.session_id:
            rec.update_session(agent.name, session_id=res.session_id, backend=agent.backend, role=agent.role,
                               turns=int(sess.get("turns", 0)) + 1, last_turn=record["turn_id"],
                               last_status=res.status)
        rec.write_turn(record)
        try:
            self.snapshot(record)
        except Exception:
            self.log("snapshot failed:\n" + traceback.format_exc())
        rec.progress(f"- `{record['turn_id']}` **{agent.name}** · {purpose} → **{res.status}** ({dur}s)")
        self.log(f"r{rnd} ■ {agent.name} → {res.status} in {dur}s")
        if res.status == "stopped":
            raise StopRequested()
        if res.status == "ok" and (self.st.get("asked") or {}).get("agent") == agent.name:
            rec.set(asked=None)  # the question has been dealt with
        if res.status == "needs_human":
            rec.set(asked={"round": rnd, "agent": agent.name})  # so the answer goes back to this agent alone
            raise NeedsHuman(record, reason)
        record["reason"] = reason
        return record

    def stage(self, items: List[Tuple[AgentConfig, str, str]], rnd: int, parallel: bool) -> List[Dict[str, Any]]:
        """Run several turns, in order or concurrently. Returns records in item order."""
        if not items:
            return []
        seqs = [self.rec.next_seq(rnd) for _ in items]
        if not parallel or len(items) == 1:
            return [self.turn(a, p, rnd, purpose, seq) for (a, p, purpose), seq in zip(items, seqs)]
        results: List[Any] = [None] * len(items)
        stopped, human = False, None
        with ThreadPoolExecutor(max_workers=len(items)) as ex:
            futs = [ex.submit(self.turn, a, p, rnd, purpose, seq) for (a, p, purpose), seq in zip(items, seqs)]
            for i, f in enumerate(futs):
                try:
                    results[i] = f.result()
                except StopRequested:
                    stopped = True
                except NeedsHuman as e:
                    human = human or e
        if human:
            raise human
        if stopped:
            raise StopRequested()
        return results

    # ------------------------------------------------------------------ helpers

    def report_text(self, r: Optional[Dict[str, Any]]) -> str:
        if not r:
            return "(no report yet)"
        body = truncate(r.get("output") or "(empty output)", self.mon.controller.max_report_chars)
        if r.get("status") != "ok" and r.get("stderr"):
            body += "\n\nstderr (tail):\n```\n" + r["stderr"][-1500:] + "\n```"
        return body

    @staticmethod
    def human_text(agent: Optional[str], msgs: List[Dict[str, Any]], annotate: bool = False) -> str:
        picked = [x for x in msgs if agent is None or x["to"] is None or agent in x["to"]]
        if not picked:
            return ""
        parts = []
        for x in picked:
            hdr = ""
            if annotate and x["to"]:
                hdr = f"(for {', '.join(x['to'])}) "
            parts.append(f"{hdr}{x['message']}")
        return "\n\n".join(parts)

    def begin_round(self, rnd: int) -> Dict[str, Any]:
        summary = {"round": rnd, "status": "running", "started_at": now_iso(), "ended_at": None, "turns": []}
        self.rec.set(round=rnd)
        self.rec.write_round(rnd, summary)
        self.rec.progress(f"## Round {rnd}")
        return summary

    def end_round(self, summary: Dict[str, Any], decision: str, **extra: Any) -> None:
        summary.update(status="done", ended_at=now_iso(), decision=decision, **extra)
        self.rec.write_round(summary["round"], summary)
        self.rec.progress(f"**Round {summary['round']} decision:** {decision}")

    def verdict(self, r: Dict[str, Any]) -> str:
        if r["status"] != "ok":
            return "ERROR"
        text = r.get("output") or ""
        c = self.mon.controller
        p = [m.end() for m in re.finditer(c.pass_pattern, text, re.I)]
        f = [m.end() for m in re.finditer(c.fail_pattern, text, re.I)]
        if not p and not f:
            return "UNKNOWN"
        return "PASS" if (max(p) if p else -1) > (max(f) if f else -1) else "FAIL"

    # ------------------------------------------------------------------ pipeline mode

    def _pipeline(self) -> str:
        m, c = self.mon, self.mon.controller
        workers = m.by_role("worker")
        verifiers = m.by_role("verifier")
        all_workers = [w.name for w in workers]
        start = int(self.st.get("completed_round", 0)) + 1
        pending: Dict[str, str] = dict(self.st.get("pending") or {})
        last: Dict[str, Dict[str, Any]] = dict(self.st.get("last_reports") or {})

        for rnd in range(start, m.max_rounds + 1):
            summary = self.begin_round(rnd)
            human = self.rec.take_inbox()

            # --- workers
            items = []
            for w in workers:
                hm = self.human_text(w.name, human)
                has_session = bool((self.st["sessions"].get(w.name) or {}).get("session_id"))
                if not has_session:
                    prompt = self.tpl("worker_initial", agent=w.name, role=w.role, description=w.description,
                                      task=w.task or "Work toward the overall goal.", round=rnd,
                                      human_messages=hm, cwd=w.working_dir)
                elif hm and (self.st.get("asked") or {}) == {"round": rnd, "agent": w.name}:
                    prompt = hm  # the agent asked a question last time: just hand over the answer
                    if c.answer_note:  # the reviewers should know what I decided, too
                        self.rec.set(forward=(self.st.get("forward") or []) + [c.answer_note.replace("{answer}", hm)])
                elif w.name in pending or hm:
                    prompt = self.tpl("worker_feedback", round=rnd, human_messages=hm, cwd=w.working_dir,
                                      feedback=pending.get(w.name) or "(no verifier feedback for you this round)")
                else:
                    continue  # nothing new for this worker
                items.append((w, prompt, "work"))
            if self.st.get("workers_done") == rnd:
                wrecs = []  # resuming mid-round (e.g. after a question to the human): workers already did this round
                self.log(f"r{rnd}: workers already finished this round; going straight to review")
            else:
                wrecs = self.stage(items, rnd, c.parallel)
                if all(r["status"] == "ok" for r in wrecs):
                    self.rec.set(workers_done=rnd, asked=None)
            for r in wrecs:
                last[r["agent"]] = {k: r.get(k) for k in ("turn_id", "status", "output", "stderr")}
            summary["turns"] += [r["turn_id"] for r in wrecs]
            errors = [r for r in wrecs if r["status"] != "ok"]
            if errors and c.stop_on_error:
                self.st["last_reports"] = last
                self.end_round(summary, "worker error → stop", errors=[r["turn_id"] for r in errors])
                return "failed"

            if not verifiers:
                self.st["completed_round"] = rnd
                self.end_round(summary, "no verifiers → done" if not errors else "worker error, no verifiers")
                return "completed" if not errors else "failed"

            # --- verifiers
            vitems = []
            for v in verifiers:
                targets = v.reviews or all_workers
                reports = "\n\n".join((f"From {t}:\n" if len(targets) > 1 else "") + self.report_text(last.get(t))
                                       for t in targets)
                has_session = bool((self.st["sessions"].get(v.name) or {}).get("session_id"))
                name = "verifier_followup" if has_session else "verifier_initial"
                vhm = "\n\n".join(x for x in [(self.st.get("nudge") or {}).get(v.name, ""),
                                              *(self.st.get("forward") or []),
                                              self.human_text(v.name, human)] if x)
                vitems.append((v, self.tpl(name, agent=v.name, role=v.role, description=v.description,
                                           round=rnd, worker_reports=reports, cwd=v.working_dir,
                                           human_messages=vhm), "verify"))
            vrecs = self.stage(vitems, rnd, c.parallel)
            if all(r["status"] == "ok" for r in vrecs):
                self.rec.set(forward=None)  # forwarded answers have reached every reviewer
            summary["turns"] += [r["turn_id"] for r in vrecs]
            verdicts = {r["agent"]: self.verdict(r) for r in vrecs}
            summary["verdicts"] = verdicts
            bad = [r for r in vrecs if verdicts[r["agent"]] in ("ERROR", "UNKNOWN")]
            if bad and c.stop_on_error:
                self.st["last_reports"] = last
                self.end_round(summary, "verifier error / no verdict → stop", errors=[r["turn_id"] for r in bad])
                return "failed"
            if c.checklist:
                nudges = {}
                for r in vrecs:
                    if verdicts[r["agent"]] != "PASS":
                        continue
                    done, open_ = checklist_state(r.get("output") or "")
                    if open_ or not done:
                        verdicts[r["agent"]] = "FAIL"
                        nudges[r["agent"]] = c.checklist_nudge
                        self.rec.progress(f"{r['agent']} said PASS but its checklist has {open_} open / {done} done "
                                          f"items → counted as FAIL")
                summary["verdicts"] = verdicts
                if nudges:
                    summary["checklist_overruled"] = sorted(nudges)
                self.rec.set(nudge=nudges or None)
            self.rec.progress("Verdicts: " + ", ".join(f"{k} **{v}**" for k, v in verdicts.items()))
            vals = list(verdicts.values())
            passed = all(x == "PASS" for x in vals) if c.require == "all" else any(x == "PASS" for x in vals)
            if passed and not errors:
                self.st["completed_round"] = rnd
                self.st["pending"] = {}
                self.end_round(summary, "verifiers passed → goal met")
                return "completed"

            # --- route feedback
            routed: Dict[str, List[str]] = {}
            for r in vrecs:
                vd = verdicts[r["agent"]]
                if vd in ("PASS", "ERROR"):
                    continue  # errored verifiers simply re-run next round
                v = m.agent(r["agent"])
                for t in (v.feedback_to or v.reviews or all_workers):
                    fb = re.sub(r"\n*\s*VERDICT:\s*\w+\s*$", "", r.get("output") or "", flags=re.I).strip()
                    if c.checklist:  # the checklist is the reviewer's own; the worker gets the prose
                        prose = strip_checklist(fb)
                        fb = prose or "\n".join("- " + _BOX.sub("", ln).strip()
                                                for ln in fb.split("\n") if _BOX.match(ln) and "[ ]" in ln)
                    routed.setdefault(t, []).append(truncate(fb, c.max_report_chars))
            for r in errors:
                routed.setdefault(r["agent"], []).append(
                    f"## Your previous turn ended with status `{r['status']}`\n"
                    f"Check the workspace state and continue the task.\n```\n{(r.get('stderr') or '')[-1500:]}\n```")
            pending = {k: "\n\n".join(v) for k, v in routed.items()}
            summary["routing"] = {k: len(v) for k, v in routed.items()}
            self.st["pending"] = pending
            self.st["last_reports"] = last
            self.st["completed_round"] = rnd
            self.end_round(summary, "feedback → " + (", ".join(sorted(pending)) or "re-verify"))
        return "max_rounds_reached"

    # ------------------------------------------------------------------ llm mode

    def roster_text(self) -> str:
        lines = []
        for a in self.mon.agents:
            desc = f": {a.description}" if a.description else ""
            lines.append(f"- **{a.name}** — role `{a.role}`, backend `{a.backend}`{desc}")
        return "\n".join(lines)

    def parse_directive(self, text: str) -> Tuple[Optional[Dict[str, Any]], str]:
        blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
        cands = list(reversed(blocks))
        if "{" in text:
            cands.append(text[text.find("{"): text.rfind("}") + 1])
        data, err = None, "no JSON object found"
        for cand in cands:
            try:
                d = json.loads(cand)
            except json.JSONDecodeError as e:
                err = f"invalid JSON: {e}"
                continue
            if isinstance(d, dict):
                data = d
                break
        if data is None:
            return None, err
        names = {a.name for a in self.mon.agents}
        dispatch = data.get("dispatch") or []
        if not isinstance(dispatch, list):
            return None, "`dispatch` must be a list"
        clean = []
        for d in dispatch:
            if not isinstance(d, dict) or "agent" not in d or "message" not in d:
                return None, "each dispatch entry needs `agent` and `message`"
            if d["agent"] not in names:
                return None, f"unknown agent {d['agent']!r}; roster: {', '.join(sorted(names))}"
            clean.append({"agent": str(d["agent"]), "message": str(d["message"])})
        data["dispatch"] = clean
        data["done"] = bool(data.get("done"))
        if not clean and not data["done"]:
            return None, "dispatch is empty but done is false — dispatch at least one agent or set done: true"
        return data, ""

    def _llm(self) -> str:
        m, c = self.mon, self.mon.controller
        ctrl = dataclasses.replace(
            c.agent,  # type: ignore[arg-type]
            system_prompt=(render(c.templates["controller_system"]).strip()
                           + ("\n\n" + c.agent.current_system_prompt() if c.agent and c.agent.current_system_prompt() else "")),
            system_prompt_path="", memory_files=[],
        )
        start = int(self.st.get("completed_round", 0)) + 1
        results_text = (self.st.get("pending") or {}).get("__controller__", "")

        for rnd in range(start, m.max_rounds + 1):
            summary = self.begin_round(rnd)
            human = self.rec.take_inbox()
            hm = self.human_text(None, human, annotate=True)
            has_session = bool((self.st["sessions"].get(ctrl.name) or {}).get("session_id"))
            if not has_session:
                prompt = self.tpl("controller_initial", roster=self.roster_text(), round=rnd, human_messages=hm)
            else:
                prompt = self.tpl("controller_update", round=rnd, human_messages=hm,
                                  results=results_text or "(no results)")
            crec = self.turn(ctrl, prompt, rnd, "control")
            summary["turns"].append(crec["turn_id"])
            directive, err = (None, f"controller turn status {crec['status']}")
            if crec["status"] == "ok":
                directive, err = self.parse_directive(crec["output"])
                if directive is None:
                    fix = (f"Your previous reply could not be used: {err}.\n"
                           "Reply again with only the JSON directive in a ```json block.")
                    crec = self.turn(ctrl, fix, rnd, "control-repair")
                    summary["turns"].append(crec["turn_id"])
                    if crec["status"] == "ok":
                        directive, err = self.parse_directive(crec["output"])
            if directive is None:
                summary["error"] = err
                if c.stop_on_error:
                    self.end_round(summary, f"controller error → stop ({err})")
                    return "failed"
                results_text = f"(Your previous directive was unusable: {err}. No agents ran.)"
                self.st.setdefault("pending", {})["__controller__"] = results_text
                self.st["completed_round"] = rnd
                self.end_round(summary, f"controller error: {err}")
                continue

            summary["directive"] = directive
            if directive.get("summary"):
                self.rec.progress(f"> **Controller:** {directive['summary']}")
            if directive["done"]:
                self.st["final_report"] = directive.get("final_report", "")
                self.st["completed_round"] = rnd
                self.st["pending"] = {}
                if directive.get("final_report"):
                    self.rec.progress(f"### Final report\n{directive['final_report']}")
                self.end_round(summary, "controller declared done")
                return "completed"

            items = []
            for d in directive["dispatch"]:
                a = m.agent(d["agent"])
                has_s = bool((self.st["sessions"].get(a.name) or {}).get("session_id"))
                prompt = self.tpl("agent_followup" if has_s else "agent_initial", agent=a.name, role=a.role,
                                  description=a.description, round=rnd, message=d["message"], cwd=a.working_dir)
                items.append((a, prompt, "dispatch"))
            names = [a.name for a, _, _ in items]
            parallel = c.parallel and bool(directive.get("parallel")) and len(set(names)) == len(names)
            recs = self.stage(items, rnd, parallel)
            summary["turns"] += [r["turn_id"] for r in recs]
            summary["dispatched"] = names
            results_text = "\n\n".join(f"## {r['agent']} — {r['status']}\n{self.report_text(r)}" for r in recs)
            self.st.setdefault("pending", {})["__controller__"] = results_text
            self.st["completed_round"] = rnd
            errors = [r for r in recs if r["status"] != "ok"]
            if errors and c.stop_on_error:
                self.end_round(summary, "agent error → stop")
                return "failed"
            self.end_round(summary, "dispatched → " + ", ".join(names))
        return "max_rounds_reached"
