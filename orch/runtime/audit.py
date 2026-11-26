"""The audit of a run (AUD-1…4): a digest of one stretch, the facts Orch checks itself, one auditor process per audit,
and every point checked before it is listed.

The stretch starts where the last audit ended, never before the run's start in this runtime (a migrated run starts at
its migration), so no audit spans the change (AUD-1). The engine starts `run_audit` in a thread of its own; nothing here
touches the store, which the engine owns: the caller passes a snapshot and the messages.
"""
from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..recorder import read_json
from .contracts import (USER, AgentSpec, GateKind, JobState, Message, MessageStatus, ProcessExited, Snapshot,
                        TurnEnded, TurnStatus)

EXCERPT = 400  # characters of each message and turn in the digest
MAX_MESSAGES = 150  # the newest ones of the stretch; the count of the others is given
MAX_TURNS = 80
POINTS = 5
WAITING_AFTER = 300.0  # seconds a message may wait unheld before it counts as not delivered
QUOTE_MIN = 15  # shorter quotes are not checked
DISMISSED = re.compile(r"won'?t fix|false alarm|not a (problem|bug|fault)|dismiss|reject|by design|intended", re.I)


# ------------------------------------------------------------------------------------------------- the stretch

def records(run_dir: Path) -> List[Dict[str, Any]]:
    """The earlier audits of the run (audits/*.json), oldest first."""
    out = []
    for p in sorted((Path(run_dir) / "audits").glob("*.json")):
        d = read_json(p, {}) or {}
        if isinstance(d, dict):
            out.append(d)
    return out


def window(run_dir: Path, snap: Snapshot) -> Tuple[float, float]:
    """From the end of the last finished audit (or the run's start in this runtime) to now."""
    done = [float(r.get("until") or 0) for r in records(run_dir) if r.get("status") == "ok"]
    return max([snap.run.started] + done), snap.now


def turns(run_dir: Path, since: float, until: float) -> List[Dict[str, Any]]:
    """The turn files this runtime wrote (they name their protocol) that started in the stretch, by start."""
    out = []
    for p in Path(run_dir).glob("rounds/r*/t*.json"):
        t = read_json(p, {}) or {}
        if not isinstance(t, dict) or not t.get("turn_id") or not t.get("communication_protocol"):
            continue
        at = _epoch(t.get("started_at"))
        if since <= at <= until:
            out.append({**t, "_at": at})
    return sorted(out, key=lambda t: t["_at"])


def _epoch(s: Any) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _hm(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%m-%d %H:%M:%S") if at else "-"


def _mins(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.1f} h"


def excerpt(text: str, limit: int = EXCERPT) -> str:
    """AUD-4: a limit of zero shows that no text is included, never that nothing was written."""
    if limit <= 0:
        return "(text not included)"
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return "(empty)"
    return flat if len(flat) <= limit else flat[:limit].rstrip() + " …"


def _text(m: Message) -> str:
    return m.rendered or m.body


# ------------------------------------------------------------------------------------------------ AUD-2 facts

def _gates(run_dir: Path, snap: Snapshot) -> List[Any]:
    from .contracts import Gate, from_dict
    found = {g.id: g for g in snap.gates.values()}
    for line in (Path(run_dir) / "archive.jsonl").read_text(encoding="utf-8").splitlines() \
            if (Path(run_dir) / "archive.jsonl").exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("gate"), dict):
            g = from_dict(Gate, row["gate"])
            found.setdefault(g.id, g)
    return list(found.values())


