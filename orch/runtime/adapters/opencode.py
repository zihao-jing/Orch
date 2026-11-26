"""Adapter for OpenCode (`opencode serve`), docs/redesign.md 3.2 and docs/probes/opencode.md.

One server per agent, on its own port with its own password, in `<run_dir>/cli/<agent>/` (server.json, password, log).
Orch may leave and join it again: the server keeps running turns and jobs by itself.

There is no event replay, so nothing depends on a live event stream. Every event is derived from the session's message
records (user, synthetic, assistant, idle, compaction), read in time order from a saved position, and its `uid` comes
from the record id. A restart, a reconnect and a live poll therefore give the same events and the store drops repeats.
The saved position (`pos`, "<created ms>:<id>") always stands at the last record after which no turn was open, so an
open turn is read again from its first record and no turn is lost or ended twice.

A message is received when the POST returns (the receipt) and taken when its user record appears. Its id "x<12 hex>" is
sent as "msg_x<12 hex>" (the client chooses the id; a repeat is not a second message).

Permissions: a session gets only deny rules (a later "allow *" would override the user's own config, the server takes
the last matching rule). Requests that still ask are answered each poll like `opencode run` does: "once" when the
agent is `auto` (default) or `permission_mode: bypassPermissions`, else rejected; question forms are cancelled.

Jobs: the job list covers the whole database and still shows jobs a dead server left, so it is filtered by session and
each shell job is checked with GET /api/shell/{id}. A process is only signalled after its start time was compared with
the one saved when it was started.
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
                         USER)
from .base import AdapterError, BaseAdapter, classify_error, job_id

OURS = re.compile(r"^x[0-9a-f]{12}$")
# JOB-1: only simple forms are caught (probe 9); a deny rule never widens what the user's own config allows
DENY = [{"action": "shell", "resource": r, "effect": "deny"} for r in ("*nohup*", "*setsid*", "*&")]
SESSION_GONE = re.compile(r"session not found|SessionNotFound", re.I)
ENDED_SHELL = ("exited", "timeout", "killed")
TRANSIENT_TYPES = ("rate", "limit", "transport", "timeout", "internal", "overload", "network", "unavailable",
                   "inactiv", "supersed")
BILLING_TYPES = ("quota", "billing", "credit", "payment")
# item 2: the position of a turn that Orch closed (stop, server exit): nothing before now is read again
CLOSED = ":0:"


def _request(port: int, pw: str, cwd: str, method: str, path: str, body: Any = None,
             timeout: float = 10.0) -> Tuple[int, Any]:
    """One HTTP call to a local server; (status, parsed JSON or None). Raises AdapterError when it cannot be reached."""
    headers = {"Authorization": "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()}
    if cwd:
        headers["x-opencode-directory"] = quote(cwd, safe="/")
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


def _tag(data: Any) -> str:
    return str(data.get("_tag", "")) if isinstance(data, dict) else ""


def _fail(status: int, data: Any, what: str) -> AdapterError:
    text = (data.get("message") if isinstance(data, dict) else str(data or "")) or ""
    if status == 404:
        kind = ErrorClass.SESSION_LOST if SESSION_GONE.search(_tag(data)) else ErrorClass.OTHER
    elif status == 429 or status >= 500:
        kind = ErrorClass.TRANSIENT
    else:
        kind = ErrorClass.OTHER
    return AdapterError(f"opencode {what}: HTTP {status} {text}"[:300], kind)


def _created(rec: Dict[str, Any]) -> int:
    t = rec.get("time")
    c = t.get("created") if isinstance(t, dict) else None
    return int(c) if isinstance(c, (int, float)) else 0


def _key(rec: Dict[str, Any]) -> str:
    return f"{_created(rec)}:{rec.get('id') or ''}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _text_of(rec: Dict[str, Any]) -> str:
    parts = [p.get("text") or "" for p in rec.get("content") or [] if isinstance(p, dict) and p.get("type") == "text"]
    return "\n\n".join(x for x in parts if x)


def _start_time(pid: int) -> float:
    """When a process started (epoch seconds), 0.0 if it cannot be told: pid and start time identify a process."""
    try:
        out = " ".join(subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                      timeout=3, env={**os.environ, "LC_ALL": "C"}).stdout.split())
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge as today's backend does it: permission rules are ordered lists that add up."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list) and k == "permissions":
            out[k] = out[k] + v
        else:
            out[k] = v
    return out


def _tool_rules(o: Dict[str, Any]) -> List[Dict[str, str]]:
    """The agent's own options as rules: disallowed_tools deny, add_dirs allow outside access to those folders only."""
    raw = o.get("disallowed_tools")
    tools = raw if isinstance(raw, list) else str(raw or "").split(",")
    rules = [{"action": str(t).strip(), "resource": "*", "effect": "deny"} for t in tools if str(t).strip()]
    dirs = o.get("add_dirs")
    for d in (shlex.split(dirs) if isinstance(dirs, str) else [str(x) for x in dirs or []]):
        rules.append({"action": "external_directory", "resource": os.path.join(os.path.expanduser(d), "*"),
                      "effect": "allow"})
    return rules


