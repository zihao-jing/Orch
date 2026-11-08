"""Agent backends: how one turn of one agent is executed.

Every backend receives a :class:`TurnRequest` and returns a :class:`TurnResult`.
Sessions are continued by passing back the ``session_id`` a previous turn returned.

Built-in backends
-----------------
claude   Claude Code CLI (``claude -p``), sessions via --session-id / --resume.
codex    OpenAI Codex CLI (``codex exec`` / ``codex exec resume``). Experimental.
command  Any CLI, described with a command template. Use this to wrap other agents.
mock     Deterministic fake agent for dry runs and tests.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class TurnRequest:
    agent: str
    role: str
    prompt: str
    system_prompt: str
    session_id: Optional[str]  # None -> start a new session
    cwd: str
    timeout: float
    options: Dict[str, Any]
    env: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)  # round, turn index, roster... (informational)
    # live progress while the turn runs: called with {"k": "text"|"tool", "s": "..."} (optional)
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None


@dataclass
class TurnResult:
    status: str  # ok | error | timeout | stopped
    output: str
    session_id: Optional[str]
    exit_code: Optional[int] = None
    stderr: str = ""
    command: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


class StopRequested(Exception):
    pass


# --------------------------------------------------------------------------- process helper


def run_process(cmd: List[str], stdin_text: Optional[str], cwd: str, timeout: float,
                env: Dict[str, str], should_stop: Callable[[], bool],
                on_line: Optional[Callable[[str], None]] = None) -> Tuple[Optional[int], str, str, str]:
    """Run a command, polling for stop requests. Returns (exit_code, stdout, stderr, status).
    ``on_line`` (optional) sees every stdout line as it arrives."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=full_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return None, "", f"executable not found: {e}", "error"
    except OSError as e:
        return None, "", f"failed to start: {e}", "error"

    out_chunks: List[str] = []
    err_chunks: List[str] = []

    def pump(stream, sink, cb=None):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if cb:
                try:
                    cb(line)
                except Exception:
                    pass  # live progress must never break a turn
        stream.close()

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, on_line), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    status = "ok"
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if should_stop():
                status = "stopped"
            elif deadline and time.monotonic() > deadline:
                status = "timeout"
            else:
                continue
            _kill(proc)
            break
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    rc = proc.returncode
    if status == "ok" and rc != 0:
        status = "error"
    return rc, "".join(out_chunks), "".join(err_chunks), status


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _opt_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return shlex.split(v)
    return [str(x) for x in v]


# --------------------------------------------------------------------------- backends


class Backend:
    name = "base"
    #: whether the backend can attach the system prompt natively; if not it is prepended to the first prompt
    native_system_prompt = False

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def first_prompt_with_system(req: TurnRequest) -> str:
        if req.session_id is None and req.system_prompt:
            return f"<system_instructions>\n{req.system_prompt}\n</system_instructions>\n\n{req.prompt}"
        return req.prompt


class ClaudeBackend(Backend):
    """Claude Code in print mode.

    options: executable, model, effort, permission_mode, allowed_tools, disallowed_tools, add_dirs,
             max_turns, mcp_config, system_prompt_mode (append|replace), extra_args
    """

    name = "claude"
    native_system_prompt = True

    def build_command(self, req: TurnRequest) -> Tuple[List[str], str]:
        o = req.options
        cmd = [str(o.get("executable", "claude")), "-p"]
        if req.on_event is not None and o.get("stream", True):
            cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        else:
            cmd += ["--output-format", "json"]
        if req.session_id:
            cmd += ["--resume", req.session_id]
            sid = req.session_id
        else:
            sid = str(uuid.uuid4())
            cmd += ["--session-id", sid]
        if req.system_prompt:
            flag = "--system-prompt" if o.get("system_prompt_mode") == "replace" else "--append-system-prompt"
            cmd += [flag, req.system_prompt]
        if o.get("model"):
            cmd += ["--model", str(o["model"])]
        if o.get("effort"):
            cmd += ["--effort", str(o["effort"])]
        if o.get("permission_mode"):
            cmd += ["--permission-mode", str(o["permission_mode"])]
        for key, flag in (("allowed_tools", "--allowedTools"), ("disallowed_tools", "--disallowedTools")):
            v = o.get(key)
            if v:
                cmd += [flag, ",".join(str(x) for x in v) if isinstance(v, list) else str(v)]
        for d in _opt_list(o.get("add_dirs")):
            cmd += ["--add-dir", os.path.expanduser(d)]
        if o.get("max_turns"):
            cmd += ["--max-turns", str(o["max_turns"])]
        if o.get("mcp_config"):
            cmd += ["--mcp-config", os.path.expanduser(str(o["mcp_config"]))]
        cmd += _opt_list(o.get("extra_args"))
        return cmd, sid

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        cmd, sid = self.build_command(req)
        cb = _claude_stream_cb(req.on_event) if "stream-json" in cmd else None
        rc, out, err, status = run_process(cmd, req.prompt, req.cwd, req.timeout, req.env, should_stop, cb)
        meta: Dict[str, Any] = {}
        output = out
        data = _parse_claude_json(out)
        if data is not None:
            output = str(data.get("result", "") or "")
            sid = data.get("session_id") or sid
            for k in ("total_cost_usd", "num_turns", "duration_ms", "is_error", "subtype"):
                if k in data:
                    meta[k] = data[k]
            if data.get("is_error") and status == "ok":
                status = "error"
        elif status == "ok":
            status = "error"
            meta["parse_error"] = "could not parse claude JSON output"
        return TurnResult(status, output, sid, rc, err, _redact(cmd, req), meta)


