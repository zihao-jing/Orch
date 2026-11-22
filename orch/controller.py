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

from .backends import (StopRequested, TurnRequest, TurnResult, backend_executable, claude_stream_report, get_backend,
                       missing_executable)
from .config import HOT_AGENT_FIELDS, RUN_PROMPT_VARS, AgentConfig, MonitorConfig, load_config
from .recorder import Recorder, now_iso, read_json
from . import jobs as _jobs
from . import tags as T
from .report import CHECKBOX, cost_basis, endpoint_host
from .templates import render
from .timing import TurnTiming

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
    """Return (status, reason). A successful turn may finish tool work without a text reply."""
    if res.status != "ok":
        return res.status, ""
    text, err = res.output or "", res.stderr or ""
    if re.search(c.human_pattern, text, re.I | re.M):
        return "needs_human", "agent asked the human a question"
    if re.search(c.permission_pattern, err + "\n" + text[:2000], re.I):
        return "needs_human", "agent needs a permission / allowance decision"
    if trailing_question(text):
        return "needs_human", "agent asked the human a question"
    if not text.strip() and not res.meta.get("tools_completed"):
        return "error", "empty reply (exit code 0 but no output)"
    return "ok", ""


# A refusal from the provider, reported by the backend as a failed turn: its text is the provider's message, not an answer.
PROVIDER_ERROR = re.compile(r"^\s*API Error\b|can't receive replies|portal\.anthropic\.com/verify|overloaded_error|rate[ _]limit",
                            re.I | re.M)


def provider_error(res: TurnResult) -> str:
    """The provider's message when a failed turn carries one; '' for anything else."""
    if res.status != "error":
        return ""
    text = (res.output or "")[:2000]
    if not PROVIDER_ERROR.search(text):
        return ""
    return truncate((text.strip().splitlines() or [""])[0], 300)


# B8: a billing or account problem: no retry or backup fixes it; the run stops and the user is told
BILLING_ERROR = re.compile(r"credit balance is too low|insufficient[_ ]quota|exceeded your current quota|"
                           r"billing|payment required|purchase (more )?credits|"
                           r"account (has been |is )?(suspended|disabled|deactivated)", re.I)


def billing_error(res: TurnResult) -> str:
    """The provider's message when a failed turn is a billing or account problem; '' otherwise."""
    if res.status != "error":
        return ""
    # the agent's own partial reply (a cut-off) is its work, not a provider message: only the CLI's error is read
    said = "" if (res.meta or {}).get("partial") else (res.output or "")[:2000]
    text = said + "\n" + (res.stderr or "")[-2000:]
    m = BILLING_ERROR.search(text)
    if not m:
        return ""
    line = next((ln for ln in text.splitlines() if BILLING_ERROR.search(ln)), m.group(0))
    return truncate(line.strip(), 300)


def _cut_input(sess: Dict[str, Any], t: Dict[str, Any]) -> str:
    """What the session holds of a cut-off turn's message (a cut-off "continue" still holds the message before it)."""
    before = (sess.get("cut_off") or {}).get("input") or "" if t.get("continues") else ""
    return (before + "\n\n" + (t.get("input") or "")).strip()


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


LARGE_TEXT = 50_000  # a prompt or reply longer than this is flagged in the progress log (never cut)


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.3)
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-tail:]}"


def relayed(text: str, limit: int) -> str:
    """What of an agent's reply another agent receives: its <instruction> blocks whole when it wrote any, otherwise
    the reply cut to `limit` characters (the start and the end, with the number of characters left out)."""
    blocks = T.bodies(text, "instruction")
    if blocks:
        return "\n\n".join(b.strip() for b in blocks)
    return truncate(text, limit)


