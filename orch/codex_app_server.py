"""One Codex turn over the app-server JSONL protocol, with incremental text output."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Callable

from .backends import Backend, TurnRequest, TurnResult, _kill, _opt_list


class _Cancelled(Exception):
    pass


def run_turn(req: TurnRequest, should_stop: Callable[[], bool]) -> TurnResult:
    o = req.options
    cmd = [str(o.get("executable", "codex")), "app-server"]
    # exec-only flags (such as --ignore-user-config) must not silently be dropped.
    cmd += _opt_list(o.get("app_server_args", o.get("extra_args")))
    if o.get("model"):
        cmd += ["-c", "model=" + json.dumps(str(o["model"]))]
    if o.get("effort"):
        cmd += ["-c", "model_reasoning_effort=" + json.dumps(str(o["effort"]))]
    sandbox = o.get("sandbox", "workspace-write")
    cmd += ["-c", "sandbox_mode=" + json.dumps(sandbox), "-c", 'approval_policy="never"']
    if o.get("full_auto"):
        return TurnResult("error", "", req.session_id, stderr="app-server uses sandbox and approval_policy; omit full_auto")
    try:
        proc = subprocess.Popen(cmd, cwd=req.cwd, env={**os.environ, **req.env},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as e:
        return TurnResult("error", "", req.session_id, stderr=str(e), command=cmd)

    incoming: queue.Queue = queue.Queue()
    errors = []

    def read_stdout():
        try:
            for line in proc.stdout:
                try:
                    incoming.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            incoming.put(None)
            proc.stdout.close()

    def read_stderr():
        for line in proc.stderr:
            errors.append(line)
        proc.stderr.close()

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + req.timeout if req.timeout and req.timeout > 0 else None
    sid, active_turn, completed = req.session_id, None, None
    streamed, finished = {}, set()
    final, request_id, text_deltas = "", 0, 0

    def emit(event):
        if req.on_event:
            try:
                req.on_event(event)
            except Exception:
                pass  # a broken live display must not break the turn

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def notification(message):
        nonlocal completed, final, active_turn, text_deltas
        method, p = message.get("method"), message.get("params") or {}
        if p.get("threadId") not in (None, sid):
            return
        if active_turn and p.get("turnId") not in (None, active_turn):
            return
        if method == "turn/started":
            active_turn = p["turn"]["id"]
        elif method == "item/agentMessage/delta":
            text_deltas += 1
            key, delta = p["itemId"], p["delta"]
            streamed[key] = streamed.get(key, "") + delta
            emit({"k": "text", "s": delta})
        elif method in ("item/started", "item/completed"):
            item = p.get("item") or {}
            key, kind = item.get("id"), item.get("type")
            if method == "item/started" and kind == "commandExecution":
                emit({"k": "tool", "s": "run: " + str(item.get("command", ""))[:160]})
            elif method == "item/completed" and kind == "agentMessage" and key not in finished:
                text = item.get("text", "")
                prior = streamed.get(key, "")
                # Some versions only emit completion events. Avoid duplicating any streamed prefix.
                if text.startswith(prior):
                    emit({"k": "text", "s": text[len(prior):] + "\n\n"})
                else:
                    emit({"k": "text", "s": "\n\n"})
                streamed[key] = text
                finished.add(key)
                if text:
                    final = text
            elif method == "item/completed" and kind == "fileChange":
                emit({"k": "tool", "s": "edit: " + ", ".join(c.get("path", "") for c in item.get("changes", []))[:160]})
        elif method == "turn/completed":
            turn = p["turn"]
            if active_turn is None or turn["id"] == active_turn:
                completed = turn

    def receive():
        while True:
            if should_stop():
                raise _Cancelled()
            if deadline and time.monotonic() >= deadline:
                raise TimeoutError("Codex app-server turn timed out")
            try:
                message = incoming.get(timeout=0.1)
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("Codex app-server exited before completing the turn")
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # Never leave an unattended run hanging on an unsupported approval/tool request.
                send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request in Orch"}})
                raise RuntimeError("Codex requested unsupported interaction: " + message["method"])
            if "method" in message:
                notification(message)
            return message

    def call(method, params):
        nonlocal request_id
        request_id += 1
        ident = request_id
        send({"id": ident, "method": method, "params": params})
        while True:
            message = receive()
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result") or {}

    status, exit_code = "error", None
    try:
        call("initialize", {"clientInfo": {"name": "orch", "version": "0.1.0"}})
        send({"method": "initialized"})
        params = {"cwd": req.cwd, "sandbox": sandbox, "approvalPolicy": "never"}
        if o.get("model"):
            params["model"] = o["model"]
        if sid:
            params["threadId"] = sid
            thread = call("thread/resume", params)
        else:
            thread = call("thread/start", params)
        sid = thread["thread"]["id"]
        emit({"k": "session", "s": sid})
        params = {"threadId": sid, "input": [{"type": "text", "text": Backend.first_prompt_with_system(req)}]}
        if o.get("effort"):
            params["effort"] = o["effort"]
        turn = call("turn/start", params)
        active_turn = turn["turn"]["id"]
        while completed is None:
            receive()
        status = {"completed": "ok", "interrupted": "stopped"}.get(completed.get("status"), "error")
        if completed.get("error"):
            errors.append(json.dumps(completed["error"]))
        exit_code = 0 if status == "ok" else None
    except _Cancelled:
        status = "stopped"
    except TimeoutError as e:
        status = "timeout"
        errors.append(str(e))
    except Exception as e:
        errors.append(str(e))
    finally:
        if status in ("stopped", "timeout") and sid and active_turn:
            try:
                send({"id": request_id + 1, "method": "turn/interrupt", "params": {"threadId": sid, "turnId": active_turn}})
            except OSError:
                pass
        if proc.poll() is None:
            _kill(proc)
        try:
            proc.stdin.close()
        except OSError:
            pass
        for reader in readers:
            reader.join(timeout=5)
    output = final if status == "ok" and final else "\n\n".join(streamed.values())
    return TurnResult(status, output, sid, exit_code, "".join(errors), cmd,
                      {"transport": "app-server", "text_deltas": text_deltas})
