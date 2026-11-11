"""The board: a small local web server where I follow a run and talk to the manager.

``orch run cfg.yaml --serve`` (same process as the run) or ``orch serve cfg.yaml`` (any time, also after the
run) starts it on http://127.0.0.1:8765. Python standard library only.

Everything goes through the run folder, so the board works the same in or out of the run's process:
answers and approvals become ``inbox/*.json`` control files, the live view tails ``live/*.jsonl``, and the
chat with the manager resumes the manager's own session (under the same per-agent lock the run uses), with
the exchange kept in ``chat.jsonl``.

Endpoints: GET / · GET /log/… (the run logs: index.html, each run's report.html and files) · GET /api/state · GET /api/turn?id= · GET /api/events (SSE) ·
POST /api/chat · /api/answer · /api/gate · /api/note · /api/stop
"""

from __future__ import annotations

import json
import mimetypes
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
from . import report as R
from .report import checklist_items, turn_costs
from .templates import render

_PURPOSE_FLOW = {  # who talks to whom in each kind of turn
    "plan": ("manager", "human"), "kickoff": ("manager", "critic"), "restart": ("manager", "critic"), "answer": ("manager", "critic"),
    "review": ("manager", "human"), "feedback": ("manager", "critic"), "brief": ("critic", "worker"),
    "verify": ("critic", "worker"), "amend": ("critic", "worker"), "guidance": ("critic", "worker"),
    "report": ("critic", "manager"), "work": ("worker", "critic"),
    "checkin": ("critic", "manager"), "ack": ("manager", "human"), "pause": ("manager", "human"),  # only its NOTE FOR <critic> part reaches the critic
}


PREVIEW_CHARS = 900  # a turn's output is sent in full up to this length; longer ones are fetched with /api/turn
CHAT_TAIL = 60       # chat messages per conversation in the state payload; older ones come from /api/chat


def _chat_rows(rd: Path) -> List[Dict[str, Any]]:
    """chat.jsonl with the lines of each message merged by id, in the order the messages first appeared."""
    rows: Dict[Any, Dict[str, Any]] = {}
    for x in _lines(rd / "chat.jsonl"):
        try:
            c = json.loads(x)
        except json.JSONDecodeError:
            continue
        if isinstance(c, dict):
            rows.setdefault(c.get("id"), {}).update(c)
    return list(rows.values())


def _chat_view(rows: List[Dict[str, Any]], mgr: str) -> Tuple[List[Dict[str, Any]], Dict[str, bool]]:
    """Every question message, and the latest CHAT_TAIL of each conversation. ``more`` names the conversations
    that have older messages."""
    groups: Dict[str, List[int]] = {}
    keep = {i for i, c in enumerate(rows) if c.get("qid")}
    for i, c in enumerate(rows):
        if not c.get("qid"):
            groups.setdefault(c.get("with") or mgr, []).append(i)
    more: Dict[str, bool] = {}
    for k, idx in groups.items():
        keep.update(idx[-CHAT_TAIL:])
        if len(idx) > CHAT_TAIL:
            more[k] = True
    return [c for i, c in enumerate(rows) if i in keep], more


def _chat_before(rows: List[Dict[str, Any]], with_: str, before: str, mgr: str) -> Dict[str, Any]:
    """The CHAT_TAIL messages of one conversation that come before message ``before``."""
    group = [c for c in rows if not c.get("qid") and (c.get("with") or mgr) == with_]
    cut = next((i for i, c in enumerate(group) if c.get("id") == before), len(group))
    older = group[:cut]
    return {"rows": older[-CHAT_TAIL:], "more": len(older) > CHAT_TAIL}


