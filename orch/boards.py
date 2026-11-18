"""Boards: long-lived state in files that agents edit in place, instead of re-writing it in every reply (batch B3).

The golden board (one file per project, `.orch/golden.md` by default) holds the rules, the user's preferences and the
key decisions. The manager owns it and edits it in place; every agent reads it (orch adds it at the end of a message
when it changed, at a new session, and now and then; orch/reminders.py `golden_board`).

The step checklist (one file per milestone, `checklist-m<N>.md` in the critic's folder) holds the critic's plan and
its checklist together. The critic edits it in place:

    ## Standing rules              items that hold for every step
    - [x] protected files unchanged

    ## Step 1: validate the labels  one section per step, in order
    - [x] 40 of 40 labels checked
    - [ ] disagreements listed

A step is done when every one of its items is `[x]`; a step with no items yet counts as done only when its heading is
marked (`## [x] Step 1: …`). PASS needs every step done and every standing item ticked. The worker never sees it.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .report import CHECKBOX

GOLDEN_LIMIT = 8000  # characters; above it the board warns and the manager is asked once to prune
CHECKLIST_CAP = 25  # items in the current step's list (standing rules included); above it the critic is asked once to merge

GOLDEN_HEAD = ("# Golden board\n\n"
               "<!-- The rules, preferences and key decisions for this work. Every agent reads it as standing rules. "
               "One short line per entry, written about the work; merge entries that overlap and remove what no "
               "longer holds. -->\n")

CHECKLIST_HEAD = ("# Checklist\n\n"
                  "<!-- One \"## Step N: …\" section per step, in order, each with its items as \"- [ ]\" / \"- [x]\" "
                  "lines. A step is done when all its items are [x] (a step with no items yet only as "
                  "\"## [x] Step N: …\"). Items under \"## Standing rules\" hold for every step. Edit this file in "
                  "place; don't paste it into replies. -->\n")

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_HEADING = re.compile(r"^\s{0,3}#{2,4}\s+(.*?)\s*#*\s*$")
_MARK = re.compile(r"^\[( |x|X|✓|✔)\]\s*")
_STEP = re.compile(r"(?i)^step\b")


def digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def read(path: Optional[Path]) -> str:
    try:
        return Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return ""


# --------------------------------------------------------------------------- the golden board


def ensure_golden(path: Path) -> bool:
    """Create the golden board with its header if it does not exist yet (True when created)."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GOLDEN_HEAD, encoding="utf-8")
    return True