def facts(run_dir: Path, snap: Snapshot, messages: Sequence[Message], turn_list: Sequence[Dict[str, Any]],
          since: float) -> List[str]:
    """What Orch knows for sure: messages not delivered, failed or unanswered; a question or pause without its gate;
    a job whose end never came or never reached its worker. One line each, with ids."""
    now, st, out = snap.now, snap.settings, []
    for m in messages:
        if m.created < since and m.status not in (MessageStatus.WAITING, MessageStatus.DELIVERED):
            continue
        who = f"{m.id} {m.sender}→{m.recipient}"
        if m.status == MessageStatus.WAITING and m.recipient != USER and now - m.created > WAITING_AFTER:
            if m.holds:
                out.append(f"held: {who}, {_mins(now - m.created)}, by {', '.join(str(h) for h in m.holds)}")
            else:
                out.append(f"not delivered: {who}, waiting {_mins(now - m.created)}, {m.attempts} attempt(s)")
        elif m.status == MessageStatus.FAILED:
            why = str(m.drop) if m.drop else (m.error or "failed")
            out.append(f"failed: {who} ({why})")
        elif m.status == MessageStatus.DELIVERED and m.needs_answer and m.delivered_at \
                and now - m.delivered_at > st.unanswered_after:
            out.append(f"unanswered: {who}, delivered {_mins(now - m.delivered_at)} ago")
    gates = _gates(run_dir, snap)
    asked = {g.source_turn for g in gates if g.kind == GateKind.QUESTION}
    for t in turn_list:
        if t.get("role") == "manager" and re.search(r"<question\b", t.get("output") or "") \
                and t["turn_id"] not in asked and t.get("status") == str(TurnStatus.OK):
            out.append(f"missing gate: {t['turn_id']} asks a <question> but no question gate came from it")
    if snap.paused and not any(g.is_open and g.kind in (GateKind.PAUSE, GateKind.OPERATIONAL) for g in gates):
        out.append("missing gate: the run is paused with no open pause gate (GATE-9)")
    for j in snap.jobs.values():
        if j.state == JobState.RUNNING and now - max(j.started, j.last_progress) > st.job_quiet_after:
            out.append(f"job without end: {j.id} ({excerpt(j.label, 80)}) running "
                       f"{_mins(now - j.started)}, no sign of progress for {_mins(now - max(j.started, j.last_progress))}")
        elif j.state == JobState.LOST and not j.stopped_by_user:
            out.append(f"job without end: {j.id} ({excerpt(j.label, 80)}) lost, without a result")
        elif j.state != JobState.RUNNING and j.ended and not (j.woke or j.stopped_by_user) \
                and now - j.ended > 2 * st.wakeup_after:
            out.append(f"job end not received: {j.id} ended {_mins(now - j.ended)} ago, its worker has not taken it in")
    return out


# ------------------------------------------------------------------------------------------------- the digest

def version_lines() -> List[str]:
    """AUD-4: the Orch that runs, and the changes it has (newest first)."""
    from .. import __version__
    from .. import update
    lines = [f"Orch {__version__}" + (f", commit {update.head()[:7]}" if update.head() else "")]
    try:
        log = update._git("log", "--format=%h %s", "-n", "10")
    except Exception:
        log = ""
    if log:
        lines += ["Its latest changes (newest first; a fault fixed by one of these is fixed in this run):"] + [
            f"- {x}" for x in log.splitlines()]
    return lines


def build(run_dir: Path, snap: Snapshot, messages: Sequence[Message], findings: Sequence[str] = (), notes: str = "",
          limit: int = EXCERPT, versions: Optional[List[str]] = None) -> Tuple[str, Dict[str, Any]]:
    """The digest of the stretch, and what verify() needs: the ids and texts it showed."""
    since, until = window(run_dir, snap)
    ms = [m for m in messages if since <= m.created <= until]
    shown_ms = ms[-MAX_MESSAGES:]
    tl = turns(run_dir, since, until)
    shown_t = tl[-MAX_TURNS:]
    names = {a.id: f"{a.id} ({a.role.value})" for a in snap.agents.values()}
    out = [f"# Audit of run {snap.run.id}: {_hm(since)} – {_hm(until)}", ""]
    out += versions if versions is not None else version_lines()
    out += ["", "## Agents", ""] + [f"- {names[a.id]}: {a.status.value}, {a.lifecycle.value}, "
                                     f"{a.actual_model or a.choice.model or a.choice.backend}"
                                     for a in snap.agents.values() if a.role.value != "auditor"]
    fl = facts(run_dir, snap, messages, tl, since)
    out += ["", "## Facts Orch checked (exact)", ""] + ([f"- {x}" for x in fl] or ["- none"])
    out += ["", f"## Messages ({len(ms)} in this stretch" + (f", the newest {len(shown_ms)} shown" if len(ms) >
                                                               len(shown_ms) else "") + ")", ""]
    for m in shown_ms:
        waited = f", delivered after {_mins(m.delivered_at - m.created)}" if m.delivered_at else ""
        out.append(f"- {m.id} · {_hm(m.created)} · {m.sender}→{m.recipient} · {m.status.value}{waited}"
                   + (f" · held by {', '.join(str(h) for h in m.holds)}" if m.holds else "")
                   + f": {excerpt(_text(m), limit)}")
    out += ["", f"## Turns ({len(tl)} in this stretch" + (f", the newest {len(shown_t)} shown" if len(tl) >
                                                            len(shown_t) else "") + ")", ""]
    for t in shown_t:
        msgs = ", ".join(t.get("messages") or []) or "no message"
        out.append(f"- {t['turn_id']} · {_hm(t['_at'])} – {_hm(_epoch(t.get('ended_at')))} · {t.get('status')} · "
                   f"took in {msgs}: {excerpt(t.get('output') or '', limit)}")
    out += ["", "## Your findings file", ""] + ([f"- {x}" for x in findings] or ["(empty)"])
    if notes.strip():
        out += ["", "## The user's notes on earlier audits", "",
                "Respect these. A dismissed point comes back only with new evidence, which you name.", "", notes.strip()]
    shown = {m.id: _text(m) for m in shown_ms}
    shown.update({t["turn_id"]: (t.get("output") or "") + "\n" + (t.get("input") or "") for t in shown_t})
    text = "\n".join(out) + "\n"
    return text, {"since": since, "until": until, "ids": shown, "digest": text, "messages": len(ms), "turns": len(tl),
                  "facts": fl}


