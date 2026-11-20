"""Agent backends: how one turn of one agent is executed.

Every backend receives a :class:`TurnRequest` and returns a :class:`TurnResult`.
Sessions are continued by passing back the ``session_id`` a previous turn returned.

Built-in backends
-----------------
claude   Claude Code CLI (``claude -p``), sessions via --session-id / --resume.
qwen     Qwen Code CLI (``qwen -p``), same event format as Claude Code; sessions via --session-id / --resume.
opencode OpenCode CLI (``opencode run --format json``, live text and steering via a private ``opencode serve``).
codex    OpenAI Codex CLI (``codex exec`` / ``codex exec resume``). Experimental.
agy      Antigravity CLI (``agy -p``) for Gemini models, sessions via --conversation. Experimental.
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
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .opencode_recovery import ProviderRecovery


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
    # Optional durable input queue. Only app-server currently supports input during a turn.
    poll_messages: Optional[Callable[[], List[Dict[str, Any]]]] = None
    on_message: Optional[Callable[[str, str, Dict[str, Any]], None]] = None


@dataclass
class TurnResult:
    status: str  # ok | error | timeout | stopped
    output: str
    session_id: Optional[str]
    exit_code: Optional[int] = None
    stderr: str = ""
    command: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    # B8: the backend's raw event stream, only when the reply had to be parsed out of it (no final result: an error
    # or a cut-off). Kept apart from the reply (a debug file beside the turn); never stored as the output.
    raw: str = ""


class StopRequested(Exception):
    pass


# --------------------------------------------------------------------------- process helper


def run_process(cmd: List[str], stdin_text: Optional[str], cwd: str, timeout: float,
                env: Dict[str, str], should_stop: Callable[[], bool],
                on_line: Optional[Callable[[str], None]] = None,
                abort_reason: Optional[Callable[[], Optional[str]]] = None,
                watch_waiters: bool = True) -> Tuple[Optional[int], str, str, str]:
    """Run a command, polling for stop requests. Returns (exit_code, stdout, stderr, status).
    ``on_line`` sees every stdout line; ``abort_reason`` can stop a failed attempt as an error.
    Explicit cancellation and the overall timeout take precedence over that callback."""
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

    last_out = [time.monotonic()]  # when the agent last wrote anything

    def pump(stream, sink, cb=None):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if cb:
                last_out[0] = time.monotonic()
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
    last_check = 0.0
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if should_stop():
                status = "stopped"
            elif deadline and time.monotonic() > deadline:
                status = "timeout"
            elif abort_reason and (why := abort_reason()):
                status = "error"
                err_chunks.append(why + "\n")
            else:
                now = time.monotonic()
                if watch_waiters and on_line is not None and now - last_out[0] > WAITER_GRACE and now - last_check >= WAITER_CHECK:
                    last_check = now
                    _stop_waiters(proc.pid, err_chunks)  # silent for a long time while waiting on a process
                continue
            _kill(proc)
            break
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    rc = proc.returncode
    if status == "ok" and rc != 0:
        status = "error"
    return rc, "".join(out_chunks), "".join(err_chunks), status


WAITER_GRACE = 300.0  # seconds an agent may be silent, with a waiter loop open, before orch stops the loop
WAITER_CHECK = 15.0   # how often that is checked


def _stop_waiters(root: int, err_chunks: List[str]) -> None:
    """Stop the loops a silent agent is waiting in (see jobs.waiters). The job they wait for is a separate, detached
    process and keeps running; only the loop's own shell is stopped, so the agent's turn can end."""
    from . import jobs
    try:
        found = jobs.waiters(root, jobs.snapshot())
    except Exception:
        return
    for pid in found:
        try:
            os.kill(pid, signal.SIGTERM)
            err_chunks.append(f"stopped a waiter loop (pid {pid}) that was silent for over "
                              f"{WAITER_GRACE:.0f}s; the process it waited for keeps running\n")
        except OSError:
            pass


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

    def compact(self, req: TurnRequest, should_stop: Callable[[], bool]) -> Optional[TurnResult]:
        """Compact an existing session between turns (B7), or None when this backend can't do it on request.
        Compaction is otherwise the CLI's own (auto-compact)."""
        return None

    @staticmethod
    def first_prompt_with_system(req: TurnRequest) -> str:
        if req.session_id is None and req.system_prompt:
            return f"<system_instructions>\n{req.system_prompt}\n</system_instructions>\n\n{req.prompt}"
        return req.prompt


class ClaudeBackend(Backend):
    """Claude Code in print mode.

    options: executable, model, effort, permission_mode, allowed_tools, disallowed_tools, add_dirs,
             max_turns, mcp_config, system_prompt_mode (append|replace), disable_hooks (bool: skip the hooks in
             your Claude settings, e.g. a terminal integration that waits on the terminal app), extra_args
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
        if o.get("disable_hooks"):
            cmd += ["--settings", json.dumps({"disableAllHooks": True})]
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
            usage = data.get("usage")
            if isinstance(usage, dict):  # tokens: the only usage figure that holds whatever the endpoint charges
                meta["usage"] = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            if isinstance(data.get("modelUsage"), dict) and data["modelUsage"]:
                meta["models"] = sorted(data["modelUsage"])  # the models that actually answered
            self._result_meta(data, out, meta)
            if data.get("is_error") and status == "ok":
                status = "error"
            if '"compact_boundary"' in out:  # the CLI compacted the session during the turn (auto-compact)
                meta["compacted"] = True
        else:  # B8: no final result (an error or a cut-off): the last assistant message, never the raw stream
            output, sid2 = _claude_stream_text(out)
            sid = sid2 or sid
            raw = out
            if status == "ok":
                status = "error"
                meta["parse_error"] = "could not parse claude JSON output"
            if out.strip():
                meta["partial"] = True
            return TurnResult(status, output, sid, rc, err, _redact(cmd, req), meta, raw=raw)
        return TurnResult(status, output, sid, rc, err, _redact(cmd, req), meta)

    def _result_meta(self, data: Dict[str, Any], out: str, meta: Dict[str, Any]) -> None:
        """Backend-specific additions to a turn's meta, from the final result and the raw output."""

    COMPACT = "/compact"

    def compact(self, req: TurnRequest, should_stop: Callable[[], bool]) -> Optional[TurnResult]:
        """Claude Code runs its slash commands in print mode too: `claude -p --resume <id>` with `/compact` as the
        prompt summarises the session in place (same session id; the transcript gets a compact boundary, as with
        auto-compact). `compact_instructions` in backend_options are passed after it (`/compact <instructions>`).
        A session can't be compacted with `--disable-slash-commands` in extra_args."""
        if not req.session_id:
            return None
        extra = str(req.options.get("compact_instructions") or "").strip()
        r = TurnRequest(**{**req.__dict__, "prompt": f"{self.COMPACT} {extra}".strip(), "on_event": None,
                           "poll_messages": None, "on_message": None})
        return self.run(r, should_stop)


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


