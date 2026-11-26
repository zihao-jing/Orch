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
import json
import mimetypes
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..boards import digest as text_digest
from ..recorder import read_json
from .contracts import (CONTRACT_VERSION, AgentStatus, Agent, Lifecycle, Message, MessageStatus, Role, RunInfo,
                        Snapshot, TextRef, UserCommand, from_dict, public_state, redact, reply_json, to_dict)

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
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
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


class Board:
    """What the server shows. `runtime`: the engine (state, submit; optionally message(id) for one full message).
    `words`: renders a TextRef. `runs_root`: folder of <project>/<run>/ folders (old runs, UI-6)."""

    def __init__(self, runtime: Any, run_dir: Path, words: Optional[Callable[[TextRef], str]] = None,
                 runs_root: Optional[Path] = None):
        self.runtime, self.run_dir, self.words, self.runs_root = runtime, Path(run_dir), words, runs_root
        self._lock = threading.Lock()
        self._at, self._state, self._hash, self.rev = -1e9, None, "", 0
        self._ledger: Tuple[Any, List[Dict[str, Any]]] = (None, [])
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
                    h = hashlib.sha1(json.dumps(st, sort_keys=True, default=str).encode()).hexdigest()
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
    def _all_messages(self) -> List[Dict[str, Any]]:
        """Ledger rows (older ones) and the state's messages, by seq, without `rendered`."""
        rows: Dict[str, Dict[str, Any]] = {}
        p = self.run_dir / "ledger.jsonl"  # CONTRACT: one to_dict(Message) per line; the store's ledger of old messages
        try:
            sig = (p.stat().st_mtime_ns, p.stat().st_size)
        except OSError:
            sig = None
        if sig != self._ledger[0]:
            old = []
            for ln in (_read(p).splitlines() if sig else []):
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    d.pop("rendered", None)
                    old.append(d)
            self._ledger = (sig, old)
        for d in self._ledger[1]:
            rows[d["id"]] = d
        for d in (self.state().get("messages") or {}).values():
            rows[d["id"]] = d
        return sorted(rows.values(), key=lambda d: d.get("seq", 0))

    def messages(self, q: Dict[str, List[str]]) -> Dict[str, Any]:
        def one(k: str, default: str = "") -> str:
            return (q.get(k) or [default])[0]
        try:
            before = int(one("before") or 1 << 60)
            limit = max(1, min(int(one("limit") or MESSAGE_PAGE), 500))
        except ValueError:
            before, limit = 1 << 60, MESSAGE_PAGE
        text, agent = one("q").lower(), one("agent")
        rows = [d for d in self._all_messages() if d.get("seq", 0) < before
                and (not agent or agent in (d.get("sender"), d.get("recipient")))]
        if text:
            rows = [d for d in rows if text in str(d.get("body", "")).lower()
                    or text in str(self.shown(d.get("text") or {}).get("shown", "")).lower()]
        return {"messages": [self.shown(d) for d in rows[-limit:]], "more": len(rows) > limit}

    def message(self, mid: str) -> Optional[Dict[str, Any]]:
        full = getattr(self.runtime, "message", None)  # CONTRACT: runtime.message(id) -> Message or None, with rendered
        try:
            m = full(mid) if full else None
        except Exception:
            m = None
        d = to_dict(m) if m is not None else next((x for x in self._all_messages() if x.get("id") == mid), None)
        return self.shown(d) if d else None

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

    def live(self, agent: str, start: int) -> Dict[str, Any]:
        """Lines `start` on of live/<agent>.jsonl (complete lines only)."""
        if not _NAME.match(agent):
            return {"items": [], "next": start}
        lines = _read(self.run_dir / "live" / f"{agent}.jsonl").split("\n")[:-1]
        items = [_live_item(ln) for ln in lines[start:start + LIVE_PAGE]]
        return {"items": [i for i in items if i], "next": min(len(lines), start + LIVE_PAGE)}

    def text_file(self, path: str) -> Dict[str, Any]:
        text = _read(Path(path)) if path else ""
        return {"text": text, "digest": text_digest(text), "path": path}

    def golden(self) -> Dict[str, Any]:
        return self.text_file(((self.state().get("golden") or {}).get("path")) or "")

    def audit_notes(self) -> Dict[str, Any]:
        """CONTRACT: <folder of run.decisions_path, else the run folder>/audit-decisions.md."""
        dp = (self.state().get("run") or {}).get("decisions_path")
        return self.text_file(str((Path(dp).parent if dp else self.run_dir) / "audit-decisions.md"))

    def audit(self) -> Dict[str, Any]:
        """CONTRACT: the newest audits/*.md of the run folder; state["audit_running"] says it is under way."""
        files = sorted((self.run_dir / "audits").glob("*.md"))
        return {"report": _read(files[-1])[:60000] if files else "", "running": bool(self.state().get("audit_running"))}

    # -- runs (UI-6)
    def runs(self) -> Dict[str, Any]:
        out = []
        for key, rd in self._run_folders().items():
            st = read_json(rd / "state.json", {}) or {}
            old = "agents" not in st
            run = st if old else (st.get("run") or {})
            status = str(st.get("status") or "") if old else ("finished" if st.get("finished") else "running")
            out.append({"id": key, "project": key.split("/")[0], "status": status, "old": old,
                        "started": _epoch(run.get("started_at" if old else "started"))})
        return {"runs": sorted(out, key=lambda r: -r["started"])}

    def run(self, key: str) -> Dict[str, Any]:
        rd = self._run_folder(key)
        if rd is None:
            return {"error": "not found"}
        st = read_json(rd / "state.json", {}) or {}
        if "agents" in st:
            try:
                d = public_state(from_dict(Snapshot, st))
            except (ValueError, TypeError, KeyError, AttributeError):
                return {"error": "unreadable run"}
        else:
            d = public_state(_old_snapshot(rd, st))
        return {**self.shown(d), "old": "agents" not in st, "rev": 0}

    def _run_folders(self) -> Dict[str, Path]:
        found = {}
        for p in sorted(self.runs_root.glob("*/*/state.json")) if self.runs_root else []:
            found[f"{p.parent.parent.name}/{p.parent.name}"] = p.parent
        return found

    def _run_folder(self, key: str) -> Optional[Path]:
        return self._run_folders().get(key)  # only folders found by listing: no path from the request is used


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
    for p in sorted(rd.glob("rounds/r*/t*.json")):
        t = read_json(p) or {}
        aid, role = str(t.get("agent") or ""), Role(str(t.get("role") or "worker"))
        if not aid or not t.get("turn_id"):
            continue
        a = agents.setdefault(aid, Agent(aid, role=role, display=aid, lifecycle=Lifecycle.CLOSED,
                                         status=AgentStatus.CLOSED))
        a.actual_model = str((t.get("meta") or {}).get("model") or a.actual_model)
        src, dst = _FLOW.get(str(t.get("purpose")), (role.value, "user"))
        messages[t["turn_id"]] = Message(
            t["turn_id"], sender=aid, recipient=dst if src == role.value else src, body=str(t.get("output") or ""),
            needs_answer=False, turn=t["turn_id"], created=_epoch(t.get("started_at")), seq=len(messages) + 1,
            status=MessageStatus.ANSWERED)
    run = RunInfo(id=str(st.get("run_id") or rd.name), project=str(st.get("monitor") or ""),
                  goal=str(st.get("goal") or ""), started=_epoch(st.get("started_at")))
    return Snapshot(now=_epoch(st.get("updated_at")), run=run, agents=agents, messages=messages, finished=True)


