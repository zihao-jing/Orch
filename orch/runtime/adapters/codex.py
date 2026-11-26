"""Codex adapter: one `codex app-server` per agent, spoken to over a WebSocket on a unix socket (docs/probes/codex.md).

The server stays up without Orch (very large thread_unload_delay_secs, or an idle thread is unloaded after a minute and
its jobs are killed). Orch connects, calls thread/resume (events reach only a connection that did), and reads what it
missed from thread/turns/list. Live notifications and the catch-up feed one state machine, so both give the same events
with the same uids; the store drops what it knows.

Rules of the app-server worth knowing here:
  - a command is a job only if it still runs when its turn ends (JobStarted just before TurnEnded; its item/completed
    comes later, into the same turn); nothing wakes the model afterwards (wakes_itself False)
  - a message goes in by turn/steer (turn/start also steers); the proof of arrival is the userMessage item whose
    clientId is the message id
  - read position `pos` is "<turn id>/<last item id>" of the last ended turn: reading resumes after that turn
"""
from __future__ import annotations

import base64
import json
import os
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import time
from collections import deque
from typing import Any, Deque, Dict, Iterator, List, Optional, Set, Tuple

from ..contracts import (AgentEvent, AgentSpec, Caps, Choice, Clock, ErrorClass, Job, JobEnded, JobStarted, JobState,
                         ModelInfo, ModelReported, ProcRef, ProcessExited, Received, Said, SessionChange,
                         SessionChanged, SessionRef, Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus, USER)
from .base import AdapterError, BaseAdapter, classify_error, job_id

UNLOAD_SECS = 31536000  # thread_unload_delay_secs: jobs must outlive a client-less minute
OUR_ID = re.compile(r"^x[0-9a-f]{12}$")  # contracts.new_id; a terminal's own messages carry a UUID

# codexErrorInfo -> class (docs/probes/codex.md section 8; only modelNotFound was seen live)
_BILLING = {"usageLimitExceeded", "sessionBudgetExceeded", "unauthorized"}
_TRANSIENT = {"rateLimitExceeded", "serverOverloaded", "internalServerError", "flexUnavailable",
              "policyCapacityUnavailable", "httpConnectionFailed", "responseStreamConnectionFailed",
              "responseStreamDisconnected", "responseTooManyFailedAttempts", "sandboxError"}


class _RpcError(Exception):
    def __init__(self, method: str, error: Dict[str, Any]):
        self.code, self.text = error.get("code"), str(error.get("message", error))
        super().__init__(f"{method}: {self.text}")


# ------------------------------------------------------------------------------------------- WebSocket, stdlib only


