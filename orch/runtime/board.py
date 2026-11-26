"""The dashboard server (UI-1 to UI-6): it reads snapshots, sends the user's commands and serves the page.

It decides nothing. The API is the one at the end of contracts.py. Standard library only.

    board = Board(runtime, run_dir, words=lambda ref: ..., runs_root=...)
    server, url = serve(board, host="127.0.0.1", port=8765)

Safety: a non-loopback bind is the user's choice (as before: no password); only files of the page folder and a few
known files of the run folder are ever read; nothing that holds an environment or a key is put into a reply; a
request is small, a client that is slow or gone only ends its own thread.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..boards import digest as text_digest
from ..recorder import read_json
from .contracts import (CONTRACT_VERSION, AgentStatus, Agent, Lifecycle, Message, MessageStatus, Role, RunInfo,
                        Snapshot, TextRef, UserCommand, from_dict, public_state, redact, reply_json, to_dict)
from .golden import read_board
from .milestones import read_checklist

PAGE_DIR = Path(__file__).with_name("page")
MAX_BODY = 1 << 20  # a command may carry a whole golden board or notes file
MAX_FILE = 4 << 20  # largest file read for a reply
MAX_SSE = 16  # open event streams
SSE_MAX_AGE = 120.0  # the page reconnects; abandoned connections do not pile up
STATE_TTL = 0.25  # seconds a state is reused between requests
TICK = 0.4  # seconds between two looks at the state and the live files
LIVE_PAGE = 500  # most lines one /api/live answer holds
MESSAGE_PAGE = 100
_NAME = re.compile(r"^[\w][\w.-]{0,63}$")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_FLOW = {  # old runs: purpose -> (from role, to role) of the turn's output
    "plan": ("manager", "user"), "kickoff": ("manager", "critic"), "restart": ("manager", "critic"),
    "answer": ("manager", "critic"), "review": ("manager", "user"), "feedback": ("manager", "critic"),
    "brief": ("critic", "worker"), "verify": ("critic", "worker"), "amend": ("critic", "worker"),
    "guidance": ("critic", "worker"), "report": ("critic", "manager"), "work": ("worker", "critic"),
    "par_assign": ("critic", "worker"), "par_review": ("critic", "worker"), "checkin": ("critic", "manager"),
    "ack": ("manager", "user"), "pause": ("manager", "user"),
}


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _epoch(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return 0.0


def _host_ok(header: str) -> bool:
    """A Host header, without its port, must be an IP literal, a loopback name, this computer's name or that name
    plus ".local". Anything else (a DNS name pointing here) is refused."""
    h = header.strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]  # [::1] or [::1]:port
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]  # name:port
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    return h in (*_LOOPBACK, me, me + ".local")


def _role(v: Any) -> Role:
    """An old run's role; a role this code does not know is a worker."""
    try:
        return Role(str(v or "worker"))
    except ValueError:
        return Role.WORKER


