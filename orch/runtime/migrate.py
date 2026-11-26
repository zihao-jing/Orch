"""Migration of existing projects and runs to the new runtime (docs/redesign.md MOD-4, PRM-2, MS-14, section 6 item 8)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import yaml

from .. import __version__, config
from .. import jobs as _jobs
from ..recorder import Recorder, read_json, write_json
from . import models, texts
from .adapters.base import SystemClock
from .contracts import (
    ORCH,
    USER,
    Agent,
    Choice,
    Clock,
    GoldenBoard,
    Message,
    MessageStatus,
    Milestone,
    MilestoneStatus,
    Role,
    RunInfo,
    SessionRef,
    Settings,
    Snapshot,
    TextKey,
    TextRef,
    Trigger,
)
from .store import Store, StoreError

RUNTIME_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class MigrationError(StoreError, config.ConfigError, RuntimeError):
    """Raised when a run or project cannot be migrated (e.g. while a run's process is still alive)."""


def _orch_dir(project: Union[str, Path]) -> Path:
    p = Path(project).expanduser().resolve()
    found = config.find_project(str(p))
    if found is not None:
        return found
    if (p / ".orch").is_dir():
        return p / ".orch"
    return p


# sha256 of the old runtime's default prompts (orch/prompts/, removed): a project copy equal to one is Orch's own
OLD_PROMPTS = {
    "manager": "980787a4e6b3cbe80a00e421f49207406ad1b6b30d83ec49b2ff6780907f95b9",
    "verifier": "a2d85647f395ec29f461e9501601bddd96c96931cba94ee0ca5194d2daca6464",
    "monitor": "008ad51ad5dcf1c5e3920e64d9a6b39c566db3fe5674ede2690944b81a953911",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _orch_prompts_for(role: str) -> set:
    """Digests of Orch's own prompts for a role: the old runtime's and the current one."""
    canon = config.ROLE_ALIASES.get(role, role)
    rt_name = "critic" if canon == "verifier" else canon
    out = {OLD_PROMPTS[canon]} if canon in OLD_PROMPTS else set()
    p = RUNTIME_PROMPTS_DIR / f"{rt_name}.md"
    if p.is_file():
        try:
            out.add(_digest(p.read_text(encoding="utf-8")))
        except OSError:
            pass
    return out


def is_orch_prompt(role: str, text: str) -> bool:
    return _digest(text) in _orch_prompts_for(role)


def backup_prompts(project: Union[str, Path], log: Any = None) -> List[str]:
    """PRM-2: for manager, critic or monitor in a new-runtime project, ignore project system_prompt.md / system_prompt;
    if its text differs from Orch's prompt, move it to agents/<name>/system_prompt.md.bak once with a notice.
    Workers' task text stays. Returns relative paths of backed-up prompt files."""
    orch_dir = _orch_dir(project)
    agents_dir = orch_dir / "agents"
    if not agents_dir.is_dir():
        return []
    moved: List[str] = []
    for d in sorted(agents_dir.iterdir()):
        ay = d / "agent.yaml"
        if not ay.is_file():
            continue
        try:
            raw = config._read_raw(ay)
        except Exception:
            continue
        role = config.ROLE_ALIASES.get(str(raw.get("role", "worker")), str(raw.get("role", "worker")))
        if role not in ("manager", "verifier", "monitor"):
            continue
        own = d / "system_prompt.md"
        bak = d / "system_prompt.md.bak"
        if own.is_file():
            text = own.read_text(encoding="utf-8").strip()
            if text and not is_orch_prompt(role, text):
                os.replace(own, bak)
                rel = f"agents/{d.name}/system_prompt.md.bak"
                moved.append(rel)
                msg = f"notice: agents/{d.name}/system_prompt.md differs from Orch's prompt; moved to {rel}"
                if callable(log):
                    log(msg)
                else:
                    print(msg, file=sys.stderr)
        if "system_prompt" in raw:
            inline = str(raw.get("system_prompt") or "").strip()
            if inline and inline not in defaults and not bak.exists():
                bak.write_text(inline + "\n", encoding="utf-8")
                rel = f"agents/{d.name}/system_prompt.md.bak"
                moved.append(rel)
                msg = f"notice: agents/{d.name}/agent.yaml system_prompt differs from Orch's prompt; saved to {rel}"
                if callable(log):
                    log(msg)
                else:
                    print(msg, file=sys.stderr)
    return moved


def write_settings(project: Union[str, Path]) -> Dict[str, Any]:
    """MOD-4: write .orch/settings.yaml from the agents' backend/model/effort lines, keep every old file,
    update agent.yaml files to refer to settings.yaml by model id (saving the original lines/files), and
    record the old files in .orch/migration.json."""
    orch_dir = _orch_dir(project)
    agents_dir = orch_dir / "agents"
    if not agents_dir.is_dir():
        raise MigrationError(f"no agents folder in {orch_dir}")

    sp = config.settings_path(orch_dir)
    settings_text = sp.read_text(encoding="utf-8") if sp and sp.is_file() else ""
    settings_data = (yaml.safe_load(settings_text) or {}) if settings_text.strip() else {}
    sp = sp or (orch_dir / "settings.yaml")

    agents_text: Dict[str, str] = {}
    agents_yaml: Dict[str, Dict[str, Any]] = {}
    for d in sorted(agents_dir.iterdir()):
        ay = d / "agent.yaml"
        if ay.is_file():
            txt = ay.read_text(encoding="utf-8")
            agents_text[d.name] = txt
            agents_yaml[d.name] = yaml.safe_load(txt) or {}

    added, refs = models.list_models(settings_data, agents_yaml)
    new_settings_text = models.with_models(settings_text, added)
    if not new_settings_text.strip():
        new_settings_text = "models: {}\n"
    models._write(sp, new_settings_text)

    mig_path = orch_dir / "migration.json"
    prev_mig = read_json(mig_path, {}) or {}
    originals: Dict[str, str] = dict(prev_mig.get("originals") or {})
    old_files: List[str] = list(prev_mig.get("files") or [])

    def record_file(rel: str) -> None:
        if rel not in old_files:
            old_files.append(rel)

    for name, ref in refs.items():
        rel = f"agents/{name}/agent.yaml"
        ay = orch_dir / rel
        if rel not in originals:
            originals[rel] = agents_text[name]
        bak = orch_dir / f"agents/{name}/agent.yaml.bak"
        if not bak.exists():
            models._write(bak, agents_text[name])
        updated = models.with_choice(agents_text[name], **ref)
        models._write(ay, updated)
        record_file(rel)

    for rel_moved in backup_prompts(orch_dir):
        record_file(rel_moved)

    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue
        for fname in ("preference.md", "system_prompt.md.bak", "system_prompt.md"):
            cand = d / fname
            if not cand.is_file():
                continue
            if fname == "system_prompt.md":
                raw = agents_yaml.get(d.name) or {}
                role = config.ROLE_ALIASES.get(str(raw.get("role", "worker")), str(raw.get("role", "worker")))
                if role == "worker":
                    continue
            record_file(f"agents/{d.name}/{fname}")

    data = {"files": old_files, "originals": originals, "refs": refs}
    write_json(mig_path, data)
    return data


def confirm(project: Union[str, Path], date: Optional[str] = None) -> Path:
    """Handle ConfirmMigration (MOD-4): move the old lines/files recorded in .orch/migration.json to
    .orch/backup/<date>/ and remove .orch/migration.json."""
    orch_dir = _orch_dir(project)
    stamp = date or _dt.date.today().isoformat()
    backup_dir = orch_dir / "backup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    mig_path = orch_dir / "migration.json"
    mig = read_json(mig_path, {}) or {}
    originals: Dict[str, str] = dict(mig.get("originals") or {})
    files: List[str] = list(mig.get("files") or [])

    sp = config.settings_path(orch_dir)
    settings_data = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}) if sp and sp.is_file() else {}
    agents_dir = orch_dir / "agents"
    if agents_dir.is_dir():
        current_yaml = {}
        current_text = {}
        for d in sorted(agents_dir.iterdir()):
            ay = d / "agent.yaml"
            if ay.is_file():
                current_text[d.name] = ay.read_text(encoding="utf-8")
                current_yaml[d.name] = yaml.safe_load(current_text[d.name]) or {}
        added, refs = models.list_models(settings_data, current_yaml)
        if added and sp:
            models._write(sp, models.with_models(sp.read_text(encoding="utf-8"), added))
        for name, ref in refs.items():
            rel = f"agents/{name}/agent.yaml"
            originals.setdefault(rel, current_text[name])
            models._write(orch_dir / rel, models.with_choice(current_text[name], **ref))
            if rel not in files:
                files.append(rel)

    for rel, old_text in originals.items():
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        models._write(dest, old_text)
        bak = (orch_dir / rel).with_name(Path(rel).name + ".bak")
        if bak.is_file():
            bak.unlink()

    for rel in files:
        if rel in originals:
            continue
        src = orch_dir / rel
        if src.is_file():
            dest = backup_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))

    if mig_path.is_file():
        mig_path.unlink()
    return backup_dir


