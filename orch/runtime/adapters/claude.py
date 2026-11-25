"""Adapters for Claude Code, Qoder and Qwen Code: one open CLI each (docs/redesign.md 3.2, docs/probes/).

The CLI runs with stream-json input and output. Its stdin is a named pipe opened read-write, its output goes to a
file, so Orch can close and come back: the process keeps running, a new Orch reads the file from the saved position.
Files are in `<run_dir>/cli/<agent>/`: `in-N.fifo`, `out-N.jsonl`, `err-N.txt` (one set per start), `pid.json` (what
start() found or left), `sent.jsonl` (the journal of messages written to the pipe).

Claude gives every message back by uuid (`command_lifecycle`, the replay echo): the message id is part of the uuid.
Qoder replays the text only (matched by text against the journal); Qwen replays nothing, its turn starts with its
first output. Qwen does not wake itself after a job and writes no job event: its jobs are read from its status files.
"""
from __future__ import annotations

import glob
import json
import os
import re
import select
import signal
import subprocess
import time
import uuid as uuidlib
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..contracts import (AgentEvent, AgentSpec, Caps, Discarded, ErrorClass, Job, JobEnded, JobNoticed, JobStarted,
                         JobState, ModelInfo, ModelReported, ProcRef, ProcessExited, Received, Said, SessionChange,
                         SessionChanged, SessionRef, Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus,
                         USER)
from .base import AdapterError, BaseAdapter, classify_error, job_id

BASH_LIMIT_MS = "86400000"  # Claude ends a background job at 30 minutes (2 hours at most) unless both are raised
NO_BACKGROUND = ("Bash(nohup *)", "Bash(setsid *)", "Bash(*&)", "Bash(* & *)")  # jobs must use the CLI's own option
COMPACT_ID = "xc0aac0aac0aa"  # the id of our own /compact line: never reported as a message

_ID = re.compile(r"x[0-9a-f]{12}")
_MARK = "6f726368"  # "orch"
_UUID = re.compile(_MARK + r"-([0-9a-f]{4})-4([0-9a-f]{3})-8([0-9a-f]{3})-([0-9a-f]{2})0{10}")
_LIMIT = re.compile(r"hit your .{0,20}limit|usage limit|limit reached", re.I)
_LOST = re.compile(r"No conversation found|Invalid session identifier|No saved session found", re.I)
_NETWORK = re.compile(r"ECONN\w*|ETIMEDOUT|ENOTFOUND|unable to connect", re.I)
_EXIT_CODE = re.compile(r"exit(?:ed with)? code (-?\d+)")
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")


def to_uuid(message_id: str) -> str:
    """A message id (x + 12 hex digits) as a UUID that reads back with from_uuid."""
    h = message_id[1:]
    return f"{_MARK}-{h[:4]}-4{h[4:7]}-8{h[7:10]}-{h[10:]}0000000000"


def from_uuid(value: Any) -> str:
    m = _UUID.fullmatch(value) if isinstance(value, str) else None
    return "x" + "".join(m.groups()) if m else ""