def _live_lines(path: Path, start: int) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Items of the complete lines from index `start` on (at most LIVE_PAGE), the index after the last line read,
    and whether the file has fewer complete lines than `start`. Read line by line, so no size cap."""
    items: List[Dict[str, Any]] = []
    n, nxt = 0, start  # n: complete lines seen so far
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # the line being written
                i, n = n, n + 1
                if i < start:
                    continue
                if len(items) >= LIVE_PAGE:
                    break
                nxt = i + 1
                item = _live_item(raw.decode("utf-8", "replace"))
                if item:
                    items.append({"n": i, **item})
    except OSError:
        pass
    return items, nxt, n < start


def _count_lines(path: Path, end: int) -> int:
    """Newlines in the first `end` bytes of a file, counted in pieces (the text is not kept)."""
    n, left = 0, end
    with path.open("rb") as fh:
        while left > 0:
            piece = fh.read(min(left, 1 << 20))
            if not piece:
                break
            n += piece.count(b"\n")
            left -= len(piece)
    return n


def _chat_rows(rd: Path) -> List[Dict[str, Any]]:
    """chat.jsonl of an old run, the lines of each conversation merged by id. Read only: no lease, no recovery."""
    rows: Dict[str, Dict[str, Any]] = {}
    for ln in _read(rd / "chat.jsonl").splitlines():
        try:
            c = json.loads(ln)
        except ValueError:
            continue
        if isinstance(c, dict):
            rows.setdefault(str(c.get("id") or ""), {}).update(c)
    return list(rows.values())


def _message_level(m: Message, roles: Dict[str, str]) -> str:
    parties = (roles.get(m.sender, m.sender), roles.get(m.recipient, m.recipient))
    if "monitor" in parties:
        return "monitor"
    if "user" in parties or ("orch" in parties and "manager" in parties):
        return "you"
    if "worker" in parties:
        return "workers"
    return "critic" if "critic" in parties else "you"


def _orch_note(m: Message) -> bool:
    key = str(m.text.key) if m.text else ""
    return m.sender == "orch" and (key.startswith(("note_", "rem_")) or key in {
        "silence_check", "gate_answers", "direct_message_notice", "checklist_unreadable",
        "job_wakeup", "job_lost", "job_no_progress", "monitor_check", "monitor_report",
        "nobody_watches", "review_rubric_missing",
    })


class Board:
    """What the server shows. `runtime`: the engine (state, submit; optionally message(id) for one full message).
    `words`: renders a TextRef. `runs_root`: folder of <project>/<run>/ folders (old runs, UI-6)."""

    def __init__(self, runtime: Any, run_dir: Path, words: Optional[Callable[[TextRef], str]] = None,
                 runs_root: Optional[Path] = None):
        self.runtime, self.run_dir, self.words, self.runs_root = runtime, Path(run_dir), words, runs_root
        self._lock = threading.Lock()
        self._at, self._state, self._hash, self.rev = -1e9, None, "", 0
        self.streams = threading.BoundedSemaphore(MAX_SSE)

    # -- state
    def state(self) -> Dict[str, Any]:
        """The latest public state with `rev`, and `shown` next to every TextRef. Reused for STATE_TTL seconds."""
        with self._lock:
            if self._state is None or time.monotonic() - self._at >= STATE_TTL:
                try:
                    st = self.shown(self.runtime.state())
                except Exception:  # the engine is busy or stopping: show the last state
                    if self._state is None:
                        raise
                else:
                    body = {k: v for k, v in st.items() if k != "now"}  # rev counts content changes only
                    h = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
                    if h != self._hash:
                        self._hash, self.rev = h, self.rev + 1
                    self._state = st
                self._at = time.monotonic()
            return {**self._state, "rev": self.rev}

    def shown(self, obj: Any) -> Any:
        """Adds `shown` (the rendered words) to every TextRef in obj, in place."""
        if isinstance(obj, dict):
            if set(obj) == {"key", "values"}:
                obj["shown"] = self._say(obj)
            else:
                for v in obj.values():
                    self.shown(v)
        elif isinstance(obj, list):
            for v in obj:
                self.shown(v)
        return obj

    def _say(self, ref: Dict[str, Any]) -> str:
        try:
            return str(self.words(from_dict(TextRef, ref))) if self.words else ""
        except Exception:
            return ""

    def command(self, body: Any) -> Tuple[int, Dict[str, Any]]:
        try:
            cmd = from_dict(UserCommand, body)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            return 400, {"ok": False, "error": str(e)[:200] or "bad command"}
        try:
            reply = self.runtime.submit(cmd)
        except Exception:
            return 500, {"ok": False, "error": "the command failed"}
        if reply.get("text") and not reply.get("shown"):
            reply["shown"] = self._say(reply["text"])
        return 200, reply

    # -- messages
    def messages(self, q: Dict[str, List[str]]) -> Dict[str, Any]:
        """Paged messages without rendered inputs. The timeline also needs count and turn metadata."""
        def one(k: str, default: str = "") -> str:
            return (q.get(k) or [default])[0]
        try:
            before = int(one("before") or 0)
            limit = max(1, min(int(one("limit") or MESSAGE_PAGE), 500))
        except ValueError:
            before, limit = 0, MESSAGE_PAGE
        search, agent = one("q"), one("agent")
        if one("timeline") != "1":
            rows, more = self.runtime.messages(before=before, limit=limit, q=search, agent=agent)
            return {"messages": [self._message_row(m) for m in rows], "more": more}

        supported = {"you", "critic", "workers", "monitor"}
        levels = set(one("levels", "you,critic,workers,monitor").split(",")) & supported
        notes = one("notes") != "0"
        roles = {aid: a.get("role", "") for aid, a in (self.state().get("agents") or {}).items()}
        selected, total = [], 0
        cursor = before
        while levels:
            batch, more = self.runtime.messages(before=cursor, limit=500, q=search, agent=agent)
            for m in sorted(batch, key=lambda item: item.seq, reverse=True):
                if _message_level(m, roles) not in levels or (not notes and _orch_note(m)):
                    continue
                total += 1
                if len(selected) < limit:
                    selected.append(m)
            if not batch or not more:
                break
            next_cursor = min(m.seq for m in batch)
            if next_cursor <= 0 or (cursor and next_cursor >= cursor):
                break
            cursor = next_cursor

        turns = {}
        rows = []
        for m in reversed(selected):
            row = self._message_row(m)
            if _orch_note(m):
                row["orch_note"] = True
            if m.turn:
                if m.turn not in turns:
                    turns[m.turn] = self._turn_summary(m.turn)
                if turns[m.turn]:
                    row["written_by"] = turns[m.turn]
            rows.append(row)
        return {"messages": rows, "more": total > len(rows), "remaining": total - len(rows)}

    def _message_row(self, m: Message) -> Dict[str, Any]:
        d = to_dict(m)
        d.pop("rendered", None)
        return self.shown(d)

    def _turn_summary(self, tid: str) -> Dict[str, Any]:
        match = re.fullmatch(r"(r[0-9]+)-(t[0-9]+-[A-Za-z0-9_-]+)", tid)
        if not match:
            return {}
        folder, name = match.groups()
        t = read_json(self.run_dir / "rounds" / folder / (name + ".json")) or {}
        if t.get("turn_id") != tid:
            return {}
        return {"model": t.get("model") or "", "input_chars": len(t.get("input") or ""),
                "output_chars": len(t.get("output") or "")}

    def message(self, mid: str) -> Optional[Dict[str, Any]]:
        """One full message, with `rendered`: runtime.message(id); None if the engine does not know it."""
        try:
            m = self.runtime.message(mid)
        except Exception:
            return None
        return self.shown(to_dict(m)) if m is not None else None

    # -- files of the run
    def turn(self, tid: str, run: str = "") -> Dict[str, Any]:
        rd = self._run_folder(run) if run else self.run_dir
        for p in sorted(rd.glob("rounds/r*/t*.json")) if rd else []:
            t = read_json(p) or {}
            if t.get("turn_id") == tid:
                out = {k: t.get(k) for k in ("turn_id", "agent", "purpose", "status", "input", "output",
                                             "system_prompt", "stderr", "session_id", "duration_s")}
                out["stderr"] = redact(str(out["stderr"] or ""))
                return out
        return {"error": "not found"}

    def live(self, agent: str, start: int, tail: int = 0, before: Optional[int] = None) -> Dict[str, Any]:
        """Lines from index `start` on of live/<agent>.jsonl, each with its index `n`. A file with fewer lines than
        `start` was replaced or cut: it is read from 0 and the answer says "reset"."""
        if not _NAME.match(agent):
            return {"items": [], "next": start}
        path = self.run_dir / "live" / f"{agent}.jsonl"
        if tail:
            last = deque(maxlen=min(LIVE_PAGE, tail))
            total, nxt = 0, 0
            try:
                with path.open("rb") as fh:
                    for i, raw in enumerate(fh):
                        if (before is not None and i >= before) or not raw.endswith(b"\n"):
                            break
                        nxt = i + 1
                        item = _live_item(raw.decode("utf-8", "replace"))
                        if item:
                            total += 1
                            last.append({"n": i, **item})
            except OSError:
                pass
            return {"items": list(last), "next": nxt, "more": total > len(last)}
        items, nxt, short = _live_lines(path, start)
        if short:
            items, nxt, _ = _live_lines(path, 0)
            return {"items": items, "next": nxt, "reset": True}
        return {"items": items, "next": nxt}

    def text_file(self, path: str) -> Dict[str, Any]:
        text = _read(Path(path)) if path else ""
        return {"text": text, "digest": text_digest(text), "path": path}

    def golden(self) -> Dict[str, Any]:
        """The golden board; a relative path is taken from the run's workspace, as the store does."""
        st = self.state()
        p = str((st.get("golden") or {}).get("path") or "")
        ws = str((st.get("run") or {}).get("workspace") or "")
        return self.text_file(str(Path(ws, p)) if p else "")

    def checklist(self, milestone: str) -> Dict[str, Any]:
        """The critic's checklist of one milestone, done ones too: its steps and items as read from the file now."""
        st = self.state()
        ms = next((m for m in st.get("milestones") or [] if m.get("id") == milestone), None)
        if ms is None:
            return {"error": "not found"}
        p = str(ms.get("checklist_path") or "")
        if not p:
            return {"path": "", "steps": [], "complete": False, "error": "none"}
        f = Path(p) if Path(p).is_absolute() else Path(str((st.get("run") or {}).get("workspace") or ""), p)
        text = _read(f) if f.is_file() else None
        return to_dict(read_checklist(p, text))

    def audit_notes(self) -> Dict[str, Any]:
        """The file run.audit_notes_path names (the one the auditor reads); nothing if the run names none."""
        p = str((self.state().get("run") or {}).get("audit_notes_path") or "")
        return self.text_file(p) if p else {"text": "", "digest": "", "path": ""}

    def audit(self, record: str = "", history: bool = False) -> Dict[str, Any]:
        """Read one audit reply, with an index so older replies can be opened on demand."""
        files = sorted((self.run_dir / "audits").glob("*.md"), reverse=True)
        selected = next((p for p in files if p.stem == record), None) if record else (files[0] if files else None)
        if record and selected is None:
            return {"error": "not found"}
        text = _read(selected) if selected else ""
        reply = text.split("## Reply\n\n", 1)[1] if "## Reply\n\n" in text else ""
        reply = re.split(r"\n## stderr\n", reply, 1)[0].strip()
        running = bool((self.state().get("run") or {}).get("audit_running"))
        result = {"report": redact(reply)[:60000], "running": running}
        if history and not record:
            records = []
            for p in files:
                try:
                    records.append({"id": p.stem, "at": p.stat().st_mtime})
                except OSError:
                    continue
            result["records"] = records
        return result

    # -- runs (UI-6)
    def runs(self) -> Dict[str, Any]:
        out = []
        for key, rd in self._run_folders().items():
            st = read_json(rd / "state.json", {}) or {}
            out.append({"id": key, "project": key.split("/")[0], "status": str(st.get("status") or ""),
                        "old": "runtime" not in st, "started": _epoch(st.get("started_at"))})
        return {"runs": sorted(out, key=lambda r: -r["started"])}

    def run(self, key: str) -> Dict[str, Any]:
        rd = self._run_folder(key)
        if rd is None:
            return {"error": "not found"}
        st = read_json(rd / "state.json", {}) or {}
        old = "runtime" not in st  # a run saved before the runtime: read from its turn files
        try:
            snap = _old_snapshot(rd, st) if old else from_dict(Snapshot, st["runtime"]["snap"])
            d = public_state(snap)
        except (ValueError, TypeError, KeyError, AttributeError):
            return {"error": "unreadable run"}
        return {**self.shown(d), "old": old, "rev": 0}

    def _run_folders(self) -> Dict[str, Path]:
        found = {}
        for p in sorted(self.runs_root.glob("*/*/state.json")) if self.runs_root else []:
            found[f"{p.parent.parent.name}/{p.parent.name}"] = p.parent
        return found

    def _run_folder(self, key: str) -> Optional[Path]:
        return self._run_folders().get(key)  # only folders found by listing: no path from the request is used


