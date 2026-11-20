"""B5 progress checks: helpers for the quick checks a monitor runs while a worker's turn or job runs.

A check is evidence, never a verdict: what the monitor saw ("item 2: done, results/x.json has 120 rows"; "log quiet
for 25 min"), ending with `<status>ok|quiet|off-track|stopped</status>`. orch reads the status to decide whether the
critic is woken now (quiet, off track, stopped) or reads the report at its next turn (ok).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import routing as RT

OK, QUIET, OFF_TRACK, STOPPED = "ok", "quiet", "off-track", "stopped"
PROBLEMS = (QUIET, OFF_TRACK, STOPPED)
_ALIASES = {"ok": OK, "fine": OK, "good": OK, "active": OK, "running": OK, "moving": OK,
            "quiet": QUIET, "stalled": QUIET, "idle": QUIET, "stuck": QUIET,
            "off-track": OFF_TRACK, "offtrack": OFF_TRACK, "off track": OFF_TRACK, "off_track": OFF_TRACK,
            "stopped": STOPPED, "died": STOPPED, "dead": STOPPED, "crashed": STOPPED, "error": STOPPED,
            "exited": STOPPED}
# a report without a status tag: words that point at a problem
_PROBLEM_WORDS = re.compile(r"\b(quiet|stall(ed)?|stuck|no (new )?(output|activity|progress)|died|dead|crash(ed)?|"
                            r"traceback|killed|off[- ]track|exited with|not running|gone)\b", re.I)
# verdict words a monitor must not give (the critic decides): the tag, "VERDICT:", and PASS/FAIL in capitals
_VERDICT_TAG = re.compile(r"<verdict>.*?</verdict>", re.S | re.I)
_VERDICT_WORD = re.compile(r"\**\b(?:VERDICT\s*:\s*)?(?:PASS(?:ED|ES)?|FAIL(?:ED|S)?)\b\**[,;]?[ \t]*")
_VERDICT_LABEL = re.compile(r"\bVERDICT\b\s*:?", re.I)
_STATUS = re.compile(r"<status>(.*?)</status>", re.S | re.I)
_EMPTY = re.compile(r"^[\s\-*•:|.,;]*$")


def parse_status(text: str) -> Tuple[str, bool]:
    """(status, from_tag): the report's `<status>` (normalised), else a guess from its words."""
    found = _STATUS.findall(text or "")
    v = " ".join(found[-1].split()).strip(" `*.").lower() if found else ""
    if v:
        return _ALIASES.get(v, _ALIASES.get(v.replace("_", "-"), STOPPED if "die" in v else QUIET)), True
    return (QUIET if _PROBLEM_WORDS.search(text or "") else OK), False


def is_problem(status: str) -> bool:
    return status in PROBLEMS


def clean_report(text: str, agent: str = "", run_dir: Any = None) -> Tuple[str, List[str]]:
    """The report as the critic reads it, and what was taken out of it: the status tag (orch reads it), verdict
    words (a monitor gives evidence, never a verdict: flagged and removed) and lines about the tooling that runs the
    team (flagged and dropped: the critic never hears of it)."""
    flags: List[str] = []
    body = _STATUS.sub("", text or "")
    if _VERDICT_TAG.search(body):
        flags.append("verdict tag")
        body = _VERDICT_TAG.sub("", body)
    out = []
    for ln in body.splitlines():
        if _VERDICT_WORD.search(ln) or _VERDICT_LABEL.search(ln):
            flags.append("verdict word: " + ln.strip()[:80])
            ln = _VERDICT_LABEL.sub("", _VERDICT_WORD.sub("", ln)).rstrip()
            if _EMPTY.match(ln):
                continue
        if RT.tooling_mentions(ln, agent, run_dir):
            flags.append("tooling: " + ln.strip()[:80])
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), flags


def has_verdict(text: str) -> bool:
    return bool(_VERDICT_TAG.search(text or "") or _VERDICT_WORD.search(text or ""))


def latest_live(run_dir: Path, agent: str, since: float = 0.0) -> Optional[Path]:
    """The newest stream file of an agent's turns (live/<turn>.jsonl) written since `since`."""
    best: Optional[Path] = None
    best_m = since
    for p in (Path(run_dir) / "live").glob(f"r*-t*-{agent}.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m >= best_m:
            best, best_m = p, m
    return best


def activity(run_dir: Path, agent: str, since: float, now: float, lines: int = 5) -> Tuple[Optional[float], List[str]]:
    """(seconds since the turn last wrote to its stream, or None when it has written nothing since `since`; its last
    few tool calls and messages, one short line each)."""
    p = latest_live(run_dir, agent, since)
    if p is None:
        return None, []
    last, shown = None, []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if ev.get("k") in ("tool", "text") and ev.get("s"):
                last = float(ev.get("ts") or last or 0)
                s = " ".join(str(ev["s"]).split())
                shown.append(s[:140] + ("…" if len(s) > 140 else ""))
    except OSError:
        return None, []
    if last is None or last < since:
        return None, []
    tail: List[str] = []
    for s in shown[::-1]:  # the last few distinct ones, oldest first
        if s not in tail:
            tail.append(s)
        if len(tail) >= lines:
            break
    return max(0.0, now - last), tail[::-1]


def minutes(seconds: float) -> int:
    return max(0, int(round(seconds / 60.0)))