# ------------------------------------------------------------------------------------------- AUD-3 the check

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[“”\"'‘’`]", "", s or "")).strip().lower()


def split_points(points: str) -> List[str]:
    return [p.strip() for p in re.split(r"(?m)^(?=\s*\d+[.)]\s)", points or "") if re.match(r"\s*\d+[.)]\s", p)]


def check(point: str, seen: Dict[str, Any], notes: str) -> str:
    """"" if the point holds against the digest and the user's notes, else why not."""
    if not any(re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", point) for i in seen["ids"]):
        return "cites no message or turn of this stretch"
    hay = _norm(seen["digest"]) + "\n" + "\n".join(_norm(v) for v in seen["ids"].values())
    for q in re.findall(r"[“\"]([^”\"]{%d,})[”\"]" % QUOTE_MIN, point):
        if _norm(q).rstrip(" .…") not in hay:
            return f"quote not found: {excerpt(q, 80)}"
    head = _norm(point.split("\n", 1)[0])
    head = re.sub(r"^\d+[.)]\s*", "", head)
    for line in notes.splitlines():
        if DISMISSED.search(line) and difflib.SequenceMatcher(None, head, _norm(line)).ratio() >= 0.6 \
                and "new evidence" not in point.lower():
            return f"dismissed in the user's notes: {excerpt(line, 80)}"
    return ""


def verify(points: str, seen: Dict[str, Any], notes: str) -> Tuple[str, List[Tuple[str, str]]]:
    """The points that hold, numbered again, and the others with the reason."""
    kept, dropped = [], []
    for p in split_points(points):
        why = check(p, seen, notes)
        (dropped.append((p, why)) if why else kept.append(re.sub(r"^\s*\d+[.)]\s*", "", p)))
    text = "\n\n".join(f"{i}. {p}" for i, p in enumerate(kept, 1)) or "Nothing new."
    return text, dropped


# ------------------------------------------------------------------------------------------------- the reply

def _tag(text: str, name: str) -> Optional[str]:
    m = re.findall(rf"<{name}>\s*(.*?)\s*</{name}>", text or "", flags=re.S | re.I)
    return m[-1].strip() if m else None


def parse_reply(text: str) -> Tuple[str, Optional[List[str]]]:
    """(points, findings lines or None when the reply has no findings list)."""
    pts = _tag(text, "points")
    if pts is None:
        pts = re.sub(r"<findings>.*?(</findings>|\Z)", "", text or "", flags=re.S | re.I).strip()
    f = _tag(text, "findings")
    lines = None if f is None else [l.strip()[2:].strip() for l in f.splitlines() if l.strip().startswith("- ")]
    return pts.strip(), lines


HEADER = """# Audit findings

Written by Orch's auditor: how the agents' communication could work better (not the task). The auditor keeps the list
under "Findings" up to date; set a status to "fixed" or "won't fix" when you act on one. Each audit's points are under
"Audits", newest first.
"""


def findings_path(snap: Snapshot, run_dir: Path) -> Path:
    """audit.md beside the user's notes (audit-decisions.md), or in the run folder."""
    return Path(snap.run.workspace or ".", snap.run.audit_notes_path).with_name("audit.md") \
        if snap.run.audit_notes_path else \
        Path(run_dir) / "audit.md"


def findings_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^## Findings[^\n]*\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    return [l.strip()[2:].strip() for l in (m.group(1).splitlines() if m else []) if l.strip().startswith("- ")]


def write_findings(path: Path, findings: Optional[List[str]], title: str, points: str) -> None:
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
    entry = f"\n### {title}\n\n{points.strip() or 'Nothing new.'}\n"
    text = re.sub(r"(^## Audits[^\n]*\n)", lambda m: m.group(1) + entry, text, count=1, flags=re.M)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------------------------------------- one audit

def ask(adapter: Any, spec: AgentSpec, prompt: str, timeout: float, stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep) -> Tuple[str, str]:
    """One auditor process: start a new session, send the digest, wait for the turn's end, stop it. (status, text)."""
    adapter.start(spec, None)
    try:
        adapter.send("audit-" + uuid.uuid4().hex[:8], prompt)
        end = time.monotonic() + timeout
        while time.monotonic() < end and not stop():
            for ev in adapter.events():
                if isinstance(ev, TurnEnded):
                    return ("ok" if ev.status == TurnStatus.OK else str(ev.status)), ev.text or ev.detail
                if isinstance(ev, ProcessExited):
                    return "error", ev.detail or f"the auditor's program ended (exit {ev.exit_code})"
            sleep(0.1)
        return ("stopped" if stop() else "timeout"), ""
    finally:
        try:
            adapter.stop()
            list(adapter.events())
        except Exception:
            pass


def run_audit(run_dir: Path, snap: Snapshot, messages: Sequence[Message], adapter: Any, spec: AgentSpec,
              timeout: float = 2400.0, stop: Callable[[], bool] = lambda: False, points: int = POINTS,
              versions: Optional[List[str]] = None, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Build the digest, ask the auditor, check its points, write audits/<stamp>.md and .json and the findings file.
    Never raises for a failed audit."""
    run_dir = Path(run_dir)
    path = findings_path(snap, run_dir)
    notes_file = Path(snap.run.workspace or ".", snap.run.audit_notes_path) if snap.run.audit_notes_path else None
    try:
        notes = notes_file.read_text(encoding="utf-8") if notes_file else ""
    except OSError:
        notes = ""
    digest, seen = build(run_dir, snap, messages, findings_lines(path), notes, versions=versions)
    stamp = datetime.fromtimestamp(snap.now).strftime("%Y%m%d-%H%M%S")
    rec_dir = run_dir / "audits"
    rec_dir.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {"since": seen["since"], "until": seen["until"], "rounds_at": snap.rounds,
                              "messages": seen["messages"],
                              "turns": seen["turns"], "facts": seen["facts"], "findings_file": str(path)}
    if not seen["messages"] and not seen["turns"]:
        record.update(status="empty")
        _write(rec_dir, stamp, record, digest, "Nothing happened since the last audit.")
        return record
    prompt = f"Audit of run {snap.run.id}. At most {points} points.\n\n{digest}"
    spec = AgentSpec(spec.agent, spec.role, spec.choice, spec.cwd, spec.system_prompt.replace("{points}", str(points)),
                     spec.run_dir)
    try:
        status, reply = ask(adapter, spec, prompt, timeout, stop, sleep)
    except Exception as exc:  # an audit never stops the run
        status, reply = "error", f"{type(exc).__name__}: {exc}"
    record.update(status=status, reply=reply)
    if status != "ok" or not reply.strip():
        _write(rec_dir, stamp, record, prompt, f"The audit did not finish ({status}). {excerpt(reply, 300)}")
        return record
    pts, findings = parse_reply(reply)
    kept, dropped = verify(pts, seen, notes)
    record.update(points=kept, not_confirmed=[{"point": p, "why": w} for p, w in dropped])
    _write(rec_dir, stamp, record, prompt, kept)
    write_findings(path, findings, f"{datetime.fromtimestamp(snap.now).strftime('%Y-%m-%d %H:%M')} · run {snap.run.id}",
                   kept)
    return record


def _write(rec_dir: Path, stamp: str, record: Dict[str, Any], sent: str, reply: str) -> None:
    base, n = stamp, 1
    while (rec_dir / f"{stamp}.json").exists():  # two audits in one second keep both records
        n += 1
        stamp = f"{base}-{n}"
    (rec_dir / f"{stamp}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    (rec_dir / f"{stamp}.md").write_text(f"# Audit ({record.get('status')})\n\n## Sent\n\n{sent}\n\n## Reply\n\n"
                                         f"{reply}\n", encoding="utf-8")
