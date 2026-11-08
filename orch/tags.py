"""The tag protocol: how agents mark the parts of a reply that orch acts on.

Every signal is an XML-style tag with a start and an end, so orch never has to guess where a block stops:

    <message to="critic">…</message>   delivered only to that agent
    <question>…</question>             one decision for the level above (one tag per question)
    <pause>…</pause>                   a pause (proposed by the critic, called by the manager)
    <checklist>…</checklist>           the critic's checklist (`- [x]` / `- [ ]` lines)
    <verdict>PASS</verdict>            the critic's verdict
    <score>4.5</score>                 the manager's overall score (out of 5)
    <decision>ACCEPT</decision>        the manager's decision: ACCEPT, REVISE, PAUSE or CONTINUE
    <no-wait pid="123"/>               the worker: don't hold the review for this process

Text outside the tags is for whoever the agent is replying to. Tags inside `code` or ``` fences are only quoted,
not used. A tag left open runs to the next protocol tag or to the end of the reply.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

NAMES = ("message", "question", "pause", "checklist", "verdict", "score", "decision", "no-wait")
_ALT = "|".join(re.escape(n) for n in NAMES)
_OPEN = re.compile(rf"<({_ALT})((?:\s+[\w-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>/]+))*)\s*(/?)>", re.I)
_ATTR = re.compile(r"([\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>/]+))")
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)

Block = Tuple[Dict[str, str], str, int, int]  # (attributes, body, start, end) in the original text


def _masked(text: str) -> str:
    """The text with code spans blanked out (same length), so quoted tags are not seen."""
    return _CODE.sub(lambda m: " " * len(m.group(0)), text)


def parse(text: str) -> List[Tuple[str, Dict[str, str], str, int, int]]:
    """Every protocol tag in order: (name, attributes, body, start, end)."""
    text = text or ""
    m_text = _masked(text)
    opens = list(_OPEN.finditer(m_text))
    out = []
    i = 0
    while i < len(opens):
        o = opens[i]
        name = o.group(1).lower()
        attrs = {a.group(1).lower(): next(g for g in a.groups()[1:] if g is not None) for a in _ATTR.finditer(o.group(2) or "")}
        if o.group(3):  # <no-wait pid="1"/>
            out.append((name, attrs, "", o.start(), o.end()))
            i += 1
            continue
        close = re.compile(rf"</{re.escape(name)}\s*>", re.I).search(m_text, o.end())
        nxt = opens[i + 1].start() if i + 1 < len(opens) else len(text)
        if close and (close.start() <= nxt or not any(x.start() < close.start() and x.group(1).lower() == name
                                                       for x in opens[i + 1:])):
            end, body_end = close.end(), close.start()
            while i + 1 < len(opens) and opens[i + 1].start() < end:  # tags nested inside are part of the body
                i += 1
        else:  # left open: up to the next tag, or the end
            end = body_end = nxt
        out.append((name, attrs, text[o.end():body_end].strip(), o.start(), end))
        i += 1
    return out


def blocks(text: str, name: str, **want: str) -> List[Tuple[Dict[str, str], str]]:
    """[(attributes, body)] of every <name> tag whose attributes match `want` (case-insensitive)."""
    res = []
    for n, attrs, body, _s, _e in parse(text):
        if n == name and all(_norm(attrs.get(k, "")) == _norm(v) for k, v in want.items()):
            res.append((attrs, body))
    return res


def bodies(text: str, name: str, **want: str) -> List[str]:
    return [b for _a, b in blocks(text, name, **want) if b]


def value(text: str, name: str) -> str:
    """The last <name>…</name> value, upper-cased and trimmed (verdict, decision); '' if none."""
    vals = [b for _a, b in blocks(text, name)]
    return re.sub(r"[^\w.]", "", vals[-1]).upper() if vals else ""


def outside(text: str) -> str:
    """The reply without any protocol tag (for whoever the agent is replying to)."""
    text = text or ""
    cut = [(s, e) for _n, _a, _b, s, e in parse(text)]
    keep, pos = [], 0
    for s, e in cut:
        keep.append(text[pos:s])
        pos = max(pos, e)
    keep.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(keep)).strip()


def without_markup(text: str, drop: Tuple[str, ...] = ()) -> str:
    """The reply with the tags themselves removed but their content kept, except the tags named in `drop`."""
    text = text or ""
    parts, pos = [], 0
    for n, attrs, body, s, e in parse(text):
        parts.append(text[pos:s])
        if n not in drop and body:
            parts.append(body)
        pos = max(pos, e)
    parts.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _norm(v: str) -> str:
    return re.sub(r"^the\s+", "", (v or "").strip().lower())


def addressed(attrs: Dict[str, str], *names: str) -> bool:
    """Is a <message to="…"> for one of these names (display name or agent name, "the" ignored)?"""
    to = _norm(attrs.get("to", ""))
    return bool(to) and any(to == _norm(n) for n in names if n)