class Viewer:
    """A saved run shown without its Orch (`orch serve`): read again when state.json changes, since an `orch run` may be
    writing it. A command goes into the run's commands/ folder, where the Orch that runs it (if any) takes it."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self._key: Any = None
        self._store: Any = None
        self._snap: Optional[Snapshot] = None
        self._lock = threading.Lock()

    def snapshot(self) -> Snapshot:
        from .adapters.base import SystemClock
        from .store import Store
        f = self.run_dir / "state.json"
        try:
            key = (f.stat().st_mtime_ns, f.stat().st_size)
        except OSError:
            key = None
        with self._lock:
            if self._snap is None or key != self._key:
                st = read_json(f, {}) or {}
                if "runtime" in st:
                    self._store = Store(self.run_dir, SystemClock(), readonly=True, read_checklist=read_checklist,
                                        read_board=read_board)   # as the engine reads them
                    self._snap = self._store.snapshot()
                else:
                    self._store, self._snap = None, _old_snapshot(self.run_dir, st)
                self._key = key
            return self._snap

    def state(self) -> Dict[str, Any]:
        out = public_state(self.snapshot())
        out["viewer"] = {"orch_running": self.orch_running()}
        return out

    def orch_running(self) -> bool:
        """Whether an Orch holds this run: the pid its run lock names is alive. Read only, so it never takes the lock
        from an Orch that is starting."""
        info = read_json(self.run_dir / "locks" / "run.lock", {}) or {}
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def submit(self, command: Any) -> Dict[str, Any]:
        self.snapshot()
        if self._store is None:
            return {"ok": False, "error": "an old run: it can be read, not changed"}
        if not self.orch_running():
            # never saved for later: a click here would otherwise act, maybe many times, when Orch next starts
            return {"ok": False, "shown": "No Orch is running this run, so nothing was done. To change it, start it: "
                                          "orch run <project> --resume --serve"}
        from ..cli import write_command
        write_command(self.run_dir, command)
        return {"ok": True, "queued": True}

    def message(self, id: str) -> Optional[Message]:
        self.snapshot()
        return self._store.message(id) if self._store is not None else self._snap.messages.get(id)

    def messages(self, before: int = 0, limit: int = 50, q: str = "", agent: str = "") -> Tuple[List[Message], bool]:
        self.snapshot()
        if self._store is not None:
            return self._store.messages(before, limit, q, agent)
        ms = [m for m in sorted(self._snap.messages.values(), key=lambda m: -m.seq)
              if (not before or m.seq < before) and (not q or q.lower() in m.body.lower())
              and (not agent or agent in (m.sender, m.recipient))]
        return ms[:limit], len(ms) > limit


def _live_item(line: str) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("k") not in ("text", "tool"):
        return None
    return {"at": d.get("at", 0), "k": d["k"], "s": str(d.get("s", ""))}


def _old_snapshot(rd: Path, st: Dict[str, Any]) -> Snapshot:
    """An old run (rounds/r*/t*.json) as a snapshot: its agents closed, one message per turn output (UI-6)."""
    agents: Dict[str, Agent] = {}
    messages: Dict[str, Message] = {}
    turns = [t for t in (read_json(p) for p in sorted(rd.glob("rounds/r*/t*.json"))) if isinstance(t, dict)]
    turns = sorted((t for t in turns if t.get("agent") and t.get("turn_id")),
                   key=lambda t: _epoch(t.get("started_at")))  # by time, not by file name
    for t in turns:
        aid, role = str(t["agent"]), _role(t.get("role"))
        a = agents.setdefault(aid, Agent(aid, role=role, display=aid, lifecycle=Lifecycle.CLOSED,
                                         status=AgentStatus.CLOSED))
        a.actual_model = str((t.get("meta") or {}).get("model") or a.actual_model)
        src, dst = _FLOW.get(str(t.get("purpose")), (role.value, "user"))
        messages[t["turn_id"]] = Message(
            t["turn_id"], sender=aid, recipient=dst if src == role.value else src, body=str(t.get("output") or ""),
            needs_answer=False, turn=t["turn_id"], created=_epoch(t.get("started_at")), seq=0,
            status=MessageStatus.ANSWERED)
    for c in _chat_rows(rd):  # the board chat, merged in by id
        cid, peer = str(c.get("id") or ""), str(c.get("who") or c.get("with") or "")
        if not cid or c.get("status") == "running" or c.get("role") not in ("human", "agent", "manager"):
            continue
        sender, recipient = ("user", peer) if c.get("role") == "human" else (peer, "user")
        messages[cid] = Message(cid, sender=sender, recipient=recipient, body=str(c.get("text") or ""),
                                needs_answer=False, turn="", created=_epoch(c.get("ts")), seq=0,
                                status=MessageStatus.ANSWERED)
    ordered = sorted(messages.values(), key=lambda m: m.created)  # the sort is stable: ties keep their order
    messages = {m.id: replace(m, seq=i) for i, m in enumerate(ordered, 1)}
    run = RunInfo(id=str(st.get("run_id") or rd.name), project=str(st.get("monitor") or ""),
                  goal=str(st.get("goal") or ""), started=_epoch(st.get("started_at")))
    return Snapshot(now=_epoch(st.get("updated_at")), run=run, agents=agents, messages=messages, finished=True)


# ------------------------------------------------------------------------------------------------------- server

class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that went away mid-request; the details could hold anything


def _handler(board: Board):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 15  # a client that stops sending or reading ends its own thread

        def log_message(self, *a: Any) -> None:
            pass

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def js(self, data: Any, code: int = 200) -> None:
            self.send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

        def allowed(self) -> bool:
            """No other web page may drive the dashboard: the Host must name this computer or loopback (DNS
            rebinding), on every bind, and a request from another origin is refused."""
            host = self.headers.get("Host") or ""
            if not _host_ok(host):
                return False
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc.lower() == host.lower()

        def do_GET(self) -> None:
            if not self.allowed():
                return self.js({"error": "forbidden"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)

            def one(k: str) -> str:
                return (q.get(k) or [""])[0]
            try:
                if not u.path.startswith("/api/"):
                    return self.page_file(u.path)
                api = u.path[5:]
                if api == "state":
                    self.js(board.state())
                elif api == "messages":
                    self.js(board.messages(q))
                elif api == "message":
                    m = board.message(one("id"))
                    self.js({"message": m}, 200 if m else 404)
                elif api == "turn":
                    t = board.turn(one("id"), one("run"))
                    self.js(t, 404 if "error" in t else 200)
                elif api == "live":
                    before = one("before")
                    self.js(board.live(one("agent"), max(0, int(one("from") or 0)), max(0, int(one("tail") or 0)),
                                       max(0, int(before)) if before else None))
                elif api == "events":
                    self.events()
                elif api == "golden":
                    self.js(board.golden())
                elif api == "checklist":
                    c = board.checklist(one("milestone"))
                    self.js(c, 404 if c.get("error") == "not found" else 200)
                elif api == "audit_notes":
                    self.js(board.audit_notes())
                elif api == "audit":
                    audit = board.audit(one("id"), one("history") == "1")
                    self.js(audit, 404 if "error" in audit else 200)
                elif api == "runs":
                    self.js(board.runs())
                elif api == "run":
                    r = board.run(one("id"))
                    self.js(r, 404 if "error" in r else 200)
                else:
                    self.js({"error": "not found"}, 404)
            except ValueError:
                self.js({"error": "bad request"}, 400)
            except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
                self.close_connection = True
            except Exception:
                self.js({"error": "failed"}, 500)  # never the exception's text

        def page_file(self, path: str) -> None:
            """A file of the page folder: nothing outside it, nothing hidden."""
            rel = path.lstrip("/") or "index.html"
            root = PAGE_DIR.resolve()
            f = (root / rel).resolve()
            if root not in f.parents or any(p.startswith(".") for p in f.relative_to(root).parts) or not f.is_file():
                return self.send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self.send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/")
                                                    or ctype.endswith(("javascript", "json")) else ""))

        def do_POST(self) -> None:
            if not self.allowed():
                return self.js({"error": "forbidden"}, 403)
            if urlparse(self.path).path != "/api/command":
                return self.js({"error": "not found"}, 404)
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self.js({"ok": False, "error": "send application/json"}, 415)
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self.js({"ok": False, "error": "length required"}, 411)
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                return self.js({"ok": False, "error": "too large"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                return self.js({"ok": False, "error": "bad json"}, 400)
            self.js(*board.command(body)[::-1])

        def events(self) -> None:
            """Server-sent events: `state` {"rev"} when the state changed, `live` for each new live line."""
            if not board.streams.acquire(blocking=False):
                return self.js({"error": "too many streams"}, 503)
            try:
                self.send_response(200)
                for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                             ("X-Accel-Buffering", "no"), ("Connection", "close")):
                    self.send_header(k, v)
                self.end_headers()
                self.close_connection = True
                offsets: Dict[str, Any] = {}
                rev, started, ping = -1, time.monotonic(), time.monotonic()
                while time.monotonic() - started < SSE_MAX_AGE:
                    out = b""
                    try:
                        now_rev = board.state()["rev"]
                    except Exception:
                        now_rev = rev
                    if now_rev != rev:
                        rev, out = now_rev, b"event: state\ndata: " + json.dumps({"rev": rev}).encode() + b"\n\n"
                    out += self.live_lines(offsets)
                    if not out and time.monotonic() - ping > 15:
                        out = b": ping\n\n"
                    if out:
                        ping = time.monotonic()
                        self.wfile.write(out)
                        self.wfile.flush()
                    time.sleep(TICK)
            except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
                pass
            finally:
                board.streams.release()

        def live_lines(self, offsets: Dict[str, Any]) -> bytes:
            """New complete lines of live/*.jsonl since the last look, each as a `live` event with its index `n`.
            The first look only notes each file's size (earlier lines come from /api/live). A file that became
            shorter was replaced or cut: it is read again from 0. A file's line count is taken when its first new
            line is read, by counting the newlines before it (in pieces, the text is not kept)."""
            first = "_" not in offsets
            offsets.setdefault("_", True)
            out = []
            for f in sorted((board.run_dir / "live").glob("*.jsonl")):
                try:
                    size = f.stat().st_size
                    if f.name not in offsets:
                        offsets[f.name] = (size, None) if first else (0, 0)
                    off, n = offsets[f.name]
                    if size < off:
                        off, n = 0, 0
                    if size > off:
                        with f.open("rb") as fh:
                            fh.seek(off)
                            chunk = fh.read(min(size - off, MAX_FILE))
                        cut = chunk.rfind(b"\n") + 1
                        if cut:
                            if n is None:
                                n = _count_lines(f, off)
                            for ln in chunk[:cut].split(b"\n")[:-1]:
                                item = _live_item(ln.decode("utf-8", "replace"))
                                if item:
                                    out.append(b"event: live\ndata: " + json.dumps(
                                        {"agent": f.stem, "n": n, **item}, ensure_ascii=False).encode() + b"\n\n")
                                n += 1
                        off += cut
                    offsets[f.name] = (off, n)
                except OSError:
                    continue
            return b"".join(out)

    return H


def serve(board: Board, host: str = "127.0.0.1", port: int = 8765) -> Tuple[ThreadingHTTPServer, str]:
    """Start the dashboard in a background thread; returns (server, url). Port 0: any free one; else the next free
    one (up to 20 tries). `server.shutdown()` stops it."""
    last: Optional[Exception] = None
    for p in ([0] if port == 0 else range(port, port + 20)):
        try:
            srv = _Server((host, p), _handler(board))
            break
        except OSError as e:
            last = e
    else:
        raise OSError(f"no free port from {port}: {last}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shown = host if host not in ("0.0.0.0", "::", "") else _lan_ip()
    return srv, f"http://{shown}:{srv.server_address[1]}/"


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
