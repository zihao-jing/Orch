"""The board: a small local web server where I follow a run and talk to the manager.

``orch run cfg.yaml --serve`` (same process as the run) or ``orch serve cfg.yaml`` (any time, also after the
run) starts it on http://127.0.0.1:8765. Python standard library only.

Everything goes through the run folder, so the board works the same in or out of the run's process:
answers and approvals become ``inbox/*.json`` control files, the live view tails ``live/*.jsonl``, and the
chat with the manager resumes the manager's own session (under the same per-agent lock the run uses), with
the exchange kept in ``chat.jsonl``.

Endpoints: GET / · GET /api/state · GET /api/turn?id= · GET /api/events (SSE) ·
POST /api/chat · /api/answer · /api/gate · /api/note · /api/stop
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .backends import TurnRequest, get_backend
from .config import MonitorConfig
from .recorder import agent_lock, live_event, now_iso, read_json
from .report import checklist_items
from .templates import render

_PURPOSE_FLOW = {  # who talks to whom in each kind of turn
    "plan": ("manager", "human"), "kickoff": ("manager", "critic"), "answer": ("manager", "critic"),
    "review": ("manager", "human"), "feedback": ("manager", "critic"), "brief": ("critic", "worker"),
    "verify": ("critic", "worker"), "amend": ("critic", "worker"), "guidance": ("critic", "worker"),
    "report": ("critic", "manager"), "work": ("worker", "critic"),
    "checkin": ("critic", "manager"), "ack": ("manager", "human"),  # only its NOTE FOR <critic> part reaches the critic
}


class Board:
    def __init__(self, name: str, run_dir: Callable[[], Optional[Path]], mon: Optional[MonitorConfig] = None):
        self.name, self._run_dir, self.mon = name, run_dir, mon
        self._chat_lock = threading.Lock()

    @property
    def run_dir(self) -> Optional[Path]:
        return self._run_dir()

    # ------------------------------------------------------------------ data
    def data(self) -> Dict[str, Any]:
        rd = self.run_dir
        if rd is None or not (rd / "state.json").is_file():
            return {"monitor": self.name, "status": "starting"}
        st = read_json(rd / "state.json", {}) or {}
        cfg = read_json(rd / "config.json", {}) or {}
        turns, series = [], []
        for p in sorted((rd / "rounds").glob("r*/t*.json")):
            t = read_json(p) or {}
            if "turn_id" not in t:
                continue
            out = t.get("output") or ""
            row = {k: t.get(k) for k in ("turn_id", "round", "seq", "agent", "role", "purpose", "status",
                                         "duration_s", "started_at", "ended_at", "milestone", "asked")}
            row["output"] = out[:30000]
            row["cost"] = (t.get("meta") or {}).get("total_cost_usd")  # a session's running total; see _turn_costs
            row["session_id"] = t.get("session_id")
            if t.get("role") == "verifier":
                items = checklist_items(out)
                if items and t.get("purpose") == "verify":  # briefs and amends list items still to do, not progress
                    row["checklist"] = [sum(1 for d, _ in items if d), len(items)]
                    series.append({"turn": t["turn_id"], "milestone": t.get("milestone"),
                                   "done": row["checklist"][0], "total": row["checklist"][1],
                                   "purpose": t.get("purpose")})
                m = re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", out, re.I)
                if m:
                    row["verdict"] = m[-1].upper()
            turns.append(row)
        turns.sort(key=lambda r: (r.get("started_at") or "", r["turn_id"]))
        live = {}
        for t in turns:
            if t["status"] == "running":
                live[t["turn_id"]] = self._live_text(rd / "live" / f"{t['turn_id']}.jsonl")
        chat = [json.loads(x) for x in _lines(rd / "chat.jsonl")]
        for c in chat:
            if c.get("status") == "running":
                live[c["id"]] = self._live_text(rd / "live" / f"{c['id']}.jsonl")
        total_cost = _turn_costs(turns, chat)
        queued: Dict[str, List[str]] = {}  # answers sent but not yet picked up (the run reads them between turns)
        for p in sorted((rd / "inbox").glob("*.json")):
            d = read_json(p) or {}
            if isinstance(d, dict) and d.get("kind") == "answer" and d.get("qid"):
                queued.setdefault(str(d["qid"]).upper(), []).append(str(d.get("message", "")))
        agents = [{"name": a["name"], "role": a["role"], "called": a.get("display_name") or a["name"],
                   "backend": a.get("backend"), "model": (a.get("backend_options") or {}).get("model")}
                  for a in cfg.get("agents") or []]
        return {
            "monitor": st.get("monitor"), "run_id": st.get("run_id"), "status": st.get("status"),
            "mode": st.get("mode"), "goal": st.get("goal"), "round": st.get("round"), "max_rounds": st.get("max_rounds"),
            "cost": total_cost, "turn_counts": st.get("turn_counts"), "updated_at": st.get("updated_at"),
            "started_at": st.get("started_at"), "error": st.get("error"),
            "managed": st.get("managed") or {}, "questions": st.get("questions") or [],
            "decisions": st.get("decisions") or [],
            "reviews": st.get("reviews") or [], "agents": agents, "turns": turns, "series": series,
            "chat": chat, "live": live, "flow": _PURPOSE_FLOW, "queued_answers": queued,
            "min_score": (cfg.get("controller") or {}).get("min_score"),
            "can_chat": self.mon is not None and bool(self.mon.by_role("manager")),
        }

    @staticmethod
    def _live_text(p: Path) -> Dict[str, Any]:
        text, tools = [], []
        for ln in _lines(p):
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if ev.get("k") == "text":
                text.append(ev.get("s", ""))
            elif ev.get("k") == "tool":
                tools.append(ev.get("s", ""))
            elif ev.get("k") == "break" and text and not text[-1].endswith("\n"):
                text.append("\n\n")
        return {"text": "".join(text)[-6000:], "tools": tools[-12:]}

    def turn(self, tid: str) -> Dict[str, Any]:
        rd = self.run_dir
        for p in (rd / "rounds").glob("r*/t*.json") if rd else []:
            t = read_json(p) or {}
            if t.get("turn_id") == tid:
                return {k: t.get(k) for k in ("turn_id", "agent", "purpose", "status", "input", "output",
                                              "system_prompt", "stderr", "session_id", "duration_s")}
        return {"error": "not found"}

    # ------------------------------------------------------------------ actions (all via the run folder)
    def control(self, data: Dict[str, Any]) -> None:
        inbox = self.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        kind = data.get("kind", "note")
        (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 100000:05d}-board-{kind}.json") \
            .write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def chat(self, message: str, qid: Optional[str] = None, settle: bool = False) -> Dict[str, Any]:
        """Talk to the manager, either in general or about one of its questions (``qid``). ``settle`` asks
        the manager to sum up what was agreed; that summary becomes the answer the critic receives."""
        if self.mon is None or not self.mon.by_role("manager"):
            return {"error": "chat needs the run's config (start the board with `orch serve <config>`)"}
        rd = self.run_dir
        st = read_json(rd / "state.json", {}) or {}
        mgr = self.mon.by_role("manager")[0]
        last = [json.loads(x) for x in _lines(rd / "chat.jsonl")]
        q = None
        if qid:
            qid = str(qid).upper()
            q = next((x for x in st.get("questions") or [] if x.get("id") == qid), None)
            if not q:
                return {"error": f"no question {qid}"}
            if q.get("status") == "answered":
                return {"error": f"{qid} is already settled"}
            if any(c.get("qid") == qid and c.get("settle") for c in last):
                return {"error": f"{qid} is being settled"}
            names = {r: (self.mon.by_role(r) or [mgr])[0].called for r in ("manager", "verifier")}
            values = dict(qid=qid, question=_question_line(q.get("text", "")), message=message,
                          manager=names["manager"], critic=names["verifier"])
            first = not any(c.get("qid") == qid for c in last)
            shown = message or "Let's settle it."
            if settle:
                message = render(self.mon.controller.templates["manager_question_settle"], **values).strip()
                if first:
                    message = render(self.mon.controller.templates["manager_question_chat"],
                                     **{**values, "message": message}).strip()
            elif first:
                message = render(self.mon.controller.templates["manager_question_chat"], **values).strip()
        else:
            shown = message
        sid = next((c.get("session_id") for c in reversed(last) if c.get("session_id")), None) \
            or (st.get("sessions", {}).get(mgr.name) or {}).get("session_id")
        if not sid:
            return {"error": f"{mgr.called} has not started yet; try again in a moment"}
        cid = f"chat-{len(last) + 1:03d}-{int(time.time())}"
        tag = {"qid": qid, "settle": True} if settle else ({"qid": qid} if qid else {})
        _append(rd / "chat.jsonl", {"id": cid + "-q", "role": "human", "text": shown, "ts": now_iso(), **tag})
        _append(rd / "chat.jsonl", {"id": cid, "role": "manager", "status": "running", "ts": now_iso(), **tag})

        def work() -> None:
            text, status, new_sid, cost = "", "error", sid, None
            try:
                with self._chat_lock, agent_lock(rd, mgr.name):
                    res = get_backend(mgr.backend).run(TurnRequest(
                        agent=mgr.name, role=mgr.role, prompt=message, system_prompt=mgr.current_system_prompt(),
                        session_id=sid, cwd=mgr.working_dir, timeout=mgr.timeout, options=mgr.backend_options,
                        env=mgr.env, context={"purpose": "chat", "monitor": self.mon.name},
                        on_event=lambda ev: live_event(rd, cid, ev)), lambda: False)
                text, status, new_sid = res.output, res.status, res.session_id or sid
                cost = (res.meta or {}).get("total_cost_usd")
                if res.status != "ok" and not text:
                    text = (res.stderr or "")[-1500:] or f"(chat turn ended with status {res.status})"
            except Exception as e:  # pragma: no cover
                text = f"(chat failed: {e!r})"
            _append(rd / "chat.jsonl", {"id": cid, "role": "manager", "status": status, "text": text,
                                        "ts": now_iso(), "session_id": new_sid, "cost": cost, **tag})
            if settle and status == "ok" and text.strip():  # the agreed answer goes to the run like any answer
                self.control({"kind": "answer", "qid": qid, "message": text.strip(), "via": "conversation"})
            from .managed import note_blocks  # "NOTE FOR critic: ..." -> that agent's next turn
            for who, body in note_blocks(text or "", self.mon.controller.note_pattern, to_end=True):
                who = re.sub(r"^the\s+", "", who.lower())
                for a in self.mon.agents:
                    if who in (a.name.lower(), re.sub(r"^the\s+", "", a.called.lower())) and a.name != mgr.name:
                        self.control({"to": [a.name], "message": body, "from": mgr.name})

        threading.Thread(target=work, daemon=True).start()
        return {"id": cid}


def _question_line(text: str) -> str:
    lines = [re.sub(r"^\W*(QUESTIONS?|NEEDS[_ ]HUMAN)\b[^:\n]*:?\s*", "", ln.strip(), flags=re.I).strip()
             for ln in (text or "").split("\n")]
    lines = [ln for ln in lines if ln]
    return next((ln for ln in lines if ln.endswith("?")), lines[0] if lines else text)[:400]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys as _sys
        if isinstance(_sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return  # a browser tab closed or reloaded mid-request: harmless
        super().handle_error(request, client_address)


def _lines(p: Path) -> List[str]:
    try:
        return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


def _append(p: Path, row: Dict[str, Any]) -> None:
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- HTTP


def _handler(boards: Dict[str, Board]):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:
            pass

        def board(self, q: Dict[str, List[str]]) -> Board:
            name = (q.get("m") or [""])[0]
            return boards.get(name) or next(iter(boards.values()))

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def js(self, data: Any, code: int = 200) -> None:
            self.send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                self.send(200, page().replace("__MONITORS__", json.dumps(list(boards))).encode("utf-8"),
                          "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self.js(self.board(q).data())
            elif u.path == "/api/turn":
                self.js(self.board(q).turn((q.get("id") or [""])[0]))
            elif u.path == "/api/events":
                self.events(self.board(q))
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            u = urlparse(self.path)
            b = self.board(parse_qs(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self.js({"error": "bad json"}, 400)
            if b.run_dir is None:
                return self.js({"error": "run not started yet"}, 409)
            msg = str(body.get("message", "")).strip()
            if u.path == "/api/chat":
                settle = bool(body.get("settle"))
                if not msg and not settle:
                    return self.js({"error": "empty message"})
                return self.js(b.chat(msg, qid=body.get("qid"), settle=settle))
            if u.path == "/api/answer":
                b.control({"kind": "answer", "qid": body.get("qid"), "message": msg})
            elif u.path == "/api/gate":
                b.control({"kind": "gate", "gate": body.get("gate"), "message": msg,
                           "decision": "approve" if body.get("decision") == "approve" else "revise"})
            elif u.path == "/api/note":
                b.control({"to": [body["to"]] if body.get("to") else None, "message": msg})
            elif u.path == "/api/stop":
                (b.run_dir / "STOP").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
            else:
                return self.js({"error": "unknown"}, 404)
            self.js({"ok": True})

        def events(self, b: Board) -> None:
            """Server-sent events: `state` when anything changed, `live` for each new progress line."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            offsets: Dict[str, int] = {}
            sig: Tuple = ()
            first = True
            last_ping = time.time()
            try:
                while True:
                    rd = b.run_dir
                    if rd is not None:
                        files = [rd / "state.json", rd / "chat.jsonl"]
                        new_sig = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
                        if new_sig != sig:
                            sig = new_sig
                            self.wfile.write(b"event: state\ndata: {}\n\n")
                        ld = rd / "live"
                        for f in (sorted(ld.glob("*.jsonl")) if ld.is_dir() else []):
                            size = f.stat().st_size
                            off = offsets.get(f.name)
                            if off is None:
                                offsets[f.name] = size if first or time.time() - f.stat().st_mtime > 5 else 0
                                if offsets[f.name] == size:
                                    continue
                                off = 0
                            if size > off:
                                with open(f, "rb") as fh:
                                    fh.seek(off)
                                    chunk = fh.read(size - off)
                                cut = chunk.rfind(b"\n") + 1
                                offsets[f.name] = off + cut
                                for ln in chunk[:cut].splitlines():
                                    try:
                                        ev = json.loads(ln)
                                    except json.JSONDecodeError:
                                        continue
                                    ev["turn"] = f.stem
                                    self.wfile.write(b"event: live\ndata: " + json.dumps(ev, ensure_ascii=False)
                                                     .encode("utf-8") + b"\n\n")
                        first = False
                    if time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        last_ping = time.time()
                    self.wfile.flush()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return H


