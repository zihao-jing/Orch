"""The one parser of replies (LAB-1..5, MSG-10): text and role in, `Reply` out. Pure.

Labels may stand anywhere in a reply. Code (fenced or inline) and quoted lines ("> ...") are only quoted, never read as
labels. A label that cannot be read is reported in `Reply.broken`, never guessed; its text is taken out of `untagged`.
Who may use which label is decided here per role; whom a message may go to is routing's business.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .contracts import (BrokenLabel, Decision, Health, OutMessage, Problem, Question, Reply, Role)

MANAGER_LABELS = ("message", "question", "pause", "rubric", "score", "decision", "summary", "state")
CRITIC_LABELS = ("message", "silent")
LABELS = {Role.MANAGER: MANAGER_LABELS, Role.CRITIC: CRITIC_LABELS}
# every name some role used to have: one the role no longer has is REMOVED
KNOWN = set(MANAGER_LABELS) | {"silent", "plan", "checklist", "verdict", "wait-job", "no-wait", "preference",
                               "instruction", "workers", "to", "discussion-resolution"}

_TAG = re.compile(r"<(/?)([a-z][a-z-]*)((?:\s+[\w-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>/]+))*)\s*(/?)>", re.I)
_ATTR = re.compile(r"([\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>/]+))")
_CODE = re.compile(r"(?:```|~~~).*?(?:```|~~~|\Z)|`[^`\n]*`", re.S)
_QUOTE = re.compile(r"^[ \t]*>.*$", re.M)
_CITATION = re.compile(r":codex-file-citation(?:\[[^\]]*\])?\{([^}]*)\}")
_PATH = re.compile(r"path=(.*?)(?=\s+[\w-]+=|$)", re.S)
_OPTION = re.compile(r"^\s*(?:[-*]|\(?[A-Za-z0-9]{1,2}[).:])\s+(\S.*)$")
_HEALTH = re.compile(r"\b(healthy|quiet|off[-_ ]?track|stopped)\b", re.I)


def clean(text: str) -> str:
    """MSG-10: citation codes become plain paths."""
    def path(m: "re.Match[str]") -> str:
        p = _PATH.search(m.group(1).strip())
        return p.group(1).strip() if p else ""
    return _CITATION.sub(path, text or "")


def _blank(m: "re.Match[str]") -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def _masked(text: str) -> str:
    """Same length, with code and quoted lines blanked out."""
    return _QUOTE.sub(_blank, _CODE.sub(_blank, text))


def _attrs(raw: str) -> Dict[str, str]:
    return {a.group(1).lower(): next(g for g in a.groups()[1:] if g is not None) for a in _ATTR.finditer(raw or "")}


def _one_line(s: str) -> str:
    return " ".join(s.split())[:120]


def _scan(text: str, names: Tuple[str, ...]):
    """[(name, attrs, body, start, end, problem)] for every label-looking tag outside code and quotes."""
    m_text = _masked(text)
    tags = [m for m in _TAG.finditer(m_text) if m.group(2).lower() in KNOWN]
    found, i = [], 0
    while i < len(tags):
        t = tags[i]
        name, attrs = t.group(2).lower(), _attrs(t.group(3))
        i += 1
        if t.group(1):  # a close with no open
            found.append((name, {}, "", t.start(), t.end(), Problem.UNCLOSED, t.group(0)))
            continue
        if name not in names:
            # a removed label still takes its whole block with it, so its inner text is not read
            close = next((c for c in tags[i:] if c.group(1) and c.group(2).lower() == name), None)
            end = close.end() if close and not t.group(4) and name != "silent" else t.end()
            while i < len(tags) and tags[i].start() < end:
                i += 1
            found.append((name, attrs, "", t.start(), end, Problem.REMOVED, t.group(0)))
            continue
        if t.group(4) or name == "silent":  # <x/>, or <silent> alone
            end = t.end()
            if name == "silent" and i < len(tags) and tags[i].group(1) and tags[i].group(2).lower() == "silent" \
                    and not m_text[end:tags[i].start()].strip():
                end = tags[i].end()
                i += 1
            found.append((name, attrs, "", t.start(), end, None, t.group(0)))
            continue
        close = next((c for c in tags[i:] if c.group(1) and c.group(2).lower() == name), None)
        again = next((c for c in tags[i:] if not c.group(1) and c.group(2).lower() == name), None)
        if close and not (again and again.start() < close.start()):
            body, end = text[t.end():close.start()], close.end()
            while i < len(tags) and tags[i].start() < end:  # tags inside a body are part of it
                i += 1
            found.append((name, attrs, body.strip(), t.start(), end, None, t.group(0)))
        else:  # left open: runs to the next label or the end
            end = tags[i].start() if i < len(tags) else len(text)
            found.append((name, attrs, text[t.end():end].strip(), t.start(), end, Problem.UNCLOSED, t.group(0)))
    return found


def _question(body: str) -> Question:
    lines, options = [], []
    for line in body.splitlines():
        m = _OPTION.match(line)
        (options if m else lines).append(m.group(1).strip() if m else line)
    return Question(" ".join(" ".join(lines).split()), options)


def _lines(body: str) -> List[str]:
    out = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip() for ln in body.splitlines()]
    return [x for x in out if x]


def parse(text: str, role: Role) -> Reply:
    """A reply as the role may write it. Worker, monitor and auditor have no labels: the whole text is the reply."""
    role = Role(role)
    text = clean(text)
    reply = Reply(text=text, untagged=text.strip())
    if role == Role.MONITOR:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        m = _HEALTH.search(first)
        reply.health = Health(re.sub(r"[-_ ]", "_", m.group(1).lower())) if m else None
    names = LABELS.get(role)
    if names is None:
        return reply
    cut: List[Tuple[int, int]] = []
    for name, attrs, body, start, end, problem, raw in _scan(text, names):
        cut.append((start, end))
        if problem is None:
            problem = _read(reply, name, attrs, body)
        if problem is not None:
            reply.broken.append(BrokenLabel(_one_line(raw if name not in names else raw + body), problem, body))
    parts, pos = [], 0
    for start, end in sorted(cut):
        parts.append(text[pos:start])
        pos = max(pos, end)
    parts.append(text[pos:])
    reply.untagged = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    return reply


def _read(reply: Reply, name: str, attrs: Dict[str, str], body: str) -> Optional[Problem]:
    """Put one well-formed label into the reply; a Problem if its content is unusable."""
    if name == "silent":
        reply.silent = True
    elif name == "pause":
        reply.pauses.append(body)
    elif not body:
        return Problem.EMPTY
    elif name == "message":
        if not attrs.get("to", "").strip():
            return Problem.NO_RECIPIENT
        reply.messages.append(OutMessage(attrs["to"].strip(), body))
    elif name == "question":
        q = _question(body)
        if not q.text and not q.options:
            return Problem.EMPTY
        reply.questions.append(q)
    elif name == "decision":
        word = re.sub(r"[^A-Za-z]", "", body).upper()
        if word not in [d.value for d in Decision]:
            return Problem.BAD_VALUE
        reply.decision = Decision(word)
    elif name == "score":
        m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:/\s*5)?\s*", body)
        if not m:
            return Problem.BAD_VALUE
        reply.score = float(m.group(1).replace(",", "."))
    elif name == "rubric":
        reply.rubric = _lines(body)
    elif name == "summary":
        reply.summary = body
    elif name == "state":
        reply.state = body
    return None