def _short_input(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if inp.get(k):
            return str(inp[k])[:160]
    return ""


def _claude_stream_cb(emit: Callable[[Dict[str, Any]], None]) -> Callable[[str], None]:
    """Turn Claude stream-json lines into small live events (text deltas and tool calls)."""
    def cb(line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        ev = json.loads(line)
        if ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            emit({"k": "session", "s": ev["session_id"]})  # known early, so an interrupted turn can be resumed
        elif ev.get("type") == "stream_event" and not ev.get("parent_tool_use_id"):
            e = ev.get("event") or {}
            d = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and d.get("type") == "text_delta" and d.get("text"):
                emit({"k": "text", "s": d["text"]})
            elif e.get("type") == "message_start":
                emit({"k": "break", "s": ""})
        elif ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    emit({"k": "tool", "s": f"{c.get('name')}: {_short_input(c.get('input'))}".rstrip(": ")})
    return cb


def _parse_claude_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    candidates: List[Any] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():  # stream-json / verbose fallback
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    flat: List[Dict[str, Any]] = []
    for c in candidates:
        if isinstance(c, list):
            flat += [x for x in c if isinstance(x, dict)]
        elif isinstance(c, dict):
            flat.append(c)
    for d in reversed(flat):
        if d.get("type") == "result" or "result" in d:
            return d
    return None


class CodexBackend(Backend):
    """OpenAI Codex CLI (experimental — flags change between versions; verify with `codex exec --help`).

    options: executable, model, effort, sandbox, full_auto (bool), extra_args
    """

    name = "codex"

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        o = req.options
        exe = str(o.get("executable", "codex"))
        flags = ["--json", "--skip-git-repo-check"]
        if o.get("model"):
            flags += ["-m", str(o["model"])]
        if o.get("effort"):
            flags += ["-c", f"model_reasoning_effort={o['effort']}"]
        if o.get("sandbox"):
            # `codex exec resume` has no --sandbox flag; the config override works for both
            flags += ["-c", f'sandbox_mode="{o["sandbox"]}"']
        if o.get("full_auto"):
            flags += ["--full-auto"]
        flags += _opt_list(o.get("extra_args"))
        prompt = self.first_prompt_with_system(req)
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as tf:
            last_msg = tf.name
        flags += ["--output-last-message", last_msg]
        if req.session_id:
            cmd = [exe, "exec", "resume", *flags, req.session_id, "-"]
        else:
            cmd = [exe, "exec", *flags, "-"]
        cb = _codex_stream_cb(req.on_event) if req.on_event is not None else None
        rc, out, err, status = run_process(cmd, prompt, req.cwd, req.timeout, req.env, should_stop, cb)
        sid = req.session_id
        final = ""
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            sid = ev.get("thread_id") or ev.get("session_id") or sid
            item = ev.get("item") or {}
            if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                final = item["text"]
        try:
            with open(last_msg, encoding="utf-8") as f:
                final = f.read().strip() or final
            os.unlink(last_msg)
        except OSError:
            pass
        if status == "ok" and not sid:
            status = "error"
        return TurnResult(status, final or out, sid, rc, err, cmd[:-1] + ["<prompt via stdin>"], {})


def _codex_stream_cb(emit: Callable[[Dict[str, Any]], None]) -> Callable[[str], None]:
    def cb(line: str) -> None:
        ev = json.loads(line)
        if isinstance(ev, dict) and ev.get("type") == "thread.started" and ev.get("thread_id"):
            emit({"k": "session", "s": ev["thread_id"]})
            return
        item = ev.get("item") if isinstance(ev, dict) else None
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if ev.get("type") == "item.started" and kind == "command_execution":
            emit({"k": "tool", "s": "run: " + str(item.get("command", ""))[:160]})
        elif ev.get("type") == "item.completed" and kind in ("agent_message", "assistant_message") and item.get("text"):
            emit({"k": "text", "s": item["text"] + "\n\n"})
        elif ev.get("type") == "item.completed" and kind == "file_change":
            files = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
            emit({"k": "tool", "s": "edit: " + ", ".join(str(f) for f in files)[:160]})
    return cb


class CommandBackend(Backend):
    """Wrap any CLI agent.

    options:
      command:        list or string; placeholders {session_id} {cwd} {prompt} {prompt_file}
                      {system_prompt} {system_prompt_file} {agent}
      resume_command: optional, used for turns after the first (defaults to `command`)
      prompt_via:     stdin (default) | arg | file   (arg/file: use {prompt} / {prompt_file} in the command)
      system_prompt:  native (use {system_prompt}/{system_prompt_file} placeholders) | prepend (default)
      output_format:  text (default) | json
      output_field:   dotted key for the reply when output_format=json (default "result")
      session_field:  dotted key for the session id in JSON output
      session_regex:  regex with one group to capture a session id from stdout
    """

    name = "command"

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        o = req.options
        if "command" not in o:
            return TurnResult("error", "", req.session_id, None, "command backend needs backend_options.command", [], {})
        new_session = req.session_id is None
        sid = req.session_id or str(uuid.uuid4())
        tmpl = o.get("resume_command") if (not new_session and o.get("resume_command")) else o["command"]
        tmpl = shlex.split(tmpl) if isinstance(tmpl, str) else [str(x) for x in tmpl]
        native_sys = o.get("system_prompt") == "native"
        prompt = req.prompt if native_sys else self.first_prompt_with_system(req)
        tmp_files: List[str] = []

        def tmp(text: str) -> str:
            fd, p = tempfile.mkstemp(suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            tmp_files.append(p)
            return p

        values = {"session_id": sid, "cwd": req.cwd, "agent": req.agent, "prompt": prompt,
                  "system_prompt": req.system_prompt}
        joined = " ".join(tmpl)
        if "{prompt_file}" in joined:
            values["prompt_file"] = tmp(prompt)
        if "{system_prompt_file}" in joined:
            values["system_prompt_file"] = tmp(req.system_prompt)
        cmd = [_safe_format(part, values) for part in tmpl]
        via = o.get("prompt_via", "stdin")
        try:
            rc, out, err, status = run_process(cmd, prompt if via == "stdin" else None, req.cwd,
                                               req.timeout, req.env, should_stop)
        finally:
            for p in tmp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        output = out
        if o.get("output_format") == "json":
            try:
                data = json.loads(out)
                output = str(_dig(data, o.get("output_field", "result")) or "")
                if o.get("session_field"):
                    sid = str(_dig(data, o["session_field"]) or sid)
            except (json.JSONDecodeError, TypeError):
                if status == "ok":
                    status = "error"
        if o.get("session_regex"):
            m = re.search(o["session_regex"], out)
            if m:
                sid = m.group(1)
        shown = [("<prompt>" if c == prompt and via == "arg" else c) for c in cmd]
        return TurnResult(status, output.strip(), sid, rc, err, shown, {})


class MockBackend(Backend):
    """Fake agent for dry runs. Never touches anything outside `write_file` if set.

    options:
      delay:        seconds to sleep per turn (default 0)
      script:       list of replies, used in order per agent session (placeholders {round} {agent})
      pass_after:   verifiers reply PASS from this round on (default 2)
      done_after:   llm controllers declare done at this round (default 2)
      fail_turns:   list of per-session turn numbers (1-based) that return an error
      write_file:   workers append a line to this file (relative to cwd) each turn
    """

    name = "mock"
    native_system_prompt = True
    _counts: Dict[str, int] = {}
    _lock = threading.Lock()

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        o = req.options
        sid = req.session_id or f"mock-{uuid.uuid4().hex[:12]}"
        with self._lock:
            n = self._counts.get(sid, 0) + 1
            self._counts[sid] = n
        delay = float(o.get("delay", 0))
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if should_stop():
                return TurnResult("stopped", "", sid, None, "", ["mock"], {})
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        rnd = int(req.context.get("round", 1))
        purpose = str(req.context.get("purpose", ""))
        if n in [int(x) for x in o.get("fail_turns", [])]:
            return TurnResult("error", "", sid, 1, "mock: simulated failure", ["mock"], {"turn": n})
        script = o.get("script")
        by_purpose = (o.get("replies") or {}).get(purpose)  # managed dry runs: {"purpose": [reply, ...]}
        if by_purpose:
            k = self._counts.setdefault(f"{sid}:{purpose}", 0) + 1
            self._counts[f"{sid}:{purpose}"] = k
            text = str(by_purpose[min(k, len(by_purpose)) - 1]).replace("{round}", str(rnd)).replace("{agent}", req.agent)
            return TurnResult("ok", text, sid, 0, "", ["mock"], {"turn": n})
        if script:
            text = str(script[min(n, len(script)) - 1]).replace("{round}", str(rnd)).replace("{agent}", req.agent)
            return TurnResult("ok", text, sid, 0, "", ["mock"], {"turn": n})
        if req.role == "manager":
            text = self._manager(purpose, req, o)
        elif req.role == "verifier" and purpose and purpose not in ("verify",):
            text = {"brief": "Here is what I need for this milestone: make the change and check it.\n\n"
                             "- [ ] the change is made\n- [ ] it has been checked",
                    "report": "| item | state |\n|---|---|\n| change made | done |\n| checked | done |\n\n"
                              "Everything on my list holds now.",
                    }.get(purpose, "Noted. Please also keep the summary short.")
        elif req.role == "verifier":
            ms_round = int(req.context.get("milestone_round", rnd))
            if ms_round >= int(o.get("pass_after", 2)):
                text = (f"Checked the workspace in round {rnd}. Everything matches the goal.\n\n"
                        "- [x] the change is made\n- [x] it has been checked\nVERDICT: PASS")
            else:
                text = (f"Round {rnd} review: the work is incomplete.\n- add input validation\n"
                        f"- add a unit test for the edge case\n\n- [ ] the change is made\n- [ ] it has been checked\n"
                        "VERDICT: FAIL")
        elif req.role == "controller":
            roster = [a for a in req.context.get("roster", []) if a != req.agent]
            if rnd >= int(o.get("done_after", 2)):
                d = {"summary": "All agents reported success.", "dispatch": [], "done": True,
                     "final_report": f"Goal met after {rnd} rounds."}
            else:
                d = {"summary": f"Round {rnd}: dispatching everyone.",
                     "dispatch": [{"agent": a, "message": f"Do your part for round {rnd}."} for a in roster],
                     "parallel": True, "done": False}
            text = "Here is my directive.\n```json\n" + json.dumps(d, indent=2) + "\n```"
        else:
            first = next((l.strip("# ").strip() for l in req.prompt.splitlines() if l.strip()), "")
            text = f"[{req.agent}] round {rnd}, session turn {n}: handled '{first[:80]}'. Changes made, tests run."
            if o.get("write_file"):
                path = os.path.join(req.cwd, str(o["write_file"]))
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{req.agent} round {rnd} turn {n}\n")
        if req.on_event:
            req.on_event({"k": "text", "s": text})
        return TurnResult("ok", text, sid, 0, "", ["mock"], {"turn": n})


    @staticmethod
    def _manager(purpose: str, req: TurnRequest, o: Dict[str, Any]) -> str:
        if purpose == "plan":
            k = int(o.get("milestones", 2))
            ms = "\n".join(f"{i}. Part {i} — part {i} of the goal is done and checked" for i in range(1, k + 1))
            return (f"Here is how I would split it.\n\n{ms}\n\n| criterion | what a 5 looks like |\n|---|---|\n"
                    "| correctness | nothing wrong |\n| clarity | easy to read |\n\nI accept a milestone at 4/5 or more.")
        if purpose == "review":
            return ("| criterion | score |\n|---|---|\n| correctness | 5 |\n| clarity | 4 |\n\n"
                    "Overall score: 4.5/5. This milestone is done.\n\nDECISION: ACCEPT")
        if purpose == "answer":
            return "Go with the simpler option for now."
        if purpose == "chat":
            return "Things are on track; nothing needs you right now."
        if purpose == "pause":
            return "Nothing useful is left until that is done. Please do the task the critic describes.\n\nDECISION: PAUSE"
        if purpose == "ack":
            return "Thanks, carry on."
        return "Please get the first part of the goal done and keep me posted."


# --------------------------------------------------------------------------- helpers


def _safe_format(s: str, values: Dict[str, str]) -> str:
    for k, v in values.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def _dig(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def _redact(cmd: List[str], req: TurnRequest) -> List[str]:
    """Hide the (long) system prompt in the recorded command line."""
    return [("<system prompt>" if req.system_prompt and c == req.system_prompt else c) for c in cmd] + ["<prompt via stdin>"]


BACKENDS: Dict[str, Backend] = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "command": CommandBackend(),
    "mock": MockBackend(),
}


def get_backend(name: str) -> Backend:
    return BACKENDS[name]
