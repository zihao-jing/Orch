"""Reusable checks that no agent learns about the layers above it (design principle 3).

Use from a test:

    from role_view_checks import assert_no_role_leaks
    assert_no_role_leaks(self, rec)

The critic must not see "manager", "orch" or "real user"; the worker must not see those or "critic". The display
names of the agents above count too. Allowed exceptions:
- the address inside `<message to="…">` tag syntax (an internal address, explained neutrally in the protocol);
- file paths such as `.orch/agents/critic/state.md` (where orch keeps an agent's files).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ABOVE = {  # words a role must never read
    "verifier": ("manager", "orch", "real user"),
    "worker": ("manager", "orch", "real user", "critic"),
}
_TAG = re.compile(r"<message\s+to\s*=\s*\"[^\"]*\"[^>]*>", re.I)
_PATH = re.compile(r"\S*[./]orch/\S*")


def _scrub(text: str) -> str:
    return _PATH.sub(" ", _TAG.sub("<message>", text or ""))


def leaks(text: str, role: str, names: Iterable[str] = ()) -> List[Tuple[str, str]]:
    """[(word, context)] for every forbidden word (or display name of an agent above) in `text`."""
    clean = _scrub(text)
    out = []
    for w in list(ABOVE.get(role, ())) + [n for n in names if n]:
        for m in re.finditer(rf"(?i)\b{re.escape(w)}\b", clean):
            out.append((w, clean[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")))
    return out


def names_above(mon: Any, role: str) -> List[str]:
    """Display names (and agent names) of the agents above `role`, from a MonitorConfig."""
    up = {"verifier": ("manager",), "worker": ("manager", "verifier")}.get(role, ())
    out = []
    for a in mon.agents:
        if a.role in up:
            out += [a.display_name, a.name]
    return [n for n in out if n and n.lower() not in ("the",)]


def run_turns(run_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted((Path(run_dir) / "rounds").glob("r*/t*.json")):
        if p.name.endswith(".input.json"):
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def role_leaks(rec: Any, mon: Optional[Any] = None, fields: Tuple[str, ...] = ("input", "system_prompt")) -> List[str]:
    """Every leak in the critic's and the workers' turns of a run: '<turn> <field>: <word> … context …'."""
    found = []
    for t in run_turns(rec.run_dir):
        role = t.get("role")
        if role not in ABOVE:
            continue
        names = names_above(mon, role) if mon is not None else []
        for f in fields:
            for w, ctx in leaks(t.get(f) or "", role, names):
                found.append(f"{t.get('turn_id')} {f}: {w!r} in …{ctx}…")
    return found


def assert_no_role_leaks(tc: Any, rec: Any, mon: Optional[Any] = None, also: Iterable[str] = ()) -> None:
    """Fail `tc` if a critic or worker turn names a layer above it, or carries any of the strings in `also`
    (e.g. a marker put in the manager's text for the user)."""
    problems = role_leaks(rec, mon)
    for t in run_turns(rec.run_dir):
        if t.get("role") in ABOVE:
            for s in also:
                if s in (t.get("input") or ""):
                    problems.append(f"{t.get('turn_id')} input carries {s!r}")
    tc.assertFalse(problems, "layer above leaked:\n" + "\n".join(problems))