def migration_files(project: Union[str, Path]) -> List[str]:
    """Files recorded in .orch/migration.json, if a migration is pending confirmation."""
    orch_dir = _orch_dir(project)
    mig = read_json(orch_dir / "migration.json", {}) or {}
    return list(mig.get("files") or [])


def migration_note(files: Sequence[str], now: float = 0.0, seq: int = 1) -> Message:
    """The NOTE_MIGRATED message shown in a run when .orch/migration.json is present."""
    return Message(
        id="xmigrated0001",
        sender=ORCH,
        recipient=USER,
        needs_answer=False,
        trigger=Trigger.ORCH,
        text=TextRef(TextKey.NOTE_MIGRATED, {"files": list(files)}),
        created=now,
        seq=seq,
        status=MessageStatus.DELIVERED,
        delivered_at=now,
    )


def _pid_alive(pid: Any) -> bool:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    return _jobs.alive(p)


def _run_is_alive(run_dir: Path, old: Dict[str, Any]) -> bool:
    if old.get("alive") is True:
        return True
    for k in ("pid", "runner_pid"):
        if old.get(k) and _pid_alive(old.get(k)):
            return True
    lock_file = run_dir / "locks" / "run.lock"
    if lock_file.is_file():
        info = read_json(lock_file, {}) or {}
        if info.get("pid") and _pid_alive(info.get("pid")):
            return True
    rec = Recorder(run_dir, run_dir.parent.parent)
    holder = rec.claim()
    if holder is not None:
        return True
    rec.release()
    return False


