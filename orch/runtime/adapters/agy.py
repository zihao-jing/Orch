"""Antigravity (`agy`) in stream-json mode: one process that stays open (docs/redesign.md 3.2, docs/probes/agy.md).

Input: one NDJSON line per message on a FIFO that Orch and the process both hold open. Output: NDJSON in a file that
outlives Orch. Messages sent during a turn queue in the CLI; a turn stays open until its async jobs end, and a job's end
is reported inside that turn (JobNoticed, then the result). An unknown conversation id silently starts a new one, so
init is checked against the requested id. The model id may carry the effort (`gemini-3.8-flash-low`); --effort is then
never passed.

Jobs (probe P6, agy 1.5.19): the stream does not say that a command went to the background; its tool step stays ACTIVE
until the command ends. agy's own transcript of the conversation (`brain/<conv>/.system_generated/logs/
transcript_full.jsonl`) does: a GENERIC step "running as a background task with task id: <conv>/task-N" (same
step_index as the tool step), and a SYSTEM_MESSAGE from `sender=<conv>/task-N` when it ends. The adapter reads it next to
the stream. Older agy (1.5.8) put the task id in the tool output and ended jobs with a bare system_message; both still
work. A job runs as `<shell> -c <command>`, a child of agy leading its own process group: stop_job ends that group.
"""
from __future__ import annotations

import calendar
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
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ..contracts import (USER, AgentEvent, AgentSpec, Caps, Clock, ErrorClass, Job, JobEnded, JobNoticed, JobStarted,
                         JobState, ModelInfo, ModelReported, ProcRef, ProcessExited, Said, SessionChange,
                         SessionChanged, SessionRef, Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus,
                         redact)
from .base import AdapterError, BaseAdapter, classify_error, job_id

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_TAIL = 2000  # bytes of stderr kept for a failure's detail
COMPACT = "\0compact"  # the queue entry of a /compact: its turn is no turn
_BG_TASK = re.compile(r"background task with task id: (\S+/task-\d+)")
_TASK_END = re.compile(r"sender=(\S+/task-\d+)\b[^\n]*? with result:")  # "finished with", "was canceled with"
_EXIT = re.compile(r"exited with code (-?\d+)")
_DESCR = re.compile(r"^Task Description: (.*)$", re.M)
_REQUEST = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.S)
_SYSTEM = re.compile(r"\A<system_instructions>.*?</system_instructions>\n*", re.S)


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


