"""Short system reminders at intervals (managed mode).

A reminder keeps a rule in view without a fixed block on every turn. Each one has an id, a short text (one or two
sentences; a mapping role -> text when the roles need different words), the agents it goes to (role names, agent
names or "all") and a schedule: ``every: N`` (every N-th turn of that agent) or ``random: [lo, hi]`` (the next one
falls lo..hi turns later, drawn each time). Counters are per agent and per reminder, kept in the run's state, so a
resumed run carries on where it was.

In the project config (any agent.yaml, or ``controller:`` of a config file)::

    reminders:
      concise: {every: 4}          # change a default's schedule
      report_length: false         # turn a default off
      cite_files:                  # add one
        text: "Name the file behind every number you report."
        to: [worker]
        random: [3, 8]
      chat_voice: {every: 5}       # board chat reminders (`only: [chat]`) count chat messages; the first gets it

``reminders: false`` turns them all off. The older ``tag_reminder: <chance>`` still works: it sets the protocol
reminder's schedule (0 turns it off). Texts may use {{critic}} and {{worker}} (the display names); a reminder for the
critic or a worker must never name an agent above it.

Reminders go at the end of the message, each wrapped in <system-reminder>…</system-reminder>; automatic checks read
messages with ``strip`` applied, so a reminder is never taken for the request.
"""
from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional

ROLES = ("manager", "critic", "worker")
ROLE_OF = {"manager": "manager", "verifier": "critic", "critic": "critic", "worker": "worker"}

# the golden board (orch/boards.py): its text is this reminder's intro, followed by the board itself (the worker gets
# only its Boundaries section). Besides its schedule, it goes to an agent when the board changed (as a diff) or the
# agent's session is new (in full); nothing while there is nothing to send
GOLDEN_BOARD = "golden_board"
STYLE_RULES = "style_rules"  # a few rules sampled from orch/prompts/style-rules.md, written at send time
GOLDEN_PRUNE = "golden_prune"  # the manager, once each time the board grows past its size limit (not in the registry)

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "concise": {
        "to": ["all"], "random": [3, 6],
        "text": {
            "manager": "Write briefly, in plain, natural language. I read every word.",
            "critic": "Write briefly, in plain, natural language. Every word you write is read, by me and by {{worker}}.",
            "worker": "Write briefly, in plain, natural language. I read every word.",
        },
    },
    "protocol": {
        "to": ["all"], "random": [3, 10],
        "text": {
            "manager": "Only `<message to=\"{{critic}}\">…</message>` reaches {{critic}}: write it as \"I\", as its user, "
                       "and keep me and our conversation out of it. Text outside tags is for me.",
            "critic": "In a review, text outside tags goes to {{worker}}; what is for me goes in "
                      "`<message to=\"you\">…</message>`. Write to {{worker}} as \"I\", one decision per `<question>`.",
        },
    },
    "report_length": {"to": ["worker"], "every": 3, "text": "Keep the report short; put details in files."},
    # board chat with the manager (replaces the fixed manager_direct_voice block on every chat message)
    "chat_voice": {
        "to": ["manager"], "every": 3, "only": ["chat"],
        "text": {"manager": "In a message to {{critic}}, write as \"I\" and give everything as your own; keep me, "
                            "our conversation and your role out of it, and your reply to me outside that tag."},
    },
    # no fixed text: `style_text` samples 2-3 of the role's rules from the style file, every 2-5 turns
    STYLE_RULES: {"to": ["all"], "random": [2, 5]},
    GOLDEN_BOARD: {
        "to": ["all"], "every": 10,
        "text": {
            "manager": "The golden board (`{{golden_board}}`) as it stands:",
            "critic": "My standing rules for this work; they hold until I change them:",
            "worker": "My boundaries for this work; they hold until I change them:",  # the board's Boundaries only
        },
    },
}

_BLOCK = re.compile(r"[ \t]*<system-reminder>.*?(?:</system-reminder>|\Z)[ \t]*\n?", re.S)


class ReminderError(ValueError):
    pass


