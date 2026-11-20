"""Checking for a newer orch on main (detection only; pulling and restarting come later).

The board asks for a check only at a safe point (the run waits at a gate or a human task, or has ended), so a fetch
never happens during a turn. The check reports the commit orch runs from and whether origin/main is ahead of it.
"""
import subprocess
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent.parent  # the checkout orch runs from
SAFE_STATUSES = ("awaiting_approval", "waiting_job", "done", "stopped", "error", "interrupted", "paused", "")


def _git(*args: str, timeout: int = 20) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)
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