def _epoch_lstart(lstart: str) -> float:
    """ps's lstart (local time, C locale) as epoch seconds; 0 if unreadable."""
    try:
        return time.mktime(time.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return 0.0


def _epoch(stamp: Any) -> float:
    """A transcript `created_at` ("2026-11-28T10:56:56Z", UTC) as epoch seconds; 0 if unreadable."""
    try:
        return float(calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return 0.0


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


def _exit_code(text: str) -> Optional[int]:
    m = _EXIT.search(text or "")
    return int(m.group(1)) if m else None


def _ps() -> List[Tuple[int, int, int, str]]:
    """Every process as (pid, ppid, pgid, command)."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,command="], capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except OSError:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].lstrip("-").isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


class AgyAdapter(BaseAdapter):
    backend = "agy"
    caps = Caps(sends_while_writing=False, reconnects=True, job_record=True, wakes_itself=True, receipts=False,
                shares_terminal=False, compacts=True, stops_jobs=True)
    brain = "~/.gemini/antigravity-cli/brain"  # agy keeps each conversation's files under brain/<conversation id>/

    def __init__(self, clock: Clock):
        super().__init__(clock)
        self.agent = ""
        self.system = ""
        self.joined = False
        self.stopping = False
        self.exited = False
        self.turn_open = False
        self.compacting = False  # a /compact was taken in: its result is SessionChanged(COMPACTED), not a turn
        self.conv = ""  # the conversation id init reported
        self.want = ""  # the conversation id asked for at start ("" for a new one)
        self.need_system = False
        self.queue: deque = deque()  # ids sent, not yet taken in (one turn each, in order)
        self.taken: Set[str] = set()  # ids taken in by this adapter's process
        self.job_map: Dict[str, Job] = {}
        self.step_job: Dict[str, str] = {}  # tool step_index -> job id, for jobs agy put in the background
        self.tool_cmd: Dict[str, str] = {}  # tool step_index -> its CommandLine
        self.tool_done: Dict[str, str] = {}  # tool step_index -> its output, once the stream said DONE
        self.pid = 0
        self.lstart = ""
        self.popen: Optional[subprocess.Popen] = None
        self.fifo_in: Optional[int] = None
        self.out_path = ""
        self.err_path = ""
        self.stream = ""
        self.off = 0  # bytes of the output file read so far (complete lines only)
        self.tr_off = 0  # bytes of the transcript read so far
        self.tr_since = 0.0  # transcript steps from before this process are not its jobs
        self.exe = "agy"

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
        self.exe = str(choice.options.get("executable") or "agy")
        cmd = [self.exe] + self._args(choice, session)
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
        self.stopping, self.exited, self.turn_open, self.compacting, self.conv = False, False, False, False, ""
        self.want = session.session_id if session else ""
        self.need_system = bool(spec.system_prompt) and session is None
        self._forget()
        self.out_path, self.err_path, self.stream, self.off = out_path, err_path, name, 0
        return ProcRef(pid=self.pid, started=self.clock.now(), endpoint=fifo, output_file=out_path, stream=name,
                       read_pos="0")

    def reconnect(self, proc: ProcRef) -> bool:
        d = os.path.dirname(proc.endpoint)
        pid, lstart = self._read_pid(d)
        self.agent = os.path.basename(d)  # run_dir/cli/<agent>/in.fifo
        self.joined, self.popen, self.pid, self.lstart = True, None, proc.pid, lstart
        self.out_path, self.stream, self.err_path = proc.output_file, proc.stream, proc.output_file[:-6] + ".err"
        self.off = int(proc.read_pos or 0)
        self.stopping, self.exited, self.turn_open, self.compacting, self.want = False, False, False, False, ""
        self.need_system, self.queue = False, deque()
        self._forget()
        self.conv = self._init_conv()  # the jobs of this process are read again from the transcript
        alive = pid == proc.pid and _alive(pid, lstart)
        self.fifo_in = os.open(proc.endpoint, os.O_RDWR) if alive else None
        return alive

    def _forget(self) -> None:
        self.queue.clear()
        self.taken, self.job_map, self.step_job, self.tool_cmd, self.tool_done = set(), {}, {}, {}, {}
        self.tr_off, self.tr_since = 0, (_epoch_lstart(self.lstart) - 2 if self.lstart else 0.0)

    def _init_conv(self) -> str:
        """The conversation id of the output file's init line."""
        try:
            with open(self.out_path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(20), f):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("event") == "init":
                        return str(rec.get("conversation_id") or (rec.get("init") or {}).get("conversation_id") or "")
        except OSError:
            pass
        return ""

    def _read_pid(self, d: str) -> Tuple[int, str]:
        try:
            with open(os.path.join(d, "pid")) as f:
                pid, lstart = f.read().split("\n", 1)
            return int(pid), lstart.strip()
        except (OSError, ValueError):
            return 0, ""

    def _args(self, choice: Any, session: Optional[SessionRef], terminal: bool = False) -> List[str]:
        o = choice.options or {}
        args = [] if terminal else ["--input-format", "stream-json", "--output-format", "stream-json"]
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
        if self.need_system:  # no system-prompt flag: it goes in front of the first message
            text = f"<system_instructions>\n{self.system}\n</system_instructions>\n\n{text}"
        self._write(text)
        self.need_system = False
        self.queue.append(message_id)

    def _write(self, text: str) -> None:
        if not self.joined or self.fifo_in is None or not self._alive_now():
            raise AdapterError("the CLI is not running")
        line = json.dumps({"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        try:
            os.write(self.fifo_in, (line + "\n").encode("utf-8"))
        except OSError as e:
            raise AdapterError(str(e))

    def arrived(self, message_id: str) -> Optional[bool]:
        """Taken in, or waiting in the CLI's own queue while it runs: True. In the queue of a process that died: False.
        A message of an earlier process (after a reconnect): cannot tell."""
        if message_id in self.taken:
            return True
        if message_id in self.queue:
            return self._alive_now()
        return None

    def compact(self) -> bool:
        """`/compact` as a message (probe P6): agy writes a checkpoint and answers with an empty result."""
        if self.need_system or self.compacting or self.turn_open or self.queue:
            return False  # nothing said yet, or not idle
        try:
            self._write("/compact")
        except AdapterError:
            return False
        self.queue.append(COMPACT)
        return True

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
            if isinstance(rec, dict) and self._needs_transcript(rec):
                yield from self._transcript_events()  # a job's start and end before the step or result that follows
            self.off = pos
            parts = self._parse(rec) if isinstance(rec, dict) else []
            for i, (kind, kw) in enumerate(parts):
                last = i == len(parts) - 1
                yield kind(agent=self.agent, at=self.clock.now(), stream=self.stream,
                           pos=str(pos if last else start), uid=f"{start}:{i}", **kw)
        yield from self._transcript_events()
        if not alive and self.off >= self._size() and not self.exited:
            self.exited = True
            yield from self._exit_events()

    @staticmethod
    def _needs_transcript(rec: Dict[str, Any]) -> bool:
        if rec.get("event") == "result":
            return True
        s = rec.get("step_update") or {}
        return rec.get("event") == "step_update" and s.get("state") == "DONE" and s.get("step_type") in (
            "tool", "system_message")

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
            if self.compacting:
                self.compacting = False
                return [(SessionChanged, {"session_id": conv, "change": SessionChange.COMPACTED})]
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
            mid = self.queue.popleft() if self.queue else ""
            if mid == COMPACT:
                self.compacting = True
                return []
            self.turn_open = True
            if mid:
                self.taken.add(mid)
            return [(TurnStarted, {"message_ids": (mid,) if mid else ()})]
        if self.compacting:
            return []
        if kind == "agent_response" and s.get("text_delta"):
            return [(Text, {"text": str(s["text_delta"])})]
        if kind == "tool":
            name, info = str(s.get("tool_name", "")), s.get("tool_info") or {}
            if state == "ACTIVE":
                args = info.get("parameters")
                call = name + "\n" + json.dumps(args, ensure_ascii=False, indent=2) if args is not None else ""
                if name == "run_command":
                    self.tool_cmd[idx] = str((info.get("parameters") or {}).get("CommandLine") or "")
                if idx in self.step_job:  # the transcript was read first: it is a job already, no tool
                    return []
                return [(ToolStart, {"tool_id": idx, "label": _command_line(name, info), "call": call})]
            if state == "DONE":
                output = str(info.get("output") or "")
                self.tool_done[idx] = output
                jid = self.step_job.get(idx)
                if jid:  # a background job's step: it ends with its command (its ToolEnd came at its start)
                    return self._job_end(jid, _exit_code(output), now)
                out: List[Tuple[type, Dict[str, Any]]] = [(ToolEnd, {"tool_id": idx})]
                m = re.search(re.escape(self.conv) + r"/task-\d+", output) if self.conv else None
                if name == "run_command" and m:  # agy 1.5.8: the task id is in the tool's result
                    job = self._job(m.group(0), _command_line(name, info), now,
                                    str((info.get("parameters") or {}).get("CommandLine") or ""))
                    out.insert(0, (JobStarted, {"job": job}))
                return out
            return []
        if kind == "system_message" and state == "DONE" and not self._transcript_path():
            # agy 1.5.8 without a transcript: a job's end, told to the running turn; the oldest running one
            running = sorted((j for j in self.job_map.values() if j.state == JobState.RUNNING), key=lambda j: j.started)
            if running:
                return self._job_end(running[0].id, None, now)
        return []

    # ------------------------------------------------------------------ jobs

    def _job(self, task: str, label: str, now: float, command: str) -> Job:
        job = Job(id=job_id(self.agent, task), agent=self.agent, label=label, started=now, command=command)
        self.job_map[job.id] = job
        return job

    def _job_end(self, jid: str, code: Optional[int], now: float) -> List[Tuple[type, Dict[str, Any]]]:
        job = self.job_map.get(jid)
        if job is None or job.state != JobState.RUNNING:
            return []
        self.job_map[jid] = replace(job, state=JobState.ENDED, ended=now, exit_code=code)
        # agy keeps the turn open for its jobs and tells it the end (the store ignores JobNoticed with no turn open)
        return [(JobEnded, {"job_id": jid, "exit_code": code}), (JobNoticed, {"job_id": jid})]

    def _transcript_path(self, conv: str = "", full: bool = True) -> str:
        conv = conv or self.conv
        if not conv:
            return ""
        path = os.path.join(os.path.expanduser(self.brain), conv, ".system_generated", "logs",
                            "transcript_full.jsonl" if full else "transcript.jsonl")
        return path if os.path.exists(path) else ""

    def _transcript_events(self) -> Iterator[AgentEvent]:
        """New steps of agy's transcript that start or end a job of this process."""
        path = self._transcript_path()
        if not path or not self.agent:
            return
        try:
            with open(path, "rb") as f:
                f.seek(self.tr_off)
                data = f.read()
        except OSError:
            return
        for raw in data.split(b"\n")[:-1]:
            self.tr_off += len(raw) + 1
            try:
                step = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(step, dict) or _epoch(step.get("created_at")) < self.tr_since:
                continue
            idx, content = str(step.get("step_index", "")), str(step.get("content") or "")
            parts: List[Tuple[type, Dict[str, Any]]] = []
            now = self.clock.now()
            started = _BG_TASK.search(content) if step.get("type") == "GENERIC" else None
            ended = _TASK_END.search(content) if step.get("type") == "SYSTEM_MESSAGE" else None
            if started and job_id(self.agent, started.group(1)) not in self.job_map:
                descr = _DESCR.search(content)
                command = self.tool_cmd.get(idx) or (descr.group(1).strip() if descr else "")
                job = self._job(started.group(1), (command or "run_command")[:160], now, command)
                self.step_job[idx] = job.id
                parts = [(JobStarted, {"job": job}), (ToolEnd, {"tool_id": idx})]
                if idx in self.tool_done:  # the stream already said the command ended
                    parts += self._job_end(job.id, _exit_code(self.tool_done[idx]), now)
            elif ended:
                parts = self._job_end(job_id(self.agent, ended.group(1)), _exit_code(content), now)
            for i, (kind, kw) in enumerate(parts):
                yield kind(agent=self.agent, at=now, stream="tr-" + self.conv, pos="", uid=f"{idx}:{i}", **kw)

    def jobs(self) -> List[Job]:  # type: ignore[override]
        return [replace(j) for j in self.job_map.values()]

    def stop_job(self, job_id_: str) -> bool:
        """End the job's shell (`<shell> -c <command>`, a child of agy leading its own group); agy tells the turn."""
        job = self.job_map.get(job_id_)
        if job is None or job.state != JobState.RUNNING or not job.command or not self._alive_now():
            return False
        shells = [r for r in _ps() if r[1] == self.pid and r[0] == r[2] and r[3].endswith(" -c " + job.command)]
        if len(shells) != 1:  # none, or two alike: which one is not known
            return False
        try:
            os.killpg(shells[0][0], signal.SIGTERM)
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------ terminal, history, models

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        """Interactive agy on the conversation; Orch's process closes first (one process per conversation)."""
        return [str(spec.choice.options.get("executable") or "agy")] + self._args(spec.choice, session, True)

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        """The user's lines and the agent's replies after `since`, from agy's transcript (seconds only)."""
        path = self._transcript_path(session.session_id, full=False)
        said: List[Said] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return said
        for line in lines:
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if not isinstance(step, dict):
                continue
            at, content = _epoch(step.get("created_at")), str(step.get("content") or "")
            if at + 1 <= since or not content.strip():
                continue
            if step.get("type") == "USER_INPUT" and step.get("source") == "USER_EXPLICIT":
                m = _REQUEST.search(content)
                text = _SYSTEM.sub("", m.group(1) if m else content).strip()
                if text and text != "/compact":
                    said.append(Said(USER, text, at))
            elif step.get("type") == "PLANNER_RESPONSE" and step.get("source") == "MODEL":
                said.append(Said(self.agent, content.strip(), at))
        return said

    def catalog(self) -> List[ModelInfo]:
        """`agy models`: one "<id>\\t<name>" per line after a "Fetching..." line. The id carries the effort."""
        try:
            out = subprocess.run([self.exe, "models"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return models_from(out)


def models_from(text: str) -> List[ModelInfo]:
    out = []
    for line in text.splitlines():
        mid, tab, label = line.partition("\t")
        mid = mid.strip()
        if not tab or not mid or " " in mid:
            continue
        out.append(ModelInfo(mid, backend="agy", model=mid, label=label.strip() or mid))
    return out
