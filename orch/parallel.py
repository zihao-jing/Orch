"""B11 parallel workers: the pure parts (tags, the budget rule, the work board, git worktrees).

The run logic lives in `managed.py` (`ph_parallel` and its helpers); this module holds what can be tested alone.

Tags
    <workers n="3" steps="2,3,4">why these steps are independent</workers>
        From the critic: a proposal; it reaches the manager as a note that wakes it (a decision). From the manager:
        its decision (n >= 2 approves that many, n="1" declines), or its own proposal, which it approves itself.
    <to worker="writer-2" step="3" out="results/step3/">instruction</to>
        The critic's reply format for several workers: one section per worker; it assigns that step (and an output
        folder, shown on the work board) and its text is the worker's instruction. In a review of one worker's step,
        text outside the tags goes to that worker, as in a single-worker review.

Budget rule (`decide`): the manager approves alone up to `max_workers` (counting the first worker), as long as the
run has a round left for each of them (every worker turn counts as a round against `max_rounds`) and, when
`parallel_turns` is set, n is within it. Above that the user decides at a gate; in night mode the manager's number
stands up to `workers_hard_cap`. A parallel section stops handing out turns once its worker turns reach
`parallel_turns` (0 = only `max_rounds` caps them).

Work board: `work-m<N>.md` in the critic's folder, rendered by orch from the run state after every change (who does
which step, its status, output folder, workspace, turns, the monitor's last line). The critic reads it instead of
remembering each worker; it may edit only the "Shared limits" section, which is kept across re-renders.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import tags as T

LIMITS_HEAD = "## Shared limits"
LIMITS_EMPTY = "- (none listed yet: add shared limits here, e.g. one GPU, an API rate limit)"
BOARD_HEAD = ("# Work board\n\n"
              "<!-- Kept up to date after every turn and review: who does which step, its status and where its output "
              "goes. Read it instead of keeping track yourself. Edit only the \"Shared limits\" section. -->\n")

# slot statuses
IDLE, READY, WORKING, JOB, REVIEW, DONE, STALLED = "idle", "ready", "working", "job", "review", "done", "stalled"
BUSY = (READY, WORKING, JOB, REVIEW)
STATUS_TEXT = {IDLE: "waiting for a step", READY: "about to start", WORKING: "working", JOB: "background job running",
               REVIEW: "waiting for review", DONE: "finished (no step left)", STALLED: "stopped: too many turns on one step"}


def _int(v: Any, default: int = 0) -> int:
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else default


def parse_steps(text: str) -> List[int]:
    """Step numbers in "2,3,4", "steps 2-4", "2, 3 and 5" (in order, without repeats)."""
    out: List[int] = []
    for a, b in re.findall(r"(\d+)\s*(?:[-–]\s*(\d+))?", text or ""):
        lo, hi = int(a), int(b) if b else int(a)
        for k in range(lo, min(hi, lo + 50) + 1):
            if k not in out:
                out.append(k)
    return out


def workers_tag(text: str) -> Optional[Dict[str, Any]]:
    """The last <workers> tag of a reply: {n, steps, reason}; None without one."""
    blocks = T.blocks(text or "", "workers")
    if not blocks:
        return None
    attrs, body = blocks[-1]
    steps = parse_steps(attrs.get("steps", "")) if attrs.get("steps") else []
    if not steps:
        m = re.search(r"(?i)\bsteps?\s+(\d[\d,\s\-–]*(?:and\s+\d+)?)", body or "")
        steps = parse_steps(m.group(1)) if m else []
    n = _int(attrs.get("n"), 0) or len(steps)  # without n: one worker per step named
    return {"n": n, "steps": steps, "reason": (body or "").strip()}


def to_sections(text: str) -> List[Dict[str, Any]]:
    """The <to worker="…" step="…" out="…"> sections of a reply, in order: [{worker, step, out, text}]."""
    out = []
    for attrs, body in T.blocks(text or "", "to"):
        who = (attrs.get("worker") or attrs.get("to") or "").strip()
        if not who:
            continue
        out.append({"worker": who, "step": _int(attrs.get("step"), 0) or None, "out": (attrs.get("out") or "").strip(),
                    "text": (body or "").strip()})
    return out


def decide(n: int, steps: List[int], max_workers: int, hard_cap: int, rounds_left: int, parallel_turns: int,
           night: bool) -> Tuple[str, int, str]:
    """The budget rule: ("approved" | "gate" | "declined", workers, why). `n` counts the first worker too."""
    if steps:
        n = min(n, len(steps))
    if n < 2:
        return "declined", 1, "fewer than two independent steps" if steps and len(steps) < 2 else "one person"
    limit = max(1, rounds_left)
    within = n <= max_workers and n <= limit and (not parallel_turns or n <= parallel_turns)
    if within:
        return "approved", n, f"within the budget of {max_workers}"
    if night:
        k = max(2, min(n, hard_cap, limit))
        return "approved", k, f"night mode: the manager's decision, within the hard cap of {hard_cap}"
    return "gate", n, f"over the budget of {max_workers} the manager may approve alone"


def step_title(ck: Dict[str, Any], step: Optional[int]) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the checklist step numbered `step`, its title); the n-th step when no title carries that number."""
    if not step:
        return None, ""
    steps = ck.get("steps") or []
    for s in steps:
        m = re.match(r"(?i)step\s*(\d+)", s.get("title") or "")
        if m and int(m.group(1)) == step:
            return s, s["title"]
    if 0 < step <= len(steps):
        return steps[step - 1], steps[step - 1]["title"]
    return None, f"Step {step}"


