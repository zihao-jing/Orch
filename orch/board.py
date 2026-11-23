"""The board: a small local web server where I follow a run and talk to the manager.

``orch run cfg.yaml --serve`` (same process as the run) or ``orch serve cfg.yaml`` (any time, also after the
run) starts it on http://127.0.0.1:8765. Python standard library only.

Everything goes through the run folder, so the board works the same in or out of the run's process:
answers and approvals become ``inbox/*.json`` control files, the live view tails ``live/*.jsonl``, and the
chat with the manager resumes the manager's own session (under the same per-agent lock the run uses), with
the exchange kept in ``chat.jsonl``.

Endpoints: GET / · GET /log/<path> (individual files below the output folder; no report or log browsing pages) ·
GET /api/state · /api/items?g= · /api/message?id= · /api/search?q= · /api/turn?id= · /api/chat · /api/audit ·
GET /api/events (SSE) · POST /api/chat · /api/answer · /api/gate · /api/note · /api/night · /api/discussion ·
/api/review · /api/review_comments · /api/job · /api/milestone · /api/stop

B9b: the page has every message (orch/timeline.py). The state carries one line per message for the open step only;
full texts load on unfold (/api/message), folded steps on opening (/api/items), so the payload stays small.

``GET /api/state?since=<ver>`` answers with only what changed since the payload ``<ver>`` (see ``StateDeltas``);
without ``since`` it is the full payload as before. JSON and text replies are gzipped when the browser accepts it.
"""

from __future__ import annotations

import gzip
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import reminders as REM
from . import routing as RT
from .backends import TurnRequest, run_variants
from .config import MonitorConfig
from .recorder import agent_lock, live_event, now_iso, read_json
from . import tags as T
from . import timeline as TL
from .report import checklist_items, review_checklist, turn_costs
from .templates import render

_PURPOSE_FLOW = {  # who talks to whom in each kind of turn
    "plan": ("manager", "human"), "kickoff": ("manager", "critic"), "restart": ("manager", "critic"), "answer": ("manager", "critic"),
    "review": ("manager", "human"), "feedback": ("manager", "critic"), "brief": ("critic", "worker"),
    "verify": ("critic", "worker"), "amend": ("critic", "worker"), "guidance": ("critic", "worker"),
    "report": ("critic", "manager"), "work": ("worker", "critic"),
    "par_assign": ("critic", "worker"), "par_review": ("critic", "worker"),
    "checkin": ("critic", "manager"), "ack": ("manager", "human"), "pause": ("manager", "human"),  # only its NOTE FOR <critic> part reaches the critic
}


STATE_GAP = 1.0     # seconds between two `state` events: a busy run writes state.json several times a second
SSE_MAX_AGE = 120.0  # periodically close streams, including abandoned connections held open by proxies
GZIP_MIN = 1024     # replies shorter than this go out uncompressed
PREVIEW_CHARS = 900  # a turn's output is sent in full up to this length; longer ones are fetched with /api/turn
CHAT_TAIL = 60       # chat messages per conversation in the state payload; older ones come from /api/chat
CHAT_CLIP = 1500     # characters of a chat message in the state payload; the rest with /api/message


def _chat_rows(rd: Path, recover: bool = True) -> List[Dict[str, Any]]:
    """chat.jsonl with the lines of each message merged by id, in the order the messages first appeared."""
    rows: Dict[Any, Dict[str, Any]] = {}
    for x in _lines(rd / "chat.jsonl"):
        try:
            c = json.loads(x)
        except json.JSONDecodeError:
            continue
        if isinstance(c, dict):
            rows.setdefault(c.get("id"), {}).update(c)
    if recover:
        for cid, row in rows.items():
            if row.get("status") != "running" or not row.get("chat_lease"):
                continue
            # A per-chat lease stays held even while queued for the agent lock.
            # Another board may own it; age and agent activity cannot prove interruption.
            with (rd / "locks" / f"{cid}.chat.lock").open("a") as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    current = next((c for c in _chat_rows(rd, recover=False) if c.get("id") == cid), {})
                    if current.get("status") == "running":
                        current.update(_interrupt_chat(rd, cid, "The dashboard chat process stopped before saving its reply."))
                    row.update(current)
                finally:
                    fcntl.flock(lease, fcntl.LOCK_UN)
    return list(rows.values())


def _interrupt_chat(rd: Path, cid: str, reason: str) -> Dict[str, Any]:
    events = []
    for line in _lines(rd / "live" / f"{cid}.jsonl"):
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            continue
    partial = "".join(str(e.get("s") or "") for e in events if e.get("k") == "text")
    row = {"id": cid, "status": "interrupt", "interrupt_reason": reason,
           "interrupted_detected_at": now_iso(), "text": partial or reason}
    if events:
        row["last_activity_ts"] = events[-1].get("ts")
    # Preserve the original chat timestamp; detection time is not the time it stopped.
    _append(rd / "chat.jsonl", row)
    return row


def _start_chat(rd: Path, row: Dict[str, Any], work: Callable[[], None]) -> None:
    """Hold a chat-specific lease from publication through final status, across boards."""
    (rd / "locks").mkdir(exist_ok=True)
    cid = row["id"]
    lease = (rd / "locks" / f"{cid}.chat.lock").open("a")
    fcntl.flock(lease, fcntl.LOCK_EX)

    def run() -> None:
        try:
            work()
        finally:
            try:
                current = next((c for c in _chat_rows(rd, recover=False) if c.get("id") == cid), {})
                if current.get("status") == "running":
                    _interrupt_chat(rd, cid, "The chat task exited before saving its reply.")
            finally:
                lease.close()

    try:
        _append(rd / "chat.jsonl", {**row, "chat_lease": True})
        threading.Thread(target=run, daemon=True).start()
    except BaseException:
        lease.close()
        raise


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


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))


class StateDeltas:
    """The last few state payloads a browser was sent, so its next poll gets only what changed.

    A payload is a dict of top-level keys. Against the version the browser holds, a reply carries
    ``set`` (keys sent whole) and ``patch`` (list keys rebuilt from the old list: ``[start, count]`` copies
    that run of old items, ``{"v": item}`` is a new one), plus ``keys`` (all keys, so dropped ones go).
    Unchanged keys are left out. An unknown or missing base gets the full payload under ``set``."""

    KEEP = 8  # versions remembered; several tabs each poll from their own

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snaps: Dict[str, Dict[str, Tuple[str, Optional[List[str]]]]] = {}  # ver -> key -> (json, items' json)

    def reply(self, data: Dict[str, Any], since: str) -> str:
        enc: Dict[str, Tuple[str, Optional[List[str]]]] = {}
        h = hashlib.sha1()
        for k, v in data.items():
            if isinstance(v, list):
                items = [_dump(x) for x in v]
                enc[k] = ("[" + ",".join(items) + "]", items)
            else:
                enc[k] = (_dump(v), None)
            h.update(_dump(k).encode("utf-8") + b"\0" + enc[k][0].encode("utf-8") + b"\0")
        ver = h.hexdigest()[:16]
        with self._lock:
            old = self._snaps.get(since) if since else None
            self._snaps.pop(ver, None)
            self._snaps[ver] = enc  # newest last
            while len(self._snaps) > self.KEEP:
                self._snaps.pop(next(iter(self._snaps)))
        head = '{"ver":' + _dump(ver) + ',"keys":' + _dump(list(enc))
        if old is None:
            return head + ',"set":{' + ",".join(_dump(k) + ":" + j for k, (j, _) in enc.items()) + "}}"
        sets, patches = [], []
        for k, (j, items) in enc.items():
            was = old.get(k)
            if was is not None and was[0] == j:
                continue
            if items is not None and was is not None and was[1] is not None:
                p = self._patch(was[1], items)
                if len(p) < len(j):
                    patches.append(_dump(k) + ":" + p)
                    continue
            sets.append(_dump(k) + ":" + j)
        return head + ',"base":' + _dump(since) + ',"set":{' + ",".join(sets) + '},"patch":{' + ",".join(patches) + "}}"

    @staticmethod
    def _patch(old: List[str], new: List[str]) -> str:
        at: Dict[str, int] = {}
        for i, s in enumerate(old):
            at.setdefault(s, i)
        ops: List[Any] = []  # [start, count] runs of old items, or the json of a new item
        for s in new:
            i = at.get(s)
            if i is None:
                ops.append('{"v":' + s + "}")
            elif ops and isinstance(ops[-1], list) and ops[-1][0] + ops[-1][1] == i:
                ops[-1][1] += 1
            else:
                ops.append([i, 1])
        return "[" + ",".join(o if isinstance(o, str) else f"[{o[0]},{o[1]}]" for o in ops) + "]"