def _claude_stream_text(text: str) -> Tuple[str, Optional[str]]:
    """The last top-level assistant message and session id from a cut-off stream.

    Deltas and assistant snapshots describe the same message. Reconcile them by message id so a later
    content block keeps the earlier text, while a new message still replaces the previous reply.
    """
    messages: Dict[str, Dict[str, Any]] = {}
    current, sid = "", None
    records = _claude_json_records(text)
    for i, ev in enumerate(records):
        if ev.get("parent_tool_use_id"):
            continue
        sid = ev.get("session_id") or sid
        if ev.get("type") == "stream_event":
            event = ev.get("event") or {}
            kind = event.get("type")
            if kind == "message_start":
                current = str((event.get("message") or {}).get("id") or f"stream-{i}")
            elif kind not in ("content_block_start", "content_block_delta"):
                continue
            if not current:
                current = f"stream-{i}"
            msg = messages.setdefault(current, {"blocks": {}, "text": ""})
            index = event.get("index", 0)
            block, delta = event.get("content_block") or {}, event.get("delta") or {}
            if kind == "content_block_start" and block.get("type") == "text":
                msg["blocks"][index] = str(block.get("text") or "")
            elif kind == "content_block_delta" and delta.get("type") == "text_delta":
                msg["blocks"][index] = msg["blocks"].get(index, "") + str(delta.get("text") or "")
        elif ev.get("type") == "assistant":
            snapshot = ev.get("message") or {}
            mid = str(snapshot.get("id") or current or f"assistant-{i}")
            msg = messages.setdefault(mid, {"blocks": {}, "text": ""})
            blocks = {n: c["text"] for n, c in enumerate(snapshot.get("content") or [])
                      if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)}
            body = "\n\n".join(blocks.values())
            previous = msg["text"]
            if body.startswith(previous):
                msg["text"] = body
            elif body and not previous.startswith(body):
                msg["text"] = previous + "\n\n" + body
            if not msg["blocks"]:
                msg["blocks"].update(blocks)
    for msg in reversed(list(messages.values())):
        streamed = "\n\n".join(msg["blocks"].values())
        body = max((msg["text"], streamed), key=len).strip()
        if body:
            return body, sid
    # A plain CLI error is useful when there is no assistant reply. JSON is never a report.
    try:
        json.loads(text)
        return "", sid
    except (ValueError, TypeError):
        pass
    plain = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("{", "["))]
    return "\n".join(plain).strip(), sid


def claude_stream_report(text: str) -> Optional[str]:
    """Recover old saved Claude streams on read; None leaves ordinary text/JSON reports alone."""
    records = _claude_json_records(text)
    if not any("session_id" in e and e.get("type") in ("system", "assistant", "stream_event", "result")
               for e in records):
        return None
    for e in reversed(records):
        if e.get("type") == "result" and not e.get("parent_tool_use_id"):
            return str(e.get("result") or "")
    return _claude_stream_text(text)[0]


