"""Antigravity (`agy`) in stream-json mode: one process that stays open (docs/redesign.md 3.2, docs/probes/agy.md).

Input: one NDJSON line per message on a FIFO that Orch and the process both hold open. Output: NDJSON in a file that
outlives Orch. Messages sent during a turn queue in the CLI; a turn stays open until its async jobs end, and a job's end
is reported inside that turn (JobNoticed, then the result). An unknown conversation id silently starts a new one, so
init is checked against the requested id. The model id may carry the effort (`gemini-3.8-flash-low`); --effort is then
never passed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..contracts import (AgentEvent, AgentSpec, Caps, Clock, ErrorClass, Job, JobEnded, JobNoticed, JobStarted,
                         JobState, ModelReported, ProcRef, ProcessExited, SessionChange, SessionChanged, SessionRef,
                         Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus, redact)
from .base import AdapterError, BaseAdapter, classify_error, job_id

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_TAIL = 2000  # bytes of stderr kept for a failure's detail


def _lstart(pid: int) -> str:
    """The process start time as ps prints it: with the pid, it identifies the process (pid reuse)."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except OSError:
        return ""
    return out.strip()


def _alive(pid: int, lstart: str) -> bool:
    return bool(pid) and bool(lstart) and _lstart(pid) == lstart


def _kill_group(pid: int, popen: Optional[subprocess.Popen]) -> None:
    """End the process group (SIGTERM, then SIGKILL after 5 s), so the CLI's jobs go with it."""
    def send(sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
    send(signal.SIGTERM)
    for _ in range(50):
        if popen is not None and popen.poll() is not None:
            return
        if popen is None and not _lstart(pid):
            return
        time.sleep(0.1)
    send(signal.SIGKILL)
    if popen is not None:
        popen.wait()


def _list(v: Any) -> List[str]:
    if not v:
        return []
    return shlex.split(v) if isinstance(v, str) else [str(x) for x in v]


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL))
            lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _command_line(name: str, info: Dict[str, Any]) -> str:
    params = info.get("parameters") or {}
    return str(params.get("CommandLine") or name)[:160]