class LiveEvents:
    """Read live files once per polling interval, shared by all connected browsers."""
    INTERVAL = 0.4
    KEEP = 128

    def __init__(self, board: Any):
        self.board = board
        self.lock = threading.Lock()
        self.scanned = float('-inf')
        self.rd = None
        self.offsets: Dict[str, int] = {}
        self.sig: Tuple = ()
        self.last_state = float('-inf')
        self.seq = 0
        self.batches = deque(maxlen=self.KEEP)

    def poll(self, cursor: Optional[int]) -> Tuple[int, bytes]:
        with self.lock:
            now = time.monotonic()
            if now - self.scanned >= self.INTERVAL:
                data = self.scan(now)
                self.scanned = now
                if data:
                    self.seq += 1
                    self.batches.append((self.seq, data))
                    while len(self.batches) > 1 and sum(len(x[1]) for x in self.batches) > 2 * 1024 * 1024:
                        self.batches.popleft()
            if cursor is None or (self.batches and cursor < self.batches[0][0] - 1):
                return self.seq, b'event: state\ndata: {}\n\n'
            return self.seq, b''.join(data for seq, data in self.batches if seq > cursor)

    def scan(self, now: float) -> bytes:
        rd = self.board.run_dir
        if rd is None:
            return b''
        first = rd != self.rd
        if first:
            self.rd, self.offsets, self.sig = rd, {}, ()
            self.batches.clear()
        files = [rd / 'state.json', rd / 'chat.jsonl']
        stamps = []
        for f in files:
            try:
                stamps.append(f.stat().st_mtime_ns)
            except FileNotFoundError:
                stamps.append(0)
        sig = tuple(stamps) + (self.board._tl[0] if self.board._tl else 0,)
        out = []
        if sig != self.sig and now - self.last_state >= STATE_GAP:
            self.sig, self.last_state = sig, now
            out.append(b'event: state\ndata: {}\n\n')
        for f in sorted((rd / 'live').glob('*.jsonl')):
            try:
                size = f.stat().st_size
                off = self.offsets.get(f.name, size if first else 0)
                if size < off:
                    off = 0
                self.offsets[f.name] = off
                if size <= off:
                    continue
                with f.open('rb') as stream:
                    stream.seek(off)
                    chunk = stream.read(size - off)
            except FileNotFoundError:
                continue
            cut = chunk.rfind(b'\n') + 1
            self.offsets[f.name] = off + cut
            for line in chunk[:cut].splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                event['turn'] = f.stem
                out.append(b'event: live\ndata: ' + json.dumps(event, ensure_ascii=False).encode() + b'\n\n')
        return b''.join(out)