def serve(boards: Dict[str, Board], host: str = "127.0.0.1", port: int = 8765) -> Tuple[ThreadingHTTPServer, str]:
    """Start the board in a background thread; returns (server, url). Tries the next ports if busy."""
    last: Optional[Exception] = None
    for p in range(port, port + 20):
        try:
            srv = _Server((host, p), _handler(boards))
            break
        except OSError as e:
            last = e
    else:
        raise OSError(f"no free port from {port}: {last}")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}/"


def _turn_costs(turns: List[Dict[str, Any]], chat: List[Dict[str, Any]]) -> Optional[float]:
    """Each turn's own cost, in place, and the run's total. Claude reports a resumed session's running total, so a
    turn costs the increase over that session's previous report (board chats share the manager's session)."""
    items = [(t.get("ended_at") or t.get("started_at") or "", t) for t in turns if t.get("status") != "running"]
    items += [(c.get("ts") or "", c) for c in chat if c.get("role") == "manager" and c.get("status") not in (None, "running")]
    last: Dict[str, float] = {}
    total, seen = 0.0, False
    for _, x in sorted(items, key=lambda i: i[0]):
        c = x.get("cost")
        if not isinstance(c, (int, float)):
            continue
        sid = x.get("session_id") or ""
        prev = last.get(sid, 0.0) if sid else 0.0
        x["cost"] = round(c - prev if c >= prev else c, 6)
        if sid:
            last[sid] = c
        total += x["cost"]
        seen = True
    return round(total, 4) if seen else None


def page() -> str:
    """board.html, read on every page load so a UI change shows up without restarting the run."""
    try:
        return Path(__file__).with_name("board.html").read_text(encoding="utf-8")
    except OSError:
        return "<p>board.html missing</p>"