# ------------------------------------------------------------------------------------------------------- server

class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that went away mid-request; the details could hold anything


def _handler(board: Board, bind: str):
    loopback = bind in _LOOPBACK

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
            """No other web page may drive the dashboard: on loopback only loopback names are served (DNS
            rebinding), and a request from another origin is refused."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if loopback and host not in _LOOPBACK:
                return False
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

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
                    self.js(board.live(one("agent"), max(0, int(one("from") or 0))))
                elif api == "events":
                    self.events()
                elif api == "golden":
                    self.js(board.golden())
                elif api == "audit_notes":
                    self.js(board.audit_notes())
                elif api == "audit":
                    self.js(board.audit())
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
                offsets: Dict[str, int] = {}
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

        def live_lines(self, offsets: Dict[str, int]) -> bytes:
            """New complete lines of live/*.jsonl since this stream began (the first look only notes the ends)."""
            first, out = not offsets and "_" not in offsets, []
            offsets.setdefault("_", 0)
            for f in sorted((board.run_dir / "live").glob("*.jsonl")):
                try:
                    size = f.stat().st_size
                    off = offsets.get(f.name, size if first else 0)
                    if size > off:
                        with f.open("rb") as fh:
                            fh.seek(off)
                            chunk = fh.read(min(size - off, MAX_FILE))
                        cut = chunk.rfind(b"\n") + 1
                        off += cut
                        for ln in chunk[:cut].decode("utf-8", "replace").splitlines():
                            item = _live_item(ln)
                            if item:
                                out.append(b"event: live\ndata: " + json.dumps(
                                    {"agent": f.stem, **item}, ensure_ascii=False).encode() + b"\n\n")
                    offsets[f.name] = off if size >= off else 0
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
            srv = _Server((host, p), _handler(board, host))
            break
        except OSError as e:
            last = e
    else:
        raise OSError(f"no free port from {port}: {last}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shown = host if host not in ("0.0.0.0", "::", "") else _lan_ip()
    return srv, f"http://{shown}:{srv.server_address[1]}/"


def _lan_ip() -> str:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