class Board:
    def __init__(self, name: str, run_dir: Callable[[], Optional[Path]], mon: Optional[MonitorConfig] = None):
        self.name, self._run_dir, self.mon = name, run_dir, mon
        self._chat_lock = threading.Lock()
        self._rows: Dict[Path, Any] = {}  # turn file -> ((mtime, size), (row, checklist item, what it got and sent) or None)
        self.deltas = StateDeltas()
        self._events: Dict[str, Any] = {}  # events.jsonl, parsed (orch/timeline.py)
        self._tl_lock = threading.Lock()
        self.background_refresh = False
        self._refresh_lock = threading.Lock()
        self._refresh_error = ""
        self._tl: Optional[Tuple[float, Any, Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], str]] = None
        self.live_events = LiveEvents(self)

    @property
    def run_dir(self) -> Optional[Path]:
        return self._run_dir()

    # ------------------------------------------------------------------ data
    def data(self) -> Dict[str, Any]:
        """The run's full model (every turn row, route and message). The page polls `state()`, a slim view of it."""
        return {k: v for k, v in self._model().items() if not k.startswith("_")}

    def _model(self, full: bool = True, history: bool = True) -> Dict[str, Any]:
        rd = self.run_dir
        if rd is None or not (rd / "state.json").is_file():
            return {"monitor": self.name, "status": "starting"}
        st = read_json(rd / "state.json", {}) or {}
        cfg = read_json(rd / "config.json", {}) or {}
        turns, series, seen = [], [], {}
        paths = ((rd / "rounds").glob("r*/t*.json") if history else
                 (rd / "rounds" / f"r{int(st.get('round') or 0):03d}").glob("t*.json"))
        if not history:
            # A parallel turn may outlive its round. Refresh it until the historical snapshot
            # also knows it finished, so cached "running" badges never resurrect on the next poll.
            cached = self._tl
            running = {t["turn_id"] for t in cached[2].get("turns", []) if t.get("status") == "running"} \
                if cached is not None and cached[1] == rd else set()
            paths = set(paths) | {p for p, (_, entry) in list(self._rows.items()) if entry is not None
                                 and (entry[0].get("status") == "running" or entry[0]["turn_id"] in running)
                                 and p.is_relative_to(rd)}
        for p in sorted(paths):
            hit = self._turn_entry(p)
            if hit is not None:
                turns.append(hit[0])
                seen[hit[0]["turn_id"]] = hit[2]
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
        ledger = st.get("messages") or []
        turn_recipients = {}
        for msg in ledger:
            if msg.get("source") and msg.get("to") and not msg.get("by_design"):
                turn_recipients.setdefault(msg["source"], []).append(
                    {k: msg.get(k) for k in ("to", "status", "i")})
        relays = _relays(rd, chat_all, turns, ledger) if history else {}
        routes, received = _routes(rd, turns, seen, cfg.get("agents") or [], st.get("worker_messages") or [], ledger)
        held = {}  # blocks of a reply orch keeps from the next agent by design: logged, not lost
        for m in ledger:
            if m.get("by_design"):
                held.setdefault(m.get("source") or "", []).append({"kind": m.get("kind"), "to": m.get("to"),
                                                                    "reason": m.get("reason")})
        # _turn_costs rewrites each row's cost into that turn's own step; the rows are cached across polls, so
        # work on copies or the next poll would take the steps for running totals and the total would fall.
        turns = [dict(t) for t in turns]
        total_cost = _turn_costs(turns, chat_all)
        if not history:
            total_cost = st.get("cost_usd")
        cost_estimate = round(sum(t["cost"] for t in turns if t.get("cost_basis") == "estimate"
                                  and isinstance(t.get("cost"), (int, float))), 4)
        if not history:
            cost_estimate = st.get("cost_estimate_usd", 0)
        night_pending: Optional[bool] = st.get("night_queued")  # B9: queued while a question for me is open
        discussion_pending: Optional[bool] = None
        discussion_applying = bool((st.get("managed") or {}).get("discussion_apply")) or any(
            d.get("discussion_result") for d in st.get("gate_queue", []))
        queued: Dict[str, List[str]] = {}  # answers sent but not yet picked up (the run reads them between turns)
        for p in sorted((rd / "inbox").glob("*.json")):
            d = read_json(p) or {}
            if isinstance(d, dict) and d.get("kind") == "night":
                night_pending = bool(d.get("on"))  # the run reads it between turns; the newest one wins
            if isinstance(d, dict) and d.get("kind") == "discussion":
                discussion_pending = bool(d.get("on"))
                discussion_applying = discussion_applying or d.get("action") == "apply"
            if isinstance(d, dict) and d.get("kind") == "answer" and d.get("qid"):
                queued.setdefault(str(d["qid"]).upper(), []).append(str(d.get("message", "")))
        agents = [{"name": a["name"], "role": a["role"], "called": a.get("display_name") or a["name"],
                   "backend": a.get("backend"), "model": (a.get("backend_options") or {}).get("model")}
                  for a in cfg.get("agents") or []]
        known = {a["name"] for a in agents}
        agents += [{"name": a["name"], "role": a["role"], "called": a.get("display_name") or a["name"],  # B11 copies
                    "backend": a.get("backend"), "model": a.get("model"), "copy_of": a.get("copy_of")}
                   for a in st.get("agents_extra") or [] if isinstance(a, dict) and a.get("name") not in known]
        return {
            "monitor": st.get("monitor"), "run_id": st.get("run_id"), "status": st.get("status"),
            "mode": st.get("mode"), "goal": st.get("goal"), "round": st.get("round"), "max_rounds": st.get("max_rounds"),
            "cost": total_cost, "cost_estimate": cost_estimate, "turn_counts": st.get("turn_counts"), "updated_at": st.get("updated_at"),
            "started_at": st.get("started_at"), "error": st.get("error"),
            "managed": st.get("managed") or {}, "night": bool(st.get("night")), "night_pending": night_pending, "questions": st.get("questions") or [],
            "discussion": bool(st.get("discussion")), "discussion_pending": discussion_pending,
            "discussion_applying": discussion_applying,
            "discussion_supported": st.get("discussion_controls") == 2,
            "review_files": _review_files(rd, st),
            "decisions": st.get("decisions") or [],
            "golden": _golden(st),
            "status_lines": st.get("status_lines") or {},  # B5: a live line per working agent from its latest check
            "reliability": st.get("reliability") or {},  # B8: retries waiting and backups in use, per agent
            "stop_reason": st.get("stop_reason"),
            "reviews": st.get("reviews") or [], "agents": agents, "turns": turns, "series": series,
            "chat": chat, "chat_more": chat_more, "relays": relays, "routes": routes, "received": received, "held": held,
            "terminal": _terminal(rd), "live": live, "flow": _PURPOSE_FLOW, "queued_answers": queued,
            "worker_messages": st.get("worker_messages") or [],
            "messages": _message_log(rd, chat_all, turns, {a["name"]: a["called"] for a in agents}, mgr_name,
                                     st.get("worker_messages") or [], st.get("fwd") or {}) if full else [],
            "_chat_all": chat_all,
            "_turn_recipients": turn_recipients,
            "min_score": (cfg.get("controller") or {}).get("min_score"),
            "can_chat": self.mon is not None and bool(self.mon.by_role("manager")),
            "audit": self._audit(rd) if history else {},
            "auditing": st.get("auditing") or None,
        }

    def audit(self) -> Dict[str, Any]:
        """The audit texts in full, for GET /api/audit (the state poll sends only the list)."""
        rd = self.run_dir
        return self._audit(rd, full=True) if rd is not None else {"configured": False, "history": []}

    def _audit(self, rd: Path, full: bool = False) -> Dict[str, Any]:
        """The auditor's findings file and the history of its audits (newest first), read-only. Without `full`
        the texts are left out: the board loads them only when the Auditor card is opened."""
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
                            **({"reply": reply[:30000]} if full else {})})
        out = {"configured": bool(auditors), "history": history, "has_findings": bool(findings.strip())}
        if full:
            out["findings"] = findings[:60000]
        return out

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
        round_path = p.parent / "round.json"
        try:
            rst = round_path.stat()
            round_key = (rst.st_mtime_ns, rst.st_size)
        except OSError:
            round_key = None
        key = (stt.st_mtime_ns, stt.st_size, round_key)
        hit = self._rows.get(p)
        if hit and hit[0] == key:
            return hit[1]
        entry = None
        t = read_json(p) or {}
        if "turn_id" in t:
            out = t.get("output") or ""
            row = {k: t.get(k) for k in ("turn_id", "round", "seq", "agent", "role", "purpose", "status",
                                         "duration_s", "started_at", "ended_at", "milestone", "asked", "tag_reminder", "reminders",
                                         "cut_short", "interruptions", "continues", "model_change", "par_worker",
                                         "par_step", "watching", "watch_kind")}
            row["session_reset"] = "; ".join(str(x.get("reason") or "") for x in t.get("session_reset") or []
                                             if isinstance(x, dict)) or None
            row["output"] = out[:PREVIEW_CHARS]
            row["output_len"] = len(out)
            row["input_len"] = len(t.get("input") or "")
            row["revise"] = bool(re.search(r"<decision>\s*REVISE", out, re.I))
            row["cost"] = (t.get("meta") or {}).get("total_cost_usd")  # a session's running total; see _turn_costs
            row["session_id"] = t.get("session_id")
            from .report import turn_cost_basis, turn_model
            row["model"], row["cost_basis"] = turn_model(t), turn_cost_basis(t)
            item = None
            if t.get("role") == "verifier":
                from . import tags as T
                snap = t.get("checklist_file") or {}  # the critic's checklist file after this turn (B3)
                body = snap.get("checklist", "") if snap else review_checklist(out)  # older runs: from the reply
                items = checklist_items(body)
                plan = ([snap["plan"]] if snap.get("plan") else []) if snap else T.bodies(out, "plan")
                steps = checklist_items(plan[-1]) if plan else []
                if steps:  # the critic's steps: where the work stands
                    row["plan"] = [sum(1 for d, _ in steps if d), len(steps)]
                    row["plan_text"] = plan[-1]
                if items and t.get("purpose") == "verify":  # briefs and amends list items still to do, not progress
                    row["checklist"] = [sum(1 for d, _ in items if d), len(items)]
                    row["checklist_text"] = body
                    item = {"turn": t["turn_id"], "milestone": t.get("milestone"),
                            "done": row["checklist"][0], "total": row["checklist"][1], "purpose": t.get("purpose")}
                v = T.value(out, "verdict")
                m = [v] if v in ("PASS", "FAIL") else re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", out, re.I)
                if m:
                    row["verdict"] = m[-1].upper()
                if t.get("status") == "ok" and t.get("purpose") in ("verify", "par_review", "verdict"):
                    # The scheduler can derive or overrule the reply's verdict from the checklist. Its saved
                    # round result is authoritative, including old reviews with no verdict tag at all.
                    summary = read_json(round_path, {}) or {}
                    verdict = (summary.get("verdicts") or {}).get(t.get("agent"))
                    if t["turn_id"] in (summary.get("turns") or []) and verdict in ("PASS", "FAIL"):
                        row["verdict"] = verdict
                    elif t.get("purpose") == "verify" and snap and items:
                        if row.get("verdict") != "FAIL":
                            row["verdict"] = "PASS" if all(d for d, _ in items + steps) else "FAIL"
            from . import tags as T
            seen = {"input": _flat(t.get("input") or ""),
                    "cut_in": [(x.get("at") or "", _flat(x.get("message") or "")) for x in t.get("interruptions") or []
                               if isinstance(x, dict)],
                    "blocks": T.blocks(out, "message")}
            entry = (row, item, seen)
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

    # ------------------------------------------------------------------ B9b: the slim state and the timeline
    def _timeline(self, fresh: float = 1.0) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], str]:
        """(model, every item in order, groups, open group key), rebuilt at most once per `fresh` seconds."""
        hit = self._tl
        if fresh >= 0 and self.background_refresh and hit is not None and hit[1] == self.run_dir and self._refresh_lock.locked():
            return hit[2], hit[3], hit[4], hit[5]
        with self._tl_lock:
            rd = self.run_dir
            hit = self._tl
            if hit is not None and hit[1] == rd and time.time() - hit[0] < fresh:
                return hit[2], hit[3], hit[4], hit[5]
            d = self._model(full=False)
            if rd is None or not d.get("run_id"):
                self._tl = (time.time(), rd, d, [], [], "")
                return d, [], [], ""
            items = TL.Builder(rd, d, d.get("_chat_all") or [], TL.read_events(rd / "events.jsonl", self._events)).build()
            recovery = read_json(rd / "timeline-steps.json", {}) or {}
            recovered = recovery.get("transitions") if isinstance(recovery, dict) and recovery.get("run_id") == d["run_id"] else None
            labels = TL.assign(items, d.get("turns") or [], recovered if isinstance(recovered, list) else None)
            items = TL.order(items)
            gs, cur = TL.groups(items, labels)
            self._tl = (time.time(), rd, d, items, gs, cur)
            return d, items, gs, cur

    def _background_timeline(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], str]:
        """Poll current status without queueing HTTP requests behind a full history scan on slow storage."""
        rd, hit = self.run_dir, self._tl
        if hit is not None and hit[1] != rd:
            hit = None
        if (hit is None or time.time() - hit[0] >= 5) and self._refresh_lock.acquire(blocking=False):
            def refresh() -> None:
                try:
                    # This is the only background builder; request handlers reuse the previous snapshot.
                    self._build_background_timeline()
                    self._refresh_error = ""
                except Exception as e:
                    self._refresh_error = f"History could not be refreshed: {e}"
                finally:
                    self._refresh_lock.release()
            threading.Thread(target=refresh, name="board-history", daemon=True).start()
        current = self._model(full=False, history=False)
        if not current.get("run_id"):
            return current, [], [], ""
        if hit is None:
            return {**current, "history_loading": True, "history_error": self._refresh_error}, [], [], ""
        d = dict(hit[2])
        d.update({k: v for k, v in current.items() if k not in (
            "turns", "series", "relays", "routes", "received", "held", "messages", "audit")})
        recent = {t["turn_id"]: t for t in current.get("turns") or []}
        d["turns"] = [recent.pop(t["turn_id"], t) for t in d.get("turns") or []] + list(recent.values())
        d.update(history_loading=False, history_error=self._refresh_error)
        return d, hit[3], hit[4], hit[5]

    def _build_background_timeline(self) -> None:
        # Bypass the stale-read path used by HTTP handlers, without changing their behavior mid-request.
        self._timeline(fresh=-1.0)

    def state(self) -> Dict[str, Any]:
        """What the page polls: the run's status, switches, what needs me and who is working, plus the timeline as
        one-line items for the open step only. Full texts come with /api/message, folded steps with /api/items."""
        d, items, gs, cur = self._background_timeline() if self.background_refresh else self._timeline(fresh=0.0)
        if not d.get("run_id"):
            return {k: v for k, v in d.items() if not k.startswith("_")}
        mine = [it for it in items if TL.key_of(it) == cur]
        shown = mine[-TL.ITEMS_MAX:]
        mg = d.get("managed") or {}
        out = {k: d.get(k) for k in (
            "monitor", "run_id", "status", "mode", "round", "max_rounds", "cost", "cost_estimate", "turn_counts", "history_loading", "history_error",
            "updated_at", "started_at", "error", "night", "night_pending", "discussion", "discussion_pending", "discussion_applying", "discussion_supported",
            "review_files", "golden", "status_lines", "reliability", "stop_reason", "reviews", "agents", "series",
            "chat", "chat_more", "terminal", "live", "queued_answers", "min_score", "can_chat", "audit", "auditing")}
        out["managed"] = _slim_managed(mg)
        qa = d.get("queued_answers") or {}
        out["questions"] = [q if q.get("status") == "open" and not qa.get(q.get("id")) else
                            {**q, "text": _clip(q.get("text")), "answer": _clip(q.get("answer")),
                             "assumption": _clip(q.get("assumption"))} for q in d.get("questions") or []]
        out["decisions"] = [{**x, "text": _clip(x.get("text"), 600), "len": len(x.get("text") or "")}
                            for x in d.get("decisions") or []]
        # chat texts are clipped too: the panel loads a whole message (/api/message?id=c:<id>) when asked
        out["chat"] = [{**c, "text": _clip(c.get("text"), CHAT_CLIP), "len": len(c.get("text") or "")}
                       if len(c.get("text") or "") > CHAT_CLIP else c for c in d.get("chat") or []]
        ids = {c.get("id") for c in d.get("chat") or []}
        out["relays"] = {k: v for k, v in (d.get("relays") or {}).items() if k in ids}
        out["running"] = [{k: t.get(k) for k in ("turn_id", "agent", "purpose", "started_at", "milestone", "model", "par_worker", "par_step")}
                          for t in d.get("turns") or [] if t.get("status") == "running"]
        slots = (mg.get("par") or {}).get("slots", {}) if (mg.get("par") or {}).get("active") else {}
        out["status_lines"] = {n: dict(x) for n, x in (out.get("status_lines") or {}).items()}
        turn_items = {it.get("tid"): it for it in items if it.get("id") == "t:" + str(it.get("tid"))}
        for row in out["running"] + list(out["status_lines"].values()):
            slot = slots.get(row.get("agent"))
            it = turn_items.get(row.get("turn_id")) or {}
            if row.get("par_worker") or slot is not None:
                step = row.get("par_step") if row.get("par_worker") else (slot or {}).get("step")
                row["step_label"] = f"Step {step}" if type(step) is int and step > 0 else "Follow-up · no step assigned"
                row["location"] = (slot or {}).get("out") or ""
            elif it.get("step"):
                row["step_label"] = f"Step {it['step']}"
            if row.get("step_label") and it:
                group = next((g for g in gs if g.get("key") == TL.key_of(it)), {})
                if group.get("label") and it.get("step"):
                    row["step_label"] += " · " + group["label"]
        if out.get("discussion_applying"):
            turn = next((t for t in out["running"] if t.get("purpose") == "discussion_apply"), None)
            pending = mg.get("discussion_apply") or {}
            if out.get("status") in ("stopped", "failed", "completed"):
                out["discussion_progress"] = {"stage": "paused", "text": "Discussion application paused. Resume the run to continue."}
            elif turn:
                out["discussion_progress"] = {"stage": "running", "text": "The manager is applying your decisions.", **turn}
            elif pending.get("result"):
                out["discussion_progress"] = {"stage": "finishing", "text": "Finishing the discussion decision."}
            else:
                out["discussion_progress"] = {"stage": "queued", "text": "Waiting for the manager to apply your decisions."}
        out["checklists"] = _checklists(d.get("turns") or [])
        out.update(tl_groups=gs, tl_cur=cur, tl_items=shown, tl_more=len(mine) - len(shown))
        return out

    def items(self, key: str, all_: bool = False) -> Dict[str, Any]:
        """The items of one (milestone, step) group, oldest first: the newest GROUP_MAX unless all are asked for."""
        _d, items, _gs, _cur = self._timeline()
        mine = [it for it in items if TL.key_of(it) == key]
        shown = mine if all_ else mine[-TL.GROUP_MAX:]
        return {"key": key, "items": shown, "more": len(mine) - len(shown)}

    def message(self, mid: str) -> Dict[str, Any]:
        """One item's full text (and, for a turn, what became of each message it passed on)."""
        rd = self.run_dir
        if rd is None:
            return {"error": "no run"}
        d, items, _gs, _cur = self._timeline()
        item = next((it for it in items if it["id"] == mid), None)
        kind, _, rest = mid.partition(":")
        out: Dict[str, Any] = {"id": mid, "item": item}
        if kind in ("t", "r"):
            tid, _, i = rest.partition(":")
            t = read_json(TL.turn_path(rd, tid)) or {}
            if not t:
                return {**out, "error": "not found"}
            text = t.get("output") or ""
            if kind == "r":
                found = re.findall(r"<system-reminder>([\s\S]*?)</system-reminder>", t.get("input") or "")
                out["text"] = "\n\n---\n\n".join(x.strip() for x in found) or "(the reminder text is in the prompt)"
            elif i:
                bl = T.blocks(text, "message")
                out["text"] = bl[int(i)][1] if i.isdigit() and int(i) < len(bl) else ""
            else:
                out.update(text=text, routes=(d.get("routes") or {}).get(tid) or [],
                           received=(d.get("received") or {}).get(tid) or [], held=(d.get("held") or {}).get(tid) or [],
                           input_len=len(t.get("input") or ""), model=t.get("model") or (t.get("options") or {}).get("model"))
            out["turn"] = tid
            return out
        if kind == "c":
            cid, _, i = rest.rpartition(":") if re.search(r":\d+$", rest) else (rest, "", "")
            c = next((x for x in d.get("_chat_all") or [] if x.get("id") == cid), None)
            if c is None:
                return {**out, "error": "not found"}
            if i:
                bl = T.blocks(c.get("text") or "", "message")
                out["text"] = bl[int(i)][1] if int(i) < len(bl) else ""
            else:
                out.update(text=c.get("text") or "", problems=c.get("problems") or [],
                           relays=(d.get("relays") or {}).get(cid) or [])
            return out
        if kind == "q":
            q = next((x for x in d.get("questions") or [] if x.get("id") == rest), None)
            if q is None:
                return {**out, "error": "not found"}
            parts = [q.get("text") or ""]
            if q.get("assumption"):
                parts.append(f"**Recommendation:** {q['assumption']}")
            if q.get("answer"):
                parts.append(f"**Answer** ({q.get('answered_by') or 'answered'}): {q['answer']}")
            out["text"] = "\n\n".join(parts)
            return out
        if kind == "e":
            ev = next((e for n, e in TL.read_events(rd / "events.jsonl", self._events) if str(n) == rest), None)
            if ev is None:
                return {**out, "error": "not found"}
            gate = (d.get("managed") or {}).get("gate") or {}
            text = ""
            if ev.get("event") == "gate" and gate and gate.get("title") == ev.get("title"):
                text = (gate.get("summary") or "") + "\n\n" + (gate.get("text") or "")
            out["text"] = (text.strip() + "\n\n" if text.strip() else "") + "```\n" + json.dumps(ev, ensure_ascii=False, indent=1) + "\n```"
            return out
        if kind == "i":
            for sub in (rd / "inbox" / "consumed", rd / "inbox"):
                p = sub / Path(rest).name
                if p.is_file():
                    x = read_json(p) or {}
                    out["text"] = str(x.get("message") or "")
                    return out
            return {**out, "error": "not found"}
        if kind == "d":
            x = next((x for x in d.get("decisions") or [] if x.get("id") == rest), None)
            return {**out, "text": x.get("text") or ""} if x else {**out, "error": "not found"}
        return {**out, "error": "unknown id"}

    def search(self, q: str) -> Dict[str, Any]:
        """Every item whose full text (or line) has `q`, newest first, at most SEARCH_MAX."""
        q = (q or "").strip()
        if len(q) < 2:
            return {"q": q, "hits": [], "more": False}
        rd = self.run_dir
        d, items, _gs, _cur = self._timeline()
        chat = {c.get("id"): c for c in d.get("_chat_all") or []}
        qs = {x.get("id"): x for x in d.get("questions") or []}
        evs = {str(n): e for n, e in TL.read_events(rd / "events.jsonl", self._events)} if rd else {}
        low, hits = q.lower(), []
        for it in reversed(items):
            kind, _, rest = it["id"].partition(":")
            text = ""
            if kind == "t" and ":" not in rest:
                p = TL.turn_path(rd, rest)
                try:
                    text = p.with_name(p.stem + ".output.md").read_text(encoding="utf-8")
                except OSError:
                    text = (read_json(p) or {}).get("output") or ""
            elif kind == "c" and not re.search(r":\d+$", rest):
                text = (chat.get(rest) or {}).get("text") or ""
            elif kind == "q":
                x = qs.get(rest) or {}
                text = "\n".join(str(x.get(k) or "") for k in ("text", "assumption", "answer"))
            elif kind == "e":
                text = json.dumps(evs.get(rest) or {}, ensure_ascii=False)
            elif kind == "i":
                for sub in (rd / "inbox" / "consumed", rd / "inbox"):
                    p = sub / Path(rest).name
                    if p.is_file():
                        text = str((read_json(p) or {}).get("message") or "")
                        break
            hay = " ".join((it.get("line") or "", str(it.get("from")), str(it.get("to")), text))
            if low in hay.lower():
                hits.append({**it, "snip": TL.snippet(text or it.get("line") or "", q)})
                if len(hits) >= TL.SEARCH_MAX:
                    return {"q": q, "hits": hits, "more": True}
        return {"q": q, "hits": hits, "more": False}

    # ------------------------------------------------------------------ actions (all via the run folder)
    def skip_retry(self, agent: str, wait_id: str) -> bool:
        rd = self.run_dir
        if not rd or not re.fullmatch(r"[0-9a-f]{32}", wait_id):
            return False
        state = read_json(rd / "state.json", {}) or {}
        wait = (state.get("reliability") or {}).get(agent) or {}
        if wait.get("wait_id") != wait_id or wait.get("state") != "retrying" or wait.get("until", 0) <= time.time():
            return False
        try:
            (rd / f".retry-{wait_id}").unlink()
            return True
        except FileNotFoundError:
            return False

    def control(self, data: Dict[str, Any]) -> None:
        inbox = self.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        kind = data.get("kind", "note")
        (inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() // 1000 % 1000000:06d}-board-{kind}.json") \
            .write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def chat_agent(self, name: str, message: str) -> Dict[str, Any]:
        """Talk to the critic or a worker directly. Its reply goes into its own conversation (the same session the run
        uses) and is shown here. A <message to="…"> block in that reply is delivered to the agent it names (urgent
        ones cut in); anything else in the reply stays with the user. Blocks that can't be delivered are reported."""
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
        if _terminal(rd).get(a.name):
            return {"error": f"{a.called}'s session is open in a terminal; talk to it there, or close it first"}
        last = [json.loads(x) for x in _lines(rd / "chat.jsonl")]
        cid = f"chat-{len(last) + 1:03d}-{int(time.time())}"
        _append(rd / "chat.jsonl", {"id": cid + "-q", "role": "human", "text": message, "ts": now_iso(), "with": a.name})

        def work() -> None:
            text, status, new_sid, cost = "", "error", None, None
            try:
                with self._chat_lock, agent_lock(rd, a.name):  # waits for the agent's current turn to end
                    # read the session now, not when the message was sent: a milestone may have started a new one
                    sess = ((read_json(rd / "state.json", {}) or {}).get("sessions") or {}).get(a.name, {})
                    sid = sess.get("session_id")
                    if not sid:
                        raise RuntimeError(f"{a.called} has no conversation now")
                    # framed as a message from the agent's usual user: no board, no one above it
                    workers = " and ".join(x.called for x in self.mon.agents if x.role == "worker") or "worker"
                    key = "chat_reminder_critic" if a.role == "verifier" else "chat_reminder_worker"
                    note = render(self.mon.controller.templates[key], worker=workers).strip()
                    prompt = message + (f"\n\n<system-reminder>\n{note}\n</system-reminder>" if note else "")
                    res = run_variants(a, TurnRequest(
                        agent=a.name, role=a.role, prompt=prompt, system_prompt=a.current_system_prompt(),
                        session_id=sid, cwd=a.working_dir, timeout=a.timeout, options=a.backend_options,
                        env=_turn_env(a, rd), context={"purpose": "chat", "monitor": self.mon.name},
                        on_event=lambda ev: live_event(rd, cid, ev)), lambda: False, sess.get("session_key"),
                        on_model=lambda model: _append(rd / "chat.jsonl", {"id": cid, "model": model}))
                text, status, new_sid = res.output, res.status, res.session_id or sid
                cost = (res.meta or {}).get("total_cost_usd")
                if res.status != "ok" and not text:
                    text = (res.stderr or "")[-1500:] or f"(chat turn ended with status {res.status})"
            except Exception as e:  # pragma: no cover
                text = f"({e})"
            problems = self._pass_on(a, text or "", cid) if status == "ok" else []
            seen = RT.tooling_mentions(text or "", a.name, rd) if status == "ok" else []
            if seen:  # a warning on the board: the critic and the worker are told to ignore orch's tooling
                problems.append(f"mentions the tooling that runs it: {'; '.join(seen[:3])}")
            _append(rd / "chat.jsonl", {"id": cid, "role": "agent", "who": a.name, "with": a.name, "status": status,
                                        "text": text, "ts": now_iso(), "session_id": new_sid, "cost": cost,
                                        **({"problems": problems} if problems else {})})

        _start_chat(rd, {"id": cid, "role": "agent", "who": a.name, "with": a.name,
                         "status": "running", "ts": now_iso()}, work)
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
        if _terminal(rd).get(mgr.name):
            return {"error": f"{mgr.called}'s session is open in a terminal; talk to it there, or close it first"}
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
        sid = manager_chat_session(last, since, st, mgr.name) or sess.get("session_id")
        if not sid and since:  # cleared: the chat starts the new session, with the handover the run would give it
            handover = "\n\n".join((st.get("fwd") or {}).get(mgr.name) or [])
            message = (handover + "\n\n---\n\n" + message).strip() if handover else message
        elif not sid:
            return {"error": f"{mgr.called} has not started yet; try again in a moment"}
        cid = f"chat-{len(last) + 1:03d}-{int(time.time())}"
        n_chat = sum(1 for r in last if r.get("role") == "human" and not r.get("with") and (r.get("ts") or "") > since)
        tag = {"qid": qid, "settle": True} if settle else ({"qid": qid} if qid else {})
        discussion_chat = self.discussion_on(rd)
        _append(rd / "chat.jsonl", {"id": cid + "-q", "role": "human", "text": shown, "ts": now_iso(), **tag})

        def work() -> None:
            text, status, new_sid, cost = "", "error", sid, None
            discussion_draft = discussion_chat
            try:
                with self._chat_lock, agent_lock(rd, mgr.name):
                    prompt = message
                    if self.mon.controller.protocol:  # the chat reminders due (orch/reminders.py, `only: [chat]`)
                        for note in self._chat_reminders(mgr, n_chat):
                            prompt = REM.append(prompt, note)
                    discussion_draft = discussion_draft or self.discussion_on(rd)
                    if discussion_draft:  # keep discussion replies from leaking out after End only
                        critic = (self.mon.by_role("verifier") or [mgr])[0]
                        prompt = REM.append(prompt, render(self.mon.controller.templates["discussion_manager"],
                                                           critic=critic.called).strip())
                    key = (((read_json(rd / "state.json", {}) or {}).get("sessions") or {}).get(mgr.name)
                           or {}).get("session_key")
                    res = run_variants(mgr, TurnRequest(
                        agent=mgr.name, role=mgr.role, prompt=prompt, system_prompt=mgr.current_system_prompt(),
                        session_id=sid, cwd=mgr.working_dir, timeout=mgr.timeout, options=mgr.backend_options,
                        env=_turn_env(mgr, rd), context={"purpose": "chat", "monitor": self.mon.name},
                        on_event=lambda ev: live_event(rd, cid, ev)), lambda: False, key,
                        on_model=lambda model: _append(rd / "chat.jsonl", {"id": cid, "model": model}))
                text, status, new_sid = res.output, res.status, res.session_id or sid
                cost = (res.meta or {}).get("total_cost_usd")
                if res.status != "ok" and not text:
                    text = (res.stderr or "")[-1500:] or f"(chat turn ended with status {res.status})"
            except Exception as e:  # pragma: no cover
                text = f"(chat failed: {e!r})"
            from .managed import manager_pause
            problems = self._pass_on(mgr, text or "", cid, discussion_draft=discussion_draft) if status == "ok" else []
            _append(rd / "chat.jsonl", {"id": cid, "role": "manager", "status": status, "text": text,
                                        "ts": now_iso(), "session_id": new_sid, "cost": cost,
                                        **({"problems": problems} if problems else {}), **tag})
            if settle and status == "ok" and text.strip():  # the agreed answer goes to the run like any answer
                self.control({"kind": "answer", "qid": qid, "message": text.strip(), "via": "conversation"})
            elif status == "ok" and T.bodies(text or "", "question"):  # a new question for me: registered by the run
                self.control({"kind": "chat_question", "agent": mgr.name, "text": text, "source": cid})
            pause = manager_pause(text) if status == "ok" else ""
            if pause:  # the manager stops the run from the chat
                self.control({"kind": "pause", "message": pause})

        _start_chat(rd, {"id": cid, "role": "manager", "status": "running", "ts": now_iso(), **tag}, work)
        return {"id": cid}

    def edit_golden(self, text: str, base: Optional[str]) -> Dict[str, Any]:
        """I edit the golden board on the page. Refused when the file changed since I opened it (the manager keeps it
        too). The run sees the change by its hash and gives every agent the new board; the manager is also told what
        I changed, so it keeps my edits."""
        from . import boards as B
        st = read_json(self.run_dir / "state.json", {}) or {}
        path = (st.get("golden") or {}).get("path")
        if not path:
            return {"error": "this run has no golden board"}
        p = Path(path)
        old = B.read(p)
        if base and B.digest(old) != base:
            return {"error": "The golden board changed while you were editing (the manager edited it). Your text is "
                             "kept in the editor: copy it, reload the board, and edit again.", "conflict": True}
        new = text.replace("\r\n", "\n")
        if not new.endswith("\n"):
            new += "\n"
        if new == old:
            return {"ok": True, "unchanged": True}
        tmp = p.with_name(p.name + ".edit")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, p)
        import difflib
        diff = "".join(difflib.unified_diff(B.golden_body(old).splitlines(True), B.golden_body(new).splitlines(True),
                                            "before", "after", n=0))
        if diff.strip():
            lines = [ln for ln in diff.splitlines() if not ln.startswith(("---", "+++", "@@"))]
            self.control({"kind": "note", "message": (
                f"I edited the golden board ({p}) myself. Keep these changes; merge or reword only if you must, "
                f"without changing their meaning:\n\n```diff\n" + "\n".join(lines) + "\n```")})
        return {"ok": True, "hash": B.digest(new)}

    def chat_question_pending(self) -> bool:
        """A question the manager asked in a board chat that the run has not registered yet (it is open)."""
        rd = self.run_dir
        for p in sorted((rd / "inbox").glob("*.json")) if rd else []:
            d = read_json(p) or {}
            if isinstance(d, dict) and d.get("kind") == "chat_question":
                return True
        return False

    @staticmethod
    def discussion_on(rd: Path) -> bool:
        """The discussion switch as last set: a switch not yet read by the run counts."""
        st = read_json(rd / "state.json", {}) or {}
        on = bool(st.get("discussion") or (st.get("managed") or {}).get("discussion_saved"))
        for p in sorted((rd / "inbox").glob("*.json")):
            d = read_json(p) or {}
            if isinstance(d, dict) and d.get("kind") == "discussion":
                on = bool(d.get("on")) or d.get("action") == "apply"
        return on

    def _chat_reminders(self, a: Any, n: int) -> List[str]:
        """Texts of the reminders for board chat (`only: [chat]`) due at this agent's n-th chat message of its session
        (0: the first, which always gets them)."""
        role = REM.ROLE_OF.get(a.role, "")
        critic = (self.mon.by_role("verifier") or [a])[0]
        workers = self.mon.by_role("worker")
        out = []
        for rem in self.mon.controller.reminders:
            text = REM.text_for(rem, role)
            if text and rem.get("only") and REM.applies(rem, "chat") and REM.targets(rem, a.name, role) \
                    and REM.chat_due(rem, n):
                out.append(render(text, critic=critic.called, worker=workers[0].called if workers else "").strip())
        return out

    def _pass_on(self, a: Any, text: str, cid: str, discussion_draft: bool = False) -> List[str]:
        """The <message> blocks of a chat reply, by the sender's role view (orch/routing.py), as notes in inbox/:
        the run records each one, delivers it (a note for an idle agent starts its turn; urgent ones cut in) and
        shows its status here. Untagged text and `to="you"` go to the sender's upper level. Bad addresses are listed and corrected at the sender's next turn."""
        sent, bad = RT.split_reply(text, a, self.mon.agents, include_upward=True)
        for x in sent:
            t, msg = x["target"], x["body"]
            if a.role == "verifier" and t.role == "manager":
                msg = render(self.mon.controller.templates["relay_note_up"], critic=a.called, note=msg).strip()
            self.control({"to": [t.name], "message": msg, "from": a.name, "source": cid, "i": x["i"], "via": "chat",
                          **({"discussion_draft": True} if discussion_draft else {}),
                          **({"urgent": True} if x["urgent"] else {}), **({"additive": True} if x["additive"] else {})})
        if bad:
            self.control({"kind": "correction", "agent": a.name, "bad": bad, "source": cid})
        return [f"{b} names no one {a.called} can write to; not sent" for b in bad]