def build_snapshot(
    mon: config.MonitorConfig,
    run_dir: Path,
    clock: Clock,
    settings: Optional[Dict[str, Any]] = None,
    migrated_files: Sequence[str] = (),
) -> Snapshot:
    """Build the initial Snapshot for a new-runtime run from a MonitorConfig."""
    now = clock.now()
    settings = dict(settings or {})
    orch_dir = config.find_project(mon.source) if mon.source else None
    run_info = RunInfo(
        id=run_dir.name,
        project=mon.name,
        goal=mon.goal,
        workspace=mon.workspace,
        run_dir=str(run_dir),
        decisions_path=str((orch_dir or run_dir) / "decisions.md"),
        audit_notes_path=str((orch_dir or run_dir) / "audit-decisions.md"),
        started=now,
        version=__version__,
    )
    rt_settings = Settings(
        round_limit=mon.max_rounds or 5000,
        retry_waits=list(settings.get("retry_waits") or config.RETRY_WAITS),
        return_after=float(settings.get("fallback_cooldown", 18000.0) or 18000.0),
        golden_limit=mon.controller.golden_limit or 8000,
        pass_score=mon.controller.min_score if mon.controller.min_score is not None else 4.5,
        writing=mon.controller.writing,
        approve_plan=mon.controller.approve_plan,
        approve_milestones=mon.controller.approve_milestones,
    )
    agents: Dict[str, Agent] = {}
    for ag in mon.agents:
        role = Role(ag.role)
        ay_dict: Dict[str, Any] = {}
        if orch_dir and (orch_dir / "agents" / ag.name / "agent.yaml").is_file():
            ay_dict = yaml.safe_load((orch_dir / "agents" / ag.name / "agent.yaml").read_text(encoding="utf-8")) or {}
        if not ay_dict:
            ay_dict = {"backend": ag.backend, "backend_options": dict(ag.backend_options), "env": dict(ag.env)}
            if ag.model_id:
                ay_dict["model"] = ag.model_id
            if ag.fallback:
                ay_dict["fallback"] = list(ag.fallback)
        try:
            ch_list = models.choices(settings, ay_dict)
        except Exception:
            ch_list = [Choice(ag.backend, model=str(ag.backend_options.get("model") or ""), model_id=ag.model_id)]
        main = ch_list[0]
        display = ag.display_name or (ag.name if role == Role.WORKER else f"the {role.value}")
        fresh = "new" if ag.fresh_session == "milestone" else ag.fresh_session
        agents[ag.name] = Agent(
            id=ag.name,
            role=role,
            display=display,
            cwd=ag.working_dir or mon.working_dir or mon.workspace,
            fresh_session=fresh,
            choice=main,
            choice_since=now,
            choices=ch_list,
            session_key=main.session_key,
            idle_since=now,
        )
    msgs: Dict[str, Message] = {}
    if migrated_files:
        note = migration_note(migrated_files, now=now, seq=1)
        msgs[note.id] = note
    return Snapshot(
        now=now,
        run=run_info,
        settings=rt_settings,
        agents=agents,
        messages=msgs,
        golden=GoldenBoard(path=mon.controller.golden_board),
        orch_started=now,
    )