class MonitorRunner:
    #: managed mode: questions in a reply do not pause the run (they go one level up instead)
    questions_pause = True

    def __new__(cls, mon: MonitorConfig, *a: Any, **kw: Any):
        if cls is MonitorRunner and mon.controller.mode == "managed":
            from .managed import ManagedRunner
            return super().__new__(ManagedRunner)
        return super().__new__(cls)

    def __init__(self, mon: MonitorConfig, global_stop: Optional[threading.Event] = None,
                 resume: bool = False, log: Optional[Callable[[str], None]] = None,
                 max_rounds_given: bool = False):
        self.mon = mon
        self.max_rounds_given = max_rounds_given  # --max-rounds on the command line: it wins over the run's own
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
            rec.claim()
            self.log(f"new run → {rec.run_dir}")
        else:
            self.rec = rec
            holder = rec.claim()
            if holder:
                # Another controller drives this run. Touching its state or answering its gates would duplicate
                # turns and overwrite its status; the board (if served) still works through the run folder.
                self.log(f"{rec.run_dir.name} is already being run by {holder}; not starting a second controller "
                         f"(stop that one first to resume here)")
                return "busy"
            self.keep_max_rounds()
            (rec.run_dir / "STOP").unlink(missing_ok=True)
            rec.event("run_resumed", from_round=rec.state.get("completed_round", 0) + 1)
            rec.progress(f"---\n\n**Resumed** {now_iso()} after round {rec.state.get('completed_round', 0)}")
            self.log(f"resuming {rec.run_dir} after round {rec.state.get('completed_round', 0)}")
            self.switch_backends()
            self.recover_interrupted()
        rec.set(status="running", ended_at=None, error=None, stop_reason=None)
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
        rec.release()
        if status == "needs_human" and answered:
            self.resume = True
            return self.run()
        return status

    def keep_max_rounds(self) -> None:
        """On resume: the round limit is the run's own (recorded in state.json), so a resume without --max-rounds
        does not fall back to a lower config value. A higher config value still raises it; --max-rounds sets it
        either way. The limit in force is written back to state.json."""
        m, rec = self.mon, self.rec
        stored = rec.state.get("max_rounds")
        limit = m.max_rounds
        if not self.max_rounds_given and isinstance(stored, int) and stored > limit:
            self.log(f"round limit {stored} kept from this run (config says {limit}; use --max-rounds to change it)")
            limit = stored
        if limit != m.max_rounds:
            self.mon = dataclasses.replace(m, max_rounds=limit)
        if stored != limit:
            rec.set(max_rounds=limit)

    def switch_backends(self) -> None:
        """On resume: an agent whose backend changed in its config can't resume its old session (a Claude
        session means nothing to Codex), so it starts a new one, told to pick up from the workspace."""
        for a in self.mon.agents:
            sess = self.st["sessions"].get(a.name) or {}
            was = sess.get("backend")
            if not sess.get("session_id") or not was or was in {v.backend for v in a.variants()}:
                continue  # still one of its models (a backup may be in use); a turn on another provider starts afresh
            prev = {k: v for k, v in sess.items() if k not in ("previous", "resets", "cut_off")}
            self.rec.update_session(a.name, session_id=None, turns=0, backend=a.backend,
                                    previous=(sess.get("previous") or []) + [prev], cut_off=None)
            self.note_session(a.name, f"backend changed ({was} → {a.backend})", previous=prev.get("session_id"))
            note = self.tpl("backend_switched", agent=a.called, cwd=a.working_dir).strip()
            q = self.st.setdefault("fwd", {}).setdefault(a.name, [])
            cut = render(self.mon.controller.templates.get("turn_interrupted", "")).strip()
            q[:] = [x for x in q if x != cut]  # "same session, pick up where you were" no longer holds
            if note:
                q.insert(0, note)
            # a turn cut off on the old backend is re-sent in full on the new one, not resumed
            self.rec.event("backend_switched", agent=a.name, old=was, new=a.backend, session_id=prev.get("session_id"))
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
        rec.state.pop("auditing", None)  # an audit cut off by the restart is run again later; not shown as running
        rec.recover_worker_messages()
        latest = {}
        for p in sorted((rec.run_dir / "rounds").glob("r*/t*.json")):
            if p.name.endswith(".input.json"):
                continue
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(t, dict) and t.get("agent"):
                latest[t["agent"]] = t
        # Older builds could skip parallel copies. Never replay an old stopped turn after newer work.
        for t in latest.values():
            if t.get("status") not in ("running", "stopped"):
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
            reached = bool(sid) and self._reached_session(agent, t)
            if sid:
                sess = self.st["sessions"].get(agent.name) or {}
                rec.update_session(agent.name, session_id=sid, backend=agent.backend, role=agent.role,
                                   turns=int(sess.get("turns", 0)) + 1, last_turn=t["turn_id"], last_status="interrupted",
                                   cut_off={"turn": t["turn_id"], "input": _cut_input(sess, t)} if reached else None)
                note = self.mon.controller.templates.get("turn_interrupted", "")
                if note and not reached:  # not sure the message got there: it is sent again, with a note
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

    def watched(self, agent: AgentConfig) -> bool:
        """Do progress checks watch this agent's turns (then only its safety limit stops them; managed mode)?"""
        return False

    def yields_to(self, agent: AgentConfig, to: List[str]) -> bool:
        """Does an urgent message for one of `to` end this agent's running turn?"""
        return False

    def cut_short_note(self, agent: AgentConfig) -> str:
        return self.tpl("urgent_cut_short").strip()

    def _reached_session(self, agent: AgentConfig, t: Dict[str, Any]) -> bool:
        """Did a cut-off turn's message reach its session? Then the session already holds it (Claude Code writes the
        prompt to the transcript before it starts work), and the turn is resumed with "continue", not sent again.
        Evidence: the backend reported anything for the turn (the live log), or a Claude transcript starts with it."""
        try:
            if any(ln.strip() for ln in (self.rec.run_dir / "live" / f"{t['turn_id']}.jsonl")
                   .read_text(encoding="utf-8").splitlines()):
                return True
        except OSError:
            pass
        return bool((t.get("output") or "").strip()) or bool(self._claude_session(agent, t))

    def note_session(self, agent: str, reason: str, change: str = "reset", **info: Any) -> None:
        """A session reset (or another change to an agent's session) and why: kept on the session until its next
        turn records it (`session_reset` in the turn record), logged as an event and shown on the board as a small
        system line."""
        entry = {"reason": reason, "change": change, "at": now_iso(), **{k: v for k, v in info.items() if v is not None}}
        with self.rec.lock:
            s = self.st["sessions"].setdefault(agent, {"session_id": None, "turns": 0})
            s.setdefault("resets", []).append(entry)
        self.rec.event("session_note", agent=agent, **entry)
        icon = {"compact": "🗜", "model": "🔀"}.get(change, "↺")
        self.rec.progress(f"{icon} {agent}: " + (reason if change != "reset" else f"new session ({reason})"))
        self.rec.save()

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
        if not agent:
            return
        answered = False  # an answer for this agent already waiting in the inbox (unrelated files don't count)
        for p in (self.rec.run_dir / "inbox").glob("*.json"):
            try:
                to = (read_json(p, {}) or {}).get("to") or []
            except Exception:
                continue
            if agent in ([to] if isinstance(to, str) else to):
                answered = True
        if answered:
            return
        if not sys.stdin.isatty():  # nobody to ask here: say so in the log, so the wait is visible after a restart
            msg = (f"⏸ waiting for your answer for {agent} (see NEEDS_HUMAN.md): "
                   f"orch inject <run> \"<answer>\" --to {agent}")
            self.rec.progress(msg)
            self.log(msg)
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

    # ------------------------------------------------------------------ settings changed during a run

    def refresh_agent(self, agent: AgentConfig) -> None:
        """Before an agent's turn: re-read the config file and apply changed hot settings (backend, model options,
        env, timeout, backups) to this agent. Runs between turns, so nothing running is interrupted. A config that
        does not load (e.g. saved half-way) is reported and the current settings are kept."""
        rec = getattr(self, "rec", None)
        if rec is None or not self.mon.source:
            return

        def note(key: str, text: str) -> None:
            if getattr(self, "_config_notes", {}).get(agent.name) != key:  # say each problem once, not every turn
                self._config_notes = {**getattr(self, "_config_notes", {}), agent.name: key}
                rec.progress(f"⚠ **{agent.name}**: {text}")
                rec.event("config_problem", agent=agent.name, problem=text)
                self.log(f"{agent.name}: {text}")

        try:
            cfg = load_config(self.mon.source)
        except Exception as e:  # ConfigError, YAML syntax errors, unreadable files: keep what the run has
            note(f"err {type(e).__name__}: {e}", f"config not re-read ({type(e).__name__}: {e}); "
                                                 "keeping the settings it had")
            return
        try:
            new_mon = next(m for m in cfg.monitors if m.name == self.mon.name)
            self.refresh_reminders(new_mon.controller.reminders)
            new = new_mon.agent(agent.name)
        except (StopIteration, KeyError):
            note("gone", f"{agent.name} is not in the config any more; keeping its settings")
            return

        for k in RUN_PROMPT_VARS:  # set by the run itself (the golden board's path), not by the config
            if k in agent.prompt_vars and k not in new.prompt_vars:
                new.prompt_vars[k] = agent.prompt_vars[k]
        changed = {f: (getattr(agent, f), getattr(new, f)) for f in HOT_AGENT_FIELDS
                   if getattr(agent, f) != getattr(new, f)}
        cold = [f.name for f in dataclasses.fields(AgentConfig)
                if f.name not in HOT_AGENT_FIELDS and f.name != "name" and getattr(agent, f.name) != getattr(new, f.name)]
        if cold:
            note("cold " + ",".join(cold), f"changed in the config but applied only at the next start: {', '.join(cold)}")
        if not changed:
            return
        try:
            for v in new.variants():  # a backend this build does not know would fail the turn: refuse the whole change
                get_backend(v.backend)
        except KeyError as e:
            note(f"unknown backend {e}", f"config names an unknown backend {e}; keeping the settings it had")
            return

        def shown(field: str, value: Any) -> str:
            if field == "env":  # keys only: the values can be secrets
                return "{" + ", ".join(sorted(value)) + "}"
            if field == "fallback":
                return "[" + ", ".join(str(f.get("label") or f.get("backend") or "?") for f in value) + "]"
            return json.dumps(value, ensure_ascii=False, default=str)

        for f, (old, value) in changed.items():
            setattr(agent, f, value)
        if "backend" in changed or "backend_options" in changed or "fallback" in changed:
            self.switch_backends()  # an agent that left its backend starts a new session (as on resume)
        parts = [f"{f} {shown(f, old)} → {shown(f, value)}" for f, (old, value) in changed.items()]
        rec.progress(f"🔧 **{agent.name}** settings changed in the config; applied before its next turn: "
                     + "; ".join(parts))
        rec.event("agent_reloaded", agent=agent.name, changed=sorted(changed))
        self.log(f"{agent.name}: settings reloaded from the config ({', '.join(sorted(changed))})")
        self._config_notes = {**getattr(self, "_config_notes", {}), agent.name: ""}

    def refresh_reminders(self, reminders: List[Dict[str, Any]]) -> None:
        """Reminders edited in the config apply from the next turn (their counters carry on)."""
        c = self.mon.controller
        if reminders == c.reminders:
            return
        old = {r["id"]: r for r in c.reminders}
        new = {r["id"]: r for r in reminders}
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        c.reminders = reminders
        self.rec.progress("🔧 reminders changed in the config; applied from the next turn: " + ", ".join(changed))
        self.rec.event("reminders_reloaded", changed=changed)

    def add_reminders(self, agent: AgentConfig, prompt: str, purpose: str) -> Tuple[str, List[str]]:
        """The prompt with the reminders due this turn at its end, and their ids. None outside managed mode."""
        return prompt, []

    def before_inbox(self, agent: AgentConfig) -> None:
        """Called before a turn takes the agent's notes from inbox/ (managed mode records them first)."""

    def on_prompt(self, agent: AgentConfig, turn_id: str, text: str, how: str) -> None:
        """Called with what an agent was actually given (its prompt, or a note cut into its turn)."""

    # ------------------------------------------------------------------ turns

    def turn(self, agent: AgentConfig, prompt: str, rnd: int, purpose: str,
             seq: Optional[int] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.should_stop():
            raise StopRequested()
        self.refresh_agent(agent)
        rec = self.rec
        base = prompt
        updates = rec.pending_worker_messages(agent.name) if agent.role == "worker" else []
        if updates:
            prompt = "\n\n".join(m["message"] for m in updates) + "\n\n" + prompt
        self.before_inbox(agent)
        human = self.human_text(agent.name, rec.take_inbox(agent.name, broadcast=self.inbox_broadcast(agent)))
        if human:  # queued with the relayed messages, so a restart mid-turn re-sends it instead of losing it
            self.st.setdefault("fwd", {}).setdefault(agent.name, []).append(human)
            rec.save()
        fwd = list((self.st.get("fwd") or {}).get(agent.name) or [])  # answers / notes relayed, and my messages
        late = "\n\n".join(x for x in fwd if x)
        if late:
            prompt = f"{late}\n\n{prompt}" if prompt.strip() else late
        sess = self.st["sessions"].get(agent.name) or {}
        sid = sess.get("session_id")
        cut = sess.get("cut_off") if sid else None
        new: List[str] = []
        if cut:  # B7: the session already has the cut-off turn's message: "continue", plus only what is new since
            had = _norm(cut.get("input") or "")
            new = [x.strip() for x in [m["message"] for m in updates] + fwd + [base] if x.strip() and _norm(x) not in had]
            prompt = "\n\n".join(new + [self.tpl("turn_continue").strip() or "continue"])
        reminders: List[str] = []
        if not cut or new:
            prompt, reminders = self.add_reminders(agent, prompt, purpose)  # last, so the message ends with them
        if reminders:
            extra = {**(extra or {}), "reminders": reminders}
            rec.save()  # the reminder counters
        big_input = len(prompt) > self.large_text
        seq = seq or rec.next_seq(rnd)
        record: Dict[str, Any] = {
            "turn_id": rec.turn_id(rnd, seq, agent.name),
            "round": rnd, "seq": seq, "agent": agent.name, "role": agent.role, "purpose": purpose,
            "backend": agent.backend, "cwd": agent.working_dir,
            "session_id": sid, "resumed_session": bool(sid), "session_turn": int(sess.get("turns", 0)) + 1,
            "status": "running", "started_at": now_iso(), "ended_at": None, "duration_s": None,
            "system_prompt": agent.current_system_prompt(), "input": prompt, "output": "",
            **(extra or {}),
        }
        resets_before, terminal_before = list(sess.get("resets") or []), sess.get("terminal_use")
        resets = [x for x in resets_before if x.get("change") != "model"]
        if resets:  # why this turn starts a new (or compacted) session: recorded with the turn
            record["session_reset"] = resets
        if cut:
            record["continues"] = cut.get("turn")
        if sid and sess.get("terminal_use"):  # turns typed in a terminal report no cost; this turn's increase has them
            record["cost_includes_terminal"] = sess["terminal_use"]
        if (self.st.get("reliability", {}).get(agent.name) or {}).get("state") == "failed":
            self.set_reliability(agent, state=None)  # the new attempt replaces the previous turn's failure
        rec.write_turn(record)  # relayed messages stay queued until the turn ends, so a restart re-sends them
        if big_input:
            self.size_warning(agent, record, f"a large input ({len(prompt):,} characters)")
        self.on_prompt(agent, record["turn_id"], prompt, "prompt")
        for m in updates:
            rec.worker_message_status(m["id"], "in_prompt", delivery_turn=record["turn_id"], via="prompt")

        def message_status(ident: str, status: str, details: Dict[str, Any]) -> None:
            rec.worker_message_status(ident, status, delivery_turn=record["turn_id"], **details)

        timing = TurnTiming()
        reached: List[bool] = []  # the backend reported something in this attempt: the message is in the session

        def live_event(ev: Dict[str, Any]) -> None:
            timing.event(ev)
            reached.append(True)
            if ev.get("k") == "input_accepted":
                for m in updates:
                    message_status(m["id"], "accepted", {"via": "prompt"})
            if ev.get("k") == "session" and ev.get("s") and ev["s"] != record.get("session_id"):
                # save the session the moment the backend names it, so a restart resumes it without guessing
                record["session_id"] = str(ev["s"])
                rec.write_turn(record)
                rec.update_session(agent.name, session_id=record["session_id"], backend=v.backend, role=agent.role,
                                   session_key=v.session_key, model=v.label)
            rec.live(record["turn_id"], ev)
        self.log(f"r{rnd} ▶ {agent.name} ({purpose}{', resume ' + sid[:8] if sid else ', new session'})")
        if sid and sess.get("model") and agent.variants() and sess["model"] != agent.variants()[0].label \
                and sess.get("session_key") == agent.variants()[0].session_key and not self.start_variant(agent, len(agent.variants())):
            # the model changed in the config; the session goes on (same provider), and the turn says so
            record["model_change"] = {"from": sess["model"], "to": agent.variants()[0].label}
            rec.event("session_note", agent=agent.name, change="model", at=now_iso(),
                      reason=f"model changed ({sess['model']} → {agent.variants()[0].label}); same session")
            rec.progress(f"🔀 {agent.name}: model changed ({sess['model']} → {agent.variants()[0].label}); same session")
        t0 = time.monotonic()
        res: TurnResult = TurnResult("error", "", sid)
        attempts: List[Dict[str, Any]] = []
        reason = ""
        lock = rec.agent_lock(agent.name)
        self.enter_agent_lock(lock)
        hit: List[str] = []
        cancel = self.turn_cancel(agent, hit)
        chain = agent.variants()
        vi = self.start_variant(agent, len(chain))
        sess_key = sess.get("session_key") or (chain[0].session_key if sess.get("backend", agent.backend) == agent.backend
                                               else str(sess.get("backend")))
        v = chain[vi]
        backend = get_backend(v.backend)
        attempt, urgent_send, entering, got_there = 0, "", True, False
        waited, fixes = 0, 0  # B8: retries of the current model after a wait, and immediate corrections
        last_pe = ""  # the provider's message from the last attempt, if it refused the request
        recovery_note = ""
        while True:
            if entering:  # the primary model, or a backup taking over this turn
                entering = False
                v = chain[vi]
                backend = get_backend(v.backend)
                host = endpoint_host(v.env)
                record.update(backend=v.backend, model=v.label, endpoint=host or None,
                              cost_basis=cost_basis(v.backend, [str(v.options.get("model") or "")], host))
                if sid and v.session_key != sess_key:  # another provider can't continue this session: a new one
                    old_sid, sid = sid, None
                    if cut:  # the new session has none of it: the whole message goes again
                        prompt = "\n\n".join(x for x in [m["message"] for m in updates] + fwd + [base] if x.strip())
                        if recovery_note:
                            prompt += "\n\n" + recovery_note
                        cut = None
                    note = self.fresh_session_note(agent)
                    if note:
                        prompt = f"{note}\n\n{prompt}"
                    why = {"reason": f"backup {v.label} on another provider", "change": "reset", "at": now_iso(),
                           "previous": old_sid}
                    record.setdefault("session_reset", []).append(why)
                    record.update(session_id=None, resumed_session=False, input=prompt)
                    rec.event("session_note", agent=agent.name, **why)
                    rec.progress(f"↺ {agent.name}: new session ({why['reason']})")
                sess_key = v.session_key
                # Resumed sessions may never emit a new session id. Persist the selected model
                # before starting every variant so live readers also see same-session switches.
                rec.write_turn(record)
            send = prompt
            if urgent_send:  # an urgent message cut in: the same session carries on with it
                send, urgent_send = urgent_send, ""
            elif attempt and sid and got_there:  # B7: the session has the message; don't send it again
                send = self.tpl("turn_continue").strip() or "continue"
                if not last_pe and res.status == "error" and (res.output or "").strip() == "" and res.exit_code == 0:
                    send = self.tpl("turn_unusable", reason=reason or res.status).strip() or send
            elif attempt and res.session_id and not last_pe:  # correction: tell the agent what went wrong in its own session
                send = (f"Your previous reply was unusable ({reason or res.status}). "
                        f"Please redo the last request and reply with text.\n\n{prompt}")
            hit.clear()
            reached.clear()
            req = TurnRequest(
                agent=agent.name, role=agent.role, prompt=send, system_prompt=record["system_prompt"],
                session_id=sid, cwd=agent.working_dir, timeout=agent.turn_limit(self.watched(agent)),
                options=v.options, env={**v.env, _jobs.TAG_VAR: _jobs.tag_for(rec.run_dir.name, agent.name)},
                context={"round": rnd, "purpose": purpose, "monitor": self.mon.name,
                         "roster": [a.name for a in self.mon.agents], **(extra or {})},
                on_event=live_event,
                poll_messages=(lambda: rec.pending_worker_messages(agent.name, record["turn_id"]))
                              if agent.role == "worker" else None,
                on_message=message_status if agent.role == "worker" else None,
            )
            try:
                res = self.run_backend(backend, req, cancel)
            except Exception:
                res = TurnResult("error", "", sid, None, traceback.format_exc(), [], {})
            got_there = bool(reached) or bool((res.output or "").strip()) or bool(res.meta)
            pe = provider_error(res)
            if pe:  # the provider's refusal is not the agent's reply: it stays out of the turn's output
                res = dataclasses.replace(res, output="")
            last_pe = pe
            if res.status == "stopped" and hit and not self.should_stop():
                if hit[0] == "resume":  # the message is for this agent: it continues, with the message first
                    self.before_inbox(agent)
                    note = self.human_text(agent.name, rec.take_inbox(agent.name, broadcast=self.inbox_broadcast(agent)))
                    sid = res.session_id or self._live_session(record["turn_id"]) or sid
                    if note:  # queued like any message until the turn ends, so a restart re-sends it
                        self.st.setdefault("fwd", {}).setdefault(agent.name, []).append(note)
                        fwd.append(note)
                        prompt = f"{note}\n\n{prompt}"  # what a correction or a fresh session re-sends
                    urgent_send = self.tpl("urgent_resume", note=note or "(no text)") if sid else prompt
                    record.setdefault("interruptions", []).append({"at": now_iso(), "message": note})
                    if note:
                        self.on_prompt(agent, record["turn_id"], note, "cut in")
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
            reason = pe or reason
            stalled = res.status == "error" and res.meta.get("error_kind") == "provider_stall"
            if stalled:
                reason = res.meta.get("error") or "OpenCode provider recovery window expired"
            if st_ != res.status:
                res = dataclasses.replace(res, status=st_)
            attempts.append({"attempt": attempt + 1, "model": v.label, "status": res.status, "exit_code": res.exit_code,
                             "reason": reason})
            if res.meta.get("provider_recovery"):
                attempts[-1]["provider_recovery"] = res.meta["provider_recovery"]
            if res.status == "needs_human" and not self.questions_pause:
                res = dataclasses.replace(res, status="ok")  # handled by the caller (goes one level up)
                record["asked"] = reason
            if res.status in ("ok", "stopped", "needs_human", "timeout") or self.should_stop():
                break  # a timed-out turn is not re-run from scratch (it may have been running a long job)
            self.log(f"r{rnd} ✗ {agent.name} attempt {attempt + 1} ({v.label}) → {res.status}" + (f" ({pe})" if pe else ""))
            bill = (billing_error(dataclasses.replace(res, output=pe or res.output)) if res.status == "error"
                    else "")  # the provider's message was taken out of the output above
            if bill:  # B8: a billing or account problem: no retry or backup; the run stops and the user is told
                record["billing_error"] = bill
                reason = f"billing or account problem: {bill}"
                attempts[-1]["reason"] = reason
                rec.event("billing_error", turn=record["turn_id"], agent=agent.name, model=v.label,
                          problem=f"{agent.name} ({v.label}): billing or account problem; the run stops: {bill}")
                rec.progress(f"⛔ `{record['turn_id']}` **{agent.name}** (`{v.label}`): billing or account problem "
                             f"({bill}). No retry or backup; the run stops. Fix the account and resume.")
                self.log(f"r{rnd} ⛔ {agent.name}: billing or account problem; stopping")
                break
            nxt_i = vi + 1
            exe = missing_executable(res)
            if exe:  # a setup problem: no retry; only a backup that starts another executable can help
                reason = f"executable not found: {exe}"
                attempts[-1]["reason"] = reason
                other = [i for i in range(vi + 1, len(chain))
                         if backend_executable(chain[i].backend, chain[i].options) != exe]
                if not other:
                    record["setup_error"] = reason
                    rec.event("setup_error", turn=record["turn_id"], agent=agent.name, model=v.label, executable=exe,
                              problem=f"{agent.name} ({v.label}): executable not found: {exe}; the run stops")
                    rec.progress(f"⛔ `{record['turn_id']}` **{agent.name}** (`{v.label}`): executable `{exe}` not "
                                 f"found. No retry, and no backup uses another executable; the run stops. Install it "
                                 f"or fix PATH / the `executable` option, then resume.")
                    self.log(f"r{rnd} ⛔ {agent.name}: executable not found: {exe}; stopping")
                    break
                nxt_i = other[0]
            # B8: a provider error, overload, rate limit or crash (anything but an unusable reply) is retried on the
            # same model after each of the configured waits (1, 2, 4 min); an unusable reply (exit 0, no text) gets
            # the `retries` immediate corrections. Then the backups take over in order.
            waits = list(getattr(agent, "retry_waits", []) or [])
            transient = not exe and (stalled or bool(pe) or not reason)
            if stalled:
                # A provider recovery timeout uses the same Orch retry budget as other transient errors.
                # Keep the session and evidence of completed work when continuing the interrupted attempt.
                note = res.meta.get("recovery_note") or ""
                if res.output:
                    note += "\nPartial reply from the previous attempt:\n" + res.output
                if note and note not in recovery_note:
                    recovery_note = "\n\n".join(x for x in (recovery_note, note) if x)
                    prompt = "\n\n".join(x for x in (prompt, note) if x)
                sid = res.session_id or record.get("session_id") or sid
            if transient and waits:
                if waited < len(waits):
                    secs = waits[waited]
                    waited += 1
                    self.retry_wait(agent, record, v, waited, len(waits), secs, pe or reason or res.status)
                    if self.should_stop():
                        break
                    if stalled and sid:
                        urgent_send = (self.tpl("turn_continue").strip() or "continue") + "\n\n" + note
                        record["input"] = urgent_send
                    attempt += 1
                    continue
            elif not stalled and not exe and fixes < v.retries:
                fixes += 1
                if pe:
                    self.wait_provider(agent, pe)
                if self.should_stop():
                    break
                attempt += 1
                continue
            if nxt_i >= len(chain):
                break
            nxt = chain[nxt_i]
            if stalled and sid and nxt.session_key == v.session_key:
                urgent_send = (self.tpl("turn_continue").strip() or "continue") + "\n\n" + recovery_note
            if stalled:
                record["input"] = urgent_send or prompt
            record.setdefault("fallbacks", []).append({"at": now_iso(), "from": v.label, "to": nxt.label,
                                                       "reason": reason or res.status})
            rec.event("fallback", turn=record["turn_id"], agent=agent.name, old=v.label, new=nxt.label,
                      reason=reason or res.status, backup=nxt_i)
            self.set_reliability(agent, model=nxt.label, state="switched", reason=reason or res.status)
            rec.progress(f"↪ `{record['turn_id']}` **{agent.name}**: `{v.label}` failed ({reason or res.status}); "
                         f"switching to backup `{nxt.label}`")
            self.log(f"r{rnd} ↪ {agent.name}: {v.label} failed, switching to backup {nxt.label}")
            vi, attempt, entering, waited, fixes = nxt_i, 0, True, 0, 0
        lock.__exit__(None, None, None)
        if attempts:  # the model this turn ended on (after any switch to a backup)
            self.turn_models = {**(getattr(self, "turn_models", None) or {}), agent.name: v.label}
        if res.status == "ok" and v.index and agent.fallback_cooldown > 0:  # the board says it is on a backup
            self.set_reliability(agent, model=v.label, state="backup", primary=chain[0].label, reason=None,
                                 until=time.time() + agent.fallback_cooldown)
        elif res.status == "ok":
            self.set_reliability(agent, state=None)
        elif res.status == "error":
            self.set_reliability(agent, model=v.label, state="failed", reason=reason or res.status)
        if res.status == "ok":
            for m in updates:
                message_status(m["id"], "accepted", {"via": "prompt"})
        billing = bool(record.get("billing_error") or record.get("setup_error"))
        if fwd and res.status != "stopped" and not billing and not self.should_stop():  # a stopped turn is re-sent on resume
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
            attempts=attempts, ended_at=now_iso(), duration_s=dur, timing=timing.finish(),
        )
        if last_pe:
            record["provider_error"] = last_pe
        record["cost_basis"] = cost_basis(v.backend, [str(v.options.get("model") or "")] + list(res.meta.get("models") or []),
                                          record.get("endpoint") or "", res.stderr or "")
        self.note_variant(agent, v, res.status)
        if res.session_id:
            rec.update_session(agent.name, session_id=res.session_id, backend=v.backend, role=agent.role,
                               session_key=v.session_key, model=v.label,
                               turns=int(sess.get("turns", 0)) + 1, last_turn=record["turn_id"],
                               last_status=res.status)
        if res.status != "stopped":  # recorded with this turn; a stopped turn is recovered (and records them) later
            now = self.st["sessions"].get(agent.name) or {}  # (notes made during the turn stay for the next one)
            rec.update_session(agent.name, cut_off=None,
                               resets=[x for x in now.get("resets") or [] if x not in resets_before],
                               terminal_use=None if now.get("terminal_use") == terminal_before else now.get("terminal_use"))
        rec.write_turn(record)
        try:
            self.snapshot(record)
        except Exception:
            self.log("snapshot failed:\n" + traceback.format_exc())
        if getattr(res, "raw", ""):  # B8: the raw event stream is never the reply; kept beside it for debugging
            path = rec.write_raw_stream(record, res.raw)
            record["raw_stream"] = {"file": path.name, "chars": len(res.raw)}
            rec.write_turn(record)
            if len(res.raw) > self.large_text:
                self.size_warning(agent, record, f"its raw event stream ({len(res.raw):,} characters) was kept "
                                                 f"apart in {path.name}; the reply is the final message only")
        if len(res.output or "") > self.large_text:
            self.size_warning(agent, record, f"a large reply ({len(res.output):,} characters)")
        rec.progress(f"- `{record['turn_id']}` **{agent.name}** · {purpose} → **{res.status}** ({dur}s)"
                     + (f" · provider: {last_pe}" if last_pe else ""))
        self.log(f"r{rnd} ■ {agent.name} → {res.status} in {dur}s")
        if res.status == "stopped":
            raise StopRequested()
        if billing:  # B8: stop the whole run (as `orch stop` would); the board says why; resume once it is fixed
            what = "billing or account problem" if record.get("billing_error") else "setup problem"
            rec.set(stop_reason=f"{what} ({agent.name}, {v.label}): "
                                f"{record.get('billing_error') or record.get('setup_error')}")
            (rec.run_dir / "STOP").write_text(what + "\n", encoding="utf-8")
            raise StopRequested()
        if res.status == "ok" and (self.st.get("asked") or {}).get("agent") == agent.name:
            rec.set(asked=None)  # the question has been dealt with
        if res.status == "needs_human":
            rec.set(asked={"round": rnd, "agent": agent.name})  # so the answer goes back to this agent alone
            raise NeedsHuman(record, reason)
        record["reason"] = reason
        return record

    def run_backend(self, backend: Any, req: TurnRequest, cancel: Callable[[], bool]) -> TurnResult:
        """The backend's turn itself (managed mode lets other threads use the run state meanwhile)."""
        return backend.run(req, cancel)

    def enter_agent_lock(self, lock: Any) -> None:
        """Take an agent's turn lock (managed mode waits for it without holding the run state)."""
        lock.__enter__()

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

    def start_variant(self, agent: AgentConfig, n: int) -> int:
        """Which of the agent's models a turn starts on: the backup that last worked while its cooldown lasts,
        otherwise the primary."""
        fb = (self.st.get("fallback") or {}).get(agent.name) or {}
        i = int(fb.get("index", 0) or 0)
        if 0 < i < n and time.time() < float(fb.get("until", 0) or 0):
            return i
        return 0

    def note_variant(self, agent: AgentConfig, v: Any, status: str) -> None:
        """Remember a backup that worked, so the next turns use it for `fallback_cooldown` seconds; a turn that
        worked on the primary clears that."""
        if status != "ok":
            return
        st = self.st.setdefault("fallback", {})
        if v.index and agent.fallback_cooldown > 0:
            if (st.get(agent.name) or {}).get("index") != v.index:
                self.rec.progress(f"↪ **{agent.name}** stays on backup `{v.label}` for the next "
                                  f"{agent.fallback_cooldown:g}s before trying its primary model again")
            st[agent.name] = {"index": v.index, "label": v.label, "until": time.time() + agent.fallback_cooldown}
        else:
            st.pop(agent.name, None)
        self.rec.save()

    def fresh_session_note(self, agent: AgentConfig) -> str:
        """Put in front of the prompt when a backup on another provider has to start a new session."""
        return self.tpl("backend_switched", agent=agent.called, cwd=agent.working_dir).strip()

    def wait_provider(self, agent: AgentConfig, reason: str) -> None:
        """Pause before re-running a turn the provider refused; a stop request ends the wait."""
        secs = max(0.0, float(getattr(self.mon.controller, "provider_retry_wait", 0) or 0))
        self.rec.progress(f"⏳ {agent.name}: the provider refused the request ({reason}); retrying in {secs:g}s")
        end = time.monotonic() + secs
        while time.monotonic() < end and not self.should_stop():
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    @property
    def large_text(self) -> int:
        return int(getattr(self.mon.controller, "large_text", 0) or LARGE_TEXT)

    def size_warning(self, agent: AgentConfig, record: Dict[str, Any], what: str) -> None:
        """B8: an out-of-range size is a board warning; nothing is cut."""
        record.setdefault("size_warnings", []).append(what)
        self.rec.progress(f"⚠ `{record['turn_id']}` **{agent.name}**: {what}")
        self.rec.event("size_warning", turn=record["turn_id"], agent=agent.name,
                       problem=f"{agent.name} `{record['turn_id']}`: {what}")

    def set_reliability(self, agent: AgentConfig, **kw: Any) -> None:
        """The agent's live retry / switch state in state.json (`reliability`), for the board; cleared on success."""
        st = self.st.setdefault("reliability", {})
        if kw.get("state") is None:
            if st.pop(agent.name, None) is not None:
                self.rec.save()
            return
        st[agent.name] = {**{k: v for k, v in kw.items() if v is not None}, "at": now_iso()}
        self.rec.save()

    def retry_wait(self, agent: AgentConfig, record: Dict[str, Any], v: Any, n: int, of: int, secs: float,
                   reason: str) -> None:
        """B8: wait, then run the turn again on the same model; recorded with the turn, in state and on the board."""
        rec = self.rec
        entry = {"at": now_iso(), "model": v.label, "retry": n, "of": of, "wait_s": secs, "reason": reason}
        record.setdefault("retries", []).append(entry)
        rec.write_turn(record)
        rec.event("retry", turn=record["turn_id"], agent=agent.name, **{k: entry[k] for k in entry if k != "at"})
        rec.progress(f"⏳ `{record['turn_id']}` **{agent.name}**: `{v.label}` failed ({reason}); retry {n}/{of} "
                     f"on the same model in {secs:g}s")
        self.log(f"{agent.name}: {v.label} failed ({reason}); retry {n}/{of} in {secs:g}s")
        self.set_reliability(agent, model=v.label, state="retrying", retry=n, of=of, wait_s=secs, reason=reason,
                             until=time.time() + secs)
        self.pause(secs)

    def pause(self, secs: float) -> None:
        """Sleep for `secs`; a stop request ends the wait. (Tests replace it with a fake clock.)"""
        end = time.monotonic() + max(0.0, float(secs))
        while time.monotonic() < end and not self.should_stop():
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def report_text(self, r: Optional[Dict[str, Any]]) -> str:
        if not r:
            return "(no report yet)"
        output = r.get("output") or ""
        incomplete = r.get("status") in ("timeout", "stopped", "error", "interrupted")
        if incomplete:
            # Older records stored the raw stream in output. Recover without changing history or sessions.
            recovered = claude_stream_report(output)
            if recovered is not None:
                output = recovered
        body = relayed(output or "(empty output)", self.mon.controller.max_report_chars)
        if incomplete:
            notice = "The text below may be incomplete." if output else "No assistant text was recovered."
            body = f"[Incomplete turn: {r['status']}. {notice}]\n\n" + body
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
                    routed.setdefault(t, []).append(relayed(fb, c.max_report_chars))
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
