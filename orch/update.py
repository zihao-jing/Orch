"""A newer orch on main: checking for it, and taking it in.

The board asks for a check only at a safe point (the run waits at a gate or a human task, or has ended), so a fetch
never happens during a turn. The check reports the commit orch runs from and whether origin/main is ahead of it.

`orch run` also updates itself (cli.py, unless --no-auto-update): every few minutes, when every run of the process
waits for me (a gate, a job, a pause) with no turn and no audit running, it checks; when main is ahead it takes the new
commits in (`apply`) and restarts as `orch run … --resume`, which carries on where it stopped.
"""
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent  # the checkout orch runs from
SAFE_STATUSES = ("awaiting_approval", "waiting_job", "done", "stopped", "error", "interrupted", "paused", "")


def _git(*args: str, timeout: int = 20, repo: Optional[Path] = None) -> str:
    out = subprocess.run(["git", "-C", str(repo or REPO), *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"git {' '.join(args)} failed")
    return out.stdout.strip()


def safe_point(status: str) -> bool:
    """True when the run is at a point where an update can wait for it (see SAFE_STATUSES)."""
    return (status or "") in SAFE_STATUSES


def check(status: str) -> Dict[str, Any]:
    """The commit orch runs from, and whether origin/main has commits it does not have. Fetches only at a safe point."""
    try:
        current = _git("rev-parse", "--short", "HEAD")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"could not read the checkout: {e}"}
    if not safe_point(status):
        return {"ok": True, "checked": False, "current": current, "branch": branch, "dirty": dirty,
                "reason": "The run is busy. Check again at a gate, while it waits for you, or after it ends."}
    try:
        _git("fetch", "--quiet", "origin", "main")
        latest = _git("rev-parse", "--short", "origin/main")
        behind = int(_git("rev-list", "--count", "HEAD..origin/main") or 0)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        return {"ok": False, "checked": False, "current": current, "branch": branch, "dirty": dirty,
                "error": f"could not reach origin: {e}"}
    return {"ok": True, "checked": True, "current": current, "branch": branch, "dirty": dirty,
            "latest": latest, "behind": behind, "update": behind > 0}


# a run waiting for me, where a restart loses nothing (an ended run is not restarted: there is nothing to carry on)
RESTART_STATUSES = ("awaiting_approval", "waiting_job", "paused")


def apply(config: str, repo: Optional[Path] = None, python: Optional[str] = None) -> Dict[str, Any]:
    """Take in origin/main: only on a clean `main` that fast-forwards to it. The new code must pass `orch validate` on
    this config before it is kept; otherwise the checkout goes back to the commit it was on. A change to
    pyproject.toml (dependencies) is left to me. {"ok", "updated", "old", "new", "error"}"""
    repo = repo or REPO

    def g(*a: str, timeout: int = 60) -> str:
        return _git(*a, timeout=timeout, repo=repo)

    try:
        if g("rev-parse", "--abbrev-ref", "HEAD") != "main":
            return {"ok": False, "updated": False, "error": "the orch checkout is not on main"}
        if g("status", "--porcelain", "--untracked-files=no"):
            return {"ok": False, "updated": False, "error": "the orch checkout has uncommitted changes"}
        g("fetch", "--quiet", "origin", "main")
        old, new = g("rev-parse", "HEAD"), g("rev-parse", "origin/main")
        if old == new or not int(g("rev-list", "--count", "HEAD..origin/main") or 0):
            return {"ok": True, "updated": False, "old": old[:7], "new": new[:7]}
        if int(g("rev-list", "--count", "origin/main..HEAD") or 0):
            return {"ok": False, "updated": False, "old": old[:7], "new": new[:7],
                    "error": "the local main has commits origin/main doesn't: it can't fast-forward"}
        if g("diff", "--name-only", old, new, "--", "pyproject.toml"):
            return {"ok": False, "updated": False, "old": old[:7], "new": new[:7],
                    "error": "the update changes pyproject.toml (dependencies): pull and reinstall by hand"}
        g("merge", "--ff-only", "--quiet", "origin/main")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        return {"ok": False, "updated": False, "error": f"git: {e}"}
    bad = validate_code(config, repo, python)
    if bad:
        try:
            g("reset", "--keep", "--quiet", old)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            bad += f"; going back to {old[:7]} failed too: {e}"
        return {"ok": False, "updated": False, "old": old[:7], "new": new[:7],
                "error": f"the new version failed `orch validate`, so orch stays on {old[:7]}: {bad}"}
    return {"ok": True, "updated": True, "old": old[:7], "new": new[:7]}


def validate_code(config: str, repo: Optional[Path] = None, python: Optional[str] = None) -> str:
    """Run `orch validate` with the code on disk, in a process of its own: "" when it loads and accepts the config."""
    try:
        chk = subprocess.run([python or sys.executable, "-m", "orch", "validate", config], cwd=str(repo or REPO),
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if chk.returncode == 0 else (chk.stderr or chk.stdout).strip()[-600:] or f"exit {chk.returncode}"


def head(repo: Optional[Path] = None) -> str:
    """The commit the checkout is on ("" when it can't be read)."""
    try:
        return _git("rev-parse", "HEAD", repo=repo)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return ""


def restart_argv(argv: List[str], port: Optional[int] = None) -> List[str]:
    """The command to restart `orch run` with after an update: the same arguments, with --resume, and the board on the
    port it has now (it may have moved past a busy one)."""
    args = list(argv[1:])
    if "run" in args and "--resume" not in args:
        args.insert(args.index("run") + 1, "--resume")
    if port:
        for i, x in enumerate(args):
            if x == "--port" and i + 1 < len(args):
                del args[i:i + 2]
                break
            if x.startswith("--port="):
                del args[i]
                break
        args += ["--port", str(port)]
    return [sys.executable, "-m", "orch", *args]