def migrate_run(
    run_dir: Union[str, Path],
    clock: Optional[Clock] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Snapshot:
    """Section 6 item 8 & MS-14: convert an old run (whose state.json has no 'runtime' key and is not running)
    to the new runtime:
      - refuse while its process is alive
      - keep old state as state.old.json
      - keep sessions per agent
      - convert waiting messages to new waiting messages
      - give milestones ids m1, m2, ... in order
      - a finished run starts with status finished and accepts added milestones (ChangeMilestones)."""
    run = Path(run_dir).expanduser().resolve()
    clock = clock or SystemClock()
    now = clock.now()
    state_path = run / "state.json"
    old = read_json(state_path)
    if not isinstance(old, dict):
        raise MigrationError(f"cannot read {state_path}")
    if "runtime" in old:
        return Store(run, clock, readonly=True).snapshot()
    if _run_is_alive(run, old):
        raise MigrationError(f"run {run} is still running; stop it before migrating")

    # Save old state as state.old.json
    shutil.copyfile(state_path, run / "state.old.json")

    cfg = read_json(run / "config.json", {}) or {}
    ctrl = cfg.get("controller") or {}
    workspace = str(old.get("workspace") or cfg.get("workspace") or "")
    working_dir = str(old.get("working_dir") or cfg.get("working_dir") or workspace or "")
    goal = str(old.get("goal") or cfg.get("goal") or "")
    project = str(old.get("monitor") or cfg.get("name") or run.parent.name)

    # Agents and sessions
    agents: Dict[str, Agent] = {}
    cfg_agents = {str(a.get("name")): a for a in (cfg.get("agents") or []) if isinstance(a, dict) and a.get("name")}
    old_sessions = old.get("sessions") or {}
    all_names: List[str] = list(cfg_agents.keys())
    for name in old_sessions:
        if name not in all_names:
            all_names.append(name)
    for default_name in ("manager", "critic", "worker"):
        if default_name not in all_names and not any(
            (cfg_agents.get(n, {}).get("role") or (old_sessions.get(n) or {}).get("role"))
            in ({default_name} | ({"verifier"} if default_name == "critic" else set()))
            for n in all_names
        ):
            all_names.append(default_name)

    for name in all_names:
        ac = cfg_agents.get(name) or {}
        sess = old_sessions.get(name) or {}
        raw_role = str(sess.get("role") or ac.get("role") or (
            "manager" if name == "manager" else
            "critic" if name in ("critic", "verifier") else
            "monitor" if name == "monitor" else
            "auditor" if name == "auditor" else "worker"
        ))
        role = Role(raw_role)
        backend = str(sess.get("backend") or ac.get("backend") or "claude")
        opts = dict(ac.get("backend_options") or {})
        raw_model = str(sess.get("model") or opts.get("model") or "")
        if raw_model.startswith(f"{backend}:"):
            raw_model = raw_model[len(backend) + 1:]
        effort = str(opts.get("effort") or "")
        model_id = str(ac.get("model_id") or "")
        ch = Choice(backend=backend, model=raw_model, effort=effort, model_id=model_id)
        skey = str(sess.get("session_key") or ch.session_key)
        cwd = str(ac.get("working_dir") or working_dir or workspace or "")
        sessions_map: Dict[str, SessionRef] = {}
        sid = sess.get("session_id")
        if sid:
            sessions_map[skey] = SessionRef(
                session_id=str(sid),
                session_key=skey,
                choice=ch,
                cwd=cwd,
                started=now,
                last_used=now,
                confirmed=True,
            )
        display = str(ac.get("display_name") or (name if role == Role.WORKER else f"the {role.value}"))
        fresh = str(ac.get("fresh_session") or "")
        if fresh == "milestone":
            fresh = "new"
        agents[name] = Agent(
            id=name,
            role=role,
            display=display,
            cwd=cwd,
            fresh_session=fresh,
            choice=ch,
            choice_since=now,
            choices=[ch],
            sessions=sessions_map,
            session_key=skey,
            idle_since=now,
        )

    # Waiting messages
    messages: Dict[str, Message] = {}
    seq = 0

    def add_waiting(recipient: str, body: str, sender: str = ORCH, mid: str = "") -> None:
        nonlocal seq
        body = str(body or "").strip()
        if not body or not recipient:
            return
        seq += 1
        msg_id = mid if (mid and mid not in messages) else f"xmig{seq:09d}"
        trig_name = {"user": "user_chat", "orch": "orch"}.get(sender, "orch")
        if sender in agents:
            r = agents[sender].role
            if r in (Role.MANAGER, Role.CRITIC, Role.WORKER, Role.MONITOR):
                trig_name = r.value
        messages[msg_id] = Message(
            id=msg_id,
            sender=sender,
            recipient=recipient,
            body=body,
            needs_answer=True,
            trigger=Trigger(trig_name),
            created=now,
            seq=seq,
            status=MessageStatus.WAITING,
        )

    # 1. fwd: {agent: [text, ...]}
    for rec_agent, items in (old.get("fwd") or {}).items():
        if isinstance(items, str):
            items = [items]
        for item in items or []:
            if isinstance(item, dict):
                add_waiting(
                    str(item.get("to") or item.get("recipient") or rec_agent),
                    str(item.get("body") or item.get("text") or item.get("message") or ""),
                    str(item.get("from") or item.get("sender") or ORCH),
                    str(item.get("id") or ""),
                )
            else:
                add_waiting(str(rec_agent), str(item))

    # 2. pending: {agent: [text, ...] or text}
    for rec_agent, items in (old.get("pending") or {}).items():
        if isinstance(items, str):
            items = [items]
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add_waiting(
                        str(item.get("to") or item.get("recipient") or rec_agent),
                        str(item.get("body") or item.get("text") or item.get("message") or ""),
                        str(item.get("from") or item.get("sender") or ORCH),
                        str(item.get("id") or ""),
                    )
                else:
                    add_waiting(str(rec_agent), str(item))

    # 3. worker_messages: [{id, to, from, message, status}]
    for wm in old.get("worker_messages") or []:
        if not isinstance(wm, dict):
            continue
        st_wm = str(wm.get("status") or "queued")
        if st_wm in ("accepted", "in_prompt", "delivered", "answered", "superseded"):
            continue
        add_waiting(
            str(wm.get("to") or wm.get("recipient") or ""),
            str(wm.get("message") or wm.get("body") or wm.get("text") or ""),
            str(wm.get("from") or wm.get("sender") or "critic"),
            str(wm.get("id") or ""),
        )

    # 4. old["messages"] if any have waiting/queued status
    raw_msgs = old.get("messages")
    if isinstance(raw_msgs, dict):
        raw_msgs = list(raw_msgs.values())
    if isinstance(raw_msgs, list):
        for rm in raw_msgs:
            if not isinstance(rm, dict):
                continue
            if str(rm.get("status") or "") in ("waiting", "queued", "pending"):
                add_waiting(
                    str(rm.get("recipient") or rm.get("to") or ""),
                    str(rm.get("body") or rm.get("text") or rm.get("message") or ""),
                    str(rm.get("sender") or rm.get("from") or ORCH),
                    str(rm.get("id") or ""),
                )

    # 5. inbox/*.json human messages
    inbox = run / "inbox"
    if inbox.is_dir():
        for p in sorted(inbox.glob("*.json")):
            data = read_json(p, {}) or {}
            if not isinstance(data, dict) or data.get("kind"):
                continue
            to_list = data.get("to")
            if isinstance(to_list, str):
                to_list = [to_list]
            targets = to_list or ["manager"]
            for t_ag in targets:
                add_waiting(str(t_ag), str(data.get("message") or ""), USER)

    # Milestones: m1, m2, ... in order
    mg = old.get("managed") or {}
    raw_ms = mg.get("milestones") if isinstance(mg.get("milestones"), list) else old.get("milestones")
    raw_ms = list(raw_ms or [])
    cur_idx = int(mg.get("ms", 0) or 0)
    old_status = str(old.get("status") or "")
    is_finished = old_status in ("completed", "finished") or str(mg.get("phase") or "") in ("done", "completed", "finished")

    milestones: List[Milestone] = []
    for idx, item in enumerate(raw_ms):
        mid = f"m{idx + 1}"
        if isinstance(item, str):
            title, text = item.strip(), ""
            item_dict: Dict[str, Any] = {}
        elif isinstance(item, dict):
            item_dict = item
            title = str(item.get("title") or f"Milestone {idx + 1}").strip()
            text = str(item.get("done_when") or item.get("text") or "").strip()
            if text == title:
                text = ""
        else:
            continue
        raw_st = str(item_dict.get("status") or "").lower()
        if is_finished or raw_st in ("done", "completed", "finished") or item_dict.get("done") is True:
            ms_status = MilestoneStatus.DONE
        elif raw_st == "dropped":
            ms_status = MilestoneStatus.DROPPED
        elif raw_st == "current":
            ms_status = MilestoneStatus.CURRENT
        elif raw_st == "pending":
            ms_status = MilestoneStatus.PENDING
        elif idx < cur_idx:
            ms_status = MilestoneStatus.DONE
        elif idx == cur_idx:
            ms_status = MilestoneStatus.CURRENT
        else:
            ms_status = MilestoneStatus.PENDING
        rubric = [str(x) for x in (item_dict.get("rubric") or [])]
        score = float(item_dict["score"]) if item_dict.get("score") is not None else None
        accepted = bool(item_dict.get("accepted") or ms_status == MilestoneStatus.DONE)
        ck_path = str(item_dict.get("checklist_path") or f"{run}/checklist-{idx + 1}.md")
        milestones.append(
            Milestone(
                id=mid,
                title=title,
                text=text,
                status=ms_status,
                rubric=rubric,
                score=score,
                checklist_path=ck_path,
                accepted=accepted,
                summary=str(item_dict.get("summary") or ""),
            )
        )

    if is_finished and not milestones:
        milestones.append(
            Milestone(
                id="m1",
                title=goal or "Milestone 1",
                text="",
                status=MilestoneStatus.DONE,
                 checklist_path=f"{run}/checklist-1.md",
                accepted=True,
            )
        )

    run_info = RunInfo(
        id=str(old.get("run_id") or run.name),
        project=project,
        goal=goal,
        workspace=workspace,
        run_dir=str(run),
        decisions_path=str(run / "decisions.md"),
        audit_notes_path=str(run / "audit-decisions.md"),
        started=now,
        version=__version__,
    )
    rt_settings = Settings(
        round_limit=int(old.get("max_rounds") or cfg.get("max_rounds") or 5000),
        golden_limit=int(ctrl.get("golden_limit") or 8000),
        pass_score=float(ctrl["min_score"]) if ctrl.get("min_score") is not None else 4.5,
        approve_plan=bool(ctrl.get("approve_plan", True)),
        approve_milestones=bool(ctrl.get("approve_milestones", True)),
    )
    snap = Snapshot(
        now=now,
        run=run_info,
        settings=rt_settings,
        agents=agents,
        messages=messages,
        milestones=milestones,
        golden=GoldenBoard(path=str(ctrl.get("golden_board") or "")),
        finished=is_finished,
        rounds=int(old.get("round", 0) or 0),
        orch_started=now,
    )
    store = Store.create(run, clock, snap)
    store.close()
    return store.snapshot()
