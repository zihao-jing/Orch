"""The `command` backend: any CLI run as one process per turn (docs/redesign.md 3.2).

The adapter stays "open" between turns; each message starts a new process whose output goes to a file. No jobs, no
receipts, no reconnect: an in-flight turn is not picked up again after an Orch restart. stop() ends the process group.
Commands, placeholders and output parsing are those of today's CommandBackend (orch/backends.py).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import uuid
from typing import Any, Dict, Iterator, List, Optional

from ..contracts import (AgentEvent, AgentSpec, Caps, Clock, ErrorClass, ProcRef, ProcessExited, SessionChange,
                         SessionChanged, SessionRef, TurnEnded, TurnStarted, TurnStatus, redact)
from .base import AdapterError, BaseAdapter, classify_error

_TAIL = 2000  # bytes of stderr kept for a failure's detail


def _dig(data: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _fill(part: str, values: Dict[str, str]) -> str:
    """Replace {name} for the known names only; anything else is left as written."""
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), part)


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL))
            lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


class CommandAdapter(BaseAdapter):
    backend = "command"
    caps = Caps()

    def __init__(self, clock: Clock):
        super().__init__(clock)
        self.agent = ""
        self.joined = False
        self.stopping = False
        self.exited = False
        self.pending_start: Optional[SessionChange] = None  # SessionChanged to report at the next events()
        self.sid = ""  # the session id this adapter runs its turns in
        self.new_session = False  # the first turn of a new session
        self.turns = 0
        self.turn: Optional[Dict[str, Any]] = None  # the running turn: message id, process, files

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        if "command" not in (spec.choice.options or {}):
            raise AdapterError("command backend needs options.command", ErrorClass.OTHER)
        self.agent, self.spec, self.joined = spec.agent, spec, True
        self.stopping, self.exited, self.turn, self.turns = False, False, None, 0
        self.new_session = session is None
        self.sid = session.session_id if session else str(uuid.uuid4())
        self.pending_start = SessionChange.RESUMED if session else SessionChange.NEW
        os.makedirs(os.path.join(spec.run_dir, "cli", spec.agent), exist_ok=True)
        return ProcRef(pid=0, started=self.clock.now(), stream=f"command-{self.sid}", read_pos="0")

    def _dir(self) -> str:
        return os.path.join(self.spec.run_dir, "cli", self.agent)

    def send(self, message_id: str, text: str) -> None:
        if not self.joined or self.exited:
            raise AdapterError("the CLI is not running")
        if self.turn is not None:
            raise AdapterError("busy", ErrorClass.OTHER)
        o = self.spec.choice.options or {}
        new = self.new_session and self.turns == 0
        tmpl = o.get("resume_command") if (not new and o.get("resume_command")) else o["command"]
        tmpl = shlex.split(tmpl) if isinstance(tmpl, str) else [str(x) for x in tmpl]
        native = o.get("system_prompt") == "native"
        system = self.spec.system_prompt or ""
        prompt = text
        if not native and new and system:
            prompt = f"<system_instructions>\n{system}\n</system_instructions>\n\n{text}"
        n = self.turns + 1
        base = os.path.join(self._dir(), f"turn-{n}")
        values = {"session_id": self.sid, "cwd": self.spec.cwd, "agent": self.agent, "prompt": prompt,
                  "system_prompt": system}
        for key, body in (("prompt_file", prompt), ("system_prompt_file", system)):
            if "{" + key + "}" in " ".join(tmpl):
                values[key] = self._write(f"{base}.{key}.md", body)
        cmd = [_fill(part, values) for part in tmpl]
        via = o.get("prompt_via", "stdin")
        stdin = self._write(f"{base}.stdin.txt", prompt) if via == "stdin" else None
        out_path, err_path = f"{base}.out", f"{base}.err"
        try:
            with open(stdin or os.devnull, "rb") as fin, open(out_path, "wb") as out, open(err_path, "wb") as err:
                popen = subprocess.Popen(cmd, stdin=fin, stdout=out, stderr=err, cwd=self.spec.cwd or None,
                                         env={**os.environ, **(self.spec.choice.env or {})}, start_new_session=True)
        except OSError as e:
            missing = isinstance(e, FileNotFoundError)
            raise AdapterError(f"executable not found: {cmd[0]}" if missing else str(e),
                               ErrorClass.MISSING_CLI if missing else ErrorClass.TRANSIENT)
        self.turns = n
        self.turn = {"n": n, "mid": message_id, "popen": popen, "out": out_path, "err": err_path,
                     "announced": False}

    def _write(self, path: str, body: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def events(self) -> Iterator[AgentEvent]:
        if not self.joined:
            return
        now = self.clock.now()
        if self.pending_start is not None:
            change, self.pending_start = self.pending_start, None
            yield SessionChanged(self.agent, now, f"command-{self.sid}", "", "start:0",
                                 session_id=self.sid, change=change)
        t = self.turn
        if t is not None and not t["announced"]:
            t["announced"] = True
            yield TurnStarted(self.agent, now, f"turn-{t['n']}", "", f"turn:{t['n']}:start",
                              message_ids=(t["mid"],))
        if t is not None and t["popen"].poll() is not None:
            self.turn = None
            yield from self._finish(t, now)
        if self.stopping and self.turn is None and not self.exited:
            self.exited = True
            yield ProcessExited(self.agent, now, f"command-{self.sid}", "", "exit:stop", asked=True)

    def _finish(self, t: Dict[str, Any], now: float) -> Iterator[AgentEvent]:
        o = self.spec.choice.options or {}
        rc = t["popen"].returncode
        with open(t["out"], encoding="utf-8", errors="replace") as f:
            out = f.read()
        output, sid, status, error, detail = out, self.sid, TurnStatus.OK, None, ""
        if o.get("output_format") == "json":
            try:
                data = json.loads(out)
                output = str(_dig(data, o.get("output_field", "result")) or "")
                sid = str(_dig(data, o["session_field"]) or sid) if o.get("session_field") else sid
            except (ValueError, TypeError):
                status, detail = TurnStatus.ERROR, "could not parse the command's JSON output"
        if o.get("session_regex"):
            m = re.search(o["session_regex"], out)
            sid = m.group(1) if m else sid
        if self.stopping:
            status = TurnStatus.STOPPED
        elif rc != 0 and status == TurnStatus.OK:
            status, detail = TurnStatus.ERROR, _tail(t["err"])
        if status == TurnStatus.ERROR:
            error = classify_error(detail)
        if sid != self.sid:  # a session id the command reports itself (a new one)
            self.sid = sid
            yield SessionChanged(self.agent, now, f"command-{sid}", "", f"session:{sid}", session_id=sid,
                                 change=SessionChange.NEW)
        yield TurnEnded(self.agent, now, f"turn-{t['n']}", "", f"turn:{t['n']}:end", text=output.strip(),
                        status=status, error=error, detail=redact(detail), session_id=sid)

    def stop(self) -> None:
        self.stopping = True
        t = self.turn
        if t is None or t["popen"].poll() is not None:
            return
        pid = t["popen"].pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
            try:
                t["popen"].wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    def detach(self) -> None:
        self.joined = False
