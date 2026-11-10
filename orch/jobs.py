"""Background jobs a worker left running: found by orch itself, so the worker doesn't have to report them.

A job is a process that started during the worker's turn, is still running after the turn ended, runs in the
workspace (its working directory is inside it) and was detached from the agent (``nohup … &``, ``setsid``): its
parent is the system's init process (or another such job). Processes of mine, an editor or a terminal keep their
own parents and are never counted.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

Proc = Tuple[int, str]  # (parent pid, command line)
_INIT = re.compile(r"(^|/)(launchd|systemd|init)(\s|$)")


def snapshot() -> Dict[int, Proc]:
    """Live processes now: {pid: (ppid, command)}. Empty if `ps` is unavailable."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,command="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    procs: Dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2][0] not in "ZXx":
            procs[int(parts[0])] = (int(parts[1]), parts[3] if len(parts) > 3 else "")
    return procs


def cwds(pids: Iterable[int]) -> Dict[int, str]:
    pids = [int(p) for p in pids]
    if not pids:
        return {}
    if sys.platform.startswith("linux"):
        out = {}
        for p in pids:
            try:
                out[p] = os.readlink(f"/proc/{p}/cwd")
            except OSError:
                pass
        return out
    try:  # macOS / BSD
        res = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fpn"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out, cur = {}, None
    for line in res.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            cur = int(line[1:])
        elif line.startswith("n") and cur is not None:
            out[cur] = line[1:]
    return out


def _inside(path: str, roots: List[str]) -> bool:
    try:
        rp = os.path.realpath(path)
    except OSError:
        return False
    return any(rp == r or rp.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def detect(before: Dict[int, Proc], workspaces: Iterable[str], ignore: Iterable[str] = (),
           after: Optional[Dict[int, Proc]] = None) -> List[Dict[str, object]]:
    """Jobs started since `before` that are still running, detached, in one of the workspaces."""
    if not before:
        return []
    after = snapshot() if after is None else after
    me = os.getpid()
    new = {p: v for p, v in after.items() if p not in before and p != me}
    if not new:
        return []
    roots = [os.path.realpath(w) for w in workspaces if w]
    where = cwds(new)
    pats = [re.compile(x) for x in ignore if x]

    def detached(pid: int, seen: int = 0) -> bool:
        ppid = new[pid][0]
        if ppid <= 1 or _INIT.search(after.get(ppid, (0, ""))[1] or ""):
            return True
        return ppid in new and seen < 20 and detached(ppid, seen + 1)

    jobs = []
    for pid, (_pp, cmd) in sorted(new.items()):
        if not _inside(where.get(pid, ""), roots) or not detached(pid):
            continue
        if any(p.search(cmd) for p in pats):
            continue
        if new[pid][0] in new and any(j["pid"] == new[pid][0] for j in jobs):
            continue  # a child of a job already listed: waiting for the parent covers it
        jobs.append({"pid": pid, "command": cmd})
    return jobs


def alive(pid: int, command: str = "") -> bool:
    """Still running (and, if given, still the same command: pids get reused)."""
    try:  # a finished child of ours stays a zombie until reaped
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    # kill(pid, 0) also succeeds for zombies owned by another parent; waitpid cannot reap them.
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,command="], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode not in (0, 1):
        return True  # unable to inspect it: keep waiting conservatively
    parts = result.stdout.strip().split(None, 1)
    if not parts or parts[0][0] in "ZXx":
        return False
    now = parts[1] if len(parts) > 1 else ""
    return not command or now == command.strip()