def _claude_json_records(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
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
    return flat


def _parse_claude_json(text: str) -> Optional[Dict[str, Any]]:
    for d in reversed(_claude_json_records(text)):
        if d.get("type") == "result" or "result" in d:
            return d
    return None


class QwenBackend(ClaudeBackend):
    """Qwen Code CLI in headless mode (``qwen -p``).

    Its print mode follows Claude Code's: the same json / stream-json events (the json output is a list of them),
    --session-id / --resume, --system-prompt / --append-system-prompt. What differs is handled here: it has no
    --verbose, --effort, --permission-mode, --disallowedTools or --settings; it compacts with /compress; it reports
    no dollar cost; and on a resumed session its result `usage` is the session's running total, so a turn's own
    tokens are summed from that turn's assistant messages instead (`session_usage` keeps the running total).

    options: executable, model, approval_mode (plan|default|auto-edit|auto|yolo; or permission_mode with Claude's
             names, mapped), allowed_tools, disallowed_tools (-> --exclude-tools), add_dirs, mcp_config,
             system_prompt_mode (append|replace), stream (default true), compact_instructions, extra_args.
             Claude-only options (effort, max_turns, disable_hooks) are ignored.
    """

    name = "qwen"
    COMPACT = "/compress"
    #: Claude Code permission modes -> Qwen Code approval modes; other values are passed through as they are
    APPROVAL_MODES = {"default": "default", "acceptEdits": "auto-edit", "plan": "plan", "auto": "auto",
                      "bypassPermissions": "yolo"}

    def build_command(self, req: TurnRequest) -> Tuple[List[str], str]:
        o = req.options
        cmd = [str(o.get("executable", "qwen")), "-p"]  # the prompt itself goes on stdin
        if req.on_event is not None and o.get("stream", True):
            cmd += ["--output-format", "stream-json", "--include-partial-messages"]
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
        mode = o.get("approval_mode") or o.get("permission_mode")
        if mode:
            cmd += ["--approval-mode", self.APPROVAL_MODES.get(str(mode), str(mode))]
        for key, flag in (("allowed_tools", "--allowed-tools"), ("disallowed_tools", "--exclude-tools")):
            v = o.get(key)
            for t in (v if isinstance(v, list) else str(v or "").split(",")):
                if str(t).strip():
                    cmd += [flag, str(t).strip()]  # one flag per tool: a list flag would swallow what follows
        for d in _opt_list(o.get("add_dirs")):
            cmd += ["--include-directories", os.path.expanduser(d)]
        if o.get("mcp_config"):
            cmd += ["--mcp-config", os.path.expanduser(str(o["mcp_config"]))]
        cmd += _opt_list(o.get("extra_args"))
        return cmd, sid

    def _result_meta(self, data: Dict[str, Any], out: str, meta: Dict[str, Any]) -> None:
        if "usage" in meta:
            meta["session_usage"] = meta.pop("usage")
        turn, models = _qwen_turn_usage(out)
        if turn:
            meta["usage"] = turn
        if models:  # the models that answered (only the json result has `stats`; every assistant message names one)
            meta["models"] = models


def _qwen_turn_usage(out: str) -> Tuple[Dict[str, Any], List[str]]:
    """(this turn's tokens, the models that answered): the sum of the usage on each assistant message the turn
    printed (one per model request, subagents' included), and the models those messages name. Qwen's own result
    total also counts the session's earlier turns once it is resumed."""
    events: List[Any] = []
    try:
        parsed = json.loads(out)
        events = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    total: Dict[str, Any] = {}
    models: List[str] = []
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "assistant":
            m = ev.get("message") or {}
            if m.get("model") and m["model"] not in models:
                models.append(str(m["model"]))
            for k, v in (m.get("usage") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total[k] = total.get(k, 0) + v
    return (total if any(total.values()) else {}), sorted(models)


class OpenCodeBackend(Backend):
    """OpenCode CLI (``opencode run --format json``), tested with OpenCode 2.3.2.

    Turns never use OpenCode's shared background service, which does not see the per-turn configuration below.
    A turn someone watches, or a worker's turn (which may get messages), runs against a private ``opencode serve``
    started for it (transport ``serve``, the default; see _run_served): text streams word by word and messages are
    steered into the running turn. Any other turn, or transport ``run``, is a plain ``opencode run --standalone``.
    The prompt goes on stdin. Sessions continue with ``--session <id>``; the id (``ses_...``) is on every JSON event.
    The run's JSON events are whole parts: step_start, text, tool_use (once the tool finished), step_finish (that
    step's tokens and cost), error.

    System prompt: OpenCode has no flag for it. By default it goes in front of a session's first message (as for
    codex and agy). With ``system_prompt_mode: replace`` it becomes the ``system`` of an agent defined for this run in
    OPENCODE_CONFIG_CONTENT (sent again every turn), which replaces OpenCode's own built-in prompt.

    options: executable, model (provider/model#variant), agent, auto (bool, default true: --auto approves every
             permission that is not explicitly denied), permission_mode (bypassPermissions -> --auto), disallowed_tools (deny rules: shell, edit, read, webfetch, ...), add_dirs
             (external_directory allow rules), config (dict merged into the per-run config), system_prompt_mode
             (prepend|replace), archived (default true), thinking (bool), transport (serve|run), extra_args,
             provider_retry_timeout (seconds after a retry without progress; default 180, 0 disables).
             Claude-only options (effort, max_turns, allowed_tools, disable_hooks, mcp_config) are ignored.
    Compaction: a private ``opencode serve`` runs for its length (``POST /api/session/{id}/compact``, then waits).
    Experimental: OpenCode's CLI changes quickly; check flags with ``opencode run --help``.
    """

    name = "opencode"
    AGENT = "orch-{role}"

    def config(self, req: TurnRequest) -> Tuple[Dict[str, Any], Optional[str]]:
        """(the per-run OpenCode config, the agent to run as)."""
        o = req.options
        cfg: Dict[str, Any] = {}
        agent = str(o["agent"]) if o.get("agent") else None
        if o.get("system_prompt_mode") == "replace" and req.system_prompt:
            agent = agent or self.AGENT.format(role=re.sub(r"[^A-Za-z0-9_-]", "-", req.role or "agent"))
            cfg["agents"] = {agent: {"mode": "primary", "system": req.system_prompt}}
        rules = []
        for t in (o.get("disallowed_tools") if isinstance(o.get("disallowed_tools"), list)
                  else str(o.get("disallowed_tools") or "").split(",")):
            if str(t).strip():
                rules.append({"action": str(t).strip(), "resource": "*", "effect": "deny"})
        for d in _opt_list(o.get("add_dirs")):
            rules.append({"action": "external_directory",
                          "resource": os.path.join(os.path.expanduser(d), "*"), "effect": "allow"})
        if rules:
            cfg["permissions"] = rules
        if isinstance(o.get("config"), dict):
            cfg = _deep_merge_dict(cfg, o["config"])
        return cfg, agent

    def build(self, req: TurnRequest, server: Optional[str] = None) -> Tuple[List[str], Dict[str, str], str]:
        """(command, env, prompt on stdin). With `server`, the run goes to that (private) server."""
        o = req.options
        cfg, agent = self.config(req)
        cmd = [str(o.get("executable", "opencode")), "run"] + (["--server", server] if server else ["--standalone"])
        cmd += ["--format", "json"]
        if req.session_id:
            cmd += ["--session", req.session_id]
        elif o.get("archived", True):
            cmd.append("--archived")  # kept out of the user's own session list
        if o.get("model"):
            cmd += ["--model", str(o["model"])]
        if agent:
            cmd += ["--agent", agent]
        if o.get("auto", True) or o.get("permission_mode") == "bypassPermissions":
            cmd.append("--auto")  # on by default: an unattended turn has no one to answer a permission that asks
        if o.get("thinking"):
            cmd.append("--thinking")
        cmd += _opt_list(o.get("extra_args"))
        env = dict(req.env or {})
        if cfg:
            own = env.get("OPENCODE_CONFIG_CONTENT", os.environ.get("OPENCODE_CONFIG_CONTENT"))
            try:
                base = json.loads(own) if own else {}
            except json.JSONDecodeError:
                base = {}
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(_deep_merge_dict(base if isinstance(base, dict) else {}, cfg))
        native = o.get("system_prompt_mode") == "replace"
        prompt = req.prompt if native else self.first_prompt_with_system(req)
        return cmd, env, prompt

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        """A turn that someone watches (live text) or may send messages into runs against a private server
        (transport `serve`, the default); otherwise, or with transport `run`, as a plain --standalone run."""
        transport = str(req.options.get("transport") or "serve")
        if transport not in ("serve", "run"):
            return TurnResult("error", "", req.session_id, stderr=f"unknown opencode transport {transport!r}")
        if transport == "serve" and (req.on_event is not None or req.poll_messages is not None):
            return self._run_served(req, should_stop)
        return self._run_plain(req, should_stop)

    def _run_plain(self, req: TurnRequest, should_stop: Callable[[], bool], note: str = "") -> TurnResult:
        cmd, env, prompt = self.build(req)
        cb = _opencode_stream_cb(req.on_event) if req.on_event is not None else None
        recovery = ProviderRecovery(req.options.get("provider_retry_timeout", 180), req.on_event)
        def on_line(line: str) -> None:
            recovery.line(line)
            if cb:
                cb(line)
        rc, out, err, status = run_process(cmd, prompt, req.cwd, req.timeout, env, should_stop, on_line,
                                         recovery.abort_reason, watch_waiters=cb is not None)
        res = recovery.annotate(self._result(req, cmd, rc, out, err, status))
        if note:
            res.meta["warning"] = note
        return res

    def _run_served(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        """`opencode run --server <private server>`: the run's own JSON output stays the source of the reply, the
        tokens and the errors. The server's live events add the text as it streams (session.text.delta), and
        messages for the worker go into the running turn through the server's API (delivery "steer"); one counts as
        accepted when the server reports it delivered to the model (session.inbox.delivered). A message that arrives
        as the turn ends is still answered before the turn returns: the session is waited on until it is idle, and
        the reply is then the session's last assistant message."""
        import secrets
        exe = str(req.options.get("executable", "opencode"))
        pw = secrets.token_urlsafe(24)
        _, env, _ = self.build(req)
        recovery = ProviderRecovery(req.options.get("provider_retry_timeout", 180), req.on_event)
        env = {**env, "OPENCODE_SERVER_PASSWORD": pw, "OPENCODE_PASSWORD": pw}
        deadline = time.monotonic() + req.timeout if req.timeout and req.timeout > 0 else None
        server, url, why = _opencode_serve(exe, req.cwd, env, should_stop)
        if server is None:
            if should_stop():
                return TurnResult("stopped", "", req.session_id, stderr=why)
            return self._run_plain(req, should_stop, note=f"live transport unavailable ({why}); ran without it")
        live = _OpenCodeLive(req, url, pw, recovery)
        try:
            cmd, _, prompt = self.build(req, server=url)
            live.start()
            rc, out, err, status = run_process(cmd, prompt, req.cwd, req.timeout, env, should_stop, live.on_run_line,
                                             recovery.abort_reason)
            res = recovery.annotate(self._result(req, cmd, rc, out, err, status))
            live.close_steering()
            if res.status == "ok" and live.undelivered():
                left = (deadline - time.monotonic()) if deadline else 600.0
                follow = live.follow_up(res.session_id, max(5.0, left), should_stop)
                if follow:
                    res.output = follow
                    res.meta["steer_followup"] = True
            live.settle(res.status)
            res.meta.update(live.meta())
            if live.usage_extra:  # steps the run's own output did not include (the follow-up's)
                u = dict(res.meta.get("usage") or {})
                for k, v in live.usage_extra.items():
                    u[k] = u.get(k, 0) + v
                res.meta["usage"] = u
            return res
        finally:
            live.stop()
            _kill(server)

    def _result(self, req: TurnRequest, cmd: List[str], rc: Optional[int], out: str, err: str,
                status: str) -> TurnResult:
        p = _parse_opencode(out)
        sid = p["session_id"] or req.session_id
        meta: Dict[str, Any] = {}
        if p["usage"]:
            meta["usage"] = p["usage"]
        if p["cost"]:  # many providers report 0
            meta["total_cost_usd"] = p["cost"]
        if req.options.get("model"):
            meta["models"] = [str(req.options["model"])]
        if p["steps"]:
            meta["num_turns"] = p["steps"]
        if p["errors"]:
            meta["error"] = "; ".join(p["errors"])[:2000]
            err = (err + "\n" if err else "") + meta["error"]
            if status == "ok":
                status = "error"
        shown = cmd + ["<prompt via stdin>"]
        raw = ""
        if not p["reply"] and out.strip():  # B8: never the raw event stream as the reply
            raw = out
            if p["parsed"] == 0:
                meta["parse_error"] = "could not parse opencode JSON output"
                if status == "ok":
                    status = "error"
        elif status != "ok" and p["reply"]:
            meta["partial"] = True
        if status == "ok" and not sid:
            status = "error"
            meta["parse_error"] = "opencode did not report a session id"
        return TurnResult(status, p["reply"], sid, rc, err, shown, meta, raw=raw)

    def compact(self, req: TurnRequest, should_stop: Callable[[], bool]) -> Optional[TurnResult]:
        """POST /api/session/{id}/compact only queues the compaction, so a one-shot `--standalone` call would exit
        before it ran, and the shared background service does not know the per-run agent the session uses. So a
        private `opencode serve` is started with the turn's own config (and a password of orch's own) for the length
        of the compaction: compact, then /api/experimental/session/{id}/wait (until the session is idle), then the
        session's messages show whether the compaction completed."""
        if not req.session_id:
            return None
        import secrets
        exe = str(req.options.get("executable", "opencode"))
        sid = req.session_id
        _, env, _ = self.build(req)
        pw = secrets.token_urlsafe(24)
        env = {**env, "OPENCODE_SERVER_PASSWORD": pw, "OPENCODE_PASSWORD": pw}
        deadline = time.monotonic() + (req.timeout or 600)
        server, url, why = _opencode_serve(exe, req.cwd, env, should_stop)
        if server is None:
            return TurnResult("error", "", sid, None, why, [exe, "serve"], {})
        steps = [[exe, "api", "--server", url, "POST", f"/api/session/{sid}/compact", "-d", "{}"],
                 [exe, "api", "--server", url, "POST", f"/api/experimental/session/{sid}/wait", "-d", "{}"],
                 [exe, "api", "--server", url, "GET", f"/api/session/{sid}/message"]]
        errs: List[str] = []
        outs: List[str] = []
        try:
            for cmd in steps:
                left = max(5.0, deadline - time.monotonic())
                rc, out, err, status = run_process(cmd, None, req.cwd, left, env, should_stop)
                outs.append(out)
                if err.strip():
                    errs.append(err.strip())
                if status != "ok":
                    return TurnResult(status, "", sid, rc, "\n".join(errs + [out.strip()]).strip(), cmd, {})
        finally:
            _kill(server)
        queued = _opencode_data(outs[0])
        want = queued.get("id") if isinstance(queued, dict) else None
        msgs = _opencode_data(outs[2])
        done = [m for m in (msgs if isinstance(msgs, list) else []) if isinstance(m, dict)
                and m.get("type") == "compaction" and (not want or m.get("id") == want)]
        meta: Dict[str, Any] = {"compacted": bool(done and done[0].get("status") == "completed")}
        if not meta["compacted"]:
            state = done[0].get("status") if done else "not found"
            return TurnResult("error", "", sid, 0, "\n".join(errs + [f"compaction {state}"]), steps[0], meta)
        return TurnResult("ok", str(done[0].get("summary") or ""), sid, 0, "\n".join(errs), steps[0], meta)


def _opencode_serve(exe: str, cwd: str, env: Dict[str, str], should_stop: Callable[[], bool],
                    wait: float = 30.0) -> Tuple[Optional[subprocess.Popen], str, str]:
    """(a private `opencode serve` on a free local port, its url, why it did not start)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        proc = subprocess.Popen([exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)], cwd=cwd,
                                env={**os.environ, **env}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    except OSError as e:
        return None, url, f"failed to start opencode serve: {e}"
    end = time.monotonic() + wait
    while time.monotonic() < end and not should_stop():
        if proc.poll() is not None:
            return None, url, "opencode serve exited: " + (proc.stderr.read() if proc.stderr else "")[-1000:]
        try:  # a plain connect: an HTTP proxy in the environment must not answer for the server
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return proc, url, ""
        except OSError:
            time.sleep(0.25)
    _kill(proc)
    return None, url, "opencode serve did not start in time"


class _OpenCodeLive:
    """The live side of a served OpenCode turn: the server's event stream (text deltas, deliveries, models, step
    tokens) and the messages sent into the running turn. Every live event goes out under one lock, so the run's own
    output (read on another thread) and the server's stream never interleave or show a text twice."""

    POLL = 0.5

    def __init__(self, req: TurnRequest, url: str, password: str,
                 recovery: Optional[ProviderRecovery] = None) -> None:
        import base64
        from urllib.parse import urlparse
        self.req = req
        self.recovery = recovery
        u = urlparse(url)
        self.host, self.port = u.hostname or "127.0.0.1", int(u.port or 80)
        self.headers = {"Authorization": "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()}
        self.lock = threading.RLock()
        self.sid: Optional[str] = req.session_id
        self.ready = False            # the run's first event: its prompt is in, messages may follow
        self.buffer: List[Dict[str, Any]] = []  # server events before the session is known
        self.shown: Dict[str, str] = {}         # assistant message id -> text shown so far
        self.part_shown: Dict[Tuple[str, Any], str] = {}
        self.sse_owned: set = set()             # messages whose text the server's deltas show
        self.run_owned: set = set()             # messages whose text the run's output shows
        self.run_text: Dict[str, str] = {}
        self.run_mids: set = set()              # messages the run's output covers (its tokens)
        self.step_tokens: Dict[str, Dict[str, Any]] = {}
        self.models: List[str] = []
        self.text_deltas = 0
        self.pending: Dict[str, Dict[str, Any]] = {}  # inbox id -> message sent, not yet delivered to the model
        self.delivered: set = set()
        self.attempted: set = set()
        self.steered = 0
        self.usage_extra: Dict[str, Any] = {}
        self.events_ok = False
        self.done = threading.Event()
        self.steer_open = threading.Event()
        self.threads: List[threading.Thread] = []
        self.sse_sock: Any = None
        self.steer_thread: Optional[threading.Thread] = None

    # ---- http
    def http(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> Tuple[int, Any]:
        import http.client
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            data = json.dumps(body) if body is not None else None
            hdrs = {**self.headers, **({"Content-Type": "application/json"} if data is not None else {})}
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError:
                return r.status, raw
        finally:
            conn.close()

    # ---- lifecycle
    def start(self) -> None:
        """Subscribe to the server's events before the run starts, so no early delta is missed; then, for a worker,
        start sending its messages once the run's prompt is in."""
        import http.client
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
            conn.connect()
            sock = conn.sock  # kept: http.client hands an unsized stream's socket over to the response
            conn.request("GET", "/api/event", headers={**self.headers, "Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status == 200:
                self.sse_sock = sock
                sock.settimeout(None)  # a long tool call is silent; the stream ends when the server does
                self.events_ok = True
                t = threading.Thread(target=self._read_events, args=(resp,), daemon=True)
                t.start()
                self.threads.append(t)
            else:
                conn.close()
        except (OSError, http.client.HTTPException):
            pass  # no live deltas: the run's own text parts are shown instead
        if self.req.poll_messages is not None:
            self.steer_open.set()
            self.steer_thread = threading.Thread(target=self._steer_loop, daemon=True)
            self.steer_thread.start()
            self.threads.append(self.steer_thread)

    def close_steering(self) -> None:
        """The run ended: nothing more is sent into it (a send already under way finishes)."""
        self.steer_open.clear()
        if self.steer_thread is not None:
            self.steer_thread.join(timeout=35)

    def stop(self) -> None:
        self.done.set()
        self.steer_open.clear()
        import socket
        if self.sse_sock is not None:
            try:
                self.sse_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in self.threads:
            t.join(timeout=2)

    # ---- output
    def emit(self, ev: Dict[str, Any]) -> None:
        if self.req.on_event is None:
            return
        try:
            self.req.on_event(ev)
        except Exception:
            pass  # a broken live display must not break the turn

    def status(self, note: Dict[str, Any], status: str, **details: Any) -> None:
        if self.req.on_message is not None:
            try:
                self.req.on_message(note["id"], status, {"via": "steer", **details})
            except Exception:
                pass

    # ---- the run's own output
    def on_run_line(self, line: str) -> None:
        if self.recovery:
            self.recovery.line(line)
        line = line.strip()
        if not line.startswith("{"):
            return
        ev = json.loads(line)
        if not isinstance(ev, dict):
            return
        with self.lock:
            sid = ev.get("sessionID")
            if sid and not self.ready:
                self.ready = True
                self.sid = self.sid or str(sid)
                self.emit({"k": "session", "s": self.sid})
                self.emit({"k": "input_accepted"})
                held, self.buffer = self.buffer, []
                for e in held:
                    self._server_event(e)
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            mid = str(part.get("messageID") or "")
            kind = ev.get("type")
            if mid:
                self.run_mids.add(mid)
            if kind == "step_start":
                self.emit({"k": "break", "s": ""})
            elif kind == "tool_use":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                self.emit({"k": "tool", "s": f"{part.get('tool')}: {_short_input(state.get('input'))}".rstrip(": ")})
            elif kind == "text" and part.get("text"):
                t = str(part["text"])
                self.run_text[mid] = self.run_text.get(mid, "") + t
                if mid in self.sse_owned:  # the deltas showed it already; show only what they missed
                    target, cur = self.run_text[mid], self.shown.get(mid, "")
                    if target.startswith(cur) and len(target) > len(cur):
                        self.emit({"k": "text", "s": target[len(cur):]})
                        self.shown[mid] = target
                else:
                    self.run_owned.add(mid)
                    self.shown[mid] = self.shown.get(mid, "") + t
                    self.emit({"k": "text", "s": t})

    # ---- the server's events
    def _read_events(self, resp: Any) -> None:
        data: List[str] = []
        try:
            while not self.done.is_set():
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    self._dispatch("\n".join(data))
                    data = []
        except Exception:
            pass
        finally:
            if data:
                self._dispatch("\n".join(data))

    def _dispatch(self, text: str) -> None:
        try:
            ev = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(ev, dict) or not isinstance(ev.get("data"), dict):
            return
        with self.lock:
            if not self.ready:
                if len(self.buffer) < 10000:
                    self.buffer.append(ev)
                return
            self._server_event(ev)

    def _server_event(self, ev: Dict[str, Any]) -> None:
        d = ev["data"]
        kind = ev.get("type")
        if kind == "session.inbox.delivered":  # inbox ids are unique; a delivery can come before the POST returns
            iid = str(d.get("inboxID") or "")
            self.delivered.add(iid)
            note = self.pending.pop(iid, None)
            if note is not None:
                self.status(note, "accepted", session_id=self.sid, opencode_message_id=iid)
                self.emit({"k": "tool", "s": "Additional instruction accepted; current tools continue."})
            return
        if d.get("sessionID") != self.sid:
            return  # another session (a subagent's)
        if self.recovery:
            self.recovery.server_event(ev)
        mid = str(d.get("assistantMessageID") or "")
        if kind == "session.text.delta" and d.get("delta"):
            if mid in self.run_owned:
                return
            delta = str(d["delta"])
            self.sse_owned.add(mid)
            self.text_deltas += 1
            key = (mid, d.get("ordinal"))
            self.part_shown[key] = self.part_shown.get(key, "") + delta
            self.shown[mid] = self.shown.get(mid, "") + delta
            self.emit({"k": "text", "s": delta})
        elif kind == "session.text.ended" and mid in self.sse_owned and isinstance(d.get("text"), str):
            key = (mid, d.get("ordinal"))
            cur, full = self.part_shown.get(key, ""), d["text"]
            if full.startswith(cur) and len(full) > len(cur):  # deltas missed by a slow stream
                self.emit({"k": "text", "s": full[len(cur):]})
                self.shown[mid] = self.shown.get(mid, "") + full[len(cur):]
                self.part_shown[key] = full
        elif kind == "session.step.started" and isinstance(d.get("model"), dict):
            m = d["model"]
            name = "/".join(str(x) for x in (m.get("providerID"), m.get("id")) if x)
            if name and name not in self.models:
                self.models.append(name)
        elif kind == "session.step.ended" and mid and isinstance(d.get("tokens"), dict):
            self.step_tokens[mid] = d["tokens"]

    # ---- messages into the running turn
    def _steer_loop(self) -> None:
        while self.steer_open.is_set() and not self.done.is_set():
            with self.lock:
                ready, sid = self.ready, self.sid
            if ready and sid:
                try:
                    notes = self.req.poll_messages() or []
                except Exception:
                    notes = []
                for note in notes:
                    if not self.steer_open.is_set():
                        break
                    if note["id"] in self.attempted:
                        continue
                    self.attempted.add(note["id"])
                    self._send(sid, note)  # one at a time, in order
            time.sleep(self.POLL)

    def _send(self, sid: str, note: Dict[str, Any]) -> None:
        self.status(note, "sending", session_id=sid)
        try:
            code, body = self.http("POST", f"/api/session/{sid}/prompt",
                                   {"text": note["message"], "delivery": "steer"})
        except Exception as e:
            self.status(note, "deferred", reason=f"{type(e).__name__}: {e}")
            return
        iid = body.get("data", {}).get("id") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
        if code != 200 or not iid:
            self.status(note, "deferred", reason=f"HTTP {code}: {json.dumps(body)[:300]}")
            return
        with self.lock:
            self.steered += 1
            if iid in self.delivered:
                self.status(note, "accepted", session_id=sid, opencode_message_id=iid)
                self.emit({"k": "tool", "s": "Additional instruction accepted; current tools continue."})
            else:
                self.pending[iid] = note

    def undelivered(self) -> bool:
        with self.lock:
            return bool(self.pending)

    def follow_up(self, sid: Optional[str], timeout: float, should_stop: Callable[[], bool]) -> Optional[str]:
        """A message admitted as the run ended: OpenCode answers it in the same session after the run has returned.
        Wait for the session to be idle (and the message delivered), then the reply is its last assistant text."""
        if not sid:
            return None
        end = time.monotonic() + timeout
        quiet = 0
        while time.monotonic() < end and not should_stop() and self.undelivered() and quiet < 3:
            try:
                self.http("POST", f"/api/experimental/session/{sid}/wait", {},
                          timeout=max(1.0, min(30.0, end - time.monotonic())))
            except Exception:
                pass  # a read timeout: wait again
            time.sleep(0.3)  # the delivery event may trail the idle state a little
            quiet = quiet + 1 if self.undelivered() else 0
        try:  # once idle, the server has finished every step it was going to run
            self.http("POST", f"/api/experimental/session/{sid}/wait", {},
                      timeout=max(1.0, min(30.0, end - time.monotonic())))
        except Exception:
            pass
        with self.lock:
            extra = [t for mid, t in self.step_tokens.items() if mid not in self.run_mids]
        for tok in extra:
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            for src, key in ((tok, "input"), (tok, "output"), (tok, "reasoning"), (cache, "read"), (cache, "write")):
                v = src.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    name = OPENCODE_TOKEN_KEYS[key if src is tok else "cache_" + key]
                    self.usage_extra[name] = self.usage_extra.get(name, 0) + v
        if not extra:
            return None
        try:
            code, body = self.http("GET", f"/api/session/{sid}/message")
        except Exception:
            return None
        msgs = body.get("data") if isinstance(body, dict) else None
        for m in msgs if isinstance(msgs, list) else []:  # newest first
            if isinstance(m, dict) and m.get("type") == "assistant":
                text = "\n\n".join(str(c.get("text") or "").strip() for c in m.get("content") or []
                                   if isinstance(c, dict) and c.get("type") == "text" and c.get("text"))
                if text.strip():
                    return text.strip()
        return None

    def settle(self, status: str) -> None:
        """Messages still undelivered when the turn ends: their outcome is unknown (OpenCode holds them in the
        session's inbox and may deliver them at its next turn), so they are never replayed automatically."""
        with self.lock:
            left, self.pending = list(self.pending.values()), {}
        for note in left:
            self.status(note, "unknown", reason=f"admitted to the OpenCode session but not delivered before the turn "
                                                f"ended ({status}); not automatically replayed")

    def meta(self) -> Dict[str, Any]:
        with self.lock:
            out: Dict[str, Any] = {"transport": "serve", "text_deltas": self.text_deltas}
            if self.models:
                out["models"] = list(self.models)
            if self.steered:
                out["steered"] = self.steered
            if not self.events_ok:
                out["warning"] = "no live event stream from the OpenCode server; text shown a part at a time"
            return out


def _deep_merge_dict(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list) and k == "permissions":
            out[k] = out[k] + v  # rules are ordered; the last match wins
        else:
            out[k] = v
    return out


def _opencode_data(text: str) -> Any:
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None
    return v.get("data") if isinstance(v, dict) and "data" in v else v


def _opencode_events(text: str) -> List[Dict[str, Any]]:
    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events


def _parse_opencode(text: str) -> Dict[str, Any]:
    """The reply (the text of the last assistant message that has text), session id, summed tokens and cost,
    the number of model steps, and any error messages, from `opencode run --format json` output."""
    events = _opencode_events(text)
    sid: Optional[str] = None
    texts: Dict[str, List[str]] = {}
    order: List[str] = []
    usage: Dict[str, Any] = {}
    cost, steps, errors = 0.0, 0, []
    for ev in events:
        sid = ev.get("sessionID") or sid
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "text" and part.get("text"):
            mid = str(part.get("messageID") or "")
            if mid not in texts:
                texts[mid] = []
                order.append(mid)
            texts[mid].append(str(part["text"]))
        elif kind == "step_finish":
            steps += 1
            c = part.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                cost += c
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            for src, key in ((tok, "input"), (tok, "output"), (tok, "reasoning"), (cache, "read"), (cache, "write")):
                v = src.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    name = OPENCODE_TOKEN_KEYS[key if src is tok else "cache_" + key]
                    usage[name] = usage.get(name, 0) + v
        elif kind == "error":
            e = ev.get("error")
            msg = (e.get("message") or e.get("type")) if isinstance(e, dict) else e
            errors.append(str(msg or "unknown error"))
    reply = "\n\n".join(t.strip() for t in texts[order[-1]] if t.strip()) if order else ""
    return {"reply": reply, "session_id": sid, "usage": usage if any(usage.values()) else {},
            "cost": round(cost, 6), "steps": steps, "errors": errors, "parsed": len(events)}


OPENCODE_TOKEN_KEYS = {"input": "input_tokens", "output": "output_tokens", "reasoning": "reasoning_tokens",
                       "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


def _opencode_stream_cb(emit: Callable[[Dict[str, Any]], None]) -> Callable[[str], None]:
    seen = [False]

    def cb(line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        ev = json.loads(line)
        if not isinstance(ev, dict):
            return
        if ev.get("sessionID") and not seen[0]:
            seen[0] = True
            emit({"k": "session", "s": ev["sessionID"]})  # known early, so an interrupted turn can be resumed
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "step_start":
            emit({"k": "break", "s": ""})
        elif ev.get("type") == "text" and part.get("text"):
            emit({"k": "text", "s": str(part["text"])})
        elif ev.get("type") == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            emit({"k": "tool", "s": f"{part.get('tool')}: {_short_input(state.get('input'))}".rstrip(": ")})
    return cb


class CodexBackend(Backend):
    """OpenAI Codex CLI (experimental — flags change between versions; verify with `codex exec --help`).

    options: executable, model, effort, sandbox, full_auto (bool), extra_args,
             transport (exec or app-server), app_server_args
    """

    name = "codex"

    def compact(self, req: TurnRequest, should_stop: Callable[[], bool]) -> Optional[TurnResult]:
        """Compaction on request goes through the app-server (`thread/compact/start`) for either transport: `codex
        exec` and the app-server share thread ids, so a thread made by `exec` is compacted in place too."""
        if not req.session_id:
            return None
        from .codex_app_server import run_turn
        r = TurnRequest(**{**req.__dict__, "prompt": "", "on_event": None, "poll_messages": None, "on_message": None})
        return run_turn(r, should_stop, compact=True)

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        o = req.options
        if o.get("transport", "exec") == "app-server":
            from .codex_app_server import run_turn
            return run_turn(req, should_stop)
        if o.get("transport", "exec") != "exec":
            return TurnResult("error", "", req.session_id, stderr="Unknown Codex transport")
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
        if final:
            return TurnResult(status, final, sid, rc, err, cmd[:-1] + ["<prompt via stdin>"], {})
        # B8: no final message: never the raw JSON event stream as the reply (plain text from the CLI is kept)
        plain = "\n".join(ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("{")).strip()
        return TurnResult(status, plain, sid, rc, err, cmd[:-1] + ["<prompt via stdin>"], {},
                          raw=out if out.strip() and plain != out.strip() else "")


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


class AgyBackend(Backend):
    """Antigravity CLI (``agy -p``), for Gemini models. Sign in once by running ``agy`` interactively.

    options: executable, model, effort, mode (accept-edits|auto|plan), dangerously_skip_permissions (bool),
             sandbox (bool), add_dirs, agent, project, settings (dict or path), print_timeout,
             prompt_via (arg (default) | stdin), extra_args
    No system-prompt flag: the system prompt goes in front of a session's first message. Sessions continue with
    --conversation <id>; the id is read from the JSON output.
    """

    name = "agy"

    def build_command(self, req: TurnRequest) -> List[str]:
        o = req.options
        cmd = [str(o.get("executable", "agy")), "--output-format", "json"]
        if req.session_id:
            cmd += ["--conversation", req.session_id]
        for key, flag in (("model", "--model"), ("effort", "--effort"), ("mode", "--mode"), ("agent", "--agent"),
                          ("project", "--project"), ("print_timeout", "--print-timeout")):
            if o.get(key):
                cmd += [flag, str(o[key])]
        if o.get("dangerously_skip_permissions"):
            cmd.append("--dangerously-skip-permissions")
        if o.get("sandbox"):
            cmd.append("--sandbox")
        for d in _opt_list(o.get("add_dirs")):
            cmd += ["--add-dir", os.path.expanduser(d)]
        if o.get("settings"):
            s = o["settings"]
            cmd += ["--settings", json.dumps(s) if isinstance(s, dict) else os.path.expanduser(str(s))]
        cmd += _opt_list(o.get("extra_args"))
        return cmd

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        prompt = self.first_prompt_with_system(req)
        cmd = self.build_command(req)
        via_arg = req.options.get("prompt_via", "arg") != "stdin"
        cmd += ["-p", prompt] if via_arg else ["-p"]
        rc, out, err, status = run_process(cmd, None if via_arg else prompt, req.cwd, req.timeout, req.env, should_stop)
        data = _parse_agy_json(out)
        sid, meta, output, raw = req.session_id, {}, out.strip(), ""
        if data is not None:
            output = str(_first(data, AGY_TEXT_KEYS) or "")
            sid = str(_first(data, AGY_SESSION_KEYS) or "") or sid
            for k in ("total_cost_usd", "num_turns", "duration_ms", "is_error", "subtype", "usage"):
                if k in data:
                    meta[k] = data[k]
            if data.get("is_error") and status == "ok":
                status = "error"
        else:
            if out.lstrip().startswith(("{", "[")):  # B8: unparsed JSON is never the reply; kept apart for debugging
                output, raw = "", out
            if status == "ok":
                status = "error"
                meta["parse_error"] = "could not parse agy JSON output"
        if status == "ok" and not sid:
            meta["warning"] = "agy did not report a conversation id; the next turn starts a new conversation"
        shown = [("<prompt>" if c == prompt else c) for c in cmd] + ([] if via_arg else ["<prompt via stdin>"])
        return TurnResult(status, output, sid, rc, err, shown, meta, raw=raw)


AGY_TEXT_KEYS = ("result", "response", "text", "output", "content", "message")
AGY_SESSION_KEYS = ("conversation_id", "conversationId", "session_id", "sessionId", "conversation")


def _first(d: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v):
            return v
        if isinstance(v, dict) and k in ("conversation", "message"):  # {"conversation": {"id": ...}}
            inner = v.get("id") if k == "conversation" else v.get("text") or v.get("content")
            if isinstance(inner, str) and inner:
                return inner
    return None


def _parse_agy_json(text: str) -> Optional[Dict[str, Any]]:
    """The final result object of `agy --output-format json` (one object, or the last of several lines); the
    conversation id is taken from any line that carries one when the final object does not."""
    text = text.strip()
    if not text:
        return None
    objs: List[Dict[str, Any]] = []
    try:
        v = json.loads(text)
        objs = [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict)]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    x = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(x, dict):
                    objs.append(x)
    if not objs:
        return None
    final = next((d for d in reversed(objs) if d.get("type") == "result" or _first(d, AGY_TEXT_KEYS)), objs[-1])
    if not _first(final, AGY_SESSION_KEYS):
        sid = next((_first(d, AGY_SESSION_KEYS) for d in objs if _first(d, AGY_SESSION_KEYS)), None)
        if sid:
            final = {**final, "conversation_id": sid}
    return final


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
      delay:        seconds to sleep per turn (default 0); delays: {"purpose": seconds} per purpose
                    delay_for: {"agent": {"purpose": seconds}} per agent (tests of parallel workers)
      activity:     while sleeping, emit a stream event every 50 ms (a turn that is visibly working)
      script:       list of replies, used in order per agent session (placeholders {round} {agent})
      pass_after:   verifiers reply PASS from this round on (default 2)
      done_after:   llm controllers declare done at this round (default 2)
      fail_turns:   list of per-session turn numbers (1-based) that return an error
      error_text:   the reply text of a failed turn (e.g. a provider's message)
      write_file:   workers append a line to this file (relative to cwd) each turn
      wait_job:     the critic answers <wait-job/> when its review lists running jobs (default true)
      files:        managed dry runs: {"purpose": [{path: text, ...}, ...]}: the k-th turn of that purpose writes these
                    files (relative to cwd; "" deletes one; {agent} in a path or text is the agent's name), as an
                    agent editing a file in place would
    """

    name = "mock"
    native_system_prompt = True
    _counts: Dict[str, int] = {}
    _lock = threading.Lock()
    _running: Dict[str, int] = {}  # sessions with a turn running now
    compacts: List[Dict[str, Any]] = []  # tests: every compact request, and whether a turn was running in that session

    def compact(self, req: TurnRequest, should_stop: Callable[[], bool]) -> Optional[TurnResult]:
        if not req.session_id or req.options.get("no_compact"):
            return None
        with self._lock:
            self.compacts.append({"agent": req.agent, "session_id": req.session_id, "at": time.time(),
                                  "mid_turn": bool(self._running.get(req.session_id))})
            meta = self._cost(req, req.session_id)
        return TurnResult("ok", "", req.session_id, 0, "", ["mock", "/compact"], meta)

    def _cost(self, req: TurnRequest, sid: str) -> Dict[str, Any]:
        """`cost_per_turn`: report a running session total like Claude Code (every use of the session adds to it)."""
        c = req.options.get("cost_per_turn")
        if not isinstance(c, (int, float)):
            return {}
        k = f"{sid}:cost"
        self._counts[k] = self._counts.get(k, 0) + 1
        return {"total_cost_usd": round(self._counts[k] * float(c), 6)}

    def run(self, req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
        with self._lock:
            sid = req.session_id or f"mock-{uuid.uuid4().hex[:12]}"
            self._running[sid] = self._running.get(sid, 0) + 1
        try:
            res = self._run(req, should_stop, sid)
        finally:
            with self._lock:
                self._running[sid] -= 1
        if res.status == "ok":
            with self._lock:
                cost = self._cost(req, sid)
            if cost:
                res = TurnResult(res.status, res.output, res.session_id, res.exit_code, res.stderr, res.command,
                                 {**res.meta, **cost})
        return res

    def _run(self, req: TurnRequest, should_stop: Callable[[], bool], sid: str) -> TurnResult:
        o = req.options
        with self._lock:
            n = self._counts.get(sid, 0) + 1
            self._counts[sid] = n
        rnd = int(req.context.get("round", 1))
        purpose = str(req.context.get("purpose", ""))
        delay = float((o.get("delays") or {}).get(purpose, o.get("delay", 0)))
        delay = float(((o.get("delay_for") or {}).get(req.agent) or {}).get(purpose, delay))  # tests: per agent
        t0 = time.monotonic()
        end = t0 + delay
        while time.monotonic() < end:
            if should_stop():
                return TurnResult("stopped", "", sid, None, "", ["mock"], {})
            if req.timeout and time.monotonic() - t0 > req.timeout:  # like run_process: past the limit, stopped
                return TurnResult("timeout", "", sid, None, "mock: timed out", ["mock"], {})
            if o.get("activity") and req.on_event:  # tests: a turn that keeps writing to its stream
                req.on_event({"k": "tool", "s": "run: step"})
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        if n in [int(x) for x in o.get("fail_turns", [])]:
            if o.get("stream_on_fail"):  # tests (B8): a Claude-like stream that ends without a result line
                raw = str(o["stream_on_fail"])
                text, _ = _claude_stream_text(raw)
                return TurnResult("error", text, sid, 1, "mock: simulated failure", ["mock"], {"turn": n}, raw=raw)
            return TurnResult("error", str(o.get("error_text", "")), sid, 1, "mock: simulated failure", ["mock"], {"turn": n})
        script = o.get("script")
        if o.get("spawn") and purpose == "work" and n == 1:  # tests: leave a detached job running in the workspace
            subprocess.run(["sh", "-c", f"nohup {o['spawn']} >/dev/null 2>&1 &"], cwd=req.cwd or None)
        edits = (o.get("files") or {}).get(purpose)  # managed dry runs: files the agent edits in this turn
        if edits:
            k = self._counts.setdefault(f"{sid}:files:{purpose}", 0) + 1
            self._counts[f"{sid}:files:{purpose}"] = k
            for path, text in (edits[k - 1] if k <= len(edits) else {}).items():
                fp = Path(req.cwd or ".") / str(path).replace("{agent}", req.agent)
                text = str(text).replace("{agent}", req.agent) if text != "" else ""
                if text == "":
                    fp.unlink(missing_ok=True)
                else:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(str(text), encoding="utf-8")
        if (req.role == "verifier" and purpose in ("verify", "par_review") and o.get("wait_job", True)
                and "reply with `<wait-job/>`" in (req.prompt or "")):  # a job still runs: the mock critic waits for it
            return TurnResult("ok", "The job is still running; I'll review when it ends.\n\n<wait-job/>", sid, 0, "",
                              ["mock"], {"turn": n})
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
                             "<checklist>\n- [ ] the change is made\n- [ ] it has been checked\n</checklist>",
                    "report": "| item | state |\n|---|---|\n| change made | done |\n| checked | done |\n\n"
                              "Everything on my list holds now.",
                    }.get(purpose, "Noted. Please also keep the summary short.")
        elif req.role == "verifier":
            ms_round = int(req.context.get("milestone_round", rnd))
            tags = "milestone_round" in req.context  # managed mode speaks the tag protocol
            ok = ms_round >= int(o.get("pass_after", 2))
            items = "- [x] the change is made\n- [x] it has been checked" if ok else \
                "- [ ] the change is made\n- [ ] it has been checked"
            body = (f"Checked the workspace in round {rnd}. Everything matches the goal." if ok else
                    f"Round {rnd} review: the work is incomplete.\n- add input validation\n"
                    f"- add a unit test for the edge case")
            v = "PASS" if ok else "FAIL"
            text = (f"{body}\n\n<checklist>\n{items}\n</checklist>\n<verdict>{v}</verdict>" if tags
                    else f"{body}\n\n{items}\nVERDICT: {v}")
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
            return (f"Here is how I would split it.\n\n{ms}\n\nI accept a milestone at 4/5 or more.\n\n"
                    "<summary>The plan covers the whole goal in order; checked against the goal; see the list above.</summary>")
        if purpose == "review":
            return ("| criterion | score |\n|---|---|\n| correctness | 5 |\n| clarity | 4 |\n\n"
                    "This milestone is done.\n\n<summary>Changed: the part is done. Checked: the critic's list. "
                    "Look at: the workspace.</summary>\n\n<score>4.5</score>\n<decision>ACCEPT</decision>")
        if purpose == "answer":
            return '<message to="critic">Go with the simpler option for now.</message>'
        if purpose == "chat":
            return "Things are on track; nothing needs you right now."
        if purpose == "pause":
            return ("Nothing useful is left until that is done. Please do the task the critic describes.\n\n"
                    "<decision>PAUSE</decision>")
        if purpose == "ack":
            return "Thanks, carry on."
        if purpose == "incoming":
            return "Noted; nothing changes for now."
        if purpose == "kickoff":
            return ('<message to="critic">Please get the first part of the goal done and keep me posted.</message>\n\n'
                    "<rubric>\n- the change is made where the goal asks\n- it is checked against the goal\n"
                    "- nothing outside the scope changed\n</rubric>")
        return '<message to="critic">Please get the first part of the goal done and keep me posted.</message>'


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


def run_variants(agent: Any, req: TurnRequest, should_stop: Callable[[], bool],
                 session_key: Optional[str] = None) -> TurnResult:
    """One turn outside the main loop (an audit, a chat from the board): the agent's primary model, then its
    backups in order until one works. When the turn continues a session, only the models that can continue it
    (same backend and provider: `session_key`) are tried."""
    import dataclasses
    res = TurnResult("error", "", req.session_id, stderr="no model to run")
    tried: List[str] = []
    for v in agent.variants():
        if req.session_id and session_key and v.session_key != session_key:
            continue
        try:
            marks = {k: x for k, x in (req.env or {}).items() if k.startswith("ORCH_")}  # orch's own, kept for backups
            res = get_backend(v.backend).run(dataclasses.replace(req, options=v.options, env={**v.env, **marks}),
                                             should_stop)
        except Exception as e:
            res = TurnResult("error", "", req.session_id, stderr=f"{type(e).__name__}: {e}")
        tried.append(v.label)
        if res.status in ("ok", "stopped", "timeout") or should_stop():
            break
    if len(tried) > 1:
        res.meta = {**(res.meta or {}), "models_tried": tried}
    return res


BACKENDS: Dict[str, Backend] = {
    "claude": ClaudeBackend(),
    "qwen": QwenBackend(),
    "opencode": OpenCodeBackend(),
    "codex": CodexBackend(),
    "agy": AgyBackend(),
    "command": CommandBackend(),
    "mock": MockBackend(),
}


def get_backend(name: str) -> Backend:
    return BACKENDS[name]
