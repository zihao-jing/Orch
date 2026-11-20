#!/usr/bin/env python3
"""Claude Code hook: relay an orch agent's interactive terminal session (usually the manager's) to the run.

When you open the manager's session yourself (`orch terminal <run>` prints the command:
`ORCH_RUN_DIR=… ORCH_AGENT=… claude --resume <session id>`), this hook

- SessionStart / UserPromptSubmit: marks the session busy in `<run>/busy/<agent>.json`, so orch holds that agent's
  turns (two writers would split the session's history);
- Stop: hands the newest assistant reply to orch as `<run>/inbox/*-terminal-reply.json`, so its
  `<message to="…">` tags are routed like any other reply (and a `<pause>` stops the work);
- SessionEnd: clears the mark. A mark that sees no activity for `terminal_busy_timeout` seconds (controller
  setting, default 3600) is treated as stale, so a crashed terminal doesn't hold the run forever.

Fast by design: standard library only, a few small file writes, and only the tail of the transcript is read. It
does nothing for turns orch runs itself (they carry ORCH_JOB in their environment) or for sessions that belong to
no orch run. Without ORCH_RUN_DIR it looks for the session id in `<cwd>/.orch/runs/*/*/state.json`.

Install (in the project's `.claude/settings.local.json`, or see `orch terminal --hooks`):

    {"hooks": {
      "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 /path/to/orch/hooks/terminal_relay.py"}]}],
      "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /path/to/orch/hooks/terminal_relay.py"}]}],
      "Stop":             [{"hooks": [{"type": "command", "command": "python3 /path/to/orch/hooks/terminal_relay.py"}]}],
      "SessionEnd":       [{"hooks": [{"type": "command", "command": "python3 /path/to/orch/hooks/terminal_relay.py"}]}]
    }}
"""
import json
import os
import sys
import time
from pathlib import Path

TAIL = 4 * 1024 * 1024  # bytes of the transcript read from its end


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _read(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def locate(data):
    """(run dir, agent name) this session belongs to, or (None, None)."""
    sid = data.get("session_id") or ""
    run = os.environ.get("ORCH_RUN_DIR")
    agent = os.environ.get("ORCH_AGENT")
    if run:
        run = Path(run)
        if agent:
            return run, agent
        st = _read(run / "state.json") or {}
        for name, s in (st.get("sessions") or {}).items():
            if (s or {}).get("session_id") == sid:
                return run, name
        return run, next((n for n, s in (st.get("sessions") or {}).items() if (s or {}).get("role") == "manager"), None)
    base = Path(data.get("cwd") or os.getcwd()) / ".orch" / "runs"
    try:
        states = sorted(base.glob("*/*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
    except OSError:
        return None, None
    for p in states:
        st = _read(p) or {}
        for name, s in (st.get("sessions") or {}).items():
            if sid and (s or {}).get("session_id") == sid:
                return p.parent, name
        for b in (p.parent / "busy").glob("*.json"):  # a terminal that moved to a new session id
            if (_read(b) or {}).get("session_id") == sid:
                return p.parent, b.stem
    return None, None


def last_reply(transcript):
    """(text, id) of the assistant's reply since the last prompt typed in the session; ('', '') if none."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return "", ""
    texts, last_id = [], ""
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "user":
            typed = isinstance(content, str) or any(isinstance(c, dict) and c.get("type") == "text" for c in content or [])
            if typed and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content or []
                                 if not isinstance(content, str)):
                texts, last_id = [], ""  # a new prompt: the reply starts again
        elif e.get("type") == "assistant":
            parts = [content] if isinstance(content, str) else \
                [c.get("text") or "" for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            texts += [x for x in parts if x.strip()]
            last_id = e.get("uuid") or msg.get("id") or last_id
    return "\n\n".join(texts).strip(), last_id


def main():
    if os.environ.get("ORCH_JOB"):
        return 0  # a turn orch runs itself
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    event = data.get("hook_event_name") or ""
    run, agent = locate(data)
    if run is None or not agent or not (run / "state.json").is_file():
        return 0
    busy = run / "busy" / ("%s.json" % agent)
    now = time.time()
    stamp = "%s-%06d" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns() // 1000 % 1000000)
    mark = _read(busy) or {}
    sid = data.get("session_id") or mark.get("session_id")
    if event == "SessionEnd":
        try:
            busy.unlink()
        except OSError:
            pass
        _write(run / "inbox" / ("%s-terminal-end.json" % stamp),
               {"kind": "terminal", "event": "end", "agent": agent, "session_id": sid})
        return 0
    new = not mark
    mark.update(agent=agent, session_id=sid, touched=now)
    mark.setdefault("since", time.strftime("%Y-%m-%dT%H:%M:%S"))
    if event == "Stop":
        text, rid = last_reply(data.get("transcript_path"))
        if not text and data.get("last_assistant_message"):
            text, rid = str(data["last_assistant_message"]), "%s-%d" % (sid, int(now))
        if text and rid and rid != mark.get("last_reply"):
            _write(run / "inbox" / ("%s-terminal-reply.json" % stamp),
                   {"kind": "reply", "via": "terminal", "agent": agent, "session_id": sid, "text": text,
                    "source": "terminal-%s" % rid})
            mark["last_reply"] = rid
    _write(busy, mark)
    if new:
        _write(run / "inbox" / ("%s-terminal-start.json" % stamp),
               {"kind": "terminal", "event": "start", "agent": agent, "session_id": sid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