class OpenCodeAdapter(BaseAdapter):
    backend = "opencode"
    caps = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=True, receipts=True,
                shares_terminal=True, compacts=True, stops_jobs=False)
    poll_every = 1.0  # seconds between two reads of the session
    stop_grace = 5.0  # seconds from SIGTERM to SIGKILL

    def __init__(self, clock: Any, exe: str = "opencode"):
        super().__init__(clock)
        self.exe = exe
        self.agent = self.sid = self.cwd = self.pw = ""
        self.port = self.pid = 0
        self.pstart = 0.0
        self.auto = True
        self.dir = Path(".")
        self.popen: Any = None
        self.attached = self._gone = False
        self._stopping: Optional[Tuple[float, List[int]]] = None  # (SIGKILL deadline, job pids)
        self._queue: List[AgentEvent] = []
        self._reset_reading("")

    def _reset_reading(self, pos: str) -> None:
        self._anchor = pos
        self._last_key = pos
        self._seen: set = {pos.split(":", 1)[1]} if ":" in pos else set()  # the record the position stands at
        self._open = False
        self._asst: Dict[str, int] = {}
        self._texts: Dict[str, str] = {}
        self._tools: Dict[str, bool] = {}
        self._known: Dict[str, Job] = {}
        self._kind: Dict[str, str] = {}
        self._ended: Dict[str, Job] = {}
        self._compacted: set = set()
        self._model = ""
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

    def _spawn_server(self, cwd: str, extra_env: Dict[str, str], log_path: Path, port: int, pw: str) -> Any:
        env = {**os.environ, **extra_env, "OPENCODE_SERVER_PASSWORD": pw, "OPENCODE_PASSWORD": pw}
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log_path, 0o600)
        with os.fdopen(fd, "ab") as log:
            return self._spawn([self.exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)], env, cwd, log)

    def _wait_up(self, popen: Any, port: int, pw: str, cwd: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if popen.poll() is not None:
                raise AdapterError("opencode serve exited at startup (see its log)", ErrorClass.TRANSIENT)
            try:
                if _request(port, pw, cwd, "GET", "/api/info", timeout=2)[0] == 200:
                    return
            except AdapterError:
                pass
            time.sleep(0.2)
        if popen.poll() is None:  # our own child, not reaped: its pid is ours
            self._signal(popen.pid, signal.SIGKILL, verified=True)
        raise AdapterError("opencode serve did not come up in 30 s", ErrorClass.TRANSIENT)

    def _info_path(self) -> Path:
        return self.dir / "server.json"

    def _write_info(self, **change: Any) -> None:
        info = {**self._read_info(), **change}
        tmp = self._info_path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(info))
        os.replace(tmp, self._info_path())

    def _read_info(self) -> Dict[str, Any]:
        try:
            return json.loads(self._info_path().read_text())
        except (OSError, ValueError):
            return {}

    def _live(self, pid: int, pstart: float) -> bool:
        """The process is ours and running: same pid and same start time (never a reused pid)."""
        if not pid:
            return False
        if self.popen is not None and self.popen.pid == pid:
            return self.popen.poll() is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return pstart > 0 and abs(_start_time(pid) - pstart) <= 2

    def _signal(self, pid: int, sig: int, verified: bool = False, pstart: float = 0.0) -> None:
        """Signal a server (its whole group if it leads one) only when it is verified to be ours."""
        if not pid or not (verified or self._live(pid, pstart)):
            return
        try:
            os.killpg(pid, sig) if os.getpgid(pid) == pid else os.kill(pid, sig)
        except OSError:
            pass

    def _adopt(self) -> bool:
        """Join the server of server.json if it lives and answers to its own password."""
        info = self._read_info()
        try:
            pw = (self.dir / "password").read_text().strip()
        except OSError:
            return False
        pid, pstart = int(info.get("pid") or 0), float(info.get("pstart") or 0)
        if not self._live(pid, pstart):
            return False
        try:
            ok = _request(int(info["port"]), pw, info.get("cwd", ""), "GET", "/api/info", timeout=3)[0] == 200
        except (AdapterError, KeyError, ValueError):
            ok = False
        if ok:
            self.pid, self.pstart, self.port, self.pw = pid, pstart, int(info["port"]), pw
            self.cwd, self.auto, self.attached = info.get("cwd", ""), bool(info.get("auto", True)), True
        return ok

    # ------------------------------------------------------------------------------------------------ start

    def _server_config(self, spec: AgentSpec) -> Tuple[str, Optional[str]]:
        """(OPENCODE_CONFIG_CONTENT or "", the agent name): today's OpenCodeBackend.config() for this agent."""
        o = spec.choice.options or {}
        cfg: Dict[str, Any] = {}
        agent = str(o["agent"]) if o.get("agent") else None
        if o.get("system_prompt_mode") == "replace" and spec.system_prompt:
            agent = agent or "orch-" + re.sub(r"[^A-Za-z0-9_-]", "-", spec.role.value or "agent")
            cfg["agents"] = {agent: {"mode": "primary", "system": spec.system_prompt}}
        rules = _tool_rules(o)
        if rules:
            cfg["permissions"] = rules
        if isinstance(o.get("config"), dict):
            cfg = _merge(cfg, o["config"])
        if not cfg:
            return "", agent
        own = spec.choice.env.get("OPENCODE_CONFIG_CONTENT", os.environ.get("OPENCODE_CONFIG_CONTENT"))
        try:
            base = json.loads(own) if own else {}
        except ValueError:
            base = {}
        return json.dumps(_merge(base if isinstance(base, dict) else {}, cfg)), agent

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        o = spec.choice.options or {}
        self.spec, self.agent, self.cwd, self._gone = spec, spec.agent, spec.cwd, False
        self.exe = str(o.get("executable", self.exe))
        self.auto = bool(o.get("auto", True)) or o.get("permission_mode") == "bypassPermissions"
        self.dir = Path(spec.run_dir or tempfile.mkdtemp(prefix="orch-oc-")) / "cli" / spec.agent
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._finish_stop()
        config, agent = self._server_config(spec)
        if not self._adopt():
            info = self._read_info()
            self._signal(int(info.get("pid") or 0), signal.SIGTERM, pstart=float(info.get("pstart") or 0))
            port, pw = _free_port(), secrets.token_urlsafe(24)
            env = dict(spec.choice.env)
            if config:
                env["OPENCODE_CONFIG_CONTENT"] = config
            self.popen = self._spawn_server(spec.cwd, env, self.dir / "server.log", port, pw)
            self.pid, self.port, self.pw, self.attached = self.popen.pid, port, pw, True
            self.pstart = _start_time(self.pid)
            fd = os.open(self.dir / "password", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(pw)
            self._write_info(pid=self.pid, pstart=self.pstart, port=port, cwd=spec.cwd, agent=spec.agent,
                             exe=self.exe, started=self.clock.now(), auto=self.auto, sid="")
            try:
                self._wait_up(self.popen, port, pw, spec.cwd)
            except AdapterError:
                self.attached = False
                raise
        changed = SessionChange.NEW
        old_sid, old_pos = self.sid, self._anchor
        sid = session.session_id if session else ""
        if sid:
            status, data = self._call("GET", f"/api/session/{sid}")
            if status == 404 and SESSION_GONE.search(_tag(data)):
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
            sid = self._create(spec, agent)
            self._write_info(system_pending=bool(spec.system_prompt) and not agent)
            (self.dir / "system.txt").write_text(spec.system_prompt or "")
        keep = sid == old_sid and bool(old_pos)
        self.sid = sid
        if not keep:
            self._reset_reading(self._boundary())
        else:
            self._reset_reading(self._latest() if old_pos == CLOSED else old_pos)
        self._write_info(sid=sid)
        self._queue.append(self._ev(SessionChanged, f"start:{self.pid}", session_id=sid, change=changed))
        return self._proc()

    def _proc(self) -> ProcRef:
        return ProcRef(pid=self.pid, started=self.pstart, endpoint=f"http://127.0.0.1:{self.port}",
                       output_file=str(self.dir / "server.log"), stream=self.sid, read_pos=self._anchor)

    def _model_body(self, choice: Choice) -> Dict[str, Any]:
        m: Dict[str, Any] = {"id": choice.model, "providerID": choice.provider}
        if choice.effort and choice.effort != "default":
            m["variant"] = choice.effort
        return m

    def _create(self, spec: AgentSpec, agent: Optional[str]) -> str:
        body: Dict[str, Any] = {"location": {"directory": spec.cwd}, "title": f"orch {spec.agent}",
                                "permissions": DENY + _tool_rules(spec.choice.options or {})}
        if spec.choice.model:
            body["model"] = self._model_body(spec.choice)
        if agent:
            body["agent"] = agent
        status, data = self._call("POST", "/api/session", body)
        if status != 200 or not isinstance(data, dict) or not data.get("data", {}).get("id"):
            raise _fail(status, data, "create session")
        sid = data["data"]["id"]
        if (spec.choice.options or {}).get("archived", True):  # kept out of the user's own session list, as today
            self._call("PATCH", f"/api/session/{sid}", {"archived": True})
        return sid

    def _set_model(self, sid: str, session: Any, choice: Choice) -> None:
        """Switch the session's model if it differs. The call accepts anything: only a turn proves the choice."""
        if not choice.model:
            return
        want, have = self._model_body(choice), ((session or {}).get("data") or {}).get("model") or {}
        if (have.get("id"), have.get("providerID"), have.get("variant") or "default") == \
                (want["id"], want["providerID"], want.get("variant") or "default"):
            return
        status, data = self._call("POST", f"/api/session/{sid}/model", {"model": want})
        if status not in (200, 204):
            raise _fail(status, data, "switch model")

    def _latest(self) -> str:
        """The position of the session's newest record: nothing before it is read."""
        recs = self._records(0, strict=False, pages=1) or []
        return _key(recs[-1]) if recs else ""

    def _boundary(self) -> str:
        """Where to read a session that was already there from: its newest record, or while a turn runs the last
        finished one, so that turn is read from its start and the history is not replayed."""
        recs = self._records(0, strict=False, pages=3) or []
        active = False
        try:
            status, data = self._call("GET", "/api/session/active")
            active = status == 200 and self.sid in ((data or {}).get("data") or {})
        except AdapterError:
            pass
        for r in reversed(recs):
            if r.get("type") == "idle" or not active:
                return _key(r)
        return ""

    def reconnect(self, proc: ProcRef) -> bool:
        self.dir = Path(proc.output_file).parent
        info = self._read_info()
        self.agent, self.cwd, self.pid, self.pstart = info.get("agent", ""), info.get("cwd", ""), proc.pid, proc.started
        self._reset_reading(proc.read_pos or "")
        self.sid = proc.stream
        self.popen, self._queue = None, []
        same = int(info.get("pid") or 0) == proc.pid and (not proc.started or
                                                          abs(float(info.get("pstart") or 0) - proc.started) <= 2)
        if same and self._adopt():
            if self._anchor == CLOSED:
                self._reset_reading(self._latest())
            return True
        self.attached = False
        if same:  # it may still answer: what it wrote before the failure is read before the end is reported
            self.port, self.pw = int(info.get("port") or 0), self._password()
        self._gone = True  # events() reports the exit
        return False

    def _password(self) -> str:
        try:
            return (self.dir / "password").read_text().strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------------------------------------ sending

    def send(self, message_id: str, text: str) -> None:
        if not self.sid:
            raise AdapterError("no session is open", ErrorClass.OTHER)
        pending = self._read_info().get("system_pending")
        if pending:  # no native system prompt: it goes in front of the first message
            try:
                system = (self.dir / "system.txt").read_text()
            except OSError:
                system = ""
            if system:
                text = f"<system_instructions>\n{system}\n</system_instructions>\n\n{text}"
        status, data = self._call("POST", f"/api/session/{self.sid}/prompt",
                                  {"id": "msg_" + message_id, "text": text, "delivery": "steer"})
        if status != 200:
            raise _fail(status, data, "send")
        if pending:
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
        if self._stopping:
            out += self._stop_events()
        elif self.attached and self.sid:
            out += self._poll()
        elif self._gone:
            out += self._exit_events()
        return iter(out)

    def _close(self) -> str:
        """An open turn that Orch closed: the position is CLOSED, so nothing before now is read again."""
        self._anchor, self._open = CLOSED, False
        return CLOSED

    def _exit_events(self) -> List[AgentEvent]:
        if self._exit_reported:
            return []
        out: List[AgentEvent] = []
        if self.sid and self.port and self.pw and self._anchor != CLOSED:
            # what the server wrote before it went (or before a failed reconnect): the real end, if there is one
            self.attached = True
            try:
                out += self._process(self._records(self._since(), pages=100))
            except AdapterError:
                pass
            self.attached = False
        self._exit_reported = True
        ended = False
        if self._open:
            ended = True
            out.append(self._ev(TurnEnded, f"end:exit:{self.pid}", self._close(), status=TurnStatus.ERROR,
                                error=ErrorClass.TRANSIENT, detail="opencode server exited", session_id=self.sid))
        out.append(self._ev(ProcessExited, f"exit:{self.pid}", self._anchor, detail="opencode server exited",
                            reported=ended))
        self.attached = False
        return out

    def _since(self) -> int:
        return int(self._anchor.split(":")[0] or 0) if self._anchor else 0

    def _poll(self) -> List[AgentEvent]:
        if not self._live(self.pid, self.pstart):
            return self._exit_events()
        now = self.clock.now()
        if now - self._last_poll < self.poll_every:
            return []
        self._last_poll = now
        try:
            recs = self._records(self._since())
        except AdapterError:
            return []
        if recs is None:  # the server says: no such session (SES-2)
            if self._lost:
                return []
            self._lost = True
            if self._open:
                return [self._ev(TurnEnded, f"end:lost:{self.sid}", self._close(), status=TurnStatus.ERROR,
                                 error=ErrorClass.SESSION_LOST, detail="session not found", session_id=self.sid)]
            return [self._ev(SessionChanged, f"lost:{self.sid}", session_id=self.sid, change=SessionChange.LOST,
                             detail="session not found")]
        out = self._process(recs)
        try:
            self._answer_requests()
        except AdapterError:
            pass
        return out

    def _process(self, recs: Optional[List[Dict[str, Any]]]) -> List[AgentEvent]:
        out: List[AgentEvent] = []
        for rec in recs or []:
            start = self._anchor
            made = self._record(rec)
            self._last_key = _key(rec)
            if not self._open:
                self._anchor = self._last_key
            at = _created(rec) / 1000.0
            for i, (cls, uid, kw) in enumerate(made):
                pos = "" if cls is Text else (start if i < len(made) - 1 else self._anchor)
                out.append(self._ev(cls, uid, pos, at=at, **kw))
        return out

    def _records(self, since: int, strict: bool = True, pages: int = 100) -> Optional[List[Dict[str, Any]]]:
        """The session's records from `since` (ms) on, oldest first; None: no such session. Pages back until older than
        `since`; where that takes more than `pages` pages it raises (strict) instead of giving part of it."""
        recs: List[Dict[str, Any]] = []
        cursor, limit = "", 20
        for _ in range(pages):
            # a cursor carries its own direction: the server refuses it together with `order`
            status, data = self._call("GET", f"/api/session/{self.sid}/message?limit={limit}" +
                                      (f"&cursor={quote(cursor)}" if cursor else "&order=desc"))
            if status == 404 and SESSION_GONE.search(_tag(data)):
                return None
            if status != 200:
                raise _fail(status, data, "read messages")
            page = [r for r in (data or {}).get("data") or [] if isinstance(r, dict)]
            recs += page
            cursor, limit = ((data or {}).get("cursor") or {}).get("next") or "", 50
            if not page or not cursor or min(_created(r) for r in page) < since:
                break
        else:
            if strict:
                raise AdapterError("too many records to read the session in one go", ErrorClass.TRANSIENT)
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
                return self._begin(rid, (mine,) if mine else ())
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

    def _begin(self, rid: str, ids: Tuple[str, ...]) -> List[Tuple[type, str, Dict[str, Any]]]:
        self._open = True
        self._texts = {}
        return [(TurnStarted, f"start:{rid}", {"message_ids": ids})]

    def _synthetic(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        md = rec.get("metadata") or {}
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        cli = str(md.get("shellID") or md.get("jobID") or "")
        if cli:
            jid = job_id(self.agent, cli)
            code = md.get("exit")
            code = int(code) if isinstance(code, (int, float)) else None
            label = (self._known.get(jid) or self._ended.get(jid) or Job(id=jid)).label
            self._ended[jid] = Job(id=jid, agent=self.agent, label=label, state=JobState.ENDED, exit_code=code,
                                   ended=_created(rec) / 1000.0)
            self._known.pop(jid, None)
            made.append((JobEnded, f"jobend:{cli}", {"job_id": jid, "exit_code": code}))
            if self._open:
                made.append((JobNoticed, f"noticed:{rid}", {"job_id": jid}))
                return made
        if not self._open:  # the server woke the session by itself (JOB-4)
            made += self._begin(rid, ())
        return made

    def _assistant(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        if not self._open:  # its turn began before the position we read from
            made += self._begin(rid, ())
        m = rec.get("model") if isinstance(rec.get("model"), dict) else {}
        model = "/".join(x for x in (m.get("providerID"), m.get("id")) if x)
        if model and model != self._model:
            self._model = model
            made.append((ModelReported, f"model:{rid}", {"model": model}))
        for i, part in enumerate(p for p in rec.get("content") or [] if isinstance(p, dict) and p.get("type") == "tool"):
            made += self._tool(part, f"{rid}:{i}", _created(rec) / 1000.0)
        text = _text_of(rec)
        if text:
            self._texts[rid] = text
        done = self._asst.get(rid, 0)
        if len(text) > done:
            made.append((Text, f"text:{rid}:{len(text)}", {"text": text[done:]}))
            self._asst[rid] = len(text)
        return made

    def _tool(self, part: Dict[str, Any], fallback: str, at: float) -> List[Tuple[type, str, Dict[str, Any]]]:
        made: List[Tuple[type, str, Dict[str, Any]]] = []
        tid = str(part.get("id") or fallback)
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if status in ("streaming", "running") and tid not in self._tools:
            self._tools[tid] = True
            made.append((ToolStart, f"toolstart:{tid}", {"tool_id": tid, "label": str(part.get("name") or "tool")}))
        inp, md = state.get("input"), state.get("metadata")
        if isinstance(inp, dict) and inp.get("background") is True and isinstance(md, dict):  # JOB-2: a shell job
            cli = str(md.get("shellID") or "")
            jid = job_id(self.agent, cli)
            if cli and jid not in self._known and jid not in self._ended:
                label = " ".join(str(inp.get("command") or inp.get("description") or cli).split())[:200]
                t = part.get("time") if isinstance(part.get("time"), dict) else {}
                started = (t.get("ran") or t.get("created") or at * 1000) / 1000.0
                self._kind[jid] = "shell"
                self._known[jid] = Job(id=jid, agent=self.agent, label=label, started=started, last_progress=started)
                made.append((JobStarted, f"jobstart:{cli}", {"job": self._known[jid]}))
        if status in ("completed", "error") and self._tools.get(tid):
            self._tools[tid] = False
            made.append((ToolEnd, f"toolend:{tid}", {"tool_id": tid}))
        return made

    def _idle(self, rec: Dict[str, Any], rid: str) -> List[Tuple[type, str, Dict[str, Any]]]:
        if not self._open:  # a repeated message id makes an empty idle record; a compaction has none of its own turn
            return []
        self._open = False
        outcome, err = rec.get("outcome"), rec.get("error")
        kw: Dict[str, Any] = {"text": "\n\n".join(self._texts.values()), "session_id": self.sid}
        if outcome == "interrupted":  # nobody here interrupts: a terminal, inactivity or a newer prompt did
            kw.update(status=TurnStatus.ERROR, error=ErrorClass.TRANSIENT, detail="turn interrupted")
        elif outcome == "failed" or err:
            kw.update(status=TurnStatus.ERROR, error=self._error_class(err), detail=self._error_text(err))
            if isinstance(err, dict) and isinstance(err.get("retryAfterMs"), (int, float)):
                kw["retry_after"] = err["retryAfterMs"] / 1000.0
        return [(TurnEnded, f"end:{rid}", kw)]

    @staticmethod
    def _error_text(err: Any) -> str:
        if isinstance(err, dict):
            return f"{err.get('type', '')}: {err.get('message', '')}"[:300]
        return str(err or "turn failed")[:300]

    def _error_class(self, err: Any) -> ErrorClass:
        text = self._error_text(err)
        kind = str(err.get("type", "")).lower() if isinstance(err, dict) else ""
        status = err.get("status") if isinstance(err, dict) else None
        if SESSION_GONE.search(text):
            return ErrorClass.SESSION_LOST
        if any(w in kind for w in BILLING_TYPES) or classify_error(text) == ErrorClass.BILLING:
            return ErrorClass.BILLING
        if kind.startswith(("provider.auth", "provider.no-route")):
            return ErrorClass.OTHER
        if any(w in kind for w in TRANSIENT_TYPES) or (isinstance(status, int) and (status == 429 or status >= 500)):
            return ErrorClass.TRANSIENT
        return classify_error(text)

    # ----------------------------------------------------------------------------------- permissions and forms

    def _family(self) -> set:
        """The session and the child sessions of its subagents."""
        fam = {self.sid}
        status, data = self._call("GET", f"/api/experimental/job?sessionID={quote(self.sid)}&descendants=true")
        for j in (data or {}).get("data") or [] if status == 200 else []:
            fam |= {j.get("sessionID"), j.get("childSessionID")}
        return fam

    def _answer_requests(self) -> None:
        """Answer what still asks, as `opencode run` does: allow once when auto, else reject; cancel question forms."""
        status, data = self._call("GET", "/api/permission/request")
        asks = (data or {}).get("data") or [] if status == 200 else []
        status, data = self._call("GET", "/api/form")
        forms = (data or {}).get("data") or [] if status == 200 else []
        if not (asks or forms):
            return
        fam = self._family()
        for r in asks:
            if r.get("sessionID") in fam:
                body: Dict[str, Any] = {"decision": "once"} if self.auto else \
                    {"decision": "reject", "message": "Nobody is here to approve this: it was not allowed"}
                self._call("POST", f"/api/session/{r['sessionID']}/permission/{r['id']}/reply", body)
        for f in forms:
            if f.get("sessionID") in fam:
                msg = quote("Nobody is here to answer questions: decide on your own")
                self._call("DELETE", f"/api/session/{f['sessionID']}/form/{f['id']}?message={msg}")

    # -------------------------------------------------------------------------------------------------- jobs

    def _shell(self, cli: str) -> Optional[Dict[str, Any]]:
        status, data = self._call("GET", f"/api/shell/{cli}")
        d = (data or {}).get("data") if isinstance(data, dict) else None
        return d if status == 200 and isinstance(d, dict) else None

    def _running_jobs(self) -> List[Job]:
        """The session's running jobs. A shell job is checked with GET /api/shell/{id} (a dead server leaves ghosts in
        the list); a subagent job runs while its child session is active."""
        status, data = self._call("GET", f"/api/experimental/job?sessionID={quote(self.sid)}")
        if status != 200:
            raise _fail(status, data, "job list")
        active: Optional[set] = None
        jobs: List[Job] = []
        for j in (data or {}).get("data") or []:
            if j.get("sessionID") != self.sid or j.get("status") != "running":
                continue
            kind = j.get("kind")
            if kind == "shell":
                cli = str(j.get("shellID") or j.get("id"))
                sh = self._shell(cli)
                if not sh or sh.get("status") != "running":
                    continue
            elif kind == "subagent":
                cli = str(j.get("id"))
                if active is None:
                    st, act = self._call("GET", "/api/session/active")
                    active = set((act or {}).get("data") or {}) if st == 200 else set()
                if j.get("childSessionID") not in active:
                    continue
            else:
                continue
            jid = job_id(self.agent, cli)
            started = ((j.get("time") or {}).get("started") or 0) / 1000.0
            label = " ".join(str(j.get("command") or j.get("agent") or cli).split())[:200]
            self._kind[jid] = kind
            jobs.append(Job(id=jid, agent=self.agent, label=label, started=started, last_progress=started))
        return jobs

    def jobs(self) -> List[Job]:
        if not (self.sid and self.attached):
            return []
        running = self._running_jobs()
        ids = {j.id for j in running}
        for jid, job in list(self._known.items()):  # known, but gone from the list: did it end?
            if jid in ids or self._kind.get(jid) != "shell":
                continue
            sh = self._shell(jid.split("/", 1)[1])  # 404: nothing known, the supervisor decides (lost)
            if sh and sh.get("status") in ENDED_SHELL and isinstance(sh.get("exit"), (int, float)):
                t = sh.get("time") if isinstance(sh.get("time"), dict) else {}
                self._ended[jid] = Job(id=jid, agent=self.agent, label=job.label, state=JobState.ENDED,
                                       exit_code=int(sh["exit"]), ended=(t.get("completed") or 0) / 1000.0)
                self._known.pop(jid)
        return running + [j for j in self._ended.values() if j.id not in ids]

    def _job_pids(self) -> List[Tuple[int, float]]:
        """(pid, start time) of running shell jobs, read just before the server is ended: they may outlive a killed
        one. A pid whose start time cannot be read is left out: it cannot be verified later."""
        pids: List[Tuple[int, float]] = []
        try:
            for j in self._running_jobs():
                sh = self._shell(j.id.split("/", 1)[1]) if self._kind.get(j.id) == "shell" else None
                if sh and sh.get("status") == "running" and int(sh.get("pid") or 0) > 1:
                    pid = int(sh["pid"])
                    if _start_time(pid) > 0:
                        pids.append((pid, _start_time(pid)))
        except AdapterError:
            pass
        return pids

    # --------------------------------------------------------------------------------------------- the rest

    def detach(self) -> None:
        self.attached = False

    def stop(self) -> None:
        if not self.pid or self._stopping:
            return
        if self._exit_reported:  # it is gone already: no signal
            self._info_path().unlink(missing_ok=True)
            return
        pids = self._job_pids() if self.attached and self.sid else []
        self._signal(self.pid, signal.SIGTERM, pstart=self.pstart)
        if self._open:
            self._queue.append(self._ev(TurnEnded, f"stop:{self.pid}", self._close(), status=TurnStatus.STOPPED,
                                        session_id=self.sid))
        self.attached = False
        self._stopping = (self.clock.now() + self.stop_grace, pids)
        for _ in range(5):  # most servers go at once; events() finishes the rest
            if not self._live(self.pid, self.pstart):
                break
            time.sleep(0.1)
        self._queue += self._stop_events()

    def _stop_events(self) -> List[AgentEvent]:
        if not self._stopping:
            return []
        deadline, pids = self._stopping
        if self._live(self.pid, self.pstart):
            if self.clock.now() >= deadline:  # it must be killed: then also what its jobs left
                self._signal(self.pid, signal.SIGKILL, pstart=self.pstart)
                for p, started in pids:
                    self._signal(p, signal.SIGKILL, pstart=started)
            return []
        self._stopping = None
        self._exit_reported = True
        try:
            self._info_path().unlink()
        except OSError:
            pass
        return [self._ev(ProcessExited, f"exit:{self.pid}", self._anchor, asked=True)]

    def _finish_stop(self) -> None:
        """A start waits for the end of the server an earlier stop() sent away."""
        end = time.monotonic() + self.stop_grace + 2
        while self._stopping and time.monotonic() < end:
            self._queue += self._stop_events()
            if self._stopping:
                time.sleep(0.1)

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
        said: List[Said] = []
        for r in self._records(int(since * 1000), strict=False) or []:
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
            port, pw = _free_port(), secrets.token_urlsafe(24)
            popen = self._spawn_server(str(tmp), {}, tmp / "server.log", port, pw)
            self._wait_up(popen, port, pw, str(tmp))
            status, data = _request(port, pw, str(tmp), "GET", "/api/model", timeout=20)
        finally:
            if popen is not None and popen.poll() is None:
                self._signal(popen.pid, signal.SIGTERM, verified=True)
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal(popen.pid, signal.SIGKILL, verified=True)
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