def _clip(x: Any, n: int = 300) -> Any:
    s = str(x or "")
    return s if len(s) <= n else s[:n] + "…"


_MS_KEYS = ("title", "done_when", "status", "score", "rounds", "summary", "gate_summary", "rubric", "reopened_at",
            "accepted", "name")
_MG_KEYS = ("ms", "phase", "gate", "job", "held", "review_req", "discussion_batch", "discussion_saved", "discussion_error")


def _slim_managed(mg: Dict[str, Any]) -> Dict[str, Any]:
    """What the page uses of the managed state: not the stored work outputs, which can be large."""
    out = {k: mg.get(k) for k in _MG_KEYS if k in mg}
    out["milestones"] = [{k: m.get(k) for k in _MS_KEYS if k in m} for m in mg.get("milestones") or []]
    return out


def _checklists(turns: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per milestone: the critic's latest plan and checklist (text) and the checklist's history by round."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in turns:
        if t.get("role") != "verifier" or t.get("milestone") is None or t.get("status") == "running":
            continue
        c = out.setdefault(str(t["milestone"]), {"hist": []})
        if t.get("plan"):
            c.update(plan=t["plan"], plan_text=t.get("plan_text") or "")
        if t.get("checklist") and t.get("purpose") == "verify":
            c.update(checklist=t["checklist"], checklist_text=t.get("checklist_text") or "", round=t.get("round"))
            c["hist"].append([t.get("round"), t["checklist"][0], t["checklist"][1]])
    return out


def _turn_env(a: Any, rd: Path) -> Dict[str, str]:
    """A chat turn's environment: the agent's own, plus the mark of a turn orch runs (the terminal hook ignores
    those, and the job finder knows whose processes they are)."""
    from . import jobs as _jobs
    return {**(a.env or {}), _jobs.TAG_VAR: _jobs.tag_for(rd.name, a.name)}


def _review_files(rd: Path, st: Dict[str, Any]) -> List[Dict[str, Any]]:
    """B9: the review files the manager wrote, newest first, each with its link under /log/."""
    out = []
    root = rd.parent.parent
    for e in reversed(st.get("review_files") or []):
        p = Path(e.get("path") or "")
        try:
            href = "log/" + str(p.resolve().relative_to(root.resolve()))
        except (ValueError, OSError):
            href = ""
        out.append({**e, "name": p.name, "href": href, "exists": p.is_file()})
    return out


def _golden(st: Dict[str, Any]) -> Dict[str, Any]:
    """The golden board as the page shows it (B3): its text, size and warnings, read from the file the run uses."""
    g = st.get("golden") or {}
    if not g.get("path"):
        return {}
    from . import boards as B
    raw = B.read(Path(g["path"]))
    body = B.golden_body(raw)
    limit = int(g.get("limit") or 0)
    return {"path": g["path"], "text": body, "raw": raw, "hash": B.digest(raw), "chars": len(body), "limit": limit,
            "over": bool(limit) and len(body) > limit, "leaks": g.get("leaks") or {}}


def _terminal(rd: Path) -> Dict[str, Dict[str, Any]]:
    """Agents whose session is open in a terminal now (the hook's busy marks, without stale ones)."""
    out: Dict[str, Dict[str, Any]] = {}
    cfg = read_json(rd / "config.json", {}) or {}
    timeout = float(((cfg.get("controller") or {}).get("terminal_busy_timeout")) or 3600)
    for p in sorted((rd / "busy").glob("*.json")) if (rd / "busy").is_dir() else []:
        d = read_json(p) or {}
        if isinstance(d, dict) and time.time() - float(d.get("touched") or 0) <= timeout:
            out[p.stem] = {"since": d.get("since"), "session_id": d.get("session_id")}
    return out


def manager_chat_session(rows: List[Dict[str, Any]], since: str, st: Dict[str, Any], manager: str) -> Optional[str]:
    """The session the manager's latest board chat left it in. Only the manager's own replies count: a chat with
    the critic or a worker (role "agent") is in that agent's session, and a session another agent holds is never
    the manager's."""
    others = {(v or {}).get("session_id") for k, v in (st.get("sessions") or {}).items() if k != manager}
    return next((r.get("session_id") for r in reversed(rows)
                 if r.get("role") == "manager" and r.get("session_id") and (r.get("ts") or "") > since
                 and r.get("session_id") not in others), None)


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


def _message_log(rd: Path, chat: List[Dict[str, Any]], turns: List[Dict[str, Any]], names: Dict[str, str],
                 mgr: str, worker_messages: List[Dict[str, Any]],
                 pending: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    """Every message between people and agents, oldest first, for the auditor's view:
    conversations (board chat, incl. manager questions), transfers (notes orch put in an agent's inbox, with when a
    turn read them), check-in updates (the worker messages a check-in queued, and the check-in replies themselves),
    and warnings for <message> blocks orch could not send. Text is kept whole."""
    out: List[Dict[str, Any]] = []

    def who(n: Any) -> str:
        n = str(n or "")
        return names.get(n, n) if n else "you"

    def add(ts: Any, kind: str, frm: str, to: str, text: Any, **extra: Any) -> None:
        out.append({"ts": str(ts or ""), "kind": kind, "from": frm, "to": to, "text": str(text or ""), **extra})

    for c in chat:  # conversations
        if c.get("status") == "running":
            continue
        if c.get("role") == "human":
            add(c.get("ts"), "conversation", "you", who(c.get("with") or mgr), c.get("text"), id=c.get("id"),
                qid=c.get("qid"))
        elif c.get("role") in ("agent", "manager"):
            frm = who(c.get("who") or c.get("with") or mgr)
            add(c.get("ts"), "conversation", frm, "you", c.get("text"), id=c.get("id"), qid=c.get("qid"),
                status=c.get("status"), problems=c.get("problems") or [])

    taken: Dict[str, str] = {}  # inbox file -> when a turn first picked it up
    for ln in _lines(rd / "events.jsonl"):
        if '"human_messages"' in ln:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            for f in ev.get("files") or []:
                taken.setdefault(f, ev.get("ts") or "")
    for where, d in (("read", rd / "inbox" / "consumed"), ("waiting", rd / "inbox")):  # transfers
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            x = read_json(p) or {}
            if not isinstance(x, dict) or not x.get("message"):
                continue
            ts = taken.get(p.name) or (f"{p.name[0:4]}-{p.name[4:6]}-{p.name[6:8]}T{p.name[9:11]}:{p.name[11:13]}:"
                                       f"{p.name[13:15]}" if p.name[:8].isdigit() else "")
            to = [who(t) for t in (x.get("to") or [])] or ["all agents"]
            add(ts, "transfer", who(x.get("from")) if x.get("from") else "you", ", ".join(to), x.get("message"),
                status=where, urgent=bool(x.get("urgent")), label=x.get("kind") or "note")

    for m in worker_messages:  # check-in updates queued for a worker
        add(m.get("created_at"), "check-in", who(m.get("from")), who(m.get("to")), m.get("message"),
            status=m.get("status"), turn=m.get("source_turn"))
    for t in turns:  # the check-in replies themselves (the critic to the manager and the worker)
        if t.get("purpose") != "checkin":
            continue
        full = read_json(rd / "rounds" / t["turn_id"].split("-")[0] / f"{'-'.join(t['turn_id'].split('-')[1:])}.json") or {}
        add(t.get("started_at"), "check-in", who(t.get("agent")), who(mgr), full.get("output") or t.get("output"),
            status=t.get("status"), turn=t.get("turn_id"))

    keys = ('"route_problem"', '"tooling_mention"', '"session_note"', '"retry"', '"fallback"', '"billing_error"',
            '"setup_error"', '"size_warning"', '"model_note"')
    for ln in _lines(rd / "events.jsonl"):  # warnings, session resets, retries and model switches (small system lines)
        if not any(k in ln for k in keys):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        kind = ev.get("event")
        if kind == "retry":  # B8: each retry of the same model, and each switch to a backup
            add(ev.get("ts"), "system", who(ev.get("agent")), "",
                f"{ev.get('model')} failed ({ev.get('reason') or 'error'}); retry {ev.get('retry')}/{ev.get('of')} "
                f"in {float(ev.get('wait_s') or 0):g}s", label="retry")
            continue
        if kind == "fallback":
            add(ev.get("ts"), "system", who(ev.get("agent")), "",
                f"{ev.get('old')} failed ({ev.get('reason') or 'error'}); switched to backup {ev.get('new')}",
                label="model switch")
            continue
        if kind == "model_note":
            add(ev.get("ts"), "system", who(ev.get("agent")), who(ev.get("to")),
                f"model changed ({ev.get('old')} → {ev.get('new')}); told in one line", label="model change")
            continue
        if kind not in ("route_problem", "tooling_mention", "session_note", "billing_error", "setup_error",
                        "size_warning"):
            continue
        if kind == "session_note":
            what = {"compact": "session compacted", "model": "model changed"}.get(ev.get("change"), "new session")
            add(ev.get("ts"), "system", who(ev.get("agent")), "", f"{what}: {ev.get('reason') or ''}".rstrip(": "),
                label=ev.get("change") or "reset")
            continue
        add(ev.get("ts"), "warning", who(ev.get("agent")), "", ev.get("problem"))

    out.sort(key=lambda m: m["ts"])
    # messages orch holds for an agent's next turn (passed on, not yet read): no time is recorded, so they go last
    for name, texts in (pending or {}).items():
        for text in texts or []:
            out.append({"ts": "", "kind": "transfer", "from": "orch", "to": who(name), "text": str(text or ""),
                        "status": "waiting", "label": "pending"})
    return out


def _ledger_row(m: Dict[str, Any], turns: List[Dict[str, Any]], ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One message's status as the page shows it, from the run's ledger (orch/routing.py): delivered (the turn
    whose prompt carried it), waiting, held (with the reason) or superseded (by which message)."""
    row: Dict[str, Any] = {"i": m.get("i"), "to": m.get("to"), "urgent": bool(m.get("urgent")), "msg": m.get("id"),
                           "via": m.get("via")}
    st = m.get("status")
    if st == "delivered":
        t = next((t for t in turns if t.get("turn_id") == m.get("turn")), None)
        row.update(status="delivered", turn=m.get("turn"), at=m.get("delivered_at"), how=m.get("how"),
                   purpose=(t or {}).get("purpose"), turn_status=(t or {}).get("status"))
    elif st == "held":
        row.update(status="held", reason=m.get("reason"), by_design=bool(m.get("by_design")))
    elif st == "superseded":
        by = next((x for x in ledger if x.get("id") == m.get("by")), None) or {}
        src = by.get("source") or m.get("by_turn") or ""
        row.update(status="superseded", reason=m.get("reason"), turn=src if re.match(r"r\d+-t\d+-", src) else None)
    elif st == "dropped":
        row.update(status="dropped", reason=m.get("reason"), at=m.get("dropped_at"))
    else:
        row["status"] = "waiting"
    return row


def _relays(rd: Path, chat: List[Dict[str, Any]], turns: List[Dict[str, Any]],
            ledger: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """For each chat reply, what became of the <message to="…"> blocks it passed on: still waiting in inbox/,
    or delivered (when, and in which turn of the agent it was for). The run's ledger has the true status;
    runs from before it are judged from the inbox."""
    from . import tags as T
    ledger = ledger or []
    out_l: Dict[str, List[Dict[str, Any]]] = {}
    for c in chat:
        ms = [m for m in ledger if m.get("source") == c.get("id") and m.get("to")]
        if ms:
            out_l[c["id"]] = [_ledger_row(m, turns, ledger) for m in ms]
    notes: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}
    for where, d in (("delivered", rd / "inbox" / "consumed"), ("waiting", rd / "inbox")):
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            x = read_json(p) or {}
            if isinstance(x, dict) and x.get("from") and not x.get("kind"):
                notes[str(x.get("message", "")).strip()] = (p.name, where, x)
    if not notes:
        return out_l
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
    out: Dict[str, List[Dict[str, Any]]] = dict(out_l)
    for c in chat:
        if c.get("role") not in ("manager", "agent") or c.get("status") != "ok" or c.get("id") in out_l:
            continue
        for i, (_attrs, body) in enumerate(T.blocks(c.get("text") or "", "message")):
            hit = notes.get((body or "").strip())
            if not hit:
                continue
            name, where, x = hit
            to = x.get("to") or []
            row: Dict[str, Any] = {"i": i, "to": to[0] if to else None, "status": where, "urgent": bool(x.get("urgent"))}
            if where == "delivered":
                ts = taken.get(name, "")
                row["at"] = ts
                t = next((t for t in turns if t.get("agent") in to and ts and (t.get("started_at") or "") >= ts), None)
                if t:
                    row.update(turn=t["turn_id"], purpose=t.get("purpose"), turn_status=t.get("status"))
            out.setdefault(c["id"], []).append(row)
    return out


_flat, _carries = RT.flat, RT.carries  # text matching, shared with the run's delivery check


def _routes(rd: Path, turns: List[Dict[str, Any]], seen: Dict[str, Any], agents: List[Dict[str, Any]],
            worker_messages: List[Dict[str, Any]], ledger: Optional[List[Dict[str, Any]]] = None
            ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    """What became of each <message to="…"> block in a turn's reply, judged from what the recipient was given:
    `read` (the first later turn of the recipient whose prompt, or a note cut into it, carries the message; or a
    live update the backend accepted), `waiting` (the recipient has had no turn since), `unseen` (it has had turns,
    but none carried the message), `unknown` (sent to a running turn, not confirmed) or `failed` (orch could not
    send it, with the reason). Where the run's ledger records the block (orch/routing.py), its status is taken from
    there instead: it was checked against the recipient's actual prompt. Returns ({source turn: [one row per block,
    in order]}, {recipient turn: [where each message it got came from]})."""
    from types import SimpleNamespace
    from . import tags as T
    people = [SimpleNamespace(name=a.get("name"), called=a.get("display_name") or a.get("name"), role=a.get("role"))
              for a in agents if a.get("name")]
    problems: List[Tuple[str, str, str]] = []  # (ts, agent, problem) that orch logged when a reply could not be sent
    superseded: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (turn, recipient) -> a later message replaced it
    for ln in _lines(rd / "events.jsonl"):
        if '"route_problem"' in ln:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            problems.append((ev.get("ts") or "", ev.get("agent") or "", ev.get("problem") or ""))
        elif '"message_superseded"' in ln:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            superseded[(ev.get("turn") or "", ev.get("to") or "")] = ev
    by_agent: Dict[str, List[Dict[str, Any]]] = {}
    for t in turns:
        by_agent.setdefault(t.get("agent") or "", []).append(t)
    out: Dict[str, List[Dict[str, Any]]] = {}
    received: Dict[str, List[Dict[str, Any]]] = {}
    ledger = ledger or []
    known = {(m.get("source"), m.get("i")): m for m in ledger if m.get("i") is not None and not m.get("by_design")}
    for t in turns:
        sv = seen.get(t["turn_id"]) or {}
        if t.get("status") == "running" or not sv.get("blocks"):
            continue
        mine = by_agent.get(t.get("agent") or "", [])
        nxt = next((x for x in mine if (x.get("started_at") or "") > (t.get("started_at") or "")), None)
        logged = [p for ts, a, p in problems if a == t.get("agent") and ts >= (t.get("ended_at") or t.get("started_at") or "")
                  and (nxt is None or ts < (nxt.get("started_at") or ""))]
        rows = []
        for i, (attrs, body) in enumerate(sv["blocks"]):
            to = (attrs.get("to") or "").strip()
            row: Dict[str, Any] = {"i": i, "to": to, "urgent": T.urgent(attrs),
                                   "preview": re.sub(r"\s+", " ", re.sub(r"[#*`>|_]+", "", body or "")).strip()[:160]}
            rows.append(row)
            lm = known.get((t["turn_id"], i))
            if lm is not None:  # the ledger: checked against the recipient's prompt
                lr = _ledger_row(lm, turns, ledger)
                lr.update(status="read" if lr["status"] == "delivered" else lr["status"], i=i, preview=row["preview"])
                if lr["status"] == "held" and not lm.get("carrier"):  # an address it may not use
                    lr.update(status="failed", problem=lm.get("reason"))
                row.clear()
                row.update(lr)
                if lr["status"] == "read" and lr.get("turn"):
                    received.setdefault(lr["turn"], []).append({"from": t.get("agent"), "turn": t["turn_id"], "i": i})
                continue
            if T._norm(to) == "you":
                row["status"] = "you"
                continue
            target = T.recipient(to, people)
            why = next((p for p in logged if f'to="{to}"' in p), None)
            if why is None:
                if target is None:
                    why = f'<message to="{to}"> names no agent; not sent'
                elif target.name == t.get("agent"):
                    why = f'<message to="{to}"> is addressed to the sender itself; not sent'
                elif target.role == "auditor":
                    why = f'<message to="{to}"> goes to the auditor, which takes no messages; not sent'
                elif not (body or "").strip():
                    why = f'<message to="{to}"> is empty; not sent'
            if target is not None:
                row["to"] = target.name
            if why:
                row.update(status="failed", problem=why)
                continue
            later = [x for x in by_agent.get(target.name, []) if (x.get("started_at") or "") > (t.get("started_at") or "")]
            hit = None
            for x in later:
                xs = seen.get(x["turn_id"]) or {}
                if _carries(xs.get("input", ""), body):
                    hit = (x, x.get("started_at"), "prompt")
                    break
                cut = next((at for at, m in xs.get("cut_in", []) if _carries(m, body)), None)
                if cut is not None:
                    hit = (x, cut or x.get("started_at"), "cut in")
                    break
            wm = None
            if hit is None:
                wm = next((m for m in worker_messages if m.get("source_turn") == t["turn_id"]
                           and m.get("to") == target.name and _carries(_flat(m.get("message") or ""), body)), None)
                # a live update goes into the recipient's turn that is already running, so it may predate the sender
                x = next((x for x in by_agent.get(target.name, []) if wm and x["turn_id"] == wm.get("delivery_turn")), None)
                if wm and wm.get("status") in ("accepted", "in_prompt") and x:
                    hit = (x, wm.get("updated_at") or x.get("started_at"), "live")
            if hit:
                x, at, how = hit
                row.update(status="read", turn=x["turn_id"], purpose=x.get("purpose"), turn_status=x.get("status"),
                           at=at, how=how)
                received.setdefault(x["turn_id"], []).append({"from": t.get("agent"), "turn": t["turn_id"], "i": i})
            elif (t["turn_id"], target.name) in superseded:
                ev = superseded[(t["turn_id"], target.name)]
                row.update(status="superseded", turn=ev.get("by"), reason=ev.get("reason"))
            elif wm and wm.get("status") == "unknown":
                row.update(status="unknown", turn=wm.get("delivery_turn"))
            elif any(x.get("status") != "running" for x in later):
                row.update(status="unseen", later=sum(1 for x in later if x.get("status") != "running"))
            else:
                row["status"] = "waiting"
        out[t["turn_id"]] = rows
    return out, received


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
            zipped = (len(body) >= GZIP_MIN and (ctype.startswith("application/json") or ctype.startswith("text/"))
                      and "gzip" in (self.headers.get("Accept-Encoding") or ""))
            if zipped:
                body = gzip.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if zipped:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
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
                b = self.board(q)
                if "since" in q or "since=" in u.query:  # parse_qs drops an empty since=, which asks for the full payload
                    self.send(200, b.deltas.reply(b.state(), (q.get("since") or [""])[0]).encode("utf-8"), "application/json")
                else:
                    self.js(b.state())
            elif u.path == "/api/items":
                self.js(self.board(q).items((q.get("g") or [""])[0], all_=(q.get("all") or [""])[0] == "1"))
            elif u.path == "/api/version":  # the update check: fetches only at a safe point (orch/update.py)
                from . import update
                b = self.board(q)
                st = (read_json(b.run_dir / "state.json", {}) or {}) if b.run_dir else {}
                self.js({**update.check(str(st.get("status") or "")), "progress": update.read_status(b.run_dir)})
            elif u.path == "/api/message":
                self.js(self.board(q).message((q.get("id") or [""])[0]))
            elif u.path == "/api/search":
                self.js(self.board(q).search((q.get("q") or [""])[0]))
            elif u.path == "/api/chat":
                self.js(self.board(q).chat_before((q.get("with") or [""])[0], (q.get("before") or [""])[0]))
            elif u.path == "/api/turn":
                self.js(self.board(q).turn((q.get("id") or [""])[0]))
            elif u.path == "/api/audit":
                self.js(self.board(q).audit())
            elif u.path == "/api/events":
                self.events(self.board(q))
            elif u.path == "/log" or u.path.startswith("/log/"):
                self.log_file(u.path[len("/log"):].lstrip("/"))
            else:
                self.send(404, b"not found", "text/plain")

        def log_file(self, rel: str) -> None:
            """Direct read-only file links used by the dashboard, without report or log browsing pages."""
            if rel in ("", "index.html", "run"):
                return self.send(404, b"not found", "text/plain")
            roots = []
            for b in boards.values():
                rd = b.run_dir
                if rd is not None and rd.parent.parent.resolve() not in roots:
                    roots.append(rd.parent.parent.resolve())
            if not roots:
                return self.send(404, b"no run yet", "text/plain")
            for root in roots:
                f = (root / rel).resolve()
                if not (f == root or root in f.parents):
                    continue  # nothing outside the output folder
                if f.name == "report.html" and (f.parent / "state.json").is_file():
                    return self.send(404, b"not found", "text/plain")
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
            elif u.path == "/api/golden":
                r = b.edit_golden(str(body.get("text") or ""), body.get("base"))
                return self.js(r, 409 if r.get("conflict") else 200)
            elif u.path == "/api/night":
                st = read_json(b.run_dir / "state.json", {}) or {}
                if any(q.get("to") == "human" and q.get("status") == "open" for q in st.get("questions") or []) \
                        or b.chat_question_pending():
                    return self.js({"error": "A question for you is open: night mode can switch once it is answered."},
                                   409)
                b.control({"kind": "night", "on": bool(body.get("on"))})
            elif u.path == "/api/discussion":
                action = body.get("action") or ("start" if body.get("on") else "end")
                if action not in ("start", "end", "apply"):
                    return self.js({"error": "unknown discussion action"}, 400)
                if (read_json(b.run_dir / "state.json", {}) or {}).get("discussion_controls") != 2:
                    return self.js({"error": "Restart the run to enable the updated discussion controls."}, 409)
                b.control({"kind": "discussion", "on": action == "start", "action": action,
                           "request": str(body.get("request") or ""),
                           **({"gate": body["gate"]} if "gate" in body else {})})
            elif u.path == "/api/milestone":
                b.control({"kind": "milestone", "action": "reopen", "ms": int(body.get("ms", -1))})
            elif u.path == "/api/review":
                b.control({"kind": "review"})
            elif u.path == "/api/review_comments":
                b.control({"kind": "review_comments", "path": str(body.get("path") or "")})
            elif u.path == "/api/skip_retry":
                if not b.skip_retry(str(body.get("agent") or ""), str(body.get("wait_id") or "")):
                    return self.js({"error": "That retry is no longer waiting."}, 409)
            elif u.path == "/api/job":
                b.control({"kind": "job"})
            elif u.path == "/api/gate":
                st = read_json(b.run_dir / "state.json", {}) or {}
                cur = ((st.get("managed") or {}).get("gate") or {}).get("key")
                if body.get("gate") and body.get("gate") != cur:  # the run drops it: say so instead of "ok"
                    return self.js({"error": f"no open gate {body.get('gate')!r}"
                                             + (f" (the open one is {cur!r})" if cur else " (none is open)")}, 409)
                b.control({"kind": "gate", "gate": body.get("gate"), "message": msg,
                           "decision": "approve" if body.get("decision") == "approve" else "revise"})
            elif u.path == "/api/note":
                b.control({"to": [body["to"]] if body.get("to") else None, "message": msg,
                           **({"urgent": True} if body.get("urgent") else {})})
            elif u.path == "/api/stop":
                (b.run_dir / "STOP").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
            elif u.path == "/api/update":  # the Update button: the run's process updates and restarts when quiet
                from . import update
                update.request(b.run_dir)
            else:
                return self.js({"error": "unknown"}, 404)
            self.js({"ok": True})

        def events(self, b: Board) -> None:
            """Server-sent events: `state` when anything changed, `live` for each new progress line."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(10)
            cursor = None
            started = last_ping = time.monotonic()
            try:
                # Bound stream lifetime so abandoned proxy connections cannot accumulate forever.
                while time.monotonic() - started < SSE_MAX_AGE:
                    cursor, data = b.live_events.poll(cursor)
                    if data:
                        self.wfile.write(data)
                    if time.monotonic() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        last_ping = time.monotonic()
                    self.wfile.flush()
                    time.sleep(LiveEvents.INTERVAL)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                self.close_connection = True

    return H


def serve(boards: Dict[str, Board], host: str = "127.0.0.1", port: int = 8765) -> Tuple[ThreadingHTTPServer, str]:
    """Start the board in a background thread; returns (server, url). Tries the next ports if busy."""
    for board in boards.values():
        board.background_refresh = True
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
    return turn_costs(list(turns) + [c for c in chat if c.get("role") in ("manager", "agent")
                                     and c.get("status") is not None])


def page() -> str:
    """board.html, read on every page load so a UI change shows up without restarting the run."""
    try:
        return Path(__file__).with_name("board.html").read_text(encoding="utf-8")
    except OSError:
        return "<p>board.html missing</p>"