def golden_body(text: str) -> str:
    """The board as agents read it: no comments, no top title; '' when nothing is on it yet."""
    body = _COMMENT.sub("", text or "").strip()
    lines = body.split("\n")
    if lines and re.match(r"^#\s+\S", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- the step checklist


def _item(line: str) -> Optional[Tuple[bool, str]]:
    m = CHECKBOX.match(line)
    return (m.group(1) != " ", line[m.end():].strip()) if m else None


def parse_checklist(text: str) -> Dict[str, Any]:
    """{"steps": [{title, mark, items: [(done, text)]}], "standing": [(done, text)]}. A file without step sections
    is one step (the whole list)."""
    steps: List[Dict[str, Any]] = []
    standing: List[Tuple[bool, str]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in _COMMENT.sub("", text or "").split("\n"):
        h = _HEADING.match(ln)
        if h:
            title = h.group(1).strip()
            m = _MARK.match(title)
            plain = title[m.end():].strip() if m else title
            if _STEP.match(plain):
                cur = {"title": plain, "mark": (m.group(1) != " ") if m else None, "items": []}
                steps.append(cur)
            else:
                cur = None  # standing rules (or any other section): items that hold for every step
            continue
        it = _item(ln)
        if it:
            (cur["items"] if cur is not None else standing).append(it)
    if not steps and standing:  # a plain list, without steps: the whole list is the current step's
        steps, standing = [{"title": "the current step", "mark": None, "items": standing, "implicit": True}], []
    return {"steps": steps, "standing": standing}


def step_done(s: Dict[str, Any]) -> bool:
    """All its items are [x]. A heading marked `[ ]` keeps a step open (written for an older-protocol plan that had
    not ticked it); a step without items is done only when its heading is marked `[x]`."""
    items = s.get("items") or []
    if s.get("mark") is False:
        return False
    return all(d for d, _ in items) if items else bool(s.get("mark"))


def current_index(ck: Dict[str, Any]) -> int:
    """The step the work is on: the first one not done (the last one when all are done); -1 without steps."""
    steps = ck.get("steps") or []
    for i, s in enumerate(steps):
        if not step_done(s):
            return i
    return len(steps) - 1


def plan_lines(ck: Dict[str, Any]) -> str:
    """The steps as plan lines ("- [x] Step 1: …"), '' without steps (a plain list is no plan)."""
    if all(s.get("implicit") for s in ck.get("steps") or []):
        return ""
    return "\n".join(f"- [{'x' if step_done(s) else ' '}] {s['title']}" for s in ck.get("steps") or [])


def current_list(ck: Dict[str, Any]) -> str:
    """The checklist that gates the current step: the standing rules and the current step's items."""
    i = current_index(ck)
    items = list(ck.get("standing") or []) + (list(ck["steps"][i]["items"]) if i >= 0 else [])
    return "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in items)


def _lines(items: List[Tuple[bool, str]]) -> str:
    return "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in items or [])


def render_checklist(ck: Dict[str, Any]) -> str:
    out = [CHECKLIST_HEAD]
    steps = ck.get("steps") or []
    if steps and all(s.get("implicit") for s in steps):  # a plain list stays a plain list
        return CHECKLIST_HEAD + "\n" + _lines(list(ck.get("standing") or []) + list(steps[0]["items"])) + "\n"
    if ck.get("standing"):
        out.append("## Standing rules\n\n" + "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in ck["standing"]) + "\n")
    for s in steps:
        mark = "[ ] " if s.get("mark") is False else "[x] " if s.get("mark") and not s.get("items") else ""
        body = "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in s.get("items") or [])
        out.append(f"## {mark}{s['title']}\n" + (f"\n{body}\n" if body else ""))
    return "\n".join(out)


def _norm(title: str) -> str:
    t = re.sub(r"(?i)^step\s*\d*\s*[:.)–—-]?\s*", "", title or "")
    return re.sub(r"\W+", " ", t.lower()).strip()


def merge_blocks(old: Optional[Dict[str, Any]], plan: str, checklist: str) -> Dict[str, Any]:
    """The file after a reply that still wrote a `<plan>` and/or `<checklist>` block (the older protocol): the plan
    sets the steps (done steps keep only their mark; open ones keep their items, matched by title), the checklist
    becomes the current step's items."""
    ck = copy.deepcopy(old) if old else {"steps": [], "standing": []}
    cur: Optional[int] = None
    if plan.strip():
        by = {_norm(s["title"]): s for s in ck.get("steps") or []}
        steps = []
        for i, ln in enumerate(x for x in plan.split("\n") if CHECKBOX.match(x)):
            done, title = _item(ln)
            title = title if _STEP.match(title) else f"Step {i + 1}: {title}"
            o = by.get(_norm(title))
            # the plan's own mark holds: a done step needs no items, an open one stays open until the plan ticks it
            steps.append({"title": title, "mark": done, "items": [] if done else list((o or {}).get("items") or [])})
            if not done and cur is None:
                cur = i
        ck["steps"] = steps
    if checklist.strip():
        items = [it for it in (_item(x) for x in checklist.split("\n")) if it]
        if not ck["steps"]:
            ck["steps"] = [{"title": "the current step", "mark": None, "items": [], "implicit": True}]
        if cur is None:
            cur = current_index(ck) if not plan.strip() else len(ck["steps"]) - 1
        ck["steps"][cur]["items"] = items
    return ck