def _ps(pid: int) -> Optional[Tuple[float, str]]:
    """(start time, state) of a process, or None."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=,stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                             env={**os.environ, "LC_ALL": "C"}).stdout.split()
        return time.mktime(time.strptime(" ".join(out[:5]), "%a %b %d %H:%M:%S %Y")), out[5]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _running(pid: int, started: float) -> bool:
    """The process with this pid and start time lives (not a zombie, not a later process with the same pid)."""
    info = _ps(pid) if pid > 0 else None
    return bool(info) and not info[1].startswith("Z") and (not started or abs(info[0] - started) < 3)


def _descendants(pid: int) -> Set[int]:
    try:
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    kids: Dict[int, List[int]] = {}
    for line in rows.splitlines():
        try:
            a, b = (int(x) for x in line.split())
        except ValueError:
            continue
        kids.setdefault(b, []).append(a)
    found, todo = set(), [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in found:
                found.add(k)
                todo.append(k)
    return found


def _group(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except OSError:
        return 0


def _text_of(blocks: List[Any]) -> str:
    return "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def _blocks(content: Any) -> List[Any]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else (content if isinstance(content, list) else [])


def _label(block: Dict[str, Any]) -> str:
    i = block.get("input") if isinstance(block.get("input"), dict) else {}
    detail = i.get("command") or i.get("file_path") or i.get("description") or i.get("pattern") or ""
    return f"{block.get('name', '')}: {detail}".strip(": ").replace("\n", " ")[:120]


def _epoch(text: Any) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


_RANK = {"try": 0, "done": 1, "queued": 2, "taken": 3, "gone": 4}


class ClaudeAdapter(BaseAdapter):
    """Claude Code. Qoder and Qwen differ only in what the class attributes and the few overridden methods say."""

    backend = "claude"
    exe = "claude"
    caps = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=True, receipts=True,
                shares_terminal=False, compacts=True, stops_jobs=False)
    roundtrip = True  # the message id comes back in the uuid
    echoes = True  # the CLI replays the messages it takes in
    stream_jobs = True  # task_* events tell about jobs
    compact_text = "/compact"
    history = "~/.claude/projects"  # where the sessions are kept, for transcript()
    efforts = ["low", "medium", "high", "xhigh", "max"]
    models = ["opus", "sonnet", "haiku"]

    def __init__(self, clock: Any):
        super().__init__(clock)
        self._dir = ""
        self._agent = ""
        self._pid = 0
        self._started = 0.0
        self._popen: Optional[subprocess.Popen] = None
        self._out = ""
        self._fifo = ""
        self._stream = ""
        self._off = 0
        self._queue: List[AgentEvent] = []
        self._journal: Dict[str, Dict[str, Any]] = {}
        self._alias: Dict[str, str] = {}
        self._joined = True
        self._asked = False  # stop() was called: the end is not a failure
        self._dead_reported = False
        self._quiet = False
        self._reset()

    def _reset(self) -> None:
        """What reading the output of one process builds up."""
        self._open = False  # a turn was started and not ended
        self._parts: List[str] = []
        self._synth: Optional[str] = None  # the error field of a synthetic (API error) message of this turn
        self._compacting = False
        self._lost = False  # session loss was reported
        self._asked_sid = ""
        self._sid = ""
        self._model = ""
        self._bg: Set[str] = set()
        self._fg: Set[str] = set()
        self._tasks: Dict[str, Job] = {}
        self._cmds: Dict[str, str] = {}  # tool_use_id -> command
        self._stamps: Dict[str, float] = {}  # tool_use_id -> when the assistant called it
        self._ended_at: Dict[str, float] = {}
        self._qseen: Set[str] = set()  # Qwen: its jobs seen in the stream
        self._qended: Set[str] = set()  # Qwen: JobEnded reported

    # ------------------------------------------------------------------------------------------------- opening

    def _args(self, spec: AgentSpec, sid: str, resume: bool, terminal: bool) -> List[str]:
        o = spec.choice.options
        cmd = [str(o.get("executable") or self.exe)]
        if not terminal:
            cmd += ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--replay-user-messages"]
        cmd += ["--resume" if resume else "--session-id", sid]
        if spec.choice.model:
            cmd += ["--model", spec.choice.model]
        if spec.choice.effort:
            cmd += ["--effort", spec.choice.effort]
        if spec.system_prompt:
            cmd += ["--system-prompt" if o.get("system_prompt_mode") == "replace" else "--append-system-prompt",
                    spec.system_prompt]
        if o.get("permission_mode"):  # only what the agent's own configuration asks for
            cmd += ["--permission-mode", str(o["permission_mode"])]
        if o.get("allowed_tools"):
            cmd += ["--allowedTools", self._join(o["allowed_tools"])]
        cmd += ["--disallowedTools", self._join(list(NO_BACKGROUND) + self._list(o.get("disallowed_tools")))]
        for d in self._list(o.get("add_dirs")):
            cmd += ["--add-dir", os.path.expanduser(d)]
        if o.get("mcp_config"):
            cmd += ["--mcp-config", os.path.expanduser(str(o["mcp_config"]))]
        return cmd + self._list(o.get("extra_args"))

    @staticmethod
    def _list(v: Any) -> List[str]:
        return [] if not v else [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else [str(x) for x in v]

    @classmethod
    def _join(cls, v: Any) -> str:
        return ",".join(cls._list(v))

    def _env(self, spec: AgentSpec) -> Dict[str, str]:
        return {**os.environ, "BASH_DEFAULT_TIMEOUT_MS": BASH_LIMIT_MS, "BASH_MAX_TIMEOUT_MS": BASH_LIMIT_MS,
                **spec.choice.env}

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        self.spec, self._agent = spec, spec.agent
        self._dir = os.path.join(spec.run_dir, "cli", spec.agent)
        os.makedirs(self._dir, exist_ok=True)
        if self._pid and self._alive():
            self.stop()
        self._drain()
        resume = session.session_id if session and session.session_id else ""
        self._load_journal()
        leftover = self._read_json(os.path.join(self._dir, "pid.json"))
        if leftover.get("pid") and _running(int(leftover["pid"]), float(leftover.get("started") or 0)):
            if resume and leftover.get("session") == resume:  # an earlier start of this Orch, same session: join it
                return self._join(leftover)
            self._kill_tree(int(leftover["pid"]))
        sid = resume or str(uuidlib.uuid4())
        n = len(glob.glob(os.path.join(self._dir, "out-*.jsonl"))) + 1
        out, err, fifo = (os.path.join(self._dir, f"{k}-{n:04d}.{x}") for k, x in (("out", "jsonl"), ("err", "txt"),
                                                                                    ("in", "fifo")))
        if os.path.exists(fifo):
            os.unlink(fifo)
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR)
        try:
            with open(out, "ab") as o, open(err, "ab") as e:
                try:
                    proc = subprocess.Popen(self._args(spec, sid, bool(resume), False), cwd=spec.cwd or None,
                                            env=self._env(spec), stdin=fd, stdout=o, stderr=e, start_new_session=True)
                except FileNotFoundError as ex:
                    if spec.cwd and ex.filename == spec.cwd:
                        raise AdapterError(f"working folder not found: {spec.cwd}", ErrorClass.OTHER)
                    raise AdapterError(f"executable not found: {ex.filename}", ErrorClass.MISSING_CLI)
                except OSError as ex:
                    raise AdapterError(f"could not start: {ex}", ErrorClass.TRANSIENT)
        finally:
            os.close(fd)
        info = _ps(proc.pid)
        started = info[0] if info else self.clock.now()
        self._attach(out, fifo, proc.pid, started, 0, sid)
        self._popen = proc
        self._write_json(os.path.join(self._dir, "pid.json"), {"pid": proc.pid, "started": started, "session": sid,
                                                              "out": out, "fifo": fifo})
        self._started_event(SessionChange.RESUMED if resume else SessionChange.NEW, sid)
        return self.proc  # type: ignore[return-value]

    def _join(self, meta: Dict[str, Any]) -> ProcRef:
        self._attach(meta["out"], meta["fifo"], int(meta["pid"]), float(meta["started"]), 0, str(meta.get("session", "")))
        self._started_event(SessionChange.RESUMED, self._asked_sid)
        return self.proc  # type: ignore[return-value]

    def _started_event(self, change: SessionChange, sid: str) -> None:
        self._queue.append(self._ev(SessionChanged, uid=f"start:{self._pid}", session_id=sid, change=change))

    def _attach(self, out: str, fifo: str, pid: int, started: float, off: int, sid: str) -> None:
        """Point at one process and its files; read its output up to `off` only to learn its state."""
        self._popen = None
        self._out, self._fifo, self._pid, self._started = out, fifo, pid, started
        self._stream, self._off = os.path.basename(out), 0
        self._asked = self._dead_reported = False
        self._reset()
        self._asked_sid = self._sid = sid
        self._joined = True
        if off:
            self._quiet = True
            try:
                self._read(limit=off)
            finally:
                self._quiet = False
        self.proc = ProcRef(pid, started, fifo, out, self._stream, str(self._off))

    def reconnect(self, proc: ProcRef) -> bool:
        self.spec = None
        self._dir = os.path.dirname(proc.output_file)
        self._agent = os.path.basename(self._dir)
        self._queue = []
        meta = self._read_json(os.path.join(self._dir, "pid.json"))
        self._load_journal()
        sid = str(meta.get("session", "")) if meta.get("pid") == proc.pid else ""
        self._attach(proc.output_file, proc.endpoint, proc.pid, proc.started, int(proc.read_pos or 0), sid)
        self.proc = proc
        return self._alive()

    # -------------------------------------------------------------------------------------------- the process

    def _alive(self) -> bool:
        if not self._pid:
            return False
        if self._popen is not None and self._popen.pid == self._pid:
            return self._popen.poll() is None
        return _running(self._pid, self._started)

    def _kill_tree(self, pid: int) -> None:
        """SIGTERM: the CLI ends its jobs itself. If it must be killed, SIGKILL leaves the jobs without an owner, so
        their process groups are killed too (found before, as their parent is gone after)."""
        kids = _descendants(pid)
        own = _group(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._gone(pid):
                return
            time.sleep(0.1)
        groups = {_group(k) for k in kids | _descendants(pid)} | {own}
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        for g in groups:
            if g > 1 and g != os.getpgrp():
                try:
                    os.killpg(g, signal.SIGKILL)
                except OSError:
                    pass
        self._gone(pid, 3)

    def _gone(self, pid: int, wait: float = 0.0) -> bool:
        if self._popen is not None and self._popen.pid == pid:
            try:
                self._popen.wait(timeout=wait or 0.01)
                return True
            except subprocess.TimeoutExpired:
                return False
        end = time.monotonic() + wait
        while True:
            if not _running(pid, 0):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.1)

    def stop(self) -> None:
        self._asked = True
        if self._pid and self._alive():
            self._kill_tree(self._pid)

    def detach(self) -> None:
        self._joined = False

    # --------------------------------------------------------------------------------------------- sending

    def _alias_of(self, message_id: str) -> str:
        if _ID.fullmatch(message_id):
            return to_uuid(message_id)
        u = str(uuidlib.uuid5(uuidlib.NAMESPACE_OID, message_id))
        self._alias[u] = message_id
        return u

    def _line(self, message_id: str, text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": text},
                           "uuid": self._alias_of(message_id)})

    def _write(self, line: str) -> None:
        if not self._joined or not self._fifo:
            raise AdapterError("not connected", ErrorClass.OTHER)
        if not self._alive():
            raise AdapterError("the CLI is not running", ErrorClass.TRANSIENT)
        data = memoryview((line + "\n").encode())
        try:
            fd = os.open(self._fifo, os.O_RDWR | os.O_NONBLOCK)
        except OSError as ex:
            raise AdapterError(f"input pipe: {ex}", ErrorClass.TRANSIENT)
        try:
            deadline = time.monotonic() + 20
            while data:
                try:
                    data = data[os.write(fd, data):]
                except BlockingIOError:
                    if time.monotonic() > deadline or not self._alive():
                        raise AdapterError("the CLI does not read its input", ErrorClass.TRANSIENT)
                    select.select([], [fd], [], 0.5)
                except OSError as ex:
                    raise AdapterError(f"input pipe: {ex}", ErrorClass.TRANSIENT)
        finally:
            os.close(fd)

    def send(self, message_id: str, text: str) -> None:
        self._note(message_id, "try", text)
        self._write(self._line(message_id, text))
        self._note(message_id, "done")

    def compact(self) -> bool:
        if not self._joined or not self._alive():
            return False
        self._compacting = True
        self._write(self._line(COMPACT_ID, self.compact_text))
        return True

    # ---------------------------------------------------------------------------------------------- journal

    def _load_journal(self) -> None:
        self._journal = {}
        try:
            with open(os.path.join(self._dir, "sent.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        self._apply(r["id"], r["s"], r.get("text"), r.get("pid"))
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass

    def _apply(self, mid: str, state: str, text: Optional[str], pid: Optional[int]) -> None:
        old = self._journal.pop(mid, None) or {}
        if state != "gone" and old and _RANK.get(old.get("s", ""), 0) > _RANK[state] and text is None:
            state = old["s"]
        self._journal[mid] = {"s": state, "text": old.get("text") if text is None else text,
                              "pid": old.get("pid") if pid is None else pid}

    def _note(self, mid: str, state: str, text: Optional[str] = None) -> None:
        if self._quiet or mid == COMPACT_ID or not self._dir:
            return
        pid = self._pid if state == "try" else None
        self._apply(mid, state, text, pid)
        rec = {"id": mid, "s": state}
        if text is not None:
            rec.update(text=text, pid=self._pid)
        try:
            with open(os.path.join(self._dir, "sent.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def arrived(self, message_id: str) -> Optional[bool]:
        r = self._journal.get(message_id)
        if r is None or r["s"] == "gone":
            return False
        if r["s"] == "taken":
            return True  # it is in the session
        if r["pid"] != self._pid:
            return False  # the queue of the process it was given to died with it
        return None if r["s"] == "try" else True

    # ---------------------------------------------------------------------------------------------- events

    def _ev(self, cls: Any, **kw: Any) -> Any:
        return cls(agent=self._agent, at=self.clock.now(), stream=self._stream, **kw)

    def events(self) -> Iterator[AgentEvent]:
        if self._joined and self._out:
            alive = self._alive()
            self._read()
            if not alive and not self._dead_reported:
                self._read()
                self._died()
            self._poll_jobs()
        while self._queue:
            yield self._queue.pop(0)

    def _drain(self) -> None:
        """Before another process replaces this one: what it left."""
        if self._out and self._joined:
            alive = self._alive()
            self._read()
            if not alive and not self._dead_reported:
                self._died()

    def _read(self, limit: Optional[int] = None) -> None:
        """Read the complete new lines of the output file (to `limit` bytes at most) and turn them into events."""
        try:
            with open(self._out, "rb") as f:
                f.seek(self._off)
                data = f.read() if limit is None else f.read(max(0, limit - self._off))
        except OSError:
            return
        data = data[:data.rfind(b"\n") + 1]
        for raw in data.splitlines(keepends=True):
            start, end = self._off, self._off + len(raw)
            self._off = end
            try:
                obj = json.loads(raw)
            except ValueError:
                continue  # not a stream line (Qoder prints a few notices)
            evs = self._handle(obj) if isinstance(obj, dict) else []
            if self._quiet:
                continue
            for i, ev in enumerate(evs):
                last = i == len(evs) - 1
                self._queue.append(replace(ev, stream=self._stream, pos=str(end if last else start),
                                           uid="" if len(evs) == 1 else f"{start}:{i}"))

    def _died(self) -> None:
        """The process is gone: end the open turn, report a lost session if the CLI said so, report the exit."""
        self._dead_reported = True
        detail = self._tail()
        asked = self._asked
        reported = False
        if self._open:
            self._open = False
            self._queue.append(self._ev(TurnEnded, uid=f"end:{self._pid}", text="", detail=detail,
                                        status=TurnStatus.STOPPED if asked else TurnStatus.ERROR,
                                        error=None if asked else ErrorClass.TRANSIENT))
            reported = not asked
        elif not asked and not self._lost and _LOST.search(detail):
            self._queue.append(self._ev(SessionChanged, uid=f"lost:{self._pid}", change=SessionChange.LOST,
                                        session_id=self._asked_sid, detail=detail))
            self._lost = True
        code = self._popen.returncode if self._popen is not None and self._popen.pid == self._pid else None
        self._queue.append(self._ev(ProcessExited, uid=f"exit:{self._pid}", exit_code=code, detail=detail, asked=asked,
                                    reported=reported or self._lost))

    def _tail(self) -> str:
        err = re.sub(r"(out-\d+)\.jsonl$", lambda m: m.group(1).replace("out", "err") + ".txt", self._out)
        try:
            with open(err, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 400))
                return f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------------------------------- one line

    def _handle(self, e: Dict[str, Any]) -> List[AgentEvent]:
        kind, sub = e.get("type"), e.get("subtype")
        if kind == "system":
            return self._system(e, sub)
        if self._compacting and kind != "result":
            return []
        if kind == "command_lifecycle":
            return self._lifecycle(e)
        if kind == "user":
            return self._user(e)
        if kind == "assistant":
            return self._assistant(e)
        if kind == "result":
            return self._result(e)
        return []

    def _report_model(self, model: Any) -> List[AgentEvent]:
        if not model or model == "<synthetic>" or model == self._model:
            return []
        self._model = str(model)
        return [self._ev(ModelReported, model=self._model)]

    def _system(self, e: Dict[str, Any], sub: Any) -> List[AgentEvent]:
        out: List[AgentEvent] = []
        if sub == "init":
            sid = e.get("session_id") or ""
            if sid and sid != self._sid:
                out.append(self._ev(SessionChanged, session_id=sid,
                                    change=SessionChange.DIFFERENT if self._sid else SessionChange.NEW))
                self._sid = sid
            out += self._report_model(e.get("model") if self.reports_init_model else "")
        elif sub == "compact_boundary":
            out.append(self._ev(SessionChanged, session_id=self._sid, change=SessionChange.COMPACTED))
            self._compact_done = True
        elif sub == "api_retry":
            out.append(self._ev(Text, text=""))  # the CLI is trying again: not frozen
        elif sub == "task_started" and self.stream_jobs:
            out += self._task_started(e)
        elif sub == "task_updated":
            end = (e.get("patch") or {}).get("end_time")
            if isinstance(end, (int, float)):
                self._ended_at[e.get("task_id", "")] = end / 1000.0
        elif sub == "task_notification" and self.stream_jobs:
            out += self._task_ended(e)
        return out

    reports_init_model = True  # Qoder's init carries the name asked for, not the one that answers
    _compact_done = False

    def _task_started(self, e: Dict[str, Any]) -> List[AgentEvent]:
        tid, flag = str(e.get("task_id", "")), e.get("is_backgrounded")
        if not (flag is True or flag == "True" or (flag is None and e.get("task_type") == "local_bash")):
            self._fg.add(tid)  # a foreground tool: no job
            return []
        self._bg.add(tid)
        use = e.get("tool_use_id", "")
        label = (self._cmds.get(use) or e.get("description") or tid).replace("\n", " ")[:200]
        job = Job(job_id(self._agent, tid), self._agent, label, started=self._stamps.get(use) or self.clock.now())
        self._tasks[job.id] = job
        return [self._ev(JobStarted, job=job)]

    def _task_ended(self, e: Dict[str, Any]) -> List[AgentEvent]:
        tid = str(e.get("task_id", ""))
        if tid in self._fg or (tid not in self._bg and not e.get("output_file")):
            self._fg.discard(tid)
            return []
        jid, status = job_id(self._agent, tid), e.get("status")
        m = _EXIT_CODE.search(str(e.get("summary", "")))
        # a job the CLI stopped (time limit, failed with "timed out") has ended, not lost; its code is unknown
        code = int(m.group(1)) if m else (0 if status == "completed" else None)
        old = self._tasks.get(jid)
        if old is not None and old.state != JobState.RUNNING:
            return []
        ended = self._ended_at.pop(tid, 0.0) or self.clock.now()
        self._tasks[jid] = replace(old or Job(jid, self._agent), state=JobState.ENDED, exit_code=code, ended=ended)
        return [self._ev(JobEnded, job_id=jid, exit_code=code)]

    def _lifecycle(self, e: Dict[str, Any]) -> List[AgentEvent]:
        mid = self._id_of(e.get("command_uuid"))
        if not mid or mid == COMPACT_ID:
            return []
        if e.get("state") == "queued":
            self._note(mid, "queued")
            return [self._ev(Received, message_id=mid)]
        if e.get("state") == "cancelled":
            self._note(mid, "gone")
            return [self._ev(Discarded, message_id=mid)]
        return []

    def _id_of(self, value: Any) -> str:
        return from_uuid(value) or self._alias.get(value, "") if isinstance(value, str) else ""

    def _match(self, e: Dict[str, Any], text: str) -> str:
        """The id of our message that an echo shows."""
        if self.roundtrip:
            return self._id_of(e.get("uuid"))
        for mid, r in self._journal.items():
            if r["s"] in ("try", "done") and r["pid"] == self._pid and r["text"] is not None \
                    and _same(r["text"], text):
                return mid
        return ""

    def _start_turn(self, ids: Tuple[str, ...] = ()) -> List[AgentEvent]:
        self._open, self._parts, self._synth = True, [], None
        return [self._ev(TurnStarted, message_ids=ids)]

    def _user(self, e: Dict[str, Any]) -> List[AgentEvent]:
        blocks = _blocks((e.get("message") or {}).get("content"))
        out: List[AgentEvent] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(self._ev(ToolEnd, tool_id=str(b.get("tool_use_id", ""))))
                out += self._tool_result(b)
        if not (e.get("isReplay") and self.echoes):
            return out
        text = _text_of(blocks)
        if "<task-notification>" in text:  # a job's end told to the running turn; a wake turn needs nothing here
            if self._open:
                out += [self._ev(JobNoticed, job_id=job_id(self._agent, t)) for t in _TASK_ID.findall(text)]
            return out
        if text.startswith(("<command-name>", "<local-command")) or e.get("isSynthetic") \
                or self._id_of(e.get("uuid")) == COMPACT_ID:
            return out
        mid = self._match(e, text)
        if not mid and self.roundtrip:
            return out  # not ours: the turn starts with its own output
        if mid:
            self._note(mid, "taken")
        if self._open:
            if mid:
                out.append(self._ev(Received, message_id=mid, taken=True))
        else:
            out += self._start_turn((mid,) if mid else ())
        return out

    def _tool_result(self, block: Dict[str, Any]) -> List[AgentEvent]:
        return []

    def _assistant(self, e: Dict[str, Any]) -> List[AgentEvent]:
        m = e.get("message") or {}
        out: List[AgentEvent] = [] if self._open else self._start_turn()
        synthetic = m.get("model") == "<synthetic>"
        if synthetic:
            self._synth = str(e.get("error") or ("api_error" if e.get("is_api_error_message") else ""))
        else:
            out += self._report_model(m.get("model"))
        stamp = _epoch(e.get("timestamp"))
        for b in _blocks(m.get("content")):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and not synthetic:
                self._parts.append(str(b.get("text", "")))
                out.append(self._ev(Text, text=str(b.get("text", ""))))
            elif b.get("type") in ("thinking", "redacted_thinking"):
                out.append(self._ev(Text, text=""))
            elif b.get("type") == "tool_use":
                use = str(b.get("id", ""))
                self._stamps[use] = stamp
                self._tool_use(use, b)
                out.append(self._ev(ToolStart, tool_id=use, label=_label(b)))
        return out

    def _tool_use(self, use: str, block: Dict[str, Any]) -> None:
        i = block.get("input") if isinstance(block.get("input"), dict) else {}
        if i.get("command"):
            self._cmds[use] = str(i["command"])

    # --------------------------------------------------------------------------------------------- result

    def _result(self, r: Dict[str, Any]) -> List[AgentEvent]:
        text = str(r.get("result") or "")
        if self._compacting:  # our own /compact: the boundary says it, a CLI that has none (Qwen) ends here
            self._compacting = False
            done, self._compact_done = self._compact_done, False
            return [] if done else [self._ev(SessionChanged, session_id=self._sid, change=SessionChange.COMPACTED)]
        failed = self._failure(r, text)
        out: List[AgentEvent] = []
        if not self._open:
            if failed and failed[0] == ErrorClass.SESSION_LOST:  # no turn: the CLI refuses the session at the start
                self._lost = True
                return [self._ev(SessionChanged, session_id=self._asked_sid, change=SessionChange.LOST,
                                 detail=failed[1])]
            if not failed and not text and not r.get("num_turns"):
                return []  # a result with no turn (around a wake, after /compact)
            out += self._start_turn()
        status = TurnStatus.OK
        error, detail = None, ""
        if self._asked:
            status = TurnStatus.STOPPED
        elif failed:
            status, (error, detail) = TurnStatus.ERROR, failed
        reply = "" if status != TurnStatus.OK else text or "\n".join(self._parts).strip()
        out.append(self._ev(TurnEnded, text=reply, status=status, error=error, detail=detail,
                            session_id=str(r.get("session_id") or self._sid), meta=self._meta(r)))
        self._open, self._parts, self._synth = False, [], None
        return out

    def _failure(self, r: Dict[str, Any], text: str) -> Optional[Tuple[ErrorClass, str]]:
        """Error of a result, whatever its subtype says: Claude reports API errors and usage limits as `success`."""
        errors = [str(x) for x in (r.get("errors") or [])]
        blob = " ".join([text] + errors)
        synth = self._synth
        bad = (r.get("is_error") is True or str(r.get("subtype", "")).startswith("error")
               or r.get("terminal_reason") in ("api_error", "prompt_too_long")
               or (synth is not None and (bool(synth) or bool(_LIMIT.search(blob)))))
        if not bad:
            return None
        detail = (text or " ".join(errors) or str(r.get("subtype", "")))[:2000]
        if _LOST.search(blob) or r.get("terminal_reason") == "prompt_too_long":
            return ErrorClass.SESSION_LOST, detail
        if synth == "billing_error":
            return ErrorClass.BILLING, detail
        if synth in ("rate_limit", "server_error") or _LIMIT.search(blob) or _NETWORK.search(blob):
            return ErrorClass.TRANSIENT, detail
        if synth in ("authentication_failed", "model_not_found", "invalid_request"):
            return ErrorClass.OTHER, detail
        return classify_error(blob), detail

    @staticmethod
    def _meta(r: Dict[str, Any]) -> Dict[str, Any]:
        meta = {k: r[k] for k in ("total_cost_usd", "total_credits", "num_turns", "duration_ms", "subtype",
                                  "terminal_reason", "stop_reason") if k in r}
        usage = r.get("usage")
        if isinstance(usage, dict):
            meta["usage"] = {k: v for k, v in usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if isinstance(r.get("modelUsage"), dict) and r["modelUsage"]:
            meta["models"] = sorted(r["modelUsage"])
        return meta

    # ------------------------------------------------------------------------------------------------- jobs

    def jobs(self) -> List[Job]:
        self._read()
        alive = self._alive()
        return [replace(j) for j in self._tasks.values() if alive or j.state != JobState.RUNNING]

    def _poll_jobs(self) -> None:
        """Only Qwen: it writes no job event."""

    # ---------------------------------------------------------------------------------- terminal, history

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        return self._args(spec, session.session_id, True, True)

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        if not self.history:
            return []
        paths = glob.glob(os.path.join(os.path.expanduser(self.history), "*", f"{session.session_id}.jsonl"))
        said: List[Said] = []
        try:
            with open(paths[0], encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (IndexError, OSError):
            return said
        for line in lines:
            try:
                r = json.loads(line)
                msg, at = r["message"], _epoch(r.get("timestamp"))
            except (ValueError, KeyError, TypeError):
                continue
            if r.get("type") not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta") or at <= since:
                continue
            text = _text_of(_blocks(msg.get("content") if isinstance(msg, dict) else ""))
            if text and not text.startswith(("<task-notification>", "<command-", "<local-command")):
                said.append(Said(USER if r["type"] == "user" else self._agent, text, at))
        return said

    def catalog(self) -> List[ModelInfo]:
        return [ModelInfo(m, backend=self.backend, model=m, label=m, efforts=list(self.efforts)) for m in self.models]

    # ------------------------------------------------------------------------------------------- small files

    @staticmethod
    def _read_json(path: str) -> Dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_json(path: str, data: Dict[str, Any]) -> None:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)


class QoderAdapter(ClaudeAdapter):
    """Qoder: no uuid round trip (matched by text), a message is taken in only after the running turn, no interrupt."""

    backend = "qoder"
    exe = "qodercli"
    caps = Caps(sends_while_writing=False, reconnects=True, job_record=True, wakes_itself=True, receipts=False,
                shares_terminal=False, compacts=True, stops_jobs=False)
    roundtrip = False
    reports_init_model = False
    history = "~/.qoder/projects"
    efforts = ["none", "low", "medium", "high", "xhigh", "max"]
    PERMISSION_MODES = {"default": "default", "plan": "plan", "acceptEdits": "accept_edits",
                        "bypassPermissions": "bypass_permissions", "dontAsk": "dont_ask", "auto": "auto"}
    NO_BACKGROUND = ("Bash(nohup:*)", "Bash(setsid:*)")

    def _args(self, spec: AgentSpec, sid: str, resume: bool, terminal: bool) -> List[str]:
        o = spec.choice.options
        cmd = [str(o.get("executable") or self.exe)]
        if not terminal:
            cmd += ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--replay-user-messages"]
        cmd += ["--resume" if resume else "--session-id", sid]
        if spec.choice.model:
            cmd += ["--model", spec.choice.model]
        if spec.choice.effort:
            cmd += ["--reasoning-effort", spec.choice.effort]
        if spec.system_prompt:
            cmd += ["--system-prompt" if o.get("system_prompt_mode") == "replace" else "--append-system-prompt",
                    spec.system_prompt]
        if o.get("permission_mode"):
            mode = str(o["permission_mode"])
            cmd += ["--permission-mode", self.PERMISSION_MODES.get(mode, mode)]
        for flag, tools in (("--allowed-tools", self._list(o.get("allowed_tools"))),
                            ("--disallowed-tools", list(self.NO_BACKGROUND) + self._list(o.get("disallowed_tools")))):
            for t in tools:
                cmd += [flag, t]
        for d in self._list(o.get("add_dirs")):
            cmd += ["--add-dir", os.path.expanduser(d)]
        if o.get("mcp_config"):
            cmd += ["--mcp-config", os.path.expanduser(str(o["mcp_config"]))]
        return cmd + self._list(o.get("extra_args"))

    def _env(self, spec: AgentSpec) -> Dict[str, str]:
        return {**os.environ, **spec.choice.env}  # the timeout variables do nothing here: the worker passes `timeout`

    def _line(self, message_id: str, text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": text}})

    def catalog(self) -> List[ModelInfo]:
        names = self._list_models()
        return [ModelInfo(n, backend=self.backend, model=n, label=n, efforts=list(self.efforts)) for n in names]

    def _list_models(self) -> List[str]:
        try:
            out = subprocess.run([self.exe, "--list-models"], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [x.strip() for x in out.splitlines() if x.strip() and " " not in x.strip()]


class QwenAdapter(ClaudeAdapter):
    """Qwen Code: no echo, so a turn starts with its first output; no wakeup and no job events: its jobs are read
    from the status files it keeps."""

    backend = "qwen"
    exe = "qwen"
    caps = Caps(sends_while_writing=False, reconnects=True, job_record=True, wakes_itself=False, receipts=False,
                shares_terminal=False, compacts=True, stops_jobs=False)
    roundtrip = False
    echoes = False
    stream_jobs = False
    compact_text = "/compress"
    history = ""
    efforts: List[str] = []  # not settable from outside (MOD-2: hidden)
    models: List[str] = []
    APPROVAL_MODES = {"default": "default", "acceptEdits": "auto-edit", "plan": "plan", "auto": "auto",
                      "bypassPermissions": "yolo"}
    NO_BACKGROUND = ("run_shell_command(nohup)", "run_shell_command(setsid)")  # CHECK: the syntax was not tried live

    def __init__(self, clock: Any):
        super().__init__(clock)
        self.config_home = os.path.expanduser("~/.qwen")
        self._pending: Dict[str, str] = {}  # tool_use_id -> command of a background shell call

    def _args(self, spec: AgentSpec, sid: str, resume: bool, terminal: bool) -> List[str]:
        o = spec.choice.options
        cmd = [str(o.get("executable") or self.exe)]
        if not terminal:
            cmd += ["--input-format", "stream-json", "--output-format", "stream-json"]
        cmd += ["--resume" if resume else "--session-id", sid]
        if spec.choice.model:
            cmd += ["--model", spec.choice.model]
        if spec.system_prompt:
            cmd += ["--system-prompt" if o.get("system_prompt_mode") == "replace" else "--append-system-prompt",
                    spec.system_prompt]
        mode = o.get("approval_mode") or o.get("permission_mode")
        if mode:
            cmd += ["--approval-mode", self.APPROVAL_MODES.get(str(mode), str(mode))]
        for flag, tools in (("--allowed-tools", self._list(o.get("allowed_tools"))),
                            ("--exclude-tools", list(self.NO_BACKGROUND) + self._list(o.get("disallowed_tools")))):
            for t in tools:
                cmd += [flag, t]  # one flag per tool: a list flag would swallow what follows
        for d in self._list(o.get("add_dirs")):
            cmd += ["--include-directories", os.path.expanduser(d)]
        if o.get("mcp_config"):
            cmd += ["--mcp-config", os.path.expanduser(str(o["mcp_config"]))]
        return cmd + self._list(o.get("extra_args"))

    def _env(self, spec: AgentSpec) -> Dict[str, str]:
        return {**os.environ, **spec.choice.env}

    def _line(self, message_id: str, text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def _tool_use(self, use: str, block: Dict[str, Any]) -> None:
        i = block.get("input") if isinstance(block.get("input"), dict) else {}
        if block.get("name") == "run_shell_command" and i.get("is_background"):
            self._pending[use] = str(i.get("command", ""))

    def _tool_result(self, block: Dict[str, Any]) -> List[AgentEvent]:
        m = re.search(r"Background shell (bg_\w+) started", str(block.get("content", "")))
        use = str(block.get("tool_use_id", ""))
        if not m or use not in self._pending:
            return []
        jid = job_id(self._agent, m.group(1))
        job = Job(jid, self._agent, self._pending.pop(use).replace("\n", " ")[:200],
                  started=self._stamps.get(use) or self.clock.now())
        self._qseen.add(jid)
        self._tasks[jid] = job
        return [self._ev(JobStarted, job=job)]

    def _status_files(self) -> List[str]:
        return glob.glob(os.path.join(self.config_home, "tmp", "*", "background-shells", self._sid or "-", "*.status"))

    def _scan(self) -> List[Job]:
        """Every job of the session as its status file says; a job recorded as running whose process is gone is
        left out (nobody can say how it ended: the supervisor marks it lost)."""
        jobs = []
        for path in self._status_files():
            s = self._read_json(path)
            if not s.get("id"):
                continue
            state, code = str(s.get("status", "")), s.get("exitCode")
            if code is None:
                m = _EXIT_CODE.search(str(s.get("error", "")))
                code = int(m.group(1)) if m else (0 if state == "completed" else None)
            job = Job(job_id(self._agent, s["id"]), self._agent, str(s.get("command", ""))[:200],
                      started=_epoch(s.get("startTime")))
            if state == "running":
                pid = int(s.get("pid") or 0)
                if not (pid and _running(pid, 0)):
                    continue
            else:
                job = replace(job, state=JobState.ENDED, exit_code=code, ended=_epoch(s.get("endTime")))
            jobs.append(job)
        return jobs

    def jobs(self) -> List[Job]:
        self._read()
        return self._scan() if self._sid else []

    def _poll_jobs(self) -> None:
        if not (self._qseen - self._qended):
            return
        for j in self._scan():
            if j.state == JobState.ENDED and j.id in self._qseen and j.id not in self._qended:
                self._qended.add(j.id)
                self._queue.append(self._ev(JobEnded, uid=f"job:{j.id}:ended", job_id=j.id, exit_code=j.exit_code))