def wrap(text: str) -> str:
    return f"<system-reminder>\n{text.strip()}\n</system-reminder>"


def append(prompt: str, text: str) -> str:
    """The prompt with one reminder at its end."""
    return f"{prompt.rstrip()}\n\n{wrap(text)}\n" if text.strip() else prompt


def strip(text: str) -> str:
    """The text without orch's <system-reminder> blocks: what automatic checks should read."""
    return re.sub(r"\n{3,}", "\n\n", _BLOCK.sub("", text or "")).strip()


def _schedule(spec: Dict[str, Any], where: str) -> Dict[str, Any]:
    if spec.get("every") is not None and spec.get("random") is not None:
        raise ReminderError(f"{where}: give `every` or `random`, not both")
    if spec.get("random") is not None:
        r = spec["random"]
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            raise ReminderError(f"{where}: `random` takes [lo, hi]")
        lo, hi = int(r[0]), int(r[1])
        if lo < 1 or hi < lo:
            raise ReminderError(f"{where}: `random: [lo, hi]` needs 1 <= lo <= hi")
        return {"random": [lo, hi]}
    every = int(spec.get("every") if spec.get("every") is not None else 5)
    if every < 1:
        raise ReminderError(f"{where}: `every` must be >= 1")
    return {"every": every}


def _targets(v: Any) -> List[str]:
    if v is None:
        return ["all"]
    items = [v] if isinstance(v, str) else list(v)
    return [ROLE_OF.get(str(x).strip().lower(), str(x).strip()) for x in items if str(x).strip()]


def _from_chance(p: float) -> Optional[Dict[str, Any]]:
    """The old `tag_reminder` chance per turn as a schedule with about the same mean gap (None: off)."""
    if p <= 0:
        return None
    if p >= 1:
        return {"every": 1}
    return {"random": [max(1, round(0.5 / p)), max(1, round(1.5 / p))]}


def parse(raw: Any, tag_reminder: Optional[float] = None, where: str = "reminders") -> List[Dict[str, Any]]:
    """The reminders of a project: the defaults, with the config's `reminders:` mapping (and the older
    `tag_reminder` chance) applied. Each: {id, text, to, every | random, skip}."""
    if raw is False or (isinstance(raw, str) and raw.strip().lower() in ("off", "none", "false")):
        return []
    if raw is not None and not isinstance(raw, dict):
        raise ReminderError(f"{where}: a mapping of id -> settings (or false)")
    specs: Dict[str, Optional[Dict[str, Any]]] = {k: dict(v) for k, v in DEFAULTS.items()}
    own = dict(raw or {})
    if tag_reminder is not None and not (isinstance(own.get("protocol"), dict)
                                         and ({"every", "random"} & set(own["protocol"]))):
        sched = _from_chance(float(tag_reminder))
        if sched is None:
            specs["protocol"] = None
        else:
            specs["protocol"] = {**{k: v for k, v in DEFAULTS["protocol"].items() if k not in ("every", "random")},
                                 **sched}
    for rid, v in own.items():
        rid = str(rid)
        w = f"{where}.{rid}"
        if v is False or (isinstance(v, str) and v.strip().lower() in ("off", "false")):
            specs[rid] = None
            continue
        if v is None:
            continue
        if isinstance(v, str):
            v = {"text": v}
        if not isinstance(v, dict):
            raise ReminderError(f"{w}: settings (text, to, every / random, skip, only) or false")
        unknown = set(v) - {"text", "to", "every", "random", "skip", "only", "enabled"}
        if unknown:
            raise ReminderError(f"{w}: unknown keys: {', '.join(sorted(unknown))}")
        if v.get("enabled") is False:
            specs[rid] = None
            continue
        base = dict(specs.get(rid) or DEFAULTS.get(rid) or {})
        if "every" in v or "random" in v:
            base.pop("every", None)
            base.pop("random", None)
        base.update({k: x for k, x in v.items() if k != "enabled"})
        if not base.get("text") and rid not in DEFAULTS:
            raise ReminderError(f"{w}: needs a `text`")
        specs[rid] = base
    out = []
    for rid, spec in specs.items():
        if spec is None:
            continue
        w = f"{where}.{rid}"
        text = spec.get("text") or ""
        if isinstance(text, dict):
            text = {ROLE_OF.get(str(k).lower(), str(k)): str(x).strip() for k, x in text.items()}
        else:
            text = str(text).strip()
        out.append({"id": rid, "text": text, "to": _targets(spec.get("to")), **_schedule(spec, w),
                    "skip": [str(x) for x in (spec.get("skip") or [])],
                    "only": [str(x) for x in (spec.get("only") or [])]})
    return out