class AgyAdapter(BaseAdapter):
    backend = "agy"
    caps = Caps(sends_while_writing=False, reconnects=True, job_record=True, wakes_itself=True, receipts=False)

    def __init__(self, clock: Clock):
        super().__init__(clock)
        self.agent = ""
        self.system = ""
        self.joined = False
        self.stopping = False
        self.exited = False
        self.turn_open = False
        self.conv = ""  # the conversation id init reported
        self.want = ""  # the conversation id asked for at start ("" for a new one)
        self.need_system = False
        self.queue: deque = deque()  # ids sent, not yet taken in (one turn each, in order)
        self.job_map: Dict[str, Job] = {}
        self.pid = 0
        self.lstart = ""
        self.popen: Optional[subprocess.Popen] = None
        self.fifo_in: Optional[int] = None
        self.out_path = ""
        self.err_path = ""
        self.stream = ""
        self.off = 0  # bytes of the output file read so far (complete lines only)

    # ------------------------------------------------------------------ process

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        d = os.path.join(spec.run_dir, "cli", spec.agent)
        os.makedirs(d, exist_ok=True)
        pid, lstart = self._read_pid(d)
        if _alive(pid, lstart):  # left by an earlier start: end it, so one conversation has one process
            _kill_group(pid, None)
        fifo = os.path.join(d, "in.fifo")
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        name = "out-" + uuid.uuid4().hex[:8]
        out_path, err_path = os.path.join(d, name + ".jsonl"), os.path.join(d, name + ".err")
        choice = spec.choice
        cmd = [str(choice.options.get("executable") or "agy")] + self._args(choice, session)
        env = {**os.environ, **(choice.env or {})}
        fin = os.open(fifo, os.O_RDWR)  # never EOF: the CLI keeps reading while Orch holds it
        try:
            with open(out_path, "ab") as out, open(err_path, "ab") as err:
                popen = subprocess.Popen(cmd, stdin=fin, stdout=out, stderr=err, cwd=spec.cwd or None, env=env,
                                         start_new_session=True)
        except OSError as e:
            raise AdapterError(f"executable not found: {cmd[0]}" if isinstance(e, FileNotFoundError) else str(e),
                               ErrorClass.MISSING_CLI if isinstance(e, FileNotFoundError) else ErrorClass.TRANSIENT)
        finally:
            os.close(fin)
        self.fifo_in = os.open(fifo, os.O_RDWR)
        self.popen, self.pid, self.lstart = popen, popen.pid, _lstart(popen.pid)
        with open(os.path.join(d, "pid"), "w") as f:
            f.write(f"{self.pid}\n{self.lstart}\n")
        self.agent, self.system, self.joined = spec.agent, spec.system_prompt, True
        self.stopping, self.exited, self.turn_open, self.conv = False, False, False, ""
        self.want = session.session_id if session else ""
        self.need_system = bool(spec.system_prompt) and session is None
        self.queue.clear()
        self.job_map = {}
        self.out_path, self.err_path, self.stream, self.off = out_path, err_path, name, 0
        return ProcRef(pid=self.pid, started=self.clock.now(), endpoint=fifo, output_file=out_path, stream=name,
                       read_pos="0")

    def reconnect(self, proc: ProcRef) -> bool:
        d = os.path.dirname(proc.endpoint)
        pid, lstart = self._read_pid(d)
        self.agent = os.path.basename(os.path.dirname(d))
        self.joined, self.popen, self.pid, self.lstart = True, None, proc.pid, lstart
        self.out_path, self.stream, self.err_path = proc.output_file, proc.stream, proc.output_file[:-6] + ".err"
        self.off = int(proc.read_pos or 0)
        self.stopping, self.exited, self.turn_open, self.conv, self.want = False, False, False, "", ""
        self.need_system, self.queue, self.job_map = False, deque(), {}
        alive = pid == proc.pid and _alive(pid, lstart)
        self.fifo_in = os.open(proc.endpoint, os.O_RDWR) if alive else None
        return alive

    def _read_pid(self, d: str) -> Tuple[int, str]:
        try:
            with open(os.path.join(d, "pid")) as f:
                pid, lstart = f.read().split("\n", 1)
            return int(pid), lstart.strip()
        except (OSError, ValueError):
            return 0, ""

    def _args(self, choice: Any, session: Optional[SessionRef]) -> List[str]:
        o = choice.options or {}
        args = ["--input-format", "stream-json", "--output-format", "stream-json"]
        if session and session.session_id:
            args += ["--conversation", session.session_id]
        model, effort = choice.model or "", choice.effort or ""
        base, _, suffix = model.rpartition("-")
        if suffix in EFFORTS:  # the id carries its effort: never with --effort
            if effort in EFFORTS:
                model = f"{base}-{effort}"
            args += ["--model", model]
        else:
            if model:
                args += ["--model", model]
            if effort:
                args += ["--effort", effort]
        if o.get("mode"):
            args += ["--mode", str(o["mode"])]
        if o.get("sandbox"):
            args.append("--sandbox")
        if o.get("dangerously_skip_permissions"):  # only when the agent's own configuration sets it
            args.append("--dangerously-skip-permissions")
        for d in _list(o.get("add_dirs")):
            args += ["--add-dir", os.path.expanduser(d)]
        return args + _list(o.get("extra_args"))

    def _alive_now(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is None
        return _alive(self.pid, self.lstart)

    # ------------------------------------------------------------------ input

    def send(self, message_id: str, text: str) -> None:
        if not self.joined or self.fifo_in is None or not self._alive_now():
            raise AdapterError("the CLI is not running")
        if self.need_system:  # no system-prompt flag: it goes in front of the first message
            text = f"<system_instructions>\n{self.system}\n</system_instructions>\n\n{text}"
        line = json.dumps({"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        try:
            os.write(self.fifo_in, (line + "\n").encode("utf-8"))
        except OSError as e:
            raise AdapterError(str(e))
        self.need_system = False
        self.queue.append(message_id)

    def stop(self) -> None:
        self.stopping = True
        if self.pid:
            _kill_group(self.pid, self.popen)
        self._close_input()

    def detach(self) -> None:
        self.joined = False
        self._close_input()

    def _close_input(self) -> None:
        if self.fifo_in is not None:
            os.close(self.fifo_in)
            self.fifo_in = None

    # ------------------------------------------------------------------ output

    def events(self) -> Iterator[AgentEvent]:
        if not self.joined or not self.out_path:
            return
        alive = self._alive_now()  # checked before reading: whatever it wrote is in the file
        try:
            with open(self.out_path, "rb") as f:
                f.seek(self.off)
                data = f.read()
        except OSError:
            data = b""
        pos = self.off
        for raw in data.split(b"\n")[:-1]:  # complete lines only
            start, pos = pos, pos + len(raw) + 1
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                rec = None
            self.off = pos
            parts = self._parse(rec) if isinstance(rec, dict) else []
            for i, (kind, kw) in enumerate(parts):
                last = i == len(parts) - 1
                yield kind(agent=self.agent, at=self.clock.now(), stream=self.stream,
                           pos=str(pos if last else start), uid=f"{start}:{i}", **kw)
        if not alive and self.off >= self._size() and not self.exited:
            self.exited = True
            yield from self._exit_events()

    def _size(self) -> int:
        try:
            return os.path.getsize(self.out_path)
        except OSError:
            return 0

    def _exit_events(self) -> Iterator[AgentEvent]:
        rc = self.popen.returncode if self.popen is not None else None
        detail = redact(_tail(self.err_path))
        now, uid = self.clock.now(), f"exit:{self.pid}"
        failed = self.turn_open and not self.stopping
        if self.turn_open:
            self.turn_open = False
            if self.stopping:
                yield TurnEnded(self.agent, now, self.stream, "", uid + ":turn", status=TurnStatus.STOPPED)
            else:
                yield TurnEnded(self.agent, now, self.stream, "", uid + ":turn", status=TurnStatus.ERROR,
                                error=classify_error(detail), detail=detail)
        yield ProcessExited(self.agent, now, self.stream, "", uid, exit_code=rc, detail=detail,
                            asked=self.stopping, reported=failed)

    def _parse(self, rec: Dict[str, Any]) -> List[Tuple[type, Dict[str, Any]]]:
        ev = rec.get("event")
        now = self.clock.now()
        if ev == "init":
            init = rec.get("init") or {}
            conv = str(rec.get("conversation_id") or init.get("conversation_id") or "")
            out: List[Tuple[type, Dict[str, Any]]] = []
            if init.get("model"):
                out.append((ModelReported, {"model": str(init["model"])}))
            if conv:
                self.conv = conv
                if self.want and conv != self.want:  # an unknown id silently starts a new conversation
                    out.append((SessionChanged, {"session_id": conv, "change": SessionChange.LOST,
                                                 "detail": f"conversation {self.want} not found"}))
                else:
                    change = SessionChange.RESUMED if self.want else SessionChange.NEW
                    out.append((SessionChanged, {"session_id": conv, "change": change}))
            return out
        if ev == "step_update":
            return self._parse_step(rec.get("step_update") or {}, now)
        if ev == "result":
            r = rec.get("result") or {}
            conv = str(r.get("conversation_id") or self.conv)
            self.turn_open = False
            meta = {k: r[k] for k in ("usage", "num_turns", "duration_seconds") if k in r}
            text = str(r.get("response") or "")
            if r.get("status") == "SUCCESS":
                return [(TurnEnded, {"text": text, "status": TurnStatus.OK, "session_id": conv, "meta": meta})]
            err = str(r.get("error") or "")
            if self.stopping or r.get("finished_reason") == "interrupted" or "context canceled" in err:
                return [(TurnEnded, {"text": text, "status": TurnStatus.STOPPED, "session_id": conv, "meta": meta})]
            return [(TurnEnded, {"text": text, "status": TurnStatus.ERROR, "error": classify_error(err),
                                 "detail": redact(err), "session_id": conv, "meta": meta})]
        return []

    def _parse_step(self, s: Dict[str, Any], now: float) -> List[Tuple[type, Dict[str, Any]]]:
        kind, state, idx = s.get("step_type"), s.get("state"), str(s.get("step_index", ""))
        if kind == "user_input" and state == "DONE":  # the message is taken in: a turn starts
            self.turn_open = True
            mid = self.queue.popleft() if self.queue else ""
            return [(TurnStarted, {"message_ids": (mid,) if mid else ()})]
        if kind == "agent_response" and s.get("text_delta"):
            return [(Text, {"text": str(s["text_delta"])})]
        if kind == "tool":
            name, info = str(s.get("tool_name", "")), s.get("tool_info") or {}
            if state == "ACTIVE":
                args = info.get("parameters")
                call = name + "\n" + json.dumps(args, ensure_ascii=False, indent=2) if args is not None else ""
                return [(ToolStart, {"tool_id": idx, "label": _command_line(name, info), "call": call})]
            if state == "DONE":
                out: List[Tuple[type, Dict[str, Any]]] = [(ToolEnd, {"tool_id": idx})]
                m = re.search(re.escape(self.conv) + r"/task-\d+", str(info.get("output") or "")) if self.conv else None
                if name == "run_command" and m:  # an async command: its task id is in the result
                    job = Job(id=job_id(self.agent, m.group(0)), agent=self.agent,
                              label=_command_line(name, info), started=now,
                              command=str((info.get("parameters") or {}).get("CommandLine") or ""))
                    self.job_map[job.id] = job
                    out.insert(0, (JobStarted, {"job": job}))
                return out
            return []
        if kind == "system_message" and state == "DONE":  # a job ended; told to the running turn
            running = sorted((j for j in self.job_map.values() if j.state == JobState.RUNNING), key=lambda j: j.started)
            if running:
                job = running[0]
                self.job_map[job.id] = replace(job, state=JobState.ENDED, ended=now)
                return [(JobEnded, {"job_id": job.id}), (JobNoticed, {"job_id": job.id})]
        return []

    # ------------------------------------------------------------------ jobs

    def jobs(self) -> List[Job]:  # type: ignore[override]
        return [replace(j) for j in self.job_map.values()]