def _frame(op: int, payload: bytes) -> bytes:
    n, mask = len(payload), os.urandom(4)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class _WS:
    """A client of the app-server's WebSocket on a unix socket: one JSON-RPC message per text frame."""

    def __init__(self, path: str, timeout: float = 5.0):
        # the server's file is a symlink to a shorter path, which also keeps us under the sun_path limit
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(os.path.realpath(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise OSError("closed during the handshake")
                buf += chunk
        except OSError:
            self.sock.close()
            raise
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            self.sock.close()
            raise OSError("no WebSocket upgrade: " + head.split(b"\r\n")[0].decode("latin-1"))
        self.frag = b""

    def send(self, obj: Dict[str, Any]) -> None:
        self.sock.settimeout(10)
        self.sock.sendall(_frame(1, json.dumps(obj).encode()))

    def read(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        """The messages available within `timeout` seconds. EOFError when the server closed."""
        out = self._parse()
        if out:
            return out
        if not select.select([self.sock], [], [], timeout)[0]:
            return []
        self.sock.settimeout(5)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("closed")
        self.buf += chunk
        return self._parse()

    def _parse(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        while len(self.buf) >= 2:
            op, n, off = self.buf[0] & 15, self.buf[1] & 127, 2
            fin, masked = self.buf[0] & 0x80, self.buf[1] & 0x80
            if n == 126:
                if len(self.buf) < 4:
                    break
                n, off = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                if len(self.buf) < 10:
                    break
                n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
            if masked:
                off += 4
            if len(self.buf) < off + n:
                break
            data = self.buf[off:off + n]
            if masked:
                key = self.buf[off - 4:off]
                data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
            self.buf = self.buf[off + n:]
            if op == 8:
                raise EOFError("closed")
            if op == 9:
                self.sock.sendall(_frame(10, data))
            elif op in (0, 1):
                self.frag += data
                if fin:
                    text, self.frag = self.frag, b""
                    try:
                        out.append(json.loads(text.decode("utf-8", "replace")))
                    except ValueError:
                        pass
        return out

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------------------------------------ small helpers


def _lstart(pid: int) -> str:
    """The process start time as `ps` prints it ("" if unknown): pid + start time identify a process."""
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _epoch(lstart: str, default: float) -> float:
    try:
        return time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return default


def _ms(v: Any) -> float:
    """A time given in seconds or milliseconds, as milliseconds."""
    if not isinstance(v, (int, float)) or not v:
        return 0.0
    return float(v) * 1000 if v < 1e11 else float(v)


def _args(v: Any) -> List[str]:
    if not v:
        return []
    return shlex.split(v) if isinstance(v, str) else [str(x) for x in v]


def _error_class(info: Any, message: str) -> ErrorClass:
    name = info if isinstance(info, str) else (next(iter(info)) if isinstance(info, dict) and info else "")
    if name in _BILLING:
        return ErrorClass.BILLING
    if name in _TRANSIENT:
        return ErrorClass.TRANSIENT
    if name == "contextWindowExceeded":
        return ErrorClass.SESSION_LOST
    if name and name not in ("other", "badRequest"):
        return ErrorClass.OTHER
    return classify_error(message)


def _user_text(item: Dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(item.get("text") or "")


class _Turn:
    """What the adapter knows of the turn it follows."""

    def __init__(self, turn_id: str):
        self.id = turn_id
        self.emitted = False  # TurnStarted reported
        self.lead: List[str] = []  # our messages that began it
        self.cmds: Dict[str, Tuple[str, str]] = {}  # running commands: item id -> (command, process id)
        self.final = ""
        self.last_text = ""
        self.compact = False
        self.seen: Set[str] = set()
        self.err = ""
        self.last_item = ""


class CodexAdapter(BaseAdapter):
    backend = "codex"
    caps = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=False, receipts=True,
                shares_terminal=True, compacts=True,
                stops_jobs=True)  # CONTRACT: backgroundTerminals/terminate is in the schema only, not probed live

    def __init__(self, clock: Clock):
        super().__init__(clock)
        self.agent = ""
        self.thread_id = ""
        self.cursor = ""  # pos of the last ended turn
        self._ws: Optional[_WS] = None
        self._popen: Optional[subprocess.Popen] = None
        self._dir = ""
        self._sock_path = ""
        self._next = 0
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._inbox: Deque[Dict[str, Any]] = deque()
        self._out: List[AgentEvent] = []
        self._t: Optional[_Turn] = None
        self._closed: Set[str] = set()  # turns that ended
        self._jobs: Dict[str, Job] = {}  # by item id
        self._late: Set[str] = set()  # replay: commands that outlived their turn
        self._replaying = False
        self._asked = False
        self._exit_reported = False
        self._detached = False
        self._n = 0

    # ------------------------------------------------------------------------------------------ opening

    @property
    def _options(self) -> Dict[str, Any]:
        return self.spec.choice.options if self.spec else {}

    def _exe(self) -> str:
        return str(self._options.get("executable", "codex"))

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        self.spec, self.agent = spec, spec.agent
        self._reset()
        self._dir = os.path.join(spec.run_dir or ".", "cli", spec.agent)
        os.makedirs(self._dir, exist_ok=True)
        self._sock_path = os.path.join(self._dir, "s.sock")
        pid, ws = self._join_old()
        if ws is None:
            pid, ws = self._spawn(spec)
        self._ws = ws
        try:
            self._handshake()
            self._open_thread(session)
        except Exception:
            self._ws = None
            ws.close()
            raise
        self.proc = ProcRef(pid=pid, started=self._proc_started(pid), endpoint=self._sock_path,
                            stream=self.thread_id, read_pos=self.cursor)
        return self.proc

    def _reset(self) -> None:
        self.thread_id = self.cursor = ""
        self._t, self._popen, self._ws = None, None, None
        self._responses.clear()
        self._inbox.clear()
        self._closed.clear()
        self._jobs.clear()
        self._asked = self._exit_reported = self._detached = False

    def _pid_file(self) -> str:
        return os.path.join(self._dir, "pid")

    def _proc_started(self, pid: int) -> float:
        return _epoch(_lstart(pid), self.clock.now())

    def _join_old(self) -> Tuple[int, Optional[_WS]]:
        """A server an earlier start left (Orch died before saving its ProcRef): join it, or end it if it does not answer."""
        try:
            with open(self._pid_file()) as f:
                rec = json.load(f)
            pid = int(rec["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0, None
        if self._alive(pid, rec.get("started", "")):
            try:
                return pid, _WS(self._sock_path)
            except OSError:
                self._kill(pid)
        try:
            os.unlink(self._pid_file())
        except OSError:
            pass
        return 0, None

    def _spawn(self, spec: AgentSpec) -> Tuple[int, _WS]:
        o = spec.choice.options
        cmd = [self._exe(), "app-server", "--listen", "unix://" + self._sock_path,
               "-c", "sandbox_mode=" + json.dumps(str(o.get("sandbox", "workspace-write"))),
               "-c", "approval_policy=" + json.dumps(str(o.get("approval_policy", "never"))),
               "-c", f"thread_unload_delay_secs={UNLOAD_SECS}"] + _args(o.get("app_server_args"))
        try:
            os.unlink(self._sock_path)
        except OSError:
            pass
        log = open(os.path.join(self._dir, "server.log"), "ab")
        try:
            self._popen = subprocess.Popen(cmd, cwd=spec.cwd or None, env={**os.environ, **spec.choice.env},
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        except FileNotFoundError as e:
            if spec.cwd and e.filename == spec.cwd:
                raise AdapterError(f"working directory not found: {spec.cwd}", ErrorClass.OTHER)
            raise AdapterError(f"executable not found: {cmd[0]}", ErrorClass.MISSING_CLI)
        except OSError as e:
            raise AdapterError(str(e), ErrorClass.OTHER)
        finally:
            log.close()
        pid = self._popen.pid
        with open(self._pid_file(), "w") as f:
            json.dump({"pid": pid, "started": _lstart(pid), "socket": self._sock_path}, f)
        end = time.monotonic() + 20
        while True:
            try:
                return pid, _WS(self._sock_path)
            except OSError as e:
                if self._popen.poll() is not None or time.monotonic() > end:
                    if self._popen.poll() is None:
                        self._kill(pid)  # never leave a server we could not use
                    raise AdapterError(f"codex app-server did not start: {e} {self._log_tail()}")
                time.sleep(0.1)

    def _log_tail(self) -> str:
        try:
            with open(os.path.join(self._dir, "server.log"), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 300))
                return f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _handshake(self) -> None:
        self._rpc("initialize", {"clientInfo": {"name": "orch", "title": "orch", "version": "0"},
                                 "capabilities": {"experimentalApi": True}})
        assert self._ws
        self._ws.send({"method": "initialized"})

    def _thread_params(self, session_id: str = "") -> Dict[str, Any]:
        spec, o = self.spec, self._options
        c = spec.choice if spec else Choice()
        p: Dict[str, Any] = {"sandbox": str(o.get("sandbox", "workspace-write")),
                             "approvalPolicy": str(o.get("approval_policy", "never"))}
        if spec and spec.cwd:
            p["cwd"] = spec.cwd
        if c.model:
            p["model"] = c.model
        if session_id:
            p["threadId"] = session_id
        elif c.effort:
            p["config"] = {"model_reasoning_effort": c.effort}
        if spec and spec.system_prompt:
            p["developerInstructions"] = spec.system_prompt
        return p

    def _open_thread(self, session: Optional[SessionRef]) -> None:
        pid_uid = f"start:{self._popen.pid if self._popen else 0}"
        if session is None:
            r = self._rpc("thread/start", self._thread_params())
            self.thread_id = r["thread"]["id"]
            self._emit(SessionChanged, pid_uid, session_id=self.thread_id, change=SessionChange.NEW)
        else:
            self.thread_id = session.session_id
            try:
                r = self._resume(session.session_id)
            except _RpcError as e:
                if re.search(r"no rollout|invalid session|not found", e.text, re.I):
                    self._emit(SessionChanged, pid_uid, session_id=session.session_id, change=SessionChange.LOST,
                               detail=e.text)
                    return
                raise AdapterError(str(e), classify_error(e.text))
            got = r["thread"]["id"]
            if got != session.session_id:
                self.thread_id = got
                self._emit(SessionChanged, pid_uid, session_id=got, change=SessionChange.DIFFERENT)
            else:
                self._emit(SessionChanged, pid_uid, session_id=got, change=SessionChange.RESUMED)
            self.cursor = self._tail()
            self._report_model(r)
            self._catch_up()
            return
        self._report_model(r)

    def _resume(self, thread_id: str) -> Dict[str, Any]:
        params = self._thread_params(thread_id)
        try:
            return self._rpc("thread/resume", params)
        except _RpcError as e:
            if "developerInstructions" in params and re.search(r"unknown field|developer", e.text, re.I):
                params.pop("developerInstructions")  # an older server: the prompt then stays with the thread
                return self._rpc("thread/resume", params)
            raise

    def _report_model(self, r: Dict[str, Any]) -> None:
        if r.get("model"):
            self._emit(ModelReported, f"model:{r['model']}:{r.get('reasoningEffort', '')}:{self._stamp()}",
                       model=str(r["model"]))

    def reconnect(self, proc: ProcRef) -> bool:
        self._reset()
        self._dir = os.path.dirname(proc.endpoint)
        self.agent = os.path.basename(self._dir)  # run_dir/cli/<agent>/s.sock: a new adapter knows no spec
        self._sock_path, self.thread_id, self.cursor = proc.endpoint, proc.stream, proc.read_pos
        self.proc = proc
        if not self._alive(proc.pid, "", proc.started):
            return False
        try:
            self._connect_resume()
        except (OSError, EOFError, _RpcError, AdapterError):
            return False
        return True

    def _connect_resume(self) -> None:
        """A fresh connection: initialize, thread/resume (events reach only a connection that did), read what was missed."""
        self._ws = _WS(self._sock_path)
        try:
            self._handshake()
            r = self._resume(self.thread_id)
        except Exception:
            self._ws.close()
            self._ws = None
            raise
        self._report_model(r)
        self._catch_up()

    # --------------------------------------------------------------------------------------------- the process

    def _alive(self, pid: int, started: Any = "", epoch: float = 0.0) -> bool:
        """`started`: the ps text from a pid file; `epoch`: a ProcRef's start time."""
        if not pid:
            return False
        if self._popen is not None and self._popen.pid == pid and self._popen.poll() is not None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False  # a process of another user is not ours
        if isinstance(started, str) and started:
            return _lstart(pid) == started
        if epoch:
            return abs(self._proc_started(pid) - epoch) < 2.5
        return True

    def _kill(self, pid: int) -> None:
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, sig) if pgid == pid else os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if self._popen is not None and self._popen.pid == pid:
                    if self._popen.poll() is not None:
                        return
                elif not self._alive(pid):
                    return
                time.sleep(0.05)

    def _check_proc(self) -> None:
        if self._exit_reported or self.proc is None or self._detached:
            return
        if self._alive(self.proc.pid, "", self.proc.started):
            return
        self._exit_reported = True
        t = self._t
        open_turn = t is not None and t.id not in self._closed
        if open_turn:
            self._emit(TurnEnded, f"turn:{t.id}:end", status=TurnStatus.STOPPED if self._asked else TurnStatus.ERROR,
                       error=None if self._asked else ErrorClass.TRANSIENT, detail="" if self._asked else
                       "codex app-server exited", session_id=self.thread_id)
            self._t = None
        code = self._popen.returncode if self._popen is not None else None
        self._emit(ProcessExited, f"exit:{self.proc.pid}", exit_code=code, asked=self._asked,
                   reported=open_turn and not self._asked,
                   detail="" if self._asked else "codex app-server exited")
        if self._ws:
            self._ws.close()
            self._ws = None

    def detach(self) -> None:
        self._detached = True
        if self._ws:
            self._ws.close()
            self._ws = None

    def stop(self) -> None:
        self._asked = True
        if self.proc is not None and self._alive(self.proc.pid, "", self.proc.started):
            self._kill(self.proc.pid)
        if self._ws:
            self._ws.close()
            self._ws = None
        for f in (self._pid_file(), self._sock_path):
            try:
                os.unlink(f)
            except OSError:
                pass

    # --------------------------------------------------------------------------------------------- transport

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        """One request. Notifications that arrive meanwhile wait in the inbox, so events keep their order."""
        if self._ws is None:
            raise AdapterError("not connected", ErrorClass.TRANSIENT)
        self._next += 1
        ident = self._next
        msg: Dict[str, Any] = {"id": ident, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._ws.send(msg)
            end = time.monotonic() + timeout
            while ident not in self._responses:
                if time.monotonic() > end:
                    raise AdapterError(f"{method}: no answer", ErrorClass.TRANSIENT)
                self._take(self._ws.read(0.2))
        except (OSError, EOFError) as e:
            self._ws_lost()
            raise AdapterError(f"{method}: {e}", ErrorClass.TRANSIENT)
        r = self._responses.pop(ident)
        if "error" in r:
            raise _RpcError(method, r["error"])
        return r.get("result") or {}

    def _take(self, messages: List[Dict[str, Any]]) -> None:
        for m in messages:
            if "method" not in m and "id" in m:
                self._responses[m["id"]] = m
            elif "method" in m and "id" not in m:
                self._inbox.append(m)
            # a request from the server (an approval) is not answered: approval_policy is "never"

    def _ws_lost(self) -> None:
        if self._ws:
            self._ws.close()
        self._ws = None

    def _pump(self) -> None:
        if self._ws is None and not self._detached and self.proc is not None and self.thread_id and not self._asked:
            if self._alive(self.proc.pid, "", self.proc.started):
                try:
                    self._connect_resume()  # the connection dropped but the server lives: read what was missed
                except (OSError, EOFError, _RpcError, AdapterError):
                    pass
        if self._ws is not None:
            try:
                self._take(self._ws.read(0))
            except (OSError, EOFError):
                self._ws_lost()
        while self._inbox:
            try:
                self._note(self._inbox.popleft())
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # a notification of a shape we do not know

    # --------------------------------------------------------------------------------------------- the events

    def events(self) -> Iterator[AgentEvent]:
        if self._detached:
            return iter(())
        self._pump()
        self._check_proc()
        out, self._out = self._out, []
        return iter(out)

    def _stamp(self) -> str:
        self._n += 1
        return f"{int(self.clock.now() * 1000)}.{self._n}"

    def _emit(self, kind: type, uid: str, pos: str = "", **kw: Any) -> None:
        self._out.append(kind(agent=self.agent, at=self.clock.now(), stream=self.thread_id, pos=pos, uid=uid, **kw))

    def _begin(self, turn_id: str) -> _Turn:
        self._t = _Turn(turn_id)
        self._closed.discard(turn_id)
        return self._t

    def _started(self, t: _Turn) -> None:
        if not t.emitted and not t.compact:
            t.emitted = True
            self._emit(TurnStarted, f"turn:{t.id}:start", message_ids=tuple(t.lead))

    def _note(self, m: Dict[str, Any]) -> None:
        method, p = m.get("method", ""), m.get("params") or {}
        if p.get("threadId") not in (None, self.thread_id):
            return
        if method == "turn/started":
            self._begin(p["turn"]["id"])
        elif method in ("item/started", "item/completed"):
            self._item(p, method.endswith("completed"))
        elif method == "item/agentMessage/delta":
            t = self._turn_of(p)
            if t is not None:
                self._started(t)
                self._emit(Text, "t" + self._stamp(), text=str(p.get("delta", "")))
        elif method == "turn/completed":
            self._turn_end(p["turn"])
        elif method == "error":
            err = p.get("error") or {}
            if p.get("willRetry"):
                self._emit(Text, "t" + self._stamp(), text="")  # the CLI's own retry: bare progress
            elif self._t is not None:
                self._t.err = str(err.get("message", ""))
        elif method in ("thread/settings/updated", "model/rerouted"):
            model = (p.get("threadSettings") or {}).get("model") or p.get("toModel") or p.get("model")
            if model:
                self._emit(ModelReported, f"model:{model}:{self._stamp()}", model=str(model))

    def _turn_of(self, p: Dict[str, Any]) -> Optional[_Turn]:
        """The turn a notification belongs to; one we joined in the middle (resume) starts being followed here."""
        tid = p.get("turnId") or (self._t.id if self._t else "")
        if tid in self._closed:
            return None
        if self._t is None or (tid and self._t.id != tid):
            if not tid:
                return None
            return self._begin(tid)
        return self._t

    def _item(self, p: Dict[str, Any], done: bool) -> None:
        item = p["item"]
        typ, iid = item.get("type"), str(item.get("id", ""))
        if typ == "commandExecution" and done and iid in self._jobs:
            code = item.get("exitCode")
            job = self._jobs[iid]
            self._jobs[iid] = Job(**{**job.__dict__, "state": JobState.ENDED, "exit_code": code,
                                     "ended": self.clock.now()})
            self._emit(JobEnded, f"job:{iid}:end", job_id=job.id, exit_code=code,
                       lost=code == -1 and item.get("status") == "failed")
            return
        t = self._turn_of(p)
        if t is None:
            return
        if typ == "contextCompaction":
            t.compact = True
            return
        if typ == "userMessage":
            if iid in t.seen:
                return
            t.seen.add(iid)
            cid = item.get("clientId") or ""
            if OUR_ID.match(cid):
                if t.emitted:
                    self._emit(Received, f"um:{iid}", message_id=cid, taken=True)
                else:
                    t.lead.append(cid)
            return
        self._started(t)
        t.last_item = iid or t.last_item
        if typ == "commandExecution":
            if not done:
                command = str(item.get("command") or "")
                t.cmds[iid] = (command, str(item.get("processId", "")))
                self._emit(ToolStart, f"ts:{iid}", tool_id=iid, label=" ".join(command.split())[:160] or "Shell",
                           call=command)
            else:
                t.cmds.pop(iid, None)
                self._emit(ToolEnd, f"te:{iid}", tool_id=iid)
        elif typ == "agentMessage" and done:
            text = str(item.get("text", ""))
            t.last_text = text
            if item.get("phase") == "final_answer":
                t.final = text
            if self._replaying and text:
                self._emit(Text, f"am:{iid}", text=text)

    def _running_ids(self) -> Set[str]:
        try:
            r = self._rpc("thread/backgroundTerminals/list", {"threadId": self.thread_id})
        except (AdapterError, _RpcError):
            return set()
        return {str(e.get(k)) for e in r.get("data") or [] for k in ("itemId", "processId") if e.get(k)}

    def _turn_end(self, turn: Dict[str, Any]) -> None:
        t = self._t if self._t is not None and self._t.id == turn["id"] else self._begin(turn["id"])
        status = turn.get("status")
        if t.compact:
            if status == "completed":
                self._emit(SessionChanged, f"compact:{t.id}", session_id=self.thread_id, change=SessionChange.COMPACTED)
            self._finish(t)
            return
        self._started(t)
        running = self._late if self._replaying else self._running_ids()
        for iid, (command, process) in list(t.cmds.items()):
            if iid in running or (process and process in running):
                job = Job(job_id(self.agent, iid), self.agent, " ".join(command.split())[:200],
                          started=self.clock.now(), command=command)
                self._jobs[iid] = job
                self._emit(JobStarted, f"job:{iid}:start", job=job)
            else:
                self._emit(ToolEnd, f"te:{iid}", tool_id=iid)
        t.cmds.clear()
        err = turn.get("error") or {}
        error, detail = None, ""
        if status == "failed":
            detail = str(err.get("message") or t.err or "the turn failed")
            error = _error_class(err.get("codexErrorInfo"), detail)
        ts = {"completed": TurnStatus.OK, "interrupted": TurnStatus.STOPPED}.get(status, TurnStatus.ERROR)
        pos = f"{t.id}/{t.last_item}"
        self._emit(TurnEnded, f"turn:{t.id}:end", pos, text="" if ts != TurnStatus.OK else (t.final or t.last_text),
                   status=ts, error=error, detail=detail, session_id=self.thread_id)
        self.cursor = pos
        self._finish(t)

    def _finish(self, t: _Turn) -> None:
        self._closed.add(t.id)
        if len(self._closed) > 200:
            self._closed = set(list(self._closed)[-100:])
        self._t = None

    # --------------------------------------------------------------------------------------------- catching up

    def _tail(self) -> str:
        """The read position after the newest ended turn: where a resumed session's news begin."""
        r = self._rpc("thread/turns/list", {"threadId": self.thread_id, "itemsView": "notLoaded",
                                            "sortDirection": "desc", "limit": 10})
        for t in r.get("data") or []:
            if t.get("status") != "inProgress":
                return f"{t['id']}/"
        return ""

    def _turns_after(self, turn_id: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        cursor: Any = None
        for _ in range(100):
            r = self._rpc("thread/turns/list", {"threadId": self.thread_id, "itemsView": "full",
                                                "sortDirection": "desc", "limit": 20, "cursor": cursor})
            for t in r.get("data") or []:
                if turn_id and t["id"] == turn_id:
                    return found[::-1]
                found.append(t)
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return found[::-1]

    def _item_times(self, turn_id: str) -> Dict[str, float]:
        try:
            r = self._rpc("thread/items/list", {"threadId": self.thread_id, "turnId": turn_id, "limit": 500})
        except (AdapterError, _RpcError):
            return {}
        return {str(e["item"]["id"]): _ms(e.get("completedAtMs")) for e in r.get("data") or []
                if isinstance(e.get("item"), dict)}

    def _catch_up(self) -> None:
        turn_id = self.cursor.split("/")[0]
        self._replaying = True
        try:
            for turn in self._turns_after(turn_id):
                self._feed(turn)
        finally:
            self._replaying = False
        self._scan_jobs(turn_id)

    def _feed(self, turn: Dict[str, Any]) -> None:
        """Replay one turn as the notifications it was made of. A command that outlived its turn (still running, or ended
        after the turn did) completes after turn/completed, as it did live."""
        tid, items = turn["id"], turn.get("items") or []
        ended = turn.get("status") in ("completed", "interrupted", "failed")
        has_cmd = any(i.get("type") == "commandExecution" for i in items)
        times = self._item_times(tid) if ended and has_cmd else {}
        end_ms = _ms(turn.get("completedAt"))
        self._late = set()
        late: List[Dict[str, Any]] = []

        def note(method: str, **params: Any) -> None:
            self._note({"method": method, "params": {"threadId": self.thread_id, "turnId": tid, **params}})

        note("turn/started", turn={"id": tid})
        for n, it in enumerate(items):
            note("item/started", item=it)
            if it.get("type") == "commandExecution":
                if it.get("status") == "inProgress":
                    if ended:
                        self._late.add(str(it["id"]))
                    continue
                done_ms = times.get(str(it["id"]), 0.0)
                if ended and end_ms and done_ms > end_ms:
                    self._late.add(str(it["id"]))
                    late.append(it)
                    continue
            elif not ended and n == len(items) - 1 and it.get("type") == "agentMessage":
                continue  # may still be written: its own item/completed follows live
            note("item/completed", item=it)
        if ended:
            note("turn/completed", turn=turn)
            for it in late:
                note("item/completed", item=it)
        self._late = set()

    def _scan_jobs(self, turn_id: str) -> None:
        """Fill the job registry from the last turns up to the cursor: jobs of earlier turns that run or have ended."""
        try:
            r = self._rpc("thread/turns/list", {"threadId": self.thread_id, "itemsView": "full",
                                                "sortDirection": "desc", "limit": 10})
        except (AdapterError, _RpcError):
            return
        for turn in r.get("data") or []:
            if turn.get("status") == "inProgress":
                continue
            cmds = [i for i in turn.get("items") or [] if i.get("type") == "commandExecution"]
            if not cmds:
                continue
            times, end_ms = self._item_times(turn["id"]), _ms(turn.get("completedAt"))
            for it in cmds:
                iid, state = str(it["id"]), JobState.RUNNING
                if it.get("status") != "inProgress":
                    if not (end_ms and times.get(iid, 0.0) > end_ms):
                        continue
                    state = JobState.ENDED
                if iid not in self._jobs:
                    command = str(it.get("command") or "")
                    self._jobs[iid] = Job(job_id(self.agent, iid), self.agent, " ".join(command.split())[:200],
                                          started=self.clock.now(), state=state,
                                          exit_code=it.get("exitCode") if state == JobState.ENDED else None,
                                          ended=self.clock.now() if state == JobState.ENDED else 0.0, command=command)

    # --------------------------------------------------------------------------------------------- sending

    def send(self, message_id: str, text: str) -> None:
        if self._detached:
            raise AdapterError("not connected", ErrorClass.OTHER)
        if self._ws is None:
            self._pump()
        if self._ws is None or not self.thread_id:
            raise AdapterError("not connected", ErrorClass.TRANSIENT)
        self._pump()  # a turn that just ended must not be steered
        inp = [{"type": "text", "text": text}]
        t = self._t
        try:
            if t is not None and t.id not in self._closed:
                try:
                    self._rpc("turn/steer", {"threadId": self.thread_id, "expectedTurnId": t.id,
                                             "clientUserMessageId": message_id, "input": inp})
                except _RpcError as e:
                    if "not steerable" in e.text or "activeTurnNotSteerable" in e.text:
                        raise AdapterError(e.text, ErrorClass.TRANSIENT)
                    if not re.search(r"no active turn|expected active turn", e.text):
                        raise
                    self._start_turn(message_id, inp)
            else:
                self._start_turn(message_id, inp)
        except _RpcError as e:
            if re.search(r"no rollout|invalid session", e.text, re.I):
                raise AdapterError(e.text, ErrorClass.SESSION_LOST)
            raise AdapterError(str(e), classify_error(e.text))
        self._emit(Received, f"recv:{message_id}:{self._stamp()}", message_id=message_id)

    def _start_turn(self, message_id: str, inp: List[Dict[str, Any]]) -> None:
        c = self.spec.choice if self.spec else Choice()
        params: Dict[str, Any] = {"threadId": self.thread_id, "clientUserMessageId": message_id, "input": inp}
        if c.model:
            params["model"] = c.model  # sticky: a switch within this session takes effect with the next turn
        if c.effort:
            params["effort"] = c.effort
        self._rpc("turn/start", params)

    def arrived(self, message_id: str) -> Optional[bool]:
        """Is a userMessage with our id in the thread? (Codex keeps no queue: a message is in a turn or not at all.)"""
        if self._ws is None or not self.thread_id:
            return None
        cursor: Any = None
        try:
            for _ in range(5):
                r = self._rpc("thread/turns/list", {"threadId": self.thread_id, "itemsView": "full",
                                                    "sortDirection": "desc", "limit": 10, "cursor": cursor})
                for t in r.get("data") or []:
                    if any(i.get("type") == "userMessage" and i.get("clientId") == message_id
                           for i in t.get("items") or []):
                        return True
                cursor = r.get("nextCursor")
                if not cursor:
                    break
        except (AdapterError, _RpcError):
            return None
        return False

    # --------------------------------------------------------------------------------------------- jobs

    def jobs(self) -> List[Job]:
        """Running (from the server's own list, minus the open turn's commands) and the ended ones this adapter saw."""
        if self._ws is None:
            return []
        self._pump()
        try:
            r = self._rpc("thread/backgroundTerminals/list", {"threadId": self.thread_id})
        except (AdapterError, _RpcError):
            return [j for j in self._jobs.values() if j.state != JobState.RUNNING]
        self._pump()
        open_cmds = self._t.cmds if self._t is not None and self._t.id not in self._closed else {}
        open_proc = {p for _c, p in open_cmds.values()}
        listed = {}
        for e in r.get("data") or []:
            iid = str(e.get("itemId", ""))
            if iid and iid not in open_cmds and str(e.get("processId", "")) not in open_proc:
                listed[iid] = e
        out: List[Job] = []
        for iid, e in listed.items():
            job = self._jobs.get(iid)
            if job is None or job.state != JobState.RUNNING:
                command = str(e.get("command") or "")
                job = self._jobs[iid] = Job(job_id(self.agent, iid), self.agent,
                                            " ".join(command.split())[:200], started=self.clock.now(), command=command)
            out.append(job)
        out += [j for iid, j in self._jobs.items() if j.state != JobState.RUNNING]
        return out

    def stop_job(self, job_id_: str) -> bool:
        if self._ws is None:
            return False
        iid = job_id_.split("/", 1)[-1]
        try:
            r = self._rpc("thread/backgroundTerminals/list", {"threadId": self.thread_id})
            for e in r.get("data") or []:
                if str(e.get("itemId")) == iid and e.get("processId"):
                    self._rpc("thread/backgroundTerminals/terminate",
                              {"threadId": self.thread_id, "processId": e["processId"]})
                    return True
        except (AdapterError, _RpcError):
            pass
        return False

    # --------------------------------------------------------------------------------------------- the rest

    def compact(self) -> bool:
        if self._ws is None or not self.thread_id:
            return False
        try:
            self._rpc("thread/compact/start", {"threadId": self.thread_id})
        except (AdapterError, _RpcError):
            return False
        return True

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        o, c = spec.choice.options, spec.choice
        sock = os.path.join(spec.run_dir or ".", "cli", spec.agent, "s.sock")
        cmd = [str(o.get("executable", "codex")), "resume", "--remote", "unix://" + sock]
        conf = {"sandbox_mode": str(o.get("sandbox", "workspace-write")),
                "approval_policy": str(o.get("approval_policy", "never"))}
        if c.model:
            conf["model"] = c.model
        if c.effort:
            conf["model_reasoning_effort"] = c.effort
        if spec.system_prompt:
            conf["developer_instructions"] = spec.system_prompt
        for k, v in conf.items():
            cmd += ["-c", f"{k}={json.dumps(v)}"]
        return cmd + [session.session_id]

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        """The user's own lines (a terminal's messages carry a UUID, not one of our ids) after `since`: Orch's replies
        were ordinary turns already."""
        if self._ws is None:
            return []
        said: List[Said] = []
        for turn in self._turns_after(""):
            at = _ms(turn.get("startedAt")) / 1000
            if at <= since:
                continue
            for it in turn.get("items") or []:
                if it.get("type") == "userMessage" and not OUR_ID.match(it.get("clientId") or ""):
                    said.append(Said(USER, _user_text(it), at))
        return said

    def catalog(self) -> List[ModelInfo]:
        data: List[Dict[str, Any]] = []
        try:
            if self._ws is not None:
                cursor: Any = None
                for _ in range(10):
                    r = self._rpc("model/list", {"cursor": cursor} if cursor else {})
                    data += r.get("data") or []
                    cursor = r.get("nextCursor")
                    if not cursor:
                        break
            else:
                data = self._catalog_stdio()
        except (AdapterError, _RpcError, OSError):
            return []
        out = []
        for m in data:
            if m.get("hidden"):
                continue
            efforts = [str(e.get("reasoningEffort")) for e in m.get("supportedReasoningEfforts") or []
                       if isinstance(e, dict) and e.get("reasoningEffort")]
            model = str(m.get("model") or m.get("id"))
            out.append(ModelInfo(id=model, backend="codex", model=model, label=str(m.get("displayName") or model),
                                 efforts=efforts, default_effort=str(m.get("defaultReasoningEffort") or "")))
        return out

    def _catalog_stdio(self) -> List[Dict[str, Any]]:
        """model/list from a short-lived server on stdin and stdout (JSON lines), when no agent is open."""
        proc = subprocess.Popen([self._exe(), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            def ask(ident: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
                assert proc.stdin and proc.stdout
                proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    if not select.select([proc.stdout], [], [], 0.5)[0]:
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("id") == ident:
                        return m.get("result") or {}
                raise OSError(f"{method}: no answer")

            ask(1, "initialize", {"clientInfo": {"name": "orch", "title": "orch", "version": "0"}})
            assert proc.stdin
            proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            proc.stdin.flush()
            data: List[Dict[str, Any]] = []
            cursor: Any = None
            for i in range(10):
                r = ask(2 + i, "model/list", {"cursor": cursor} if cursor else {})
                data += r.get("data") or []
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            return data
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
