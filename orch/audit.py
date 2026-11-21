"""The auditor's view of a run: what was passed between the agents, compared in advance.

The auditor judges the conversation, not the task. It never sees an agent's own tool calls, the repository or full
state files: only the messages orch delivered to each agent in the window and what each agent sent back, broken into
parts by where each part came from. Everything that can be counted is counted here, in code:

- origin: each paragraph of a message is traced to the turn output, chat reply or human note it was copied from
  (unmatched text is orch's own: templates, handovers, reminders)
- repeats: paragraphs the same agent was already sent in its previous message
- relays: how much of each source output reached the recipient, and what was left out
- request vs reply: near-empty replies, bursts of short turns by one agent, questions and who they went to
- waits: how long a human or relayed note sat in the inbox before an agent read it
- times: each turn's start and end, each message's status in the routing ledger (`state["messages"]`) with how long
  it waited from sent to delivered, and when each question opened and closed (`state["questions"]`)

Every check reads messages with orch's reminders stripped (`reminders.strip`): they are by design, not content.

The digest is markdown; excerpts are capped so the auditor reads structure and samples, not the whole task.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import reminders as R
from . import tags as T
from . import protocol as CP
from .timing import describe as describe_timing
from .routing import DISCUSSION as RT_DISCUSSION, NEXT_TURN as RT_NEXT_TURN
from .recorder import read_json
from .report import turn_cost_basis, turn_model

EXCERPT = 600  # chars per excerpt
MAX_EXCERPTS = 12  # per digest, most notable first
PER_KIND = 6  # flag lines per kind
MIN_PARA = 60  # shorter paragraphs are not traced (headings, "Thanks.")
TINY_REPLY = 300
REPEAT_SHARE, REPEAT_MIN = 0.3, 1500
BIG_INPUT = 20000
WAIT_MIN = 30  # minutes a note may wait before it is flagged
MAX_LEDGER = 30  # message lines in the digest; ones that waited long or are held (not by design) always shown
SOURCE_WINDOW = 40  # earlier turns searched for the origin of a paragraph
ROLE = {"manager": "manager", "verifier": "critic", "worker": "worker"}
# who normally speaks to whom in managed mode: manager <-> critic <-> worker, the human -> manager
EXPECTED = {("manager", "critic"), ("critic", "manager"), ("critic", "worker"), ("worker", "critic"),
            ("human", "manager"), ("manager-chat", "manager"), ("manager-chat", "critic"), ("human", "critic"),
            ("human", "worker")}


def norm(text: str) -> str:
    return " ".join((text or "").split())


def paragraphs(text: str) -> List[str]:
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        b = norm(block)
        if len(b) >= MIN_PARA:
            out.append(b)
    return out


def clip(text: str, limit: int = EXCERPT) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit].rstrip() + f" … [{len(t)} chars]"


def _ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _hms(s: Optional[str]) -> str:
    """'HH:MM:SS' of an ISO time ('' when missing)."""
    m = re.search(r"T(\d{2}:\d{2}:\d{2})", s or "")
    return m.group(1) if m else ""


def _mins(a: Optional[float], b: Optional[float]) -> str:
    return f"{(b - a) / 60:.0f} min" if a is not None and b is not None and b - a >= 60 else (
        f"{max(0, int(b - a))} s" if a is not None and b is not None else "?")


def _file_ts(name: str) -> Optional[float]:
    m = re.match(r"(\d{8}-\d{6})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- loading


def load_turns(run_dir: Path) -> List[Dict[str, Any]]:
    """Every finished turn of the agents (not the auditor's own), in order."""
    out = []
    for p in sorted((run_dir / "rounds").glob("r*/t*.json")):
        t = read_json(p, {}) or {}
        if not t.get("turn_id") or t.get("role") == "auditor" or t.get("status") == "running":
            continue
        out.append(t)
    out.sort(key=lambda t: (t.get("started_at") or "", t["turn_id"]))
    return out


def load_sources(run_dir: Path) -> List[Dict[str, Any]]:
    """Text that came from outside the three agents' turns: the human (chat, board notes, gates, answers) and the
    manager's chat replies. Each: {id, origin, text, ts}."""
    out = []
    for line in (run_dir / "chat.jsonl").read_text(encoding="utf-8").splitlines() if (run_dir / "chat.jsonl").is_file() else []:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("text") and d.get("role") in ("human", "manager"):
            out.append({"id": d.get("id", ""), "origin": "human" if d["role"] == "human" else "manager-chat",
                        "text": R.strip(d["text"]), "ts": _ts(d.get("ts"))})
    for sub in ("consumed", ""):
        d = run_dir / "inbox" / sub if sub else run_dir / "inbox"
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            m = read_json(p, {}) or {}
            if m.get("message"):
                out.append({"id": p.name, "origin": f"note from {m['from']}" if m.get("from") else "human",
                            "text": R.strip(m["message"]), "ts": _file_ts(p.name)})
    return out


def note_waits(run_dir: Path, lo_ts: float, hi_ts: float) -> List[Dict[str, Any]]:
    """Notes read by an agent in the window: how long each waited in the inbox, and for whom."""
    seen = {}
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if (run_dir / "events.jsonl").is_file() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "human_messages":
            for f in e.get("files") or []:
                seen.setdefault(f, _ts(e.get("ts")))
    out = []
    for f, read in seen.items():
        made = _file_ts(f)
        if made is None or read is None or not (lo_ts <= read <= hi_ts):
            continue
        m = read_json(run_dir / "inbox" / "consumed" / f, {}) or {}
        out.append({"file": f, "minutes": (read - made) / 60, "to": m.get("to"), "from": m.get("from") or "human",
                    "urgent": bool(m.get("urgent")), "text": R.strip(m.get("message") or "")})
    pending = []
    for p in sorted((run_dir / "inbox").glob("*.json")) if (run_dir / "inbox").is_dir() else []:
        m = read_json(p, {}) or {}
        made = _file_ts(p.name)
        if m.get("message") and made is not None and not m.get("kind"):
            pending.append({"file": p.name, "minutes": (hi_ts - made) / 60, "to": m.get("to"),
                            "from": m.get("from") or "human", "urgent": bool(m.get("urgent")),
                            "text": R.strip(m["message"]), "pending": True})
    return out + pending


def _held_kind(m: Dict[str, Any]) -> str:
    """Why a held message is held: 'by design' (never meant for that agent: the critic's checklist, plan, verdict),
    'next turn' (a note up with nothing to decide), 'discussion' (the manager's messages while the user discusses
    with it), or '' for a hold that is not by design (a bad address, a terminal open)."""
    if m.get("by_design"):
        return "by design"
    if m.get("next_turn") or m.get("reason") == RT_NEXT_TURN:
        return "next turn"
    if m.get("reason") == RT_DISCUSSION:
        return "discussion"
    return ""


def ledger(run_dir: Path, lo_ts: float, hi_ts: float) -> Tuple[List[str], Dict[str, Any], List[Dict[str, Any]]]:
    """The routing ledger's messages that touch the window: (lines, counts, flags). Each line: id, sender →
    recipient, sent time, then what became of it and how long it waited. Held-by-design messages are listed as such:
    they were never meant to arrive and are not lost."""
    rows = (read_json(run_dir / "state.json", {}) or {}).get("messages") or []
    picked = []
    for m in rows:
        made, got = _ts(m.get("created_at")), _ts(m.get("delivered_at"))
        waiting = m.get("status") in ("sent", "held")
        if made is None:
            continue
        if lo_ts <= made <= hi_ts + 1 or (got is not None and lo_ts <= got <= hi_ts + 1) or (waiting and made <= hi_ts + 1):
            picked.append(m)
    counts: Dict[str, Any] = {"messages": len(picked)}
    lines, flags, notable = [], [], set()
    for m in picked:
        made, got = _ts(m.get("created_at")), _ts(m.get("delivered_at"))
        st, kind = m.get("status") or "?", _held_kind(m) if m.get("status") == "held" else ""
        key = f"held {kind}" if st == "held" and kind else ("held (not by design)" if st == "held" else st)
        counts[key] = counts.get(key, 0) + 1
        head = f"- {m['id']} {m.get('from', '?')} → {m.get('to') or '?'} sent {_hms(m.get('created_at'))}" + (
            " (urgent)" if m.get("urgent") else "")
        if st == "delivered":
            wait = (got - made) / 60 if got is not None else 0
            line = f"{head}, delivered {_hms(m.get('delivered_at'))} in {m.get('turn', '?')}" + (
                f" ({m['how']})" if m.get("how") and m.get("how") != "prompt" else "") + f", waited {_mins(made, got)}"
            if m.get("next_turn"):
                line += " (held for the manager's next turn, by design)"
            elif wait >= WAIT_MIN:
                notable.add(m["id"])
                flags.append({"kind": "message waited", "weight": 2, "turn": m.get("turn", ""),
                              "line": f"{m['id']} {m.get('from')} → {m.get('to')} waited {wait:.0f} min before "
                                      f"{m.get('turn', '?')} read it", "excerpt": clip(R.strip(m.get("body") or ""), 300)})
        elif st == "held":
            why = {"by design": f"held by design ({m.get('kind') or m.get('reason') or ''}): never meant for "
                                f"{m.get('to')}, not lost",
                   "next turn": "held until the manager's next regular turn, by design",
                   "discussion": "held for the discussion, by design; the manager's message when it closes replaces it"
                   }.get(kind, f"held: {m.get('reason') or '?'}")
            line = f"{head}, {why}" + ("" if kind == "by design" else f", {_mins(made, hi_ts)} so far")
            if not kind:
                notable.add(m["id"])
        elif st == "superseded":
            line = f"{head}, superseded {_hms(m.get('updated_at'))} by {m.get('by', '?')} before delivery"
        elif st == "dropped":  # nothing would ever carry it (its recipient stopped, its review or milestone is over)
            line = f"{head}, dropped {_hms(m.get('dropped_at') or m.get('updated_at'))}: {m.get('reason') or '?'} " \
                   "(closed, not waiting)"
        else:
            line = f"{head}, still waiting after {_mins(made, hi_ts)}"
            if made is not None and (hi_ts - made) / 60 >= WAIT_MIN:
                notable.add(m["id"])
                flags.append({"kind": "message waited", "weight": 2, "turn": "",
                              "line": f"{m['id']} {m.get('from')} → {m.get('to')} is still waiting after "
                                      f"{(hi_ts - made) / 60:.0f} min", "excerpt": clip(R.strip(m.get("body") or ""), 300)})
        lines.append((m["id"], line))
    keep = [x for x in lines if x[0] in notable]
    keep += [x for x in lines if x[0] not in notable][:max(0, MAX_LEDGER - len(keep))]
    shown = [ln for mid, ln in lines if (mid, ln) in keep]
    if len(lines) > len(shown):
        shown.append(f"- … and {len(lines) - len(shown)} more, all delivered or held by design")
    meant = len(picked) - counts.get("held by design", 0) - counts.get("superseded", 0) - counts.get("dropped", 0)
    counts["delivered_share"] = counts.get("delivered", 0) / meant if meant else None
    return shown, counts, flags


def questions(run_dir: Path, lo_ts: float, hi_ts: float) -> List[str]:
    """Questions open in the window: who asked whom, when each opened and closed (or how long it has been open)."""
    out = []
    for q in (read_json(run_dir / "state.json", {}) or {}).get("questions") or []:
        a, c = _ts(q.get("asked_at")), _ts(q.get("answered_at"))
        if a is None or a > hi_ts + 1 or (c is not None and c < lo_ts):
            continue
        line = f"{q['id']}: {q.get('from', '?')} → {q.get('to', '?')} opened {_hms(q.get('asked_at'))}"
        if q.get("status") == "open" or c is None:
            line += f", still open after {_mins(a, hi_ts)}"
        else:
            by = f" by {q['answered_by']}" if q.get("answered_by") else ""
            line += f", {q.get('status')}{by} {_hms(q.get('answered_at'))} (open {_mins(a, c)})"
        out.append(line)
    return out


# --------------------------------------------------------------------------- analysis


def trace(turns: List[Dict[str, Any]], sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each turn: its input paragraphs, each with its origin and whether it repeats the agent's previous input."""
    prev_input: Dict[str, set] = {}
    outs = [(t, norm(R.strip(t.get("output") or "")), _ts(t.get("started_at")) or 0.0) for t in turns]
    srcs = [(s, norm(s["text"])) for s in sources]
    traced = []
    for i, t in enumerate(turns):
        start = _ts(t.get("started_at")) or 0.0
        cand = [(f"{ROLE.get(o.get('role'), o.get('role'))}", o["turn_id"], txt)
                for o, txt, ts in outs[max(0, i - SOURCE_WINDOW):i] if txt and ts <= start]
        cand += [(s["origin"], s["id"], txt) for s, txt in srcs if txt and (s["ts"] or 0) <= start]
        paras = []
        before = prev_input.get(t["agent"], set())
        for p in paragraphs(R.strip(t.get("input") or "")):  # orch's reminders are by design, not content
            origin, src = "orch", ""
            for o, sid, txt in reversed(cand):  # the most recent source that holds it
                if p in txt:
                    origin, src = o, sid
                    break
            paras.append({"text": p, "origin": origin, "source": src, "repeat": p in before})
        prev_input[t["agent"]] = {x["text"] for x in paras}
        traced.append({"turn": t, "paras": paras})
    return traced


def passable(src: Dict[str, Any], to: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
    """The part of `src`'s reply that orch passes on to the agent of turn `to`, and what it withholds by design
    ({what: chars}): protocol tags (checklist, verdict, state…) never travel, a <message> goes only to the agent it
    names, and when the manager writes a <message> to someone, the rest of its reply is for the human."""
    text = R.strip(src.get("output") or "")
    names = [to.get("agent") or "", ROLE.get(to.get("role"), "")]
    uniform = src.get("communication_protocol") == CP.VERSION
    upper = {"worker": "verifier", "verifier": "manager", "manager": "human"}.get(src.get("role"))
    to_upper = uniform and to.get("role") == upper
    if to_upper:
        names.append("you")
    keep, withheld = [], {}
    mine = False
    for n, attrs, body, _s, _e in T.parse(text):
        if (n == "message" and T.addressed(attrs, *names)) or (
                uniform and n == "to" and attrs.get("worker") == to.get("agent")):
            keep.append(body)
            mine = True
        elif body:
            what = f"message to {attrs.get('to', '?')}" if n == "message" else n
            withheld[what] = withheld.get(what, 0) + len(body)
    rest = T.outside(text)
    if uniform and not to_upper:
        if rest:
            withheld["reply to upper level"] = len(rest)
    elif mine and src.get("role") == "manager":
        if rest:
            withheld["reply to the human"] = len(rest)
    else:
        keep.insert(0, rest)
    return "\n\n".join(k for k in keep if k), withheld


def relay_coverage(tr: Dict[str, Any], turns_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each turn output relayed into this input: how much of what orch passes on arrived, the first paragraphs
    of it left out, and what orch withheld by design."""
    out = []
    used: Dict[str, List[str]] = {}
    for p in tr["paras"]:
        if p["source"] in turns_by_id:
            used.setdefault(p["source"], []).append(p["text"])
    for sid, got in used.items():
        text, withheld = passable(turns_by_id[sid], tr["turn"])
        src = paragraphs(text)
        total = sum(len(x) for x in src) or 1
        carried = set(got)
        left = [x for x in src if x not in carried and not any(x in g or g in x for g in got)]
        out.append({"source": sid, "share": sum(len(x) for x in src if x not in left) / total,
                    "source_chars": total, "left_out": left[:3], "withheld": withheld})
    return out


CUT = ("interrupted", "stopped")  # a turn a restart cut off (the run stopped or crashed mid-turn)


def resumes(run_dir: Path) -> List[float]:
    """When the run was resumed after a restart (`run_resumed` events)."""
    out = []
    p = run_dir / "events.jsonl"
    for line in p.read_text(encoding="utf-8").splitlines() if p.is_file() else []:
        if '"run_resumed"' not in line:
            continue
        try:
            ts = _ts(json.loads(line).get("ts"))
        except ValueError:
            continue
        if ts is not None:
            out.append(ts)
    return out


def redos(turns: List[Dict[str, Any]], resumed: List[float]) -> Dict[str, str]:
    """{turn id: the turn it redoes} for turns run again after a restart: the same agent's previous turn had the same
    purpose and was cut off (interrupted or stopped), or the run was resumed between the two and they belong to the
    same round. Their input repeats the earlier turn's by design."""
    out: Dict[str, str] = {}
    last: Dict[str, Dict[str, Any]] = {}
    for t in turns:
        u = last.get(t["agent"])
        last[t["agent"]] = t
        if u is None or u.get("purpose") != t.get("purpose"):
            continue
        a, b = _ts(u.get("started_at")) or 0.0, _ts(t.get("started_at")) or 0.0
        if u.get("status") in CUT or (u.get("round") == t.get("round") and any(a <= x <= b for x in resumed)):
            out[t["turn_id"]] = u["turn_id"]
    return out


def _agent_of(t: Dict[str, Any]) -> str:
    return ROLE.get(t.get("role"), t.get("role") or t.get("agent", "?"))


def build(run_dir: Path, lo: int, hi: int, findings: Iterable[str] = (),
          prompts_seen: Optional[Dict[str, str]] = None) -> Tuple[str, Dict[str, Any]]:
    """The digest of rounds lo..hi (inclusive) and the numbers behind it. `prompts_seen`: {agent: hash} of the system
    prompts at the last audit; only changed ones are shown."""
    run_dir = Path(run_dir)
    allt = load_turns(run_dir)
    by_id = {t["turn_id"]: t for t in allt}
    traced = trace(allt, load_sources(run_dir))
    win = [x for x in traced if lo <= int(x["turn"].get("round") or 0) <= hi]
    stats: Dict[str, Any] = {"rounds": [lo, hi], "turns": len(win), "flags": [], "origin": {}, "prompts": {}}
    if not win:
        return f"# Audit digest: rounds {lo}–{hi}\n\nNo turns in this window.\n", stats
    lo_ts = _ts(win[0]["turn"].get("started_at")) or 0.0
    hi_ts = max(_ts(x["turn"].get("ended_at")) or 0.0 for x in win)
    flags: List[Dict[str, Any]] = []  # {kind, weight, turn, line, excerpt}

    rows, origin, relays = [], {}, []
    roles = {t["agent"]: _agent_of(t) for t in allt}
    redo = redos(allt, resumes(run_dir))  # turns run again after a restart: not duplicates
    redone = {v: k for k, v in redo.items()}
    prev = None
    burst: List[Dict[str, Any]] = []
    for x in win:
        t, paras = x["turn"], x["paras"]
        who = _agent_of(t)
        # every check reads the message without orch's reminders at its end (lessons §9); *in* counts it that way
        inp = asked = R.strip(t.get("input") or "")
        out = R.strip(t.get("output") or "")
        rep = sum(len(p["text"]) for p in paras if p["repeat"])
        mix: Dict[str, int] = {}
        for p in paras:
            mix[p["origin"]] = mix.get(p["origin"], 0) + len(p["text"])
            origin.setdefault(who, {}).setdefault(p["origin"], 0)
            origin[who][p["origin"]] += len(p["text"])
        cost = ((t.get("meta") or {}).get("turn_cost_usd")) or 0
        est = turn_cost_basis(t) == "estimate"
        sess = f"same session, turn {t.get('session_turn')}" if t.get("resumed_session") else "new session"
        rem = f" (↺ {', '.join(t['reminders'])})" if t.get("reminders") else ""  # reminders sent at its end
        mark = (f" · redo of {redo[t['turn_id']]} after a restart" if t["turn_id"] in redo else
                f" · cut off by a restart, redone in {redone[t['turn_id']]}" if t["turn_id"] in redone else "")
        rows.append(f"| {t['turn_id']} | {_hms(t.get('started_at'))}–{_hms(t.get('ended_at'))} | "
                    f"{t.get('purpose', '')}{rem} | {t.get('status', '')}{mark} | {sess} | "
                    f"{turn_model(t) or '?'} | {len(inp)} | {rep} | "
                    f"{len(out)} | {int(t.get('duration_s') or 0)} | {'~' if est else ''}{cost:.2f} | "
                    + ", ".join(f"{k} {v}" for k, v in sorted(mix.items(), key=lambda kv: -kv[1])) + " |")
        if t.get("status") in ("interrupted", "stopped"):  # cut off by a restart; the output is the raw stream
            flags.append({"kind": "interrupted", "weight": 0, "turn": t["turn_id"],
                          "line": f"{t['turn_id']} ({t.get('purpose')}) was cut off after "
                                  f"{int(t.get('duration_s') or 0)} s", "excerpt": ""})
        elif t.get("status") != "ok" or len(out.strip()) < TINY_REPLY:
            flags.append({"kind": "short or failed reply", "weight": 2, "turn": t["turn_id"],
                          "line": f"{t['turn_id']} ({t.get('purpose')}) replied {len(out.strip())} chars, status "
                                  f"{t.get('status')}", "excerpt": f"Asked (end of message): {clip(asked[-EXCERPT:])}\n"
                                                                   f"Reply: {clip(out) or '(empty)'}"})
        if rep >= REPEAT_MIN and rep >= REPEAT_SHARE * max(len(inp), 1) and t["turn_id"] not in redo:
            first = next(p for p in paras if p["repeat"])
            flags.append({"kind": "repeated content", "weight": 3, "turn": t["turn_id"],
                          "line": f"{t['turn_id']}: {rep} of {len(inp)} chars were already in {t['agent']}'s "
                                  f"previous message", "excerpt": clip(first["text"])})
        if len(inp) >= BIG_INPUT:
            top = max(mix.items(), key=lambda kv: kv[1]) if mix else ("?", 0)
            own = sorted((p["text"] for p in paras if p["origin"] == "orch"), key=len, reverse=True)[:4]
            flags.append({"kind": "large message", "weight": 1, "turn": t["turn_id"],
                          "line": f"{t['turn_id']}: {len(inp)} chars sent; largest part from {top[0]} ({top[1]})",
                          "excerpt": ("orch's largest parts: " + " | ".join(f"[{len(x)}] {x[:140]}" for x in own))
                          if own else ""})
        for p in paras:
            sender = p["origin"]
            if sender.startswith("note from "):  # an agent's note passed on from its chat: judged by that agent
                sender = roles.get(sender[len("note from "):], sender)
            if p["origin"] != "orch" and (sender, who) not in EXPECTED and sender != who:
                flags.append({"kind": "unexpected origin", "weight": 3, "turn": t["turn_id"],
                              "line": f"{t['turn_id']}: {who} received text from the {p['origin']} "
                                      f"({p['source']})", "excerpt": clip(p["text"])})
                break
        rel = [rc for rc in relay_coverage(x, by_id) if by_id[rc["source"]]["agent"] != t["agent"]]
        if rel:
            relays.append(f"- {t['turn_id']} ⇐ " + ", ".join(
                f"{rc['source']} {rc['share']:.0%} of {rc['source_chars']}"
                + (" (withheld by design: " + ", ".join(f"{k} {v}" for k, v in rc["withheld"].items()) + ")"
                   if rc["withheld"] else "") for rc in rel))
        # bursts: 3+ turns of one agent in a row within 10 minutes, mostly short (a redo replaces its cut-off turn)
        if t["turn_id"] in redo and burst and burst[-1]["turn_id"] == redo[t["turn_id"]]:
            burst[-1] = t
        elif prev is not None and prev["agent"] == t["agent"] and \
                (_ts(t.get("started_at")) or 0) - (_ts(prev.get("ended_at")) or 0) < 600:
            burst.append(t)
        else:
            _burst_flag(burst, flags)
            burst = [t]
        prev = t
    _burst_flag(burst, flags)

    for w in note_waits(run_dir, lo_ts, hi_ts):
        if w["minutes"] >= WAIT_MIN:
            to = ", ".join(w["to"] or []) or "the manager"
            flags.append({"kind": "note waited", "weight": 2, "turn": "",
                          "line": f"note from {w['from']} to {to}{' (urgent)' if w['urgent'] else ''} "
                                  f"{'is still waiting after' if w.get('pending') else 'waited'} {w['minutes']:.0f} min",
                          "excerpt": clip(w["text"], 300)})

    qs = questions(run_dir, lo_ts, hi_ts)
    led, led_counts, led_flags = ledger(run_dir, lo_ts, hi_ts)
    flags += led_flags
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if (run_dir / "events.jsonl").is_file() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not (run_dir / "state.json").is_file() and e.get("event") == "question" and lo_ts <= (_ts(e.get("ts")) or 0) <= hi_ts + 1:
            qs.append(f"{e.get('qid')}: {e.get('frm')} → {e.get('to')}")

    # system prompts: only the ones that changed since the last audit
    seen = dict(prompts_seen or {})
    changed = []
    for t in reversed(win):
        a = t["turn"]["agent"]
        if a in stats["prompts"]:
            continue
        sp = t["turn"].get("system_prompt") or ""
        h = hashlib.sha1(sp.encode()).hexdigest()[:12]
        stats["prompts"][a] = h
        if seen.get(a) != h:
            old = next((u["turn"].get("system_prompt") or "" for u in reversed(traced)
                        if u["turn"]["agent"] == a and hashlib.sha1((u["turn"].get("system_prompt") or "").encode())
                        .hexdigest()[:12] == seen.get(a)), "")
            if old:
                diff = "\n".join(difflib.unified_diff(old.splitlines(), sp.splitlines(), lineterm="", n=0))
                changed.append(f"**{a}** (changed since the last audit):\n```diff\n{clip(diff, 1500)}\n```")
            else:
                changed.append(f"**{a}** ({len(sp)} chars):\n```\n{clip(sp, 2500)}\n```")

    flags.sort(key=lambda f: -f["weight"])
    stats["flags"] = flags
    stats["origin"] = origin
    stats["questions"] = qs
    stats["ledger"] = led_counts

    md = [f"# Audit digest: rounds {lo}–{hi}",
          f"{len(win)} turns, {win[0]['turn'].get('started_at', '')[:16]} to "
          f"{max(x['turn'].get('ended_at') or '' for x in win)[:16]}. Sizes are characters. *repeat*: already in that "
          f"agent's previous message. Origins: the turn output, chat reply (*manager-chat*) or *human* note a "
          f"paragraph was copied from; *note from X* is a note agent X passed on from its chat; *orch* is orch's own "
          f"text (templates, handovers). *↺ ids*: short reminders orch adds at the end of a message at intervals, by "
          f"design; they are left out of the origins and repeats. *session*: *same session, turn n* continues the agent's session at its n-th "
          f"turn (consecutive numbers are one session, not new ones), so the agent still has everything said before in "
          f"it: a short message there is not a lost task. "
          f"*redo of X after a restart* (status column): the run was restarted while turn X ran, so this turn runs X "
          f"again; its message repeats X's by design, or is just *continue* when X's message had already reached the "
          f"session (the agent still has it, so nothing is lost), and the pair is not a duplicate. "
          f"*$*: the backend's figure; *~* marks Claude Code's own estimate for a model on another provider, priced "
          f"at Claude rates, which is not what the provider charged — don't judge spend on it. Times are HH:MM:SS; "
          f"*in* is the message without orch's reminders.",
          "", "## Turns", "",
          "| turn | start–end | purpose | status | session | model | in | repeat | out | s | $ | origin of the message |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|", *rows,
          ]
    md += ["", "## Communication protocol for these turns", ""]
    segments = []
    for x in win:
        t = x["turn"]
        version = t.get("communication_protocol") or CP.LEGACY
        if segments and segments[-1][0] == version:
            segments[-1][2] = t["turn_id"]
        else:
            segments.append([version, t["turn_id"], t["turn_id"]])
    for version, first, last in segments:
        md.append(f"- {first} through {last}: {version}. " + CP.DESCRIPTIONS.get(
            version, "Unknown protocol: do not infer routing rules; request its specification."))
    md.append("Records without a protocol marker predate version tracking and use the historical rules above. "
              "Do not apply a newer design retroactively. Session turn numbers are not session ids.")
    md += ["", "## Observed timing", ""]
    for x in win:
        t = x["turn"]
        if t.get("timing"):
            md.append(f"- {t['turn_id']}: {describe_timing(t['timing'])}")
    if not any(x["turn"].get("timing") for x in win):
        md.append("No timing breakdown recorded. Total duration cannot distinguish tool work, model activity or waiting.")
    cols = sorted({o for v in origin.values() for o in v})
    md += ["", "## Who received text from whom (chars)", "",
           "| recipient | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for who, v in sorted(origin.items()):
        md.append(f"| {who} | " + " | ".join(str(v.get(c, 0)) for c in cols) + " |")
    md += ["", "## Questions (opened and closed)", "", *(f"- {q}" for q in qs or ["none"])]
    if led:
        c = led_counts
        share = f"{c['delivered_share']:.0%}" if c.get("delivered_share") is not None else "n/a"
        md += ["", "## Messages (orch's routing ledger: what each recipient was actually given)", "",
               f"{c['messages']} messages: " + ", ".join(f"{k} {v}" for k, v in c.items()
                                                       if k not in ("messages", "delivered_share")) +
               f". Delivered share of those meant to arrive (not held by design, not superseded, not dropped): {share}. "
               f"*dropped*: closed undelivered because nothing would carry it any more (its recipient stopped, or "
               f"its review, milestone or run was over); it is not a wait. A message "
               f"counts as delivered only when it was found in the recipient's actual prompt.", "", *led]
    md += ["", "## Relays (share of what orch passes on that reached the recipient, in chars; tags such as the "
               "critic's plan and checklist, and messages for someone else, are withheld by design and listed apart)", "",
           *(relays or ["none"])]
    md += ["", "## Flags (orch's checks; hints, not verdicts)", ""]
    if not flags:
        md.append("none")
    shown, per_kind = 0, {}
    for f in flags:
        n = per_kind[f["kind"]] = per_kind.get(f["kind"], 0) + 1
        if n > PER_KIND:
            continue
        md.append(f"- [{f['kind']}] {f['line']}")
        if f["excerpt"] and shown < MAX_EXCERPTS:
            shown += 1
            md.append("  > " + f["excerpt"].replace("\n", "\n  > "))
    for k, n in per_kind.items():
        if n > PER_KIND:
            md.append(f"- [{k}] … and {n - PER_KIND} more")
    if changed:
        md += ["", "## System prompts", "", *changed]
    of = [x for x in findings if x]
    md += ["", "## Your findings file (from earlier audits)", "", *(f"- {x}" for x in of or ["none yet"])]
    return "\n".join(md) + "\n", stats


def _burst_flag(burst: List[Dict[str, Any]], flags: List[Dict[str, Any]]) -> None:
    if len(burst) >= 3:
        short = sum(1 for t in burst if len(R.strip(t.get("output") or "")) < 1000)
        if short >= 2:
            flags.append({"kind": "burst of turns", "weight": 2, "turn": burst[0]["turn_id"],
                          "line": f"{len(burst)} {burst[0]['agent']} turns in a row ({burst[0]['turn_id']} … "
                                  f"{burst[-1]['turn_id']}), {short} with short replies: "
                                  + "; ".join(f"{t['turn_id'].split('-')[1]} {t.get('purpose')}: "
                                              f"{norm(R.strip(t.get('output') or ''))[:80]!r}" for t in burst[:5]),
                          "excerpt": ""})


# --------------------------------------------------------------------------- findings file and the audit itself

HEADER = """# Audit findings

Written by orch's auditor: how the agents' conversation could work better (not the task itself). The auditor keeps
the list under "Findings" up to date; change a status to "fixed" or "won't fix" when you act on one. Each audit's
points are under "Audits", newest first.
"""


def findings_lines(path: Path) -> List[str]:
    """The lines of the findings list in the auditor's file (under '## Findings')."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^## Findings[^\n]*\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    return [l.strip()[2:].strip() for l in (m.group(1).splitlines() if m else []) if l.strip().startswith("- ")]


def _tag(text: str, name: str) -> Optional[str]:
    m = re.findall(rf"<{name}>\s*(.*?)\s*</{name}>", text or "", flags=re.S | re.I)
    return m[-1].strip() if m else None


def parse_reply(text: str, max_points: int) -> Tuple[str, Optional[List[str]]]:
    """(points, findings lines or None if the reply has no findings list). Points beyond max_points are cut."""
    pts = _tag(text, "points")
    if pts is None:
        pts = re.sub(r"<findings>.*?(</findings>|\Z)", "", text or "", flags=re.S | re.I).strip()
    items = re.split(r"(?m)^(?=\s*\d+[.)]\s)", pts)
    head, nums = [x for x in items if not re.match(r"\s*\d+[.)]\s", x)], [x for x in items if re.match(r"\s*\d+[.)]\s", x)]
    if len(nums) > max_points:
        pts = "".join(head + nums[:max_points]).strip()
    f = _tag(text, "findings")
    lines = None if f is None else [l.strip()[2:].strip() for l in f.splitlines() if l.strip().startswith("- ")]
    return pts.strip(), lines


def write_findings(path: Path, findings: Optional[List[str]], section_title: str, points: str) -> None:
    """Replace the findings list (if the auditor sent one) and add this audit's points at the top of 'Audits'."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "## Findings" not in text:
        text = HEADER + "\n## Findings\n\n(none yet)\n\n## Audits\n"
    if findings is not None:
        body = "\n".join(f"- {x}" for x in findings) or "(none)"
        text = re.sub(r"(^## Findings[^\n]*\n)(.*?)(?=^## |\Z)", lambda m: m.group(1) + "\n" + body + "\n\n",
                      text, count=1, flags=re.M | re.S)
    if "## Audits" not in text:
        text = text.rstrip() + "\n\n## Audits\n"
    entry = f"\n### {section_title}\n\n{points.strip() or 'Nothing new.'}\n"
    text = re.sub(r"(^## Audits[^\n]*\n)", lambda m: m.group(1) + entry, text, count=1, flags=re.M)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def findings_path(agent: Any, run_dir: Path) -> Path:
    """agents/<auditor>/audit.md in a project folder; next to the monitor's runs otherwise."""
    if getattr(agent, "state_file", ""):
        return Path(agent.state_file).parent / "audit.md"
    return Path(run_dir).parent / f"audit-{agent.name}.md"


def run_audit(mon: Any, agent: Any, run_dir: Path, lo: int, hi: int, prompts_seen: Optional[Dict[str, str]] = None,
              should_stop: Any = None, log: Any = None) -> Dict[str, Any]:
    """One audit of rounds lo..hi: build the digest, ask the auditor (a fresh session every time), write its findings
    file and keep a record in <run>/audits/. Never raises for a failed audit; the run goes on regardless."""
    from .backends import TurnRequest, TurnResult, run_variants
    from .templates import render
    run_dir = Path(run_dir)
    path = findings_path(agent, run_dir)
    digest, stats = build(run_dir, lo, hi, findings_lines(path), prompts_seen)
    decisions_path = path.with_name("audit-decisions.md")
    try:
        decisions = decisions_path.read_text(encoding="utf-8").strip()
    except OSError:
        decisions = ""
    if decisions:
        digest += ("\n\n## Human-reviewed audit decisions\n\n"
                   "Respect these dispositions. Revisit only with new contradictory evidence or a material change.\n\n"
                   + decisions)
    c = mon.controller
    n = max(1, int(getattr(c, "audit_points", 5) or 5))
    sp = render(agent.current_system_prompt(), points=n).strip()
    tpl = (c.templates or {}).get("audit_request") or "{{digest}}"
    prompt = render(tpl, digest=digest, lo=lo, hi=hi, run=run_dir.name, points=n).strip()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rec_dir = run_dir / "audits"
    rec_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Any] = {"rounds": [lo, hi], "prompts": stats.get("prompts") or {}, "flags": len(stats["flags"]),
                           "digest_chars": len(digest), "file": str(path), "at": stamp,
                           "decisions_file": str(decisions_path) if decisions else None}
    if not stats.get("turns"):
        out.update(status="empty", points="")
        return out
    req = TurnRequest(agent=agent.name, role="auditor", prompt=prompt, system_prompt=sp, session_id=None,
                      cwd=agent.working_dir or mon.working_dir, timeout=agent.timeout, options=agent.backend_options,
                      env=agent.env, context={"purpose": "audit", "monitor": mon.name, "rounds": [lo, hi]})
    try:
        res = run_variants(agent, req, should_stop or (lambda: False))
    except Exception as e:  # an audit never stops the run
        res = TurnResult("error", "", None, stderr=f"{type(e).__name__}: {e}")
    cost = float((res.meta or {}).get("total_cost_usd") or 0)
    (rec_dir / f"{stamp}-r{lo:03d}-r{hi:03d}.md").write_text(
        f"# Audit of rounds {lo}–{hi} ({res.status}, ${cost:.2f})\n\n## Sent\n\n{prompt}\n\n## Reply\n\n"
        f"{res.output}\n" + (f"\n## stderr\n\n{res.stderr}\n" if res.stderr else ""), encoding="utf-8")
    out.update(status=res.status, cost_usd=cost)
    if res.status != "ok" or not (res.output or "").strip():
        out["points"] = ""
        if log:
            log(f"  audit of rounds {lo}–{hi} failed ({res.status}); see {rec_dir}")
        return out
    points, findings = parse_reply(res.output, n)
    title = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · run {run_dir.name} · rounds {lo}–{hi}"
    write_findings(path, findings, title, points)
    out["points"] = points
    out["count"] = len(re.findall(r"(?m)^\s*\d+[.)]\s", points))
    return out