def keep_limits(old: str) -> str:
    """The "Shared limits" section of an existing board (the critic's own lines), or the empty placeholder."""
    i = (old or "").find(LIMITS_HEAD)
    if i < 0:
        return LIMITS_EMPTY
    body = old[i + len(LIMITS_HEAD):].strip()
    return body or LIMITS_EMPTY


def render_board(par: Dict[str, Any], called: Dict[str, str], ck: Dict[str, Any], old: str = "") -> str:
    """The work board as Markdown."""
    rows = ["| Who (id) | Step | Status | Output | Workspace | Turns | Last check |",
            "|---|---|---|---|---|---|---|"]
    for name, s in par.get("slots", {}).items():
        _st, title = step_title(ck, s.get("step"))
        status = STATUS_TEXT.get(s.get("status", ""), s.get("status", ""))
        if s.get("status") == WORKING and s.get("round"):
            status += f" (round {s['round']})"
        cell = lambda v: str(v or "—").replace("|", "/").replace("\n", " ")  # noqa: E731
        who = called.get(name, name)
        rows.append(f"| {cell(who) + ' ' if who != name else ''}(`{name}`) | {cell(title)} | {cell(status)} | {cell(s.get('out'))} | "
                    f"{cell(s.get('workspace'))} | {s.get('turns', 0)} | {cell(s.get('check'))} |")
    lines = [BOARD_HEAD, "\n".join(rows), ""]
    queue = par.get("queue") or []
    lines.append("Steps not handed out yet: " + (", ".join(step_title(ck, k)[1] for k in queue) if queue else "none")
                 + ".")
    done = [(k, n) for n, s in par.get("slots", {}).items() for k in s.get("done_steps") or []]
    lines.append("Steps done here: " + (", ".join(f"{step_title(ck, k)[1]} ({called.get(n, n)})" for k, n in done)
                                        if done else "none yet") + ".")
    if par.get("worktrees"):
        lines.append("Separate workspaces: each id above works in its own git worktree (its branch is named on the "
                     "line below); merge a passed step's changes into the main repository yourself.")
        for n, s in par.get("slots", {}).items():
            if s.get("branch"):
                lines.append(f"- `{n}`: branch `{s['branch']}` in `{s.get('workspace')}`")
    lines += ["", LIMITS_HEAD, "", keep_limits(old), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- git worktrees (worker_worktrees: true)


def _git(args: List[str], cwd: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or p.stderr or "").strip()


def repo_root(path: str) -> Optional[str]:
    code, out = _git(["rev-parse", "--show-toplevel"], path)
    return out if code == 0 and out else None


def worktree_add(working_dir: str, name: str, run_id: str) -> Optional[Dict[str, str]]:
    """A git worktree for one worker beside the repository (`<repo>-worktrees/<run>-<name>`, branch
    `parallel/<run>-<name>` from HEAD): {path, branch, root, base, cwd}; None when the folder is not a git repository
    or git refuses."""
    root = repo_root(working_dir)
    if not root:
        return None
    code, base = _git(["rev-parse", "HEAD"], root)
    if code != 0:
        return None
    rootp = Path(root)
    path = rootp.parent / f"{rootp.name}-worktrees" / f"{run_id}-{name}"
    branch = f"parallel/{run_id}-{name}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        code, out = _git(["worktree", "add", "-b", branch, str(path), base], root)
        if code != 0:
            code, out = _git(["worktree", "add", str(path), branch], root)  # the branch is left from a stopped run
            if code != 0:
                return None
    try:
        rel = Path(working_dir).resolve().relative_to(Path(root).resolve())
    except ValueError:
        rel = Path(".")
    return {"path": str(path), "branch": branch, "root": root, "base": base, "cwd": str(path / rel)}


def worktree_remove(wt: Dict[str, str]) -> Tuple[bool, str]:
    """Remove a worker's worktree when nothing would be lost: no uncommitted changes, and its branch has no commits
    beyond where it started or is merged into the main repository's HEAD. Otherwise it is kept (False, why)."""
    path, root, branch = wt.get("path", ""), wt.get("root", ""), wt.get("branch", "")
    if not path or not Path(path).exists():
        return True, "already gone"
    code, dirty = _git(["status", "--porcelain"], path)
    if code != 0:
        return False, "git status failed"
    if dirty:
        return False, "it has uncommitted changes"
    code, ahead = _git(["rev-list", "--count", f"{wt.get('base')}..{branch}"], root)
    merged = _git(["merge-base", "--is-ancestor", branch, "HEAD"], root)[0] == 0
    if code == 0 and ahead.strip() != "0" and not merged:
        return False, f"branch {branch} has commits not merged into the main repository"
    code, out = _git(["worktree", "remove", path], root)
    if code != 0:
        return False, out or "git worktree remove failed"
    _git(["branch", "-D" if merged or ahead.strip() == "0" else "-d", branch], root)
    try:
        Path(path).parent.rmdir()  # the worktrees folder, once empty
    except OSError:
        pass
    return True, "removed"