class Board:
    def __init__(self, name: str, run_dir: Callable[[], Optional[Path]], mon: Optional[MonitorConfig] = None):
        self.name, self._run_dir, self.mon = name, run_dir, mon
        self._chat_lock = threading.Lock()
        self._rows: Dict[Path, Any] = {}  # turn file -> ((mtime, size), (row, checklist item) or None)

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
            hit = self._turn_entry(p)
            if hit is not None:
                turns.append(hit[0])
                if hit[1] is not None:
                    series.append(hit[1])
        turns.sort(key=lambda r: (r.get("started_at") or "", r["turn_id"]))
        live = {}
        for t in turns:
            if t["status"] == "running":
                live[t["turn_id"]] = self._live_text(rd / "live" / f"{t['turn_id']}.jsonl")
        chat_all = _chat_rows(rd)
        mgr_name = self.mon.by_role("manager")[0].name if self.mon is not None and self.mon.by_role("manager") else "manager"
        chat, chat_more = _chat_view(chat_all, mgr_name)
        for c in chat:
            if c.get("status") == "running":
                live[c["id"]] = self._live_text(rd / "live" / f"{c['id']}.jsonl")
        relays = _relays(rd, chat, turns)
        total_cost = _turn_costs(turns, chat_all)
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
            "chat": chat, "chat_more": chat_more, "relays": relays, "live": live, "flow": _PURPOSE_FLOW, "queued_answers": queued,
            "worker_messages": st.get("worker_messages") or [],
            "min_score": (cfg.get("controller") or {}).get("min_score"),
            "can_chat": self.mon is not None and bool(self.mon.by_role("manager")),
            "audit": self._audit(rd),
        }

    def _audit(self, rd: Path) -> Dict[str, Any]:
        """The auditor's findings file and the history of its audits (newest first), read-only."""
        from . import audit as A
        auditors = self.mon.by_role("auditor") if self.mon is not None else []
        findings = ""
        if auditors:
            try:
                findings = A.findings_path(auditors[0], rd).read_text(encoding="utf-8")
            except OSError:
                findings = ""
        history = []
        files = sorted((rd / "audits").glob("*.md"), reverse=True)[:20] if (rd / "audits").is_dir() else []
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            m = re.match(r"# Audit of rounds (\d+)–(\d+) \((\w+), \$([\d.]+)\)", text)
            reply = text.split("## Reply\n\n", 1)[1] if "## Reply\n\n" in text else ""
            reply = re.split(r"\n## stderr\n", reply, 1)[0].strip()
            history.append({"file": p.name, "rounds": [int(m.group(1)), int(m.group(2))] if m else None,
                            "status": m.group(3) if m else "unknown", "cost": float(m.group(4)) if m else None,
                            "reply": reply[:30000]})
        return {"configured": bool(auditors), "findings": findings[:60000], "history": history}

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

    def _turn_entry(self, p: Path) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
        """One turn file as the page sees it, parsed again only when the file has changed."""
        try:
            stt = p.stat()
        except OSError:
            return None
        key = (stt.st_mtime_ns, stt.st_size)
        hit = self._rows.get(p)
        if hit and hit[0] == key:
            return hit[1]
        entry = None
        t = read_json(p) or {}
        if "turn_id" in t:
            out = t.get("output") or ""
            row = {k: t.get(k) for k in ("turn_id", "round", "seq", "agent", "role", "purpose", "status",
                                         "duration_s", "started_at", "ended_at", "milestone", "asked", "tag_reminder",
                                         "cut_short", "interruptions")}
            row["output"] = out[:PREVIEW_CHARS]
            row["output_len"] = len(out)
            row["revise"] = bool(re.search(r"<decision>\s*REVISE", out, re.I))
            row["cost"] = (t.get("meta") or {}).get("total_cost_usd")  # a session's running total; see _turn_costs
            row["session_id"] = t.get("session_id")
            item = None
            if t.get("role") == "verifier":
                from . import tags as T
                ck = T.bodies(out, "checklist")
                body = "\n".join(ck) if ck else out  # older runs: the list lines anywhere in the reply
                items = checklist_items(body)
                if items and t.get("purpose") == "verify":  # briefs and amends list items still to do, not progress
                    row["checklist"] = [sum(1 for d, _ in items if d), len(items)]
                    row["checklist_text"] = body
                    item = {"turn": t["turn_id"], "milestone": t.get("milestone"),
                            "done": row["checklist"][0], "total": row["checklist"][1], "purpose": t.get("purpose")}
                v = T.value(out, "verdict")
                m = [v] if v in ("PASS", "FAIL") else re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", out, re.I)
                if m:
                    row["verdict"] = m[-1].upper()
            entry = (row, item)
        self._rows[p] = (key, entry)
        return entry

    def chat_before(self, with_: str, before: str) -> Dict[str, Any]:
        """Older chat messages of one conversation (for "show older")."""
        rd = self.run_dir
        if rd is None:
            return {"rows": [], "more": False}
        mgr = self.mon.by_role("manager")[0].name if self.mon is not None and self.mon.by_role("manager") else "manager"
        return _chat_before(_chat_rows(rd), with_ or mgr, before, mgr)

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

    def chat_agent(self, name: str, message: str) -> Dict[str, Any]:
        """Talk to the critic or a worker directly. Its reply goes into its own conversation (the same session the run
        uses) and is shown here. Its reply does not route <message> tags: nothing is sent on by this chat."""
        rd = self.run_dir
        try:
            a = self.mon.agent(name)
        except Exception:
            return {"error": f"no agent named {name}"}
        if a.role not in ("verifier", "worker"):
            return {"error": f"{a.called} is not talked to from here"}
        st = read_json(rd / "state.json", {}) or {}
        if not ((st.get("sessions") or {}).get(a.name) or {}).get("session_id"):
            return {"error": f"{a.called} has no conversation yet; try again after its first turn"}
        last = [json.loads(x) for x in _lines(rd / "chat.jsonl")]
        cid = f"chat-{len(last) + 1:03d}-{int(time.time())}"
        _append(rd / "chat.jsonl", {"id": cid + "-q", "role": "human", "text": message, "ts": now_iso(), "with": a.name})
        _append(rd / "chat.jsonl", {"id": cid, "role": "agent", "who": a.name, "with": a.name, "status": "running",
                                    "ts": now_iso()})

        def work() -> None:
            text, status, new_sid, cost = "", "error", None, None
            try:
                with self._chat_lock, agent_lock(rd, a.name):  # waits for the agent's current turn to end
                    # read the session now, not when the message was sent: a milestone may have started a new one
                    sid = ((read_json(rd / "state.json", {}) or {}).get("sessions") or {}).get(a.name, {}).get("session_id")
                    if not sid:
                        raise RuntimeError(f"{a.called} has no conversation now")
                    prompt = (message + "\n\n<system-reminder>\nThe user is talking to you directly from the board. "
                              "Answer them. Do not start new work or change files unless they ask for it. Your reply is "
                              "shown to them.\n</system-reminder>")
                    res = get_backend(a.backend).run(TurnRequest(
                        agent=a.name, role=a.role, prompt=prompt, system_prompt=a.current_system_prompt(),
                        session_id=sid, cwd=a.working_dir, timeout=a.timeout, options=a.backend_options,
                        env=a.env, context={"purpose": "chat", "monitor": self.mon.name},
                        on_event=lambda ev: live_event(rd, cid, ev)), lambda: False)
                text, status, new_sid = res.output, res.status, res.session_id or sid
                cost = (res.meta or {}).get("total_cost_usd")
                if res.status != "ok" and not text:
                    text = (res.stderr or "")[-1500:] or f"(chat turn ended with status {res.status})"
            except Exception as e:  # pragma: no cover
                text = f"({e})"
            _append(rd / "chat.jsonl", {"id": cid, "role": "agent", "who": a.name, "with": a.name, "status": status,
                                        "text": text, "ts": now_iso(), "session_id": new_sid, "cost": cost})

        threading.Thread(target=work, daemon=True).start()
        return {"id": cid}

    def chat(self, message: str, qid: Optional[str] = None, settle: bool = False,
             agent: Optional[str] = None) -> Dict[str, Any]:
        """Talk to the manager, either in general or about one of its questions (``qid``). ``settle`` asks
        the manager to sum up what was agreed; that summary becomes the answer the critic receives.
        ``agent`` names the critic or a worker instead: a plain talk in that agent's own conversation."""
        if self.mon is None or not self.mon.by_role("manager"):
            return {"error": "chat needs the run's config (start the board with `orch serve <config>`)"}
        if agent and agent != self.mon.by_role("manager")[0].name:
            if qid or settle:
                return {"error": "questions are settled with the manager"}
            return self.chat_agent(agent, message)
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
                if values["message"].strip():  # what I typed with "Settle it" is my answer: it must reach the manager
                    message = render(self.mon.controller.templates["manager_question_settle_mine"], **values).strip()
                if first:
                    message = render(self.mon.controller.templates["manager_question_chat"],
                                     **{**values, "message": message}).strip()
            elif first:
                message = render(self.mon.controller.templates["manager_question_chat"], **values).strip()
        else:
            shown = message
        sess = st.get("sessions", {}).get(mgr.name) or {}
        since = sess.get("reset_at") or ""  # a cleared session is not resumed from an older chat
        sid = next((c.get("session_id") for c in reversed(last) if c.get("session_id") and (c.get("ts") or "") > since),
                   None) or sess.get("session_id")
        if not sid and since:  # cleared: the chat starts the new session, with the handover the run would give it
            handover = "\n\n".join((st.get("fwd") or {}).get(mgr.name) or [])
            message = (handover + "\n\n---\n\n" + message).strip() if handover else message
        elif not sid:
            return {"error": f"{mgr.called} has not started yet; try again in a moment"}
        cid = f"chat-{len(last) + 1:03d}-{int(time.time())}"
        tag = {"qid": qid, "settle": True} if settle else ({"qid": qid} if qid else {})
        _append(rd / "chat.jsonl", {"id": cid + "-q", "role": "human", "text": shown, "ts": now_iso(), **tag})
        _append(rd / "chat.jsonl", {"id": cid, "role": "manager", "status": "running", "ts": now_iso(), **tag})

        def work() -> None:
            text, status, new_sid, cost = "", "error", sid, None
            try:
                with self._chat_lock, agent_lock(rd, mgr.name):
                    prompt = message
                    if self.mon.controller.protocol:
                        critic = (self.mon.by_role("verifier") or [mgr])[0]
                        note = render(self.mon.controller.templates["manager_direct_voice"], critic=critic.called).strip()
                        if note:
                            prompt += f"\n\n<system-reminder>\n{note}\n</system-reminder>"
                    res = get_backend(mgr.backend).run(TurnRequest(
                        agent=mgr.name, role=mgr.role, prompt=prompt, system_prompt=mgr.current_system_prompt(),
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
            from . import tags as T
            from .managed import manager_pause
            pause = manager_pause(text) if status == "ok" else ""
            if pause:  # the manager stops the run from the chat
                self.control({"kind": "pause", "message": pause})
            for attrs, body in T.blocks(text or "", "message"):  # <message to="critic"> -> that agent's next turn
                for a in self.mon.agents:
                    if body and a.name != mgr.name and a.role != "auditor" and T.addressed(attrs, a.name, a.called):
                        self.control({"to": [a.name], "message": body, "from": mgr.name,
                                      **({"urgent": True} if T.urgent(attrs) else {})})

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


def _relays(rd: Path, chat: List[Dict[str, Any]], turns: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """For each chat reply, what became of the <message to="…"> blocks it passed on: still waiting in inbox/,
    or delivered (when, and in which turn of the agent it was for)."""
    from . import tags as T
    notes: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}
    for where, d in (("delivered", rd / "inbox" / "consumed"), ("waiting", rd / "inbox")):
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            x = read_json(p) or {}
            if isinstance(x, dict) and x.get("from") and not x.get("kind"):
                notes[str(x.get("message", "")).strip()] = (p.name, where, x)
    if not notes:
        return {}
    taken: Dict[str, str] = {}  # inbox file -> when a turn picked it up
    for ln in _lines(rd / "events.jsonl"):
        if '"human_messages"' not in ln:
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        for f in ev.get("files") or []:
            taken.setdefault(f, ev.get("ts") or "")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for c in chat:
        if c.get("role") != "manager" or c.get("status") != "ok":
            continue
        for _attrs, body in T.blocks(c.get("text") or "", "message"):
            hit = notes.get((body or "").strip())
            if not hit:
                continue
            name, where, x = hit
            to = x.get("to") or []
            row: Dict[str, Any] = {"to": to[0] if to else None, "status": where, "urgent": bool(x.get("urgent"))}
            if where == "delivered":
                ts = taken.get(name, "")
                row["at"] = ts
                t = next((t for t in turns if t.get("agent") in to and ts and (t.get("started_at") or "") >= ts), None)
                if t:
                    row.update(turn=t["turn_id"], purpose=t.get("purpose"), turn_status=t.get("status"))
            out.setdefault(c["id"], []).append(row)
    return out


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
            elif u.path == "/api/chat":
                self.js(self.board(q).chat_before((q.get("with") or [""])[0], (q.get("before") or [""])[0]))
            elif u.path == "/api/turn":
                self.js(self.board(q).turn((q.get("id") or [""])[0]))
            elif u.path == "/api/events":
                self.events(self.board(q))
            elif u.path == "/log" or u.path.startswith("/log/"):
                self.log_file(u.path[len("/log"):].lstrip("/"), q)
            else:
                self.send(404, b"not found", "text/plain")

        def redirect(self, to: str) -> None:
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_file(self, rel: str, q: Dict[str, List[str]]) -> None:
            """The run logs, read-only: /log/ is the runs index, /log/run the current run's report, and any
            file below the output folder by its path there. Reports are rendered fresh on each request."""
            roots = []
            for b in boards.values():
                rd = b.run_dir
                if rd is not None and rd.parent.parent.resolve() not in roots:
                    roots.append(rd.parent.parent.resolve())
            if not roots:
                return self.send(404, b"no run yet", "text/plain")
            if rel == "run":
                rd = self.board(q).run_dir
                if rd is None:
                    return self.redirect("index.html")
                return self.redirect(f"{rd.parent.name}/{rd.name}/report.html")
            rel = rel or "index.html"
            for root in roots:
                f = (root / rel).resolve()
                if not (f == root or root in f.parents):
                    continue  # nothing outside the output folder
                if f.name == "index.html" and f.parent == root:
                    return self.send(200, R.render_index(root).encode("utf-8"), "text/html; charset=utf-8")
                if f.name == "report.html" and (f.parent / "state.json").is_file():
                    return self.send(200, R.render_run_report(f.parent).encode("utf-8"), "text/html; charset=utf-8")
                if f.is_file():
                    ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
                    if f.suffix in (".md", ".jsonl", ".log", ".txt", ".yaml", ".yml", ".tex", ".csv", ".py") or ctype.startswith("text/"):
                        ctype = ("text/html" if f.suffix in (".html", ".htm") else "text/plain") + "; charset=utf-8"
                    return self.send(200, f.read_bytes(), ctype)
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
                return self.js(b.chat(msg, qid=body.get("qid"), settle=settle, agent=body.get("agent") or None))
            if u.path == "/api/answer":
                b.control({"kind": "answer", "qid": body.get("qid"), "message": msg})
            elif u.path == "/api/job":
                b.control({"kind": "job"})
            elif u.path == "/api/gate":
                b.control({"kind": "gate", "gate": body.get("gate"), "message": msg,
                           "decision": "approve" if body.get("decision") == "approve" else "revise"})
            elif u.path == "/api/note":
                b.control({"to": [body["to"]] if body.get("to") else None, "message": msg,
                           **({"urgent": True} if body.get("urgent") else {})})
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
            self.send_header("X-Accel-Buffering", "no")
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


def _turn_costs(turns: List[Dict[str, Any]], chat: List[Dict[str, Any]]) -> Optional[float]:
    """Each turn's own cost, in place, and the run's total (board chats share the manager's session)."""
    return turn_costs(list(turns) + [c for c in chat if c.get("role") == "manager" and c.get("status") is not None])


def page() -> str:
    """board.html, read on every page load so a UI change shows up without restarting the run."""
    try:
        return Path(__file__).with_name("board.html").read_text(encoding="utf-8")
    except OSError:
        return "<p>board.html missing</p>"