def applies(rem: Dict[str, Any], purpose: str) -> bool:
    """Does this reminder go with a turn of this purpose? `skip` leaves purposes out; `only` limits it to some
    (e.g. `only: [chat]`: board chat messages, which are not round turns)."""
    if purpose in rem.get("skip", ()):
        return False
    return not rem.get("only") or purpose in rem["only"]


def chat_due(rem: Dict[str, Any], n: int) -> bool:
    """For board chat, counted by the chat messages of the agent's current session (n: 0 for the first): the first
    message always gets it, then every `every`-th (with `random: [lo, hi]`, every lo-th). Board chat keeps no counters
    of its own, so the count comes from the chat log."""
    step = int(rem.get("every") or (rem.get("random") or [1])[0] or 1)
    return n % max(1, step) == 0


def text_for(rem: Dict[str, Any], role: str) -> str:
    t = rem.get("text") or ""
    return (t.get(role) or t.get("all") or "") if isinstance(t, dict) else t


def targets(rem: Dict[str, Any], agent_name: str, role: str) -> bool:
    to = rem.get("to") or ["all"]
    return "all" in to or role in to or agent_name in to


def _key(rem: Dict[str, Any]) -> str:
    return f"every:{rem['every']}" if rem.get("every") else "random:{}-{}".format(*rem["random"])


def _gap(rem: Dict[str, Any], rng: Any) -> int:
    if rem.get("every"):
        return int(rem["every"])
    lo, hi = rem["random"]
    return rng.randint(lo, hi)


def due(state: Dict[str, Any], rem: Dict[str, Any], rng: Any = random) -> bool:
    """Count one turn of an agent for this reminder and say whether the reminder goes with it. `state` is that agent's
    counters ({id: {n, due, sched}}), kept in the run state. A changed schedule starts its gap from this turn."""
    s = state.setdefault(rem["id"], {"n": 0})
    key = _key(rem)
    if s.get("sched") != key or "due" not in s:
        s["sched"] = key
        s["due"] = int(s.get("n", 0)) + _gap(rem, rng)
    s["n"] = int(s.get("n", 0)) + 1
    if s["n"] >= s["due"]:
        s["due"] = s["n"] + _gap(rem, rng)
        return True
    return False


STYLE_HEADING = "How I want you to write and communicate:"


def style_pool(text: str, role: str) -> List[str]:
    """A role's rules in the style file: the "## All" lines and those under its own role's heading (manager, critic,
    worker). The same rules open the system prompt and feed the reminder."""
    want = {"all", ROLE_OF.get(role, role)}
    out: List[str] = []
    section = ""
    for line in re.sub(r"<!--.*?-->", "", text or "", flags=re.S).splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
        elif section in want and line.startswith("- ") and line[2:].strip():
            out.append(line[2:].strip())
    return out


def style_section(text: str, role: str) -> str:
    """The system prompt's part of the style file for this role ('' when it has none)."""
    pool = style_pool(text, role)
    return STYLE_HEADING + "\n\n" + "\n".join(f"- {x}" for x in pool) if pool else ""


def style_text(text: str, role: str, rng: Any = random) -> str:
    """One reminder of 2-3 of this role's rules, drawn at random ('' when the file has none)."""
    pool = style_pool(text, role)
    if not pool:
        return ""
    return "\n".join(f"- {x}" for x in rng.sample(pool, min(len(pool), rng.randint(2, 3))))
