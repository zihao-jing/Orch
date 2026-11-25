"""Adapter for OpenCode (`opencode serve`), docs/redesign.md 3.2 and docs/probes/opencode.md.

One server per agent, on its own port with its own password, in `<run_dir>/cli/<agent>/` (server.json, password, log).
Orch may leave and join it again: the server keeps running turns and jobs by itself.

There is no event replay, so nothing here depends on a live event stream. Every event is derived from the session's
message records (user, synthetic, assistant, idle, compaction), read in time order from a saved position, and its `uid`
comes from the record id. A restart, a reconnect and a live poll therefore give the same events, and the store drops
repeats. A message is received when the POST returns (the receipt) and taken when its user record appears (delivered).
`pos` is "<created ms of the last record>:<1 if a turn was open after it>".

Ids: a message id "x<12 hex>" is sent as "msg_x<12 hex>" (the client chooses the id; a repeat is not a second message).
Jobs: the job list covers the whole database and still shows jobs a dead server left, so jobs are filtered by session
and each is checked with GET /api/shell/{id}.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

from ..contracts import (AgentEvent, AgentSpec, Caps, Choice, ErrorClass, Job, JobEnded, JobNoticed, JobStarted,
                         JobState, ModelInfo, ModelReported, ProcRef, ProcessExited, Received, Said, SessionChange,
                         SessionChanged, SessionRef, Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus,
                         USER, redact)
from .base import AdapterError, BaseAdapter, classify_error, job_id

OURS = re.compile(r"^x[0-9a-f]{12}$")
ALLOW = [{"action": "*", "resource": "*", "effect": "allow"}]
# JOB-1: only simple forms are caught (probe 9); later rules win, so these come after the allow
DENY = [{"action": "shell", "resource": r, "effect": "deny"} for r in ("*nohup*", "*setsid*", "*&")]
SESSION_GONE = re.compile(r"session not found|SessionNotFound", re.I)


def _request(port: int, pw: str, cwd: str, method: str, path: str, body: Any = None,
             timeout: float = 10.0) -> Tuple[int, Any]:
    """One HTTP call to a local server; (status, parsed JSON or None). Raises AdapterError when it cannot be reached."""
    headers = {"Authorization": "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()}
    if cwd and cwd.isascii():
        headers["x-opencode-directory"] = cwd
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body), "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        raise AdapterError(f"opencode server not reachable: {e}") from e
    finally:
        conn.close()
    try:
        return resp.status, (json.loads(raw) if raw.strip() else None)
    except ValueError:
        return resp.status, raw


def _fail(status: int, data: Any, what: str) -> AdapterError:
    text = (data.get("message") if isinstance(data, dict) else str(data or "")) or ""
    if status == 404:
        kind = ErrorClass.SESSION_LOST if SESSION_GONE.search(json.dumps(data) if data else "") else ErrorClass.OTHER
    elif status == 429 or status >= 500:
        kind = ErrorClass.TRANSIENT
    else:
        kind = ErrorClass.OTHER
    return AdapterError(f"opencode {what}: HTTP {status} {text}"[:300], kind)


def _created(rec: Dict[str, Any]) -> int:
    t = rec.get("time")
    c = t.get("created") if isinstance(t, dict) else None
    return int(c) if isinstance(c, (int, float)) else 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _text_of(rec: Dict[str, Any]) -> str:
    parts = [p.get("text") or "" for p in rec.get("content") or [] if isinstance(p, dict) and p.get("type") == "text"]
    return "\n\n".join(x for x in parts if x)


class OpenCodeAdapter(BaseAdapter):
    backend = "opencode"
    caps = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=True, receipts=True,
                shares_terminal=True, compacts=True, stops_jobs=False)
    poll_every = 1.0  # seconds between two reads of the session

    def __init__(self, clock: Any, exe: str = "opencode"):
        super().__init__(clock)
        self.exe = exe
        self.agent = self.sid = self.cwd = self.pw = ""
        self.port = self.pid = 0
        self.dir = Path(".")
        self.popen: Any = None
        self.attached = self._gone = False
        self._queue: List[AgentEvent] = []
        self._reset_reading("")

    def _reset_reading(self, pos: str) -> None:
        self._pos = pos
        self._open = pos.endswith(":1")
        self._seen: set = set()
        self._asst: Dict[str, Dict[str, Any]] = {}
        self._tools: Dict[str, bool] = {}
        self._announced: set = set()
        self._ended: Dict[str, Job] = {}
        self._compacted: set = set()
        self._model = ""
        self._turn_text = ""
        self._last_poll = -1e18
        self._exit_reported = self._lost = False

    # ------------------------------------------------------------------------------------------------- server

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 10.0) -> Tuple[int, Any]:
        if not (self.port and self.attached):
            raise AdapterError("opencode server is not connected")
        return _request(self.port, self.pw, self.cwd, method, path, body, timeout)

    def _spawn(self, argv: List[str], env: Dict[str, str], cwd: str, log: Any) -> Any:
        """Start a server process of its own (a session of its own, so it can be signalled as a group)."""
        try:
            return subprocess.Popen(argv, env=env, cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError as e:
            raise AdapterError(f"opencode executable not found: {e}", ErrorClass.MISSING_CLI) from e

    def _launch(self, cwd: str, extra_env: Dict[str, str], log_path: Path) -> Tuple[Any, int, str]:
        port, pw = _free_port(), secrets.token_urlsafe(24)
        env = {**os.environ, **extra_env, "OPENCODE_SERVER_PASSWORD": pw}
        with open(log_path, "ab") as log:
            popen = self._spawn([self.exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)], env, cwd, log)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if popen.poll() is not None:
                raise AdapterError("opencode serve exited at startup (see its log)", ErrorClass.TRANSIENT)
            try:
                if _request(port, pw, cwd, "GET", "/api/info", timeout=2)[0] == 200:
                    return popen, port, pw
            except AdapterError:
                pass
            time.sleep(0.2)
        self._kill_group(popen.pid)
        raise AdapterError("opencode serve did not come up in 30 s", ErrorClass.TRANSIENT)

    def _info_path(self) -> Path:
        return self.dir / "server.json"

    def _write_info(self, **change: Any) -> None:
        info = {**self._read_info(), **change}
        tmp = self._info_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(info))
        os.replace(tmp, self._info_path())

    def _read_info(self) -> Dict[str, Any]:
        try:
            return json.loads(self._info_path().read_text())
        except (OSError, ValueError):
            return {}

    def _pid_alive(self, pid: int) -> bool:
        if not pid:
            return False
        if self.popen is not None and self.popen.pid == pid:
            return self.popen.poll() is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _adopt(self) -> bool:
        """Join the server of server.json if it lives and answers to its own password."""
        info = self._read_info()
        try:
            pw = (self.dir / "password").read_text().strip()
        except OSError:
            return False
        if not info.get("pid") or not self._pid_alive(int(info["pid"])):
            return False
        try:
            ok = _request(int(info["port"]), pw, info.get("cwd", ""), "GET", "/api/info", timeout=3)[0] == 200
        except AdapterError:
            ok = False
        if ok:
            self.pid, self.port, self.pw, self.cwd, self.attached = int(info["pid"]), int(info["port"]), pw, \
                info.get("cwd", ""), True
        return ok

    def _kill_group(self, pid: int) -> None:
        """SIGKILL the server's group, only when the server leads its own (never a shared one)."""
        try:
            os.killpg(pid, signal.SIGKILL) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _term(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # ------------------------------------------------------------------------------------------------ start

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        self.spec, self.agent, self.cwd, self._gone = spec, spec.agent, spec.cwd, False
        self.dir = Path(spec.run_dir or tempfile.mkdtemp(prefix="orch-oc-")) / "cli" / spec.agent
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self._adopt():
            info = self._read_info()
            if info.get("pid") and self._pid_alive(int(info["pid"])):
                self._term(int(info["pid"]))  # an unanswering server of an earlier start
            fd = os.open(self.dir / "password.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
            self.popen, self.port, self.pw = self._launch(spec.cwd, dict(spec.choice.env), self.dir / "server.log")
            (self.dir / "password.new").write_text(self.pw)
            os.replace(self.dir / "password.new", self.dir / "password")
            self.pid, self.attached = self.popen.pid, True
            self._write_info(pid=self.pid, port=self.port, cwd=spec.cwd, agent=spec.agent, exe=self.exe,
                             started=self.clock.now())
        changed = SessionChange.NEW
        old_sid, old_pos = self.sid, self._pos
        sid = session.session_id if session else ""
        if sid:
            status, data = self._call("GET", f"/api/session/{sid}")
            if status == 404:
                self._reset_reading("")
                self.sid = ""
                self._queue.append(self._ev(SessionChanged, f"lost:{sid}", stream=sid, session_id=sid,
                                            change=SessionChange.LOST, detail="session not found"))
                return self._proc()
            if status != 200:
                raise _fail(status, data, "session")
            self._set_model(sid, data, spec.choice)
            changed = SessionChange.RESUMED
        else:
            sid = self._create(spec)
            self._write_info(system_pending=bool(spec.system_prompt))
            (self.dir / "system.txt").write_text(spec.system_prompt or "")
        keep = sid == old_sid and bool(old_pos)
        self._reset_reading(old_pos if keep else "")
        self.sid = sid
        if not keep:
            self._pos = self._boundary()
            self._open = self._pos.endswith(":1")
        self._write_info(sid=sid)
        self._queue.append(self._ev(SessionChanged, f"start:{self.pid}", session_id=sid, change=changed))
        return self._proc()

    def _proc(self) -> ProcRef:
        return ProcRef(pid=self.pid, started=self.clock.now(), endpoint=f"http://127.0.0.1:{self.port}",
                       output_file=str(self.dir / "server.log"), stream=self.sid, read_pos=self._pos)

    def _model_body(self, choice: Choice) -> Dict[str, Any]:
        m: Dict[str, Any] = {"id": choice.model, "providerID": choice.provider}
        if choice.effort and choice.effort != "default":
            m["variant"] = choice.effort
        return m

    def _create(self, spec: AgentSpec) -> str:
        body: Dict[str, Any] = {"location": {"directory": spec.cwd}, "title": f"orch {spec.agent}",
                                "permissions": ALLOW + DENY}
        if spec.choice.model:
            body["model"] = self._model_body(spec.choice)
        status, data = self._call("POST", "/api/session", body)
        if status != 200 or not isinstance(data, dict) or not data.get("data", {}).get("id"):
            raise _fail(status, data, "create session")
        return data["data"]["id"]

    def _set_model(self, sid: str, session: Any, choice: Choice) -> None:
        """Switch the session's model if it differs. The call accepts anything: only a turn proves the choice."""
        if not choice.model:
            return
        want, have = self._model_body(choice), ((session or {}).get("data") or {}).get("model") or {}
        if (have.get("id"), have.get("providerID")) == (want["id"], want["providerID"]) and \
                ("variant" not in want or have.get("variant") == want["variant"]):
            return
        status, data = self._call("POST", f"/api/session/{sid}/model", {"model": want})
        if status not in (200, 204):
            raise _fail(status, data, "switch model")

    def _boundary(self) -> str:
        """Where to start reading a session that was already there: after the last finished turn, so a running one is
        seen from its start and the history is not replayed."""
        recs = self._records(0, limit_pages=3) or []
        active = False
        try:
            status, data = self._call("GET", "/api/session/active")
            active = status == 200 and self.sid in ((data or {}).get("data") or {})
        except AdapterError:
            pass
        pick = None
        for r in reversed(recs):
            if (r.get("type") == "idle") if active else True:
                pick = r
                break
        return f"{_created(pick)}:0" if pick else "0:0"

    def reconnect(self, proc: ProcRef) -> bool:
        self.dir = Path(proc.output_file).parent
        info = self._read_info()
        self.agent, self.cwd, self.pid = info.get("agent", ""), info.get("cwd", ""), proc.pid
        self._reset_reading(proc.read_pos or "")
        self.sid = proc.stream
        self.popen, self._queue = None, []
        if int(info.get("pid") or 0) == proc.pid and self._adopt():
            return True
        self.attached = False
        self._gone = True  # events() reports the exit
        return False

    # ------------------------------------------------------------------------------------------------ sending

    def send(self, message_id: str, text: str) -> None:
        if not self.sid:
            raise AdapterError("no session is open", ErrorClass.OTHER)
        info = self._read_info()
        if info.get("system_pending"):  # no native system prompt: it goes in front of the first message
            system = (self.dir / "system.txt").read_text() if (self.dir / "system.txt").exists() else ""
            if system:
                text = f"<system_instructions>\n{system}\n</system_instructions>\n\n{text}"
        status, data = self._call("POST", f"/api/session/{self.sid}/prompt",
                                  {"id": "msg_" + message_id, "text": text, "delivery": "steer"})
        if status != 200:
            raise _fail(status, data, "send")
        if info.get("system_pending"):
            self._write_info(system_pending=False)
        self._queue.append(self._ev(Received, f"recv:{message_id}", message_id=message_id))

    def arrived(self, message_id: str) -> Optional[bool]:
        if not (self.sid and self.attached):
            return None
        try:
            status, _ = self._call("GET", f"/api/session/{self.sid}/message/msg_{message_id}")
            if status == 200:
                return True
            status, data = self._call("GET", f"/api/session/{self.sid}/inbox")
        except AdapterError:
            return None
        if status != 200:
            return None if status >= 500 else False
        return any(i.get("id") == "msg_" + message_id for i in (data or {}).get("data") or [])

    # ------------------------------------------------------------------------------------------------ reading

    def _ev(self, cls: type, uid: str, pos: str = "", stream: Optional[str] = None, at: float = 0.0,
            **kw: Any) -> AgentEvent:
        return cls(agent=self.agent, at=at or self.clock.now(), stream=self.sid if stream is None else stream,
                   pos=pos, uid=uid, **kw)

    def events(self) -> Iterator[AgentEvent]:
        out, self._queue = self._queue, []
        if self.attached and self.sid:
            out += self._poll()
        elif self._gone:
            out += self._exit_events()
        return iter(out)

    def _exit_events(self) -> List[AgentEvent]:
        if self._exit_reported:
            return []
        self._exit_reported = True
        out: List[AgentEvent] = []
        if self._open:
            self._open = False
            out.append(self._ev(TurnEnded, f"end:exit:{self.pid}", self._pos, status=TurnStatus.ERROR,
                                error=ErrorClass.TRANSIENT, detail="opencode server exited", session_id=self.sid))
        out.append(self._ev(ProcessExited, f"exit:{self.pid}", self._pos, detail="opencode server exited",
                            reported=bool(out)))
        self.attached = False
        return out

    def _poll(self) -> List[AgentEvent]:
        if not self._pid_alive(self.pid):
            return self._exit_events()
        now = self.clock.now()
        if now - self._last_poll < self.poll_every:
            return []
        self._last_poll = now
        try:
            recs = self._records(int(self._pos.split(":")[0] or 0) if self._pos else 0)
        except AdapterError:
            return []
        if recs is None:  # 404: the session is gone (SES-2)
            if self._lost:
                return []
            self._lost = True
            if self._open:
                self._open = False
                return [self._ev(TurnEnded, f"end:lost:{self.sid}", self._pos, status=TurnStatus.ERROR,
                                 error=ErrorClass.SESSION_LOST, detail="session not found", session_id=self.sid)]
            return [self._ev(SessionChanged, f"lost:{self.sid}", session_id=self.sid, change=SessionChange.LOST,
                             detail="session not found")]
        out: List[AgentEvent] = []
        for rec in recs:
            start = self._pos
            made = self._record(rec)
            self._pos = f"{_created(rec)}:{int(self._open)}"
            at = _created(rec) / 1000.0
            for i, (cls, uid, kw) in enumerate(made):
                pos = "" if cls is Text else (start if i < len(made) - 1 else self._pos)
                out.append(self._ev(cls, uid, pos, at=at, **kw))
        return out

    def _records(self, since: int, limit_pages: int = 50) -> Optional[List[Dict[str, Any]]]:
        """The session's records from `since` (ms) on, oldest first; None: no such session."""
        recs: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(limit_pages):
            status, data = self._call("GET", f"/api/session/{self.sid}/message?limit=50&order=desc" +
                                      (f"&cursor={quote(cursor)}" if cursor else ""))
            if status == 404:
                return None
            if status != 200:
                raise _fail(status, data, "read messages")
            page = [r for r in (data or {}).get("data") or [] if isinstance(r, dict)]
            recs += page
            cursor = ((data or {}).get("cursor") or {}).get("next") or ""
            if not page or not cursor or min(_created(r) for r in page) < since:
                break
        recs.reverse()
        recs.sort(key=_created)
        return [r for r in recs if _created(r) >= since]

    def _record(self, rec: Dict[str, Any]) -> List[Tuple[type, str, Dict[str, Any]]]:
        """The events one record gives; updates the reading state."""
        rid, kind = str(rec.get("id") or ""), rec.get("type")
        if kind in ("user", "synthetic", "idle"):
            if rid in self._seen:
                return []
            self._seen.add(rid)
        if kind == "user":
            mine = rid[4:] if rid.startswith("msg_") and OURS.match(rid[4:]) else ""
            if not self._open:
                self._open, self._turn_text = True, ""
                return [(TurnStarted, f"start:{rid}", {"message_ids": (mine,) if mine else ()})]
            return [(Received, f"took:{rid}", {"message_id": mine, "taken": True})] if mine else []
        if kind == "synthetic":
            return self._synthetic(rec, rid)
        if kind == "assistant":
            return self._assistant(rec, rid)
        if kind == "idle":
            return self._idle(rec, rid)
        if kind == "compaction" and rec.get("status") == "completed" and rid not in self._compacted:
            self._compacted.add(rid)
            return [(SessionChanged, f"compact:{rid}", {"session_id": self.sid, "change": SessionChange.COMPACTED})]
        return []

    def _synthetic(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        md = rec.get("metadata") or {}
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        if md.get("source") == "shell" and (md.get("shellID") or md.get("jobID")):
            cli = str(md.get("shellID") or md.get("jobID"))
            jid = job_id(self.agent, cli)
            code = md.get("exit")
            code = int(code) if isinstance(code, (int, float)) else None
            self._ended[jid] = Job(id=jid, agent=self.agent, label=self._ended[jid].label if jid in self._ended else "",
                                   state=JobState.ENDED, exit_code=code, ended=_created(rec) / 1000.0)
            made.append((JobEnded, f"jobend:{cli}", {"job_id": jid, "exit_code": code}))
            if self._open:
                made.append((JobNoticed, f"noticed:{rid}", {"job_id": jid}))
                return made
        if not self._open:  # the server woke the session by itself (JOB-4)
            self._open, self._turn_text = True, ""
            made.append((TurnStarted, f"start:{rid}", {"message_ids": ()}))
        return made

    def _assistant(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        m = rec.get("model") if isinstance(rec.get("model"), dict) else {}
        model = "/".join(x for x in (m.get("providerID"), m.get("id")) if x)
        if model and model != self._model:
            self._model = model
            made.append((ModelReported, f"model:{rid}", {"model": model}))
        for i, part in enumerate(p for p in rec.get("content") or [] if isinstance(p, dict) and p.get("type") == "tool"):
            tid = str(part.get("id") or part.get("callID") or f"{rid}:{i}")
            status = (part.get("state") or {}).get("status") if isinstance(part.get("state"), dict) else None
            if status in ("running", "pending") and tid not in self._tools:
                self._tools[tid] = True
                made.append((ToolStart, f"toolstart:{tid}", {"tool_id": tid, "label": str(part.get("name") or "tool")}))
            elif status in ("completed", "error") and self._tools.get(tid):
                self._tools[tid] = False
                made.append((ToolEnd, f"toolend:{tid}", {"tool_id": tid}))
        text = _text_of(rec)
        st = self._asst.setdefault(rid, {"len": 0})
        if len(text) > st["len"]:
            made.append((Text, f"text:{rid}:{len(text)}", {"text": text[st["len"]:]}))
            st["len"] = len(text)
        if text:
            self._turn_text = text
        return made

    def _idle(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        if not self._open:  # a repeated message id makes an empty idle record; a compaction has none of its own turn
            return []
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        try:
            for j in self._running_jobs():
                if j.id not in self._announced:
                    self._announced.add(j.id)
                    made.append((JobStarted, f"jobstart:{j.id}", {"job": j}))
        except AdapterError:
            pass
        self._open = False
        outcome, err = rec.get("outcome"), rec.get("error")
        kw: Dict[str, Any] = {"text": self._turn_text, "session_id": self.sid}
        if outcome == "interrupted":
            kw["status"] = TurnStatus.STOPPED
        elif outcome == "failed" or err:
            kw.update(status=TurnStatus.ERROR, error=self._error_class(err), detail=self._error_text(err))
        made.append((TurnEnded, f"end:{rid}", kw))
        return made

    @staticmethod
    def _error_text(err: Any) -> str:
        if isinstance(err, dict):
            return f"{err.get('type', '')}: {err.get('message', '')}"[:300]
        return str(err or "turn failed")[:300]

    def _error_class(self, err: Any) -> ErrorClass:
        text = self._error_text(err)
        kind = str(err.get("type", "")) if isinstance(err, dict) else ""
        status = err.get("status") if isinstance(err, dict) else None
        if SESSION_GONE.search(text):
            return ErrorClass.SESSION_LOST
        if kind.startswith(("provider.auth", "provider.no-route")):
            return ErrorClass.BILLING if classify_error(text) == ErrorClass.BILLING else ErrorClass.OTHER
        if isinstance(status, int) and (status == 429 or status >= 500):
            return ErrorClass.TRANSIENT
        return classify_error(text)

    # -------------------------------------------------------------------------------------------------- jobs

    def _running_jobs(self, known: bool = False) -> List[Job]:
        """The session's running jobs, ghosts left out (GET /api/shell/{id} says 404 for a dead one)."""
        status, data = self._call("GET", f"/api/experimental/job?sessionID={quote(self.sid)}")
        if status != 200:
            raise _fail(status, data, "job list")
        jobs: List[Job] = []
        for j in (data or {}).get("data") or []:
            if j.get("sessionID") != self.sid or j.get("status") != "running":
                continue
            cli = str(j.get("shellID") or j.get("id"))
            if self._call("GET", f"/api/shell/{cli}")[0] != 200:
                continue
            started = (j.get("time") or {}).get("started") or 0
            label = " ".join(str(j.get("command") or j.get("title") or cli).split())[:200]
            jobs.append(Job(id=job_id(self.agent, cli), agent=self.agent, label=label, started=started / 1000.0,
                            last_progress=started / 1000.0))
        return jobs

    def jobs(self) -> List[Job]:
        if not (self.sid and self.attached):
            return []
        running = self._running_jobs()
        ids = {j.id for j in running}
        return running + [j for j in self._ended.values() if j.id not in ids]

    # --------------------------------------------------------------------------------------------- the rest

    def detach(self) -> None:
        self.attached = False

    def stop(self) -> None:
        if not self.pid:
            return
        pids: List[int] = []
        if self.attached and self.sid:
            try:
                for j in self._running_jobs():  # their processes may outlive a killed server
                    st, data = self._call("GET", f"/api/shell/{j.id.split('/', 1)[1]}")
                    if st == 200 and isinstance(data, dict) and data.get("pid"):
                        pids.append(int(data["pid"]))
            except AdapterError:
                pass
        self._term(self.pid)
        for _ in range(50):
            if not self._pid_alive(self.pid):
                break
            time.sleep(0.1)
        else:
            self._kill_group(self.pid)
            for p in pids:
                self._kill_group(p)
        if not self._exit_reported:
            self._exit_reported = True
            if self._open:
                self._queue.append(self._ev(TurnEnded, f"stop:{self.pid}", self._pos, status=TurnStatus.STOPPED,
                                            session_id=self.sid))
            self._queue.append(self._ev(ProcessExited, f"exit:{self.pid}", self._pos, asked=True))
        self._open = self.attached = False
        try:
            self._info_path().unlink()
        except OSError:
            pass

    def compact(self) -> bool:
        if not (self.sid and self.attached) or self._open:
            return False
        try:
            status, _ = self._call("POST", f"/api/session/{self.sid}/compact", {})
        except AdapterError:
            return False
        return status == 200

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        d = Path(spec.run_dir) / "cli" / spec.agent
        try:
            port = json.loads((d / "server.json").read_text())["port"]
        except (OSError, ValueError, KeyError) as e:
            raise AdapterError("the opencode server of this agent is not running", ErrorClass.OTHER) from e
        # the password is read from its file by the shell, so it is never in the command or the environment of Orch
        script = (f"cd {shlex.quote(session.cwd or spec.cwd)} && OPENCODE_SERVER_PASSWORD=\"$(cat "
                  f"{shlex.quote(str(d / 'password'))})\" exec {shlex.quote(self.exe)} --server "
                  f"http://127.0.0.1:{port} --session {shlex.quote(session.session_id)}")
        return ["sh", "-c", script]

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        if not (self.attached and self.sid == session.session_id):
            return []
        recs = self._records(int(since * 1000)) or []
        said: List[Said] = []
        for r in recs:
            if _created(r) / 1000.0 <= since:
                continue
            if r.get("type") == "user":
                said.append(Said(USER, str(r.get("text") or ""), _created(r) / 1000.0))
            elif r.get("type") == "assistant" and _text_of(r):
                said.append(Said(self.agent, _text_of(r), _created(r) / 1000.0))
        return said

    def catalog(self) -> List[ModelInfo]:
        """Models with their variants, read from a private server that lives for this call only."""
        tmp = Path(tempfile.mkdtemp(prefix="orch-oc-catalog-"))
        popen = None
        try:
            popen, port, pw = self._launch(str(tmp), {}, tmp / "server.log")
            status, data = _request(port, pw, str(tmp), "GET", "/api/model", timeout=20)
        finally:
            if popen is not None:
                self._term(popen.pid)
                try:
                    popen.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self._kill_group(popen.pid)
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()
        if status != 200:
            raise _fail(status, data, "model list")
        return catalog_from(data)


def catalog_from(data: Any) -> List[ModelInfo]:
    """ModelInfo from GET /api/model; `settings` (which can hold keys) is never read."""
    out: List[ModelInfo] = []
    for m in (data or {}).get("data") or []:
        if not m.get("enabled", True):
            continue
        provider, model = str(m.get("providerID") or ""), str(m.get("modelID") or m.get("id") or "")
        out.append(ModelInfo(id=f"{provider}/{model}", backend="opencode", provider=provider, model=model,
                             label=str(m.get("name") or model),
                             efforts=[str(v.get("id")) for v in m.get("variants") or [] if v.get("id")]))
    return out
