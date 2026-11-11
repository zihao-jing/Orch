"""On-disk record of every run, round, session and turn.

Layout (one directory per run)::

    <output_dir>/
      index.html                         all monitors and runs
      <monitor>/
        <run-id>/                        e.g. 20261107-193000
          config.json                    snapshot of the monitor config used
          state.json                     live status, sessions, rounds summary, pending inputs
          events.jsonl                   append-only event log
          progress.md                    human-readable running log
          report.html                    self-contained viewer (auto-refreshes while running)
          inbox/                         drop messages here (or `orch inject`) to steer the run;
                                         {"kind": "answer"|"gate", ...} files are answers / approvals (managed mode)
          live/<turn>.jsonl              what an agent is doing right now (text deltas, tool calls)
          chat.jsonl                     managed mode: your chat with the manager
          STOP                           create this file (or `orch stop`) to stop after the current turn
          rounds/
            r001/
              round.json                 round summary (decisions, verdicts, routing)
              t01-implementer.json       turn record: input, output, status, session, timing, command
              t01-implementer.input.md
              t01-implementer.output.md
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_index_lock = threading.Lock()


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@contextlib.contextmanager
def agent_lock(run_dir: Path, agent: str):
    """One turn per agent session at a time, also across processes (run + `orch serve` chat)."""
    import fcntl
    d = Path(run_dir) / "locks"
    d.mkdir(exist_ok=True)
    with open(d / f"{agent}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def live_event(run_dir: Path, key: str, ev: Dict[str, Any]) -> None:
    d = Path(run_dir) / "live"
    d.mkdir(exist_ok=True)
    with open(d / f"{key}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **ev}, ensure_ascii=False) + "\n")


class Recorder:
    def __init__(self, run_dir: Path, output_root: Path):
        self.run_dir = run_dir
        self.output_root = output_root
        self.lock = threading.RLock()
        self.state: Dict[str, Any] = read_json(run_dir / "state.json", {}) or {}
        (run_dir / "rounds").mkdir(parents=True, exist_ok=True)
        (run_dir / "inbox").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(cls, output_root: str, monitor: str, snapshot: Dict[str, Any]) -> "Recorder":
        root = Path(output_root)
        base = root / monitor
        base.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = base / stamp
        i = 1
        while run_dir.exists():
            i += 1
            run_dir = base / f"{stamp}-{i}"
        run_dir.mkdir()
        write_json(run_dir / "config.json", snapshot)
        rec = cls(run_dir, root)
        rec.state = {
            "monitor": monitor,
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "status": "starting",
            "mode": snapshot["controller"]["mode"],
            "goal": snapshot["goal"],
            "workspace": snapshot["workspace"],
            "working_dir": snapshot["working_dir"],
            "max_rounds": snapshot["max_rounds"],
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "ended_at": None,
            "round": 0,
            "sessions": {},
            "rounds": [],
            "pending": {},
            "turn_counts": {"total": 0, "ok": 0, "error": 0, "timeout": 0, "stopped": 0},
            "cost_usd": 0.0,
        }
        rec.save()
        rec.event("run_started", monitor=monitor)
        rec.progress(f"# {monitor} — run {run_dir.name}\n\nStarted {rec.state['started_at']}\n\n"
                     f"**Goal:** {snapshot['goal'] or '(none)'}\n")
        return rec

    @classmethod
    def latest(cls, output_root: str, monitor: str) -> Optional["Recorder"]:
        base = Path(output_root) / monitor
        if not base.is_dir():
            return None
        runs = sorted([p for p in base.iterdir() if (p / "state.json").is_file()], key=lambda p: p.name)
        return cls(runs[-1], Path(output_root)) if runs else None

    # ------------------------------------------------------------------ state

    def save(self) -> None:
        with self.lock:
            self.state["updated_at"] = now_iso()
            write_json(self.run_dir / "state.json", self.state)

    def set(self, **kv: Any) -> None:
        with self.lock:
            self.state.update(kv)
            self.save()

    def event(self, kind: str, **data: Any) -> None:
        line = json.dumps({"ts": now_iso(), "event": kind, **data}, ensure_ascii=False, default=str)
        with self.lock:
            with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def progress(self, text: str) -> None:
        with self.lock:
            with open(self.run_dir / "progress.md", "a", encoding="utf-8") as f:
                f.write(text.rstrip() + ("\n" if text.startswith("- ") else "\n\n"))

    # ------------------------------------------------------------------ rounds & turns

    def round_dir(self, rnd: int) -> Path:
        d = self.run_dir / "rounds" / f"r{rnd:03d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def finished_turns(self, rnd: int, purpose: str) -> Dict[str, Dict[str, Any]]:
        """Turns of one round and purpose that already finished ok, by agent (a redone round keeps them)."""
        out: Dict[str, Dict[str, Any]] = {}
        d = self.run_dir / "rounds" / f"r{rnd:03d}"
        for p in sorted(d.glob("t*.json")) if d.is_dir() else []:
            t = read_json(p, {}) or {}
            if t.get("purpose") == purpose and t.get("status") == "ok" and t.get("agent"):
                out[t["agent"]] = t
        return out

    def next_seq(self, rnd: int) -> int:
        with self.lock:
            key = f"r{rnd:03d}"
            seqs = self.state.setdefault("seq", {})
            seqs[key] = seqs.get(key, 0) + 1
            return seqs[key]

    def turn_id(self, rnd: int, seq: int, agent: str) -> str:
        return f"r{rnd:03d}-t{seq:02d}-{agent}"

    def write_turn(self, rec: Dict[str, Any]) -> None:
        d = self.round_dir(rec["round"])
        stem = f"t{rec['seq']:02d}-{rec['agent']}"
        with self.lock:
            meta = rec.get("meta") or {}
            if rec.get("status") != "running" and isinstance(meta.get("total_cost_usd"), (int, float)) \
                    and "turn_cost_usd" not in meta:
                meta["turn_cost_usd"] = self.turn_cost(rec.get("session_id"), meta["total_cost_usd"])
            write_json(d / f"{stem}.json", rec)
            (d / f"{stem}.input.md").write_text(rec.get("input", ""), encoding="utf-8")
            if rec.get("status") != "running":
                (d / f"{stem}.output.md").write_text(rec.get("output", ""), encoding="utf-8")
                tc = self.state["turn_counts"]
                tc["total"] += 1
                tc[rec["status"]] = tc.get(rec["status"], 0) + 1
                cost = (rec.get("meta") or {}).get("turn_cost_usd")
                if isinstance(cost, (int, float)):
                    self.state["cost_usd"] = round(self.state.get("cost_usd", 0.0) + cost, 6)
            self.save()
        self.event("turn_" + ("started" if rec["status"] == "running" else "finished"),
                   turn=rec["turn_id"], agent=rec["agent"], status=rec["status"],
                   session_id=rec.get("session_id"), duration_s=rec.get("duration_s"))
        self.refresh_reports()

    def turn_cost(self, session_id: Optional[str], total: float) -> float:
        """Claude reports a resumed session's running total, not the turn's own cost: take the increase."""
        sc = self.state.setdefault("session_costs", {})
        prev = float(sc.get(session_id, 0.0)) if session_id else 0.0
        if session_id:
            sc[session_id] = total
        return round(total - prev if total >= prev else total, 6)

    def write_round(self, rnd: int, summary: Dict[str, Any]) -> None:
        with self.lock:
            write_json(self.round_dir(rnd) / "round.json", summary)
            rounds = [r for r in self.state["rounds"] if r.get("round") != rnd]
            rounds.append({k: summary.get(k) for k in ("round", "status", "started_at", "ended_at", "decision", "turns")})
            self.state["rounds"] = sorted(rounds, key=lambda r: r["round"])
            self.save()
        self.event("round_" + ("started" if summary.get("status") == "running" else "finished"), round=rnd,
                   status=summary.get("status"), decision=summary.get("decision"))
        self.refresh_reports()

    def update_session(self, agent: str, **kv: Any) -> None:
        with self.lock:
            s = self.state["sessions"].setdefault(agent, {"session_id": None, "turns": 0})
            s.update(kv)
            self.save()

    # ------------------------------------------------------------------ control files

    def queue_worker_message(self, agent: str, message: str, source_turn: str, sender: str) -> Dict[str, Any]:
        """Keep explicit check-in instructions separate from interrupting inbox notes and review feedback."""
        ident = hashlib.sha256(json.dumps([source_turn, agent, message]).encode()).hexdigest()[:24]
        with self.lock:
            rows = self.state.setdefault("worker_messages", [])
            existing = next((m for m in rows if m["id"] == ident), None)
            if existing:
                return dict(existing)
            row = {"id": ident, "to": agent, "from": sender, "message": message,
                   "source_turn": source_turn, "status": "queued", "created_at": now_iso()}
            rows.append(row)
            self.save()
            self.event("worker_message", **row)
            return dict(row)

    def pending_worker_messages(self, agent: str, turn: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(m) for m in self.state.get("worker_messages", []) if m["to"] == agent
                    and (m["status"] in ("queued", "deferred")
                         or (turn is None and m["status"] == "in_prompt"))
                    and (turn is None or m.get("delivery_turn") != turn)]

    def worker_message_status(self, ident: str, status: str, **details: Any) -> None:
        with self.lock:
            row = next(m for m in self.state["worker_messages"] if m["id"] == ident)
            row.update(status=status, updated_at=now_iso(), **details)
            self.save()
            self.event("worker_message", **row)

    def recover_worker_messages(self) -> None:
        """A crash after send but before acknowledgement has an unknown outcome; never blindly replay it."""
        with self.lock:
            for row in self.state.get("worker_messages", []):
                if row["status"] == "sending":
                    self.worker_message_status(row["id"], "unknown", reason="Run restarted before acknowledgement")

    def stop_requested(self) -> bool:
        return (self.run_dir / "STOP").exists()

    def take_inbox(self, agent: Optional[str] = None, broadcast: bool = True) -> List[Dict[str, Any]]:
        """Consume human messages dropped into inbox/. Returns [{to: [..]|None, message, file}].
        ``broadcast=False`` leaves unaddressed messages for someone else. Answers and approvals
        (files with a ``kind``) are left for :meth:`take_control`."""
        inbox = self.run_dir / "inbox"
        done = inbox / "consumed"
        msgs: List[Dict[str, Any]] = []
        for p in sorted(inbox.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            to = None
            message = text
            if p.suffix == ".json":
                try:
                    data = json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    continue  # still being written
                if data.get("kind"):
                    continue
                to = data.get("to") or None
                if isinstance(to, str):
                    to = [to]
                message = str(data.get("message", ""))
            if agent is not None and to and agent not in to:
                continue  # addressed to someone else: leave it for their turn
            if not to and not broadcast:
                continue
            if message.strip():
                msgs.append({"to": to, "message": message.strip(), "file": p.name})
            done.mkdir(exist_ok=True)
            os.replace(p, done / p.name)
        if msgs:
            self.event("human_messages", count=len(msgs), files=[m["file"] for m in msgs])
        return msgs

    def urgent_notes(self) -> List[Dict[str, Any]]:
        """Urgent human messages waiting in inbox/ (not consumed): [{to: [..]|None}]. One of these cuts into the
        turn that is running now, instead of waiting for the next one."""
        out: List[Dict[str, Any]] = []
        try:
            files = sorted((self.run_dir / "inbox").glob("*.json"))
        except OSError:
            return out
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("kind") or not data.get("urgent"):
                continue
            if not str(data.get("message", "")).strip():
                continue
            to = data.get("to") or None
            out.append({"to": [to] if isinstance(to, str) else to})
        return out

    def take_control(self, kinds: tuple = ("answer", "gate", "job", "pause")) -> List[Dict[str, Any]]:
        """Consume answer / approval files from inbox/ (oldest first)."""
        inbox = self.run_dir / "inbox"
        out: List[Dict[str, Any]] = []
        for p in sorted(inbox.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("kind") not in kinds:
                continue
            data["file"] = p.name
            out.append(data)
            (inbox / "consumed").mkdir(exist_ok=True)
            os.replace(p, inbox / "consumed" / p.name)
        if out:
            self.event("human_control", items=[{k: d.get(k) for k in ("kind", "qid", "decision")} for d in out])
        return out

    # ------------------------------------------------------------------ live progress, chat, locks

    def live(self, turn_id: str, ev: Dict[str, Any]) -> None:
        live_event(self.run_dir, turn_id, ev)

    def agent_lock(self, agent: str):
        return agent_lock(self.run_dir, agent)

    # ------------------------------------------------------------------ reports

    def refresh_reports(self) -> None:
        from . import report
        try:
            with self.lock:
                report.write_run_report(self.run_dir)
            with _index_lock:
                report.write_index(self.output_root)
        except Exception as e:  # reporting must never break a run
            self.event("report_error", error=repr(e))
