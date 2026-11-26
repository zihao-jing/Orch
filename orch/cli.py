"""Command-line interface: ``orch <command>`` (or ``python -m orch <command>``)."""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import os
import json
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .config import Config, ConfigError, find_project, is_config, load_config, require_managed, settings_path, validate
from .recorder import read_json, write_json
from . import report

_REAL_SUBPROCESS_RUN = subprocess.run
Engine: Optional[Any] = None  # can be patched in tests before orch.runtime.engine is merged

EXAMPLE = Path(__file__).with_name("example_config.yaml")


# --------------------------------------------------------------------------- target resolution


def _runs_in(monitor_dir: Path) -> List[Path]:
    return sorted(p for p in monitor_dir.iterdir() if (p / "state.json").is_file())


def resolve_runs(target: str, monitors: Optional[List[str]] = None) -> List[Path]:
    """Turn a config file, output dir, monitor dir or run dir into a list of run dirs (latest per monitor)."""
    p = Path(target).expanduser().resolve()
    if is_config(str(p)) and not (p / "state.json").is_file():
        cfg = load_config(str(p))
        out = []
        for m in cfg.monitors:
            if monitors and m.name not in monitors:
                continue
            md = Path(m.output_dir) / m.name
            if md.is_dir() and _runs_in(md):
                out.append(_runs_in(md)[-1])
        return out
    if not p.is_dir():
        raise SystemExit(f"not found: {p}")
    if (p / "state.json").is_file():
        return [p]
    if _runs_in(p):
        return [_runs_in(p)[-1]]
    out = []
    for md in sorted(x for x in p.iterdir() if x.is_dir()):
        if monitors and md.name not in monitors:
            continue
        runs = _runs_in(md)
        if runs:
            out.append(runs[-1])
    return out


# --------------------------------------------------------------------------- commands


def cmd_init(a: argparse.Namespace) -> int:
    if a.project:
        return init_project(Path(a.path if a.path != "orch.yaml" else "."), a.force)
    dest = Path(a.path)
    if dest.exists() and not a.force:
        print(f"{dest} exists (use --force to overwrite)")
        return 1
    dest.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {dest}. Edit workspace/goal/agents, then: orch validate {dest} && orch run {dest}")
    return 0


def init_project(root: Path, force: bool) -> int:
    """Scaffold <root>/.orch: settings.yaml (the model list) and one folder per agent referring to it by model id."""
    d = root.resolve() / ".orch"
    if (d / "agents").exists() and not force:
        print(f"{d / 'agents'} exists (use --force to overwrite)")
        return 1
    files = {
        ".gitignore": "runs/\nagents/*/state.md\nagents/*/checklist-m*.md\n",
        "settings.yaml": SETTINGS_YAML,
        "agents/manager/agent.yaml": """\
role: manager
description: Owns the milestones and the rubric; reports to me.
model: opus
backup: codex-astra
timeout: 2400
retries: 1
# check_every: 60           # minutes between status lines for me from the progress checks (no turn; 0 = off)
# the run as a whole
goal_file: goal.md          # only the manager reads the goal
max_rounds: 400             # worker/critic rounds in all milestones together (safety cap); keep it well above
                            # milestone_rounds, or the run stops in the middle of a milestone
approve_plan: true          # wait for my OK on the milestone plan
approve_milestones: true    # and after each milestone the manager accepts
# min_score: 4              # an ACCEPT below this overall score counts as REVISE
""",
        "agents/manager/goal.md": "Describe what you want done, and any decisions already made.\n",
        "agents/critic/agent.yaml": """\
role: critic
description: Turns milestones into instructions and reviews every change.
model: gpt-6.2-sol
backup: claude-haiku
backup2: qwen4-max-preview
timeout: 2400
retries: 1
checklist: true             # a PASS only counts when every checklist item (and every step of the plan) is [x]
milestone_rounds: 50        # rounds without a step of the critic's plan finishing before the manager and I look
checkin_every: 2            # every 2 rounds, a status line for the manager (from the plan: no model turn; 0 = off)
# job_wait: 21600           # 0 = don't look for jobs the worker left running; caps a wait the critic asks for only when checks are off
# check_session: side       # without a monitor, the critic runs the checks: side (a fresh session) or own
# job_ignore: ["status_loop"]  # commands never listed as running jobs (regexes), e.g. a long-lived helper
""",
        "agents/worker/agent.yaml": """\
role: worker
description: Does the work in the repository.
model: claude-haiku
backup: deepseek-v4-pro
backup2: glm-5.3
safety_timeout: 0           # no turn deadline; progress checks watch long-running work
check_every: 20             # minutes between quick checks of a running turn or background job (0 = off)
retries: 1
fresh_session: compact      # each milestone: the session is summarised in place (a new one if that fails); milestone = always new
# compact_between_steps: true  # Claude Code: /compact between steps of the critic's plan (never mid-task)
# snapshot: [docs]          # folders whose every version is kept in the run log
""",
        "agents/auditor/agent.yaml": AUDITOR_YAML,
        "agents/monitor/agent.yaml": MONITOR_YAML,
    }
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists() and not force:
            continue
        f.write_text(text, encoding="utf-8")
    print(f"created {d}\nWrite the goal in agents/manager/goal.md; edit models in .orch/settings.yaml. "
          f"Then: orch validate {root} && orch run {root} --serve")
    return 0


SETTINGS_YAML = """\
models:
  opus:
    backend: claude
    backend_options: {model: opus, effort: medium, permission_mode: auto, extra_args: ["--autocompact", "400k"]}
  codex-astra:
    backend: codex
    backend_options: {model: gpt-6.2-astra, effort: medium}
  gpt-6.2-sol:
    backend: codex
    backend_options: {model: gpt-6.2-sol, effort: medium}
  claude-haiku:
    backend: claude
    backend_options: {model: haiku, effort: medium, permission_mode: auto, extra_args: ["--autocompact", "400k"]}
  qwen4-max-preview:
    backend: opencode
    backend_options: {model: alibaba-token-plan-cn/qwen4-max-preview}
  deepseek-v4-pro:
    backend: opencode
    backend_options: {model: zenmux/deepseek/deepseek-v4-pro}
  glm-5.3:
    backend: opencode
    backend_options: {model: alibaba-token-plan-cn/glm-5.3}
  deepseek-v4-pro-auditor:
    backend: opencode
    backend_options:
      model: alibaba-token-plan-cn/deepseek-v4-pro
      disallowed_tools: [Bash, Read, Edit, Write, Glob, Grep, WebFetch, WebSearch, Task, NotebookEdit]
  deepseek-v4-pro-zenmux:
    backend: opencode
    backend_options:
      model: zenmux/deepseek/deepseek-v4-pro
      disallowed_tools: [Bash, Read, Edit, Write, Glob, Grep, WebFetch, WebSearch, Task, NotebookEdit]
  qwen3.8-flash:
    backend: opencode
    backend_options:
      model: alibaba-token-plan-cn/qwen3.8-flash
      disallowed_tools: [Edit, Write, NotebookEdit, Task, WebFetch, WebSearch]
  deepseek-v4.1-flash:
    backend: opencode
    backend_options:
      model: zenmux/deepseek/deepseek-v4.1-flash
      disallowed_tools: [Edit, Write, NotebookEdit, Task, WebFetch, WebSearch]
"""

AUDITOR_YAML = """\
role: auditor
description: Reads how the agents talk to each other (never the task) and keeps a short list of findings in audit.md.
model: deepseek-v4-pro-auditor
backup: deepseek-v4-pro-zenmux
timeout: 900
audit_every: 10             # rounds between audits, plus one when the run ends (0 = no automatic audits, not even the final one; only `orch audit`)
audit_points: 5             # at most this many points per audit
"""

MONITOR_YAML = """\
role: monitor
description: Watches the worker's long turns and jobs; reports evidence (never verdicts) to the critic.
model: qwen3.8-flash
backup: deepseek-v4.1-flash
timeout: 600
check_session: own          # own: one continuing session for its checks; side: a fresh session per check
"""

def cmd_audit(a: argparse.Namespace) -> int:
    """Audit how the agents of the latest run talked (AUD): the running Orch starts one auditor process; the points
    appear on the dashboard's audit page and in audit.md. --digest prints what the auditor would read (no model call)."""
    from .runtime import audit
    from .runtime.contracts import AuditRun
    from .runtime.store import Store
    from .runtime.adapters.base import SystemClock
    runs = _new_runs(a)
    for run in runs:
        if a.digest:
            store = Store(run, SystemClock(), readonly=True)
            snap = store.snapshot()
            messages, _ = store.messages(limit=0)
            print(audit.build(run, dataclasses.replace(snap, now=time.time()), messages,
                              audit.findings_lines(audit.findings_path(snap, run)))[0])
            continue
        print(f"queued: {write_command(run, AuditRun(at=time.time(), id=_new_command_id()))}")
        print("the running Orch starts the audit; the points appear on the dashboard's audit page")
    return 0 if runs else 1


def _load(path: str, check_paths: bool) -> Config:
    try:
        cfg = load_config(path)
        require_managed(cfg)
        for w in validate(cfg, check_paths=check_paths):
            print(f"warning: {w}", file=sys.stderr)
        return cfg
    except ConfigError as e:
        raise SystemExit(f"config error: {e}")


def cmd_validate(a: argparse.Namespace) -> int:
    cfg = _load(a.config, check_paths=True)
    print(f"OK  {cfg.path}\noutput_dir: {cfg.output_dir}")
    for m in cfg.monitors:
        flag = "" if m.enabled else "  (disabled)"
        print(f"\n● {m.name}{flag}  mode={m.controller.mode}  max_rounds={m.max_rounds}")
        print(f"  workspace:   {m.workspace}\n  working_dir: {m.working_dir}\n  output:      {Path(m.output_dir) / m.name}")
        if m.controller.agent:
            ca = m.controller.agent
            print(f"  controller:  {ca.name} [{ca.backend}]")
        for ag in m.agents:
            extra = ""
            if ag.role == "verifier":
                extra = f"  reviews={ag.reviews or 'all workers'} feedback_to={ag.feedback_to or ag.reviews or 'all workers'}"
            print(f"  - {ag.name:<18} {ag.role:<10} [{ag.backend}]{extra}")
            src = ag.system_prompt_path or ("inline" if ag.system_prompt else "none")
            mem = [f for f in ag.memory_files if Path(f).is_file() or Path(f).is_dir()]
            print(f"      prompt: {src}" + (f"   memory: {', '.join(Path(f).name for f in mem)}" if mem else ""))
    return 0


def _get_engine_cls() -> Any:
    if Engine is not None:
        return Engine
    from .runtime.engine import Engine as RtEngine
    return RtEngine


def _make_adapters(clock: Any) -> Dict[str, Callable[[], Any]]:
    from .runtime import adapters as rt_adapters
    backends = ("claude", "qoder", "qwen", "codex", "opencode", "agy", "command", "mock")
    return {b: (lambda b=b: rt_adapters.make(b, clock)) for b in backends if b in rt_adapters.ADAPTERS}


_STARTED_HEAD = ""  # the orch commit this process runs (read at its first run)


def _run_new_runtime(a: argparse.Namespace, cfg: Config, selected: List[Any]) -> int:
    from .runtime import migrate as rt_migrate
    from .runtime.adapters.base import SystemClock

    proj = find_project(a.config)
    if proj is not None:
        if settings_path(proj) is None:
            rt_migrate.write_settings(proj)
            cfg = _load(a.config, check_paths=True)
            selected = [m for m in cfg.monitors if (m.name in a.monitor if a.monitor else m.enabled)]
        rt_migrate.backup_prompts(proj)
    migrated_files = rt_migrate.migration_files(proj) if proj is not None else []
    settings = dict(cfg.settings or {})

    stop = threading.Event()

    def on_sigint(signum: Any, frame: Any) -> None:
        if stop.is_set():
            raise SystemExit(130)
        stop.set()

    prev_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_sigint)
    try:
        signal.signal(signal.SIGTERM, on_sigint)
    except (ValueError, OSError):
        pass

    global _STARTED_HEAD
    if not _STARTED_HEAD and not os.environ.get("ORCH_NO_GIT"):
        from . import update
        _STARTED_HEAD = update.head()
    engine_cls = _get_engine_cls()
    clock = SystemClock()
    adapters = _make_adapters(clock)
    engines: List[Any] = []
    servers: List[Any] = []
    restart: Optional[Tuple[List[str], str]] = None

    try:
        for m in selected:
            base = Path(m.output_dir) / m.name
            existing = _runs_in(base) if base.is_dir() else []
            if existing and (a.resume or "runtime" not in (read_json(existing[-1] / "state.json", {}) or {})):
                run_dir = existing[-1]
            elif existing and "runtime" in (read_json(existing[-1] / "state.json", {}) or {}):
                run_dir = existing[-1]
            else:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                run_dir = base / stamp
                idx = 1
                while run_dir.exists():
                    idx += 1
                    run_dir = base / f"{stamp}-{idx}"
                run_dir.mkdir(parents=True, exist_ok=True)

            st_file = run_dir / "state.json"
            snap = None
            if st_file.is_file():
                st_data = read_json(st_file, {}) or {}
                if "runtime" not in st_data:
                    rt_migrate.migrate_run(run_dir, clock=clock, settings=settings)
            else:
                snap = rt_migrate.build_snapshot(m, run_dir, clock, settings=settings, migrated_files=migrated_files)

            ctrl = getattr(m, "controller", None)
            eng = engine_cls(run_dir, clock, adapters,
                             dict(settings, audit_every=getattr(ctrl, "audit_every", 10),
                                  audit_points=getattr(ctrl, "audit_points", 5)))
            eng.start(snap)  # None: load the saved run
            engines.append(eng)
            if getattr(a, "serve", False):
                from .runtime import board as rt_board
                from .runtime import texts as rt_texts
                words = (lambda ref, eng=eng: rt_texts.words(ref, eng.store.snapshot()))
                srv, url = rt_board.serve(rt_board.Board(eng, run_dir, words=words, runs_root=base.parent),
                                          host=getattr(a, "host", "127.0.0.1"), port=getattr(a, "port", 8765))
                servers.append(srv)
                print(f"{m.name}: board at {url}", flush=True)
        # every monitor runs at the same time, as in the old modes
        threads = [threading.Thread(target=eng.run, args=(stop,), name=f"orch-engine-{i}", daemon=True)
                   for i, eng in enumerate(engines)]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(0.5)
                if any(getattr(e, "update_requested", False) for e in engines):
                    restart = _take_update(a, engines, servers[0].server_address[1] if servers else None)
                    if restart:
                        stop.set()
                        break
        except KeyboardInterrupt:
            stop.set()
    finally:
        stop.set()
        for srv in servers:
            srv.shutdown()
        for eng in engines:
            try:
                eng.close()  # agents and jobs keep running (REC-9)
            except Exception as exc:
                print(f"orch: could not close a run cleanly: {exc}", file=sys.stderr)
        try:
            signal.signal(signal.SIGINT, prev_int)
        except (ValueError, OSError):
            pass
    if restart:  # the new code passed: carry on in it from where each run is; the agents never stopped (REC-9)
        print(f"restarting with orch {restart[1]}: {' '.join(restart[0][1:])}", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(restart[0][0], restart[0])
    return 0


def _take_update(a: argparse.Namespace, engines: List[Any], port: Optional[int]) -> Optional[Tuple[List[str], str]]:
    """The dashboard's Update button: take in origin/main (or code already pulled by hand), check it with `orch
    validate`, and return the command to restart with. No waiting for a quiet moment: the agents keep running while Orch
    restarts and it reconnects to them. Each step goes to update.json in the run folders."""
    from . import update
    for e in engines:
        e.update_requested = False

    def tell(text: str, **status: Any) -> None:
        print(text, flush=True)
        for e in engines:
            update.write_status(e.run_dir, {"message": text, **status})

    config = str(Path(a.config).resolve())
    started = _STARTED_HEAD or update.head()
    res = update.apply(config)
    if not res.get("ok"):
        tell(f"⬆ update not done: {res.get('error')}", state="failed", error=res.get("error"))
        return None
    now = update.head()
    if not now or now == started:
        tell("⬆ orch is already up to date", state="current")
        return None
    if not res.get("updated"):  # pulled by hand since this process started: the code must still pass
        bad = update.validate_code(config)
        if bad:
            tell(f"⬆ the orch checkout is at {now[:7]}, but it fails `orch validate`: {bad}", state="failed", error=bad)
            return None
    tell(f"⬆ orch updated {started[:7]} → {now[:7]}; Orch restarts and carries on where it stopped",
         state="restarting", old=started[:7], new=now[:7])
    return update.restart_argv(sys.argv, port), now[:7]


def cmd_run(a: argparse.Namespace) -> int:
    cfg = _load(a.config, check_paths=True)
    selected = [m for m in cfg.monitors if (m.name in a.monitor if a.monitor else m.enabled)]
    if a.monitor:
        missing = set(a.monitor) - {m.name for m in cfg.monitors}
        if missing:
            raise SystemExit(f"unknown monitor(s): {', '.join(sorted(missing))}")
    if not selected:
        raise SystemExit("no monitors selected")
    return _run_new_runtime(a, cfg, selected)


def cmd_serve(a: argparse.Namespace) -> int:
    """The dashboard for saved runs, without running them: a project or config (its latest run; the others in the run
    list), or a run folder. Old runs are read-only; a command for a new one waits in its commands/ folder for its Orch."""
    from .runtime import board as rt_board
    from .runtime import texts as rt_texts
    runs = resolve_runs(a.target, a.monitor)
    if not runs:
        raise SystemExit("nothing to show")
    run = max(runs, key=lambda r: (r / "state.json").stat().st_mtime if (r / "state.json").exists() else 0)
    viewer = rt_board.Viewer(run)
    words = (lambda ref: rt_texts.words(ref, viewer.snapshot()))
    _, url = rt_board.serve(rt_board.Board(viewer, run, words=words, runs_root=run.parent.parent),
                            host=a.host, port=a.port)
    print(f"board: {url}   (Ctrl-C to quit)", flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def _is_new_run(run: Path, target: str = "") -> bool:
    st = read_json(run / "state.json", {}) or read_json(run / "state.prev.json", {}) or {}
    if "runtime" in st or "runtime_version" in st or "snap" in st:
        return True
    if target and is_config(target):
        try:
            cfg = load_config(target)
            if cfg.runtime == "new" and "rounds" not in st and "pending" not in st:
                return True
        except Exception:
            pass
    return False


def _new_runs(a: argparse.Namespace) -> List[Path]:
    """The runs a command is for; an old run (before the runtime) can only be read (`orch serve`, `orch show`)."""
    runs = resolve_runs(a.target, a.monitor)
    if not runs:
        print("no runs found")
    new = [r for r in runs if _is_new_run(r, a.target)]
    for r in runs:
        if r not in new:
            print(f"{r}: an old run, which can only be read (orch serve / orch show)")
    return new


def _new_command_id() -> str:
    return "c" + uuid.uuid4().hex[:11]


def write_command(run_dir: Path, command: Any) -> Path:
    """Atomically write <run_dir>/commands/<time>-<id>.json with to_dict(command)."""
    from .runtime.contracts import to_dict

    cmd_dir = Path(run_dir) / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    now_ms = int((getattr(command, "at", 0.0) or time.time()) * 1000)
    # names sort in the order written: never at or before a waiting command's time (same millisecond, clock step)
    for p in cmd_dir.glob("*.json"):
        head = p.name.split("-", 1)[0]
        if head.isdigit():
            now_ms = max(now_ms, int(head) + 1)
    cid = getattr(command, "id", "") or _new_command_id()
    fname = f"{now_ms:013d}-{cid}.json"
    dest = cmd_dir / fname
    fd, tmp_str = tempfile.mkstemp(dir=str(cmd_dir), prefix=f".{fname}.", suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(to_dict(command), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _open_gate_id(run: Path, gate_arg: str = "") -> str:
    if gate_arg:
        return gate_arg
    st = read_json(run / "state.json", {}) or {}
    gates = ((st.get("runtime") or {}).get("snap") or {}).get("gates") or {}
    for g in gates.values():
        if isinstance(g, dict) and not g.get("closed") and g.get("kind") in ("plan", "milestone", "milestone_change"):
            return str(g.get("id") or "")
    for g in gates.values():
        if isinstance(g, dict) and not g.get("closed"):
            return str(g.get("id") or "")
    return ""


def cmd_approve(a: argparse.Namespace) -> int:
    from .runtime.contracts import GateAnswer
    runs = _new_runs(a)
    for run in runs:
        gid = _open_gate_id(run, getattr(a, "gate", "") or "")
        if a.revise is not None:
            cmd = GateAnswer(at=time.time(), id=_new_command_id(), gate_id=gid, text=a.revise, approve=False)
        else:
            cmd = GateAnswer(at=time.time(), id=_new_command_id(), gate_id=gid, text=a.note or "", approve=True)
        print(f"queued: {write_command(run, cmd)}")
    return 0 if runs else 1


def cmd_answer(a: argparse.Namespace) -> int:
    from .runtime.contracts import GateAnswer
    runs = _new_runs(a)
    for run in runs:
        cmd = GateAnswer(at=time.time(), id=_new_command_id(), gate_id=a.qid, text=a.message)
        print(f"queued: {write_command(run, cmd)}")
    return 0 if runs else 1


def cmd_status(a: argparse.Namespace) -> int:
    p = Path(a.target).expanduser().resolve()
    roots = []
    if is_config(str(p)):
        cfg = _load(str(p), check_paths=False)
        roots = sorted({Path(m.output_dir) for m in cfg.monitors})
    else:
        roots = [p]
    any_run = False
    for root in roots:
        runs = report.collect_runs(root)
        if a.latest:
            seen, keep = set(), []
            for r in runs:
                if r.get("monitor") not in seen:
                    seen.add(r.get("monitor"))
                    keep.append(r)
            runs = keep
        for r in runs:
            any_run = True
            tc = r.get("turn_counts") or {}
            print(f"{r.get('monitor', '?'):<20} {r.get('run_id', '?'):<18} {r.get('status', '?'):<20} "
                  f"round {r.get('round')}/{r.get('max_rounds')}  turns {tc.get('total', 0)} "
                  f"(err {tc.get('error', 0) + tc.get('timeout', 0)})  {(r.get('updated_at') or '')[:19]}")
    if not any_run:
        print("no runs found")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    for run in resolve_runs(a.target, a.monitor):
        st = read_json(run / "state.json", {}) or {}
        print(f"═══ {st.get('monitor')} / {st.get('run_id')}  [{st.get('status')}]  {run}")
        if a.turn:
            hits = list((run / "rounds").glob(f"*/t*-*.json"))
            for h in hits:
                d = read_json(h, {}) or {}
                if d.get("turn_id") == a.turn or d.get("turn_id", "").startswith(a.turn):
                    print(json.dumps({k: v for k, v in d.items() if k not in ("input", "output", "system_prompt")}, indent=2))
                    print("\n──── INPUT ────\n" + (d.get("input") or ""))
                    print("──── OUTPUT ────\n" + (d.get("output") or ""))
                    if d.get("stderr"):
                        print("──── STDERR ────\n" + d["stderr"])
            continue
        for rdir in sorted((run / "rounds").iterdir()):
            rs = read_json(rdir / "round.json", {}) or {}
            if a.round and rs.get("round") != a.round:
                continue
            print(f"\nRound {rs.get('round')}  [{rs.get('status')}]  {rs.get('decision') or ''}")
            if rs.get("verdicts"):
                print("  verdicts: " + ", ".join(f"{k}={v}" for k, v in rs["verdicts"].items()))
            for tp in sorted(rdir.glob("t*.json")):
                t = read_json(tp, {}) or {}
                out = (t.get("output") or "").strip().replace("\n", " ")
                print(f"  {t.get('turn_id', '?'):<32} {t.get('status', '?'):<8} {str(t.get('duration_s')):>7}s  "
                      f"{out[:a.width] + ('…' if len(out) > a.width else '')}")
        print(f"\nreport: {run / 'report.html'}")
    return 0


def cmd_stop(a: argparse.Namespace) -> int:
    from .runtime.contracts import StopOrch
    runs = _new_runs(a)
    for run in runs:
        cmd = StopOrch(at=time.time(), id=_new_command_id(), agents=bool(getattr(a, "agents", False)))
        print(f"stop requested: {write_command(run, cmd)}")
    return 0 if runs else 1


def cmd_tell(a: argparse.Namespace) -> int:
    """Message one agent directly: `orch tell <run> <agent> "<text>"`."""
    from .runtime.contracts import Chat
    text = a.message if a.message != "-" else sys.stdin.read()
    if not text.strip():
        print("empty message")
        return 1
    runs = _new_runs(a)
    status = 0 if runs else 1
    for run in runs:
        st = read_json(run / "state.json", {}) or {}
        names = list((((st.get("runtime") or {}).get("snap") or {}).get("agents") or {}).keys())
        if names and a.agent not in names:
            print(f"{run}: no agent named {a.agent!r} (agents: {', '.join(names)})")
            status = 1
            continue
        p = write_command(run, Chat(agent=a.agent, at=time.time(), id=_new_command_id(), text=text))
        print(f"queued for {a.agent}: {p}")
    return status


def _terminal_new_run(a: argparse.Namespace, run: Path) -> int:
    from .runtime import adapters as rt_adapters
    from .runtime import models as rt_models
    from .runtime import texts as rt_texts
    from .runtime.adapters.base import SystemClock
    from .runtime.contracts import AgentSpec, Role, Snapshot, TerminalClose, TerminalOpen, from_dict

    st = read_json(run / "state.json", {}) or {}
    snap_raw = (st.get("runtime") or {}).get("snap") or {}
    snap = from_dict(Snapshot, snap_raw)
    name = a.agent or (snap.manager.id if snap.manager else next(iter(snap.agents), None))
    ag = snap.agents.get(name) if name else None
    sess = (ag.session or next(iter(ag.sessions.values()), None)) if ag else None
    if ag is None or sess is None or not sess.session_id:
        print(f"{run}: {name or 'the manager'} has no session yet")
        return 1

    settings_data: Dict[str, Any] = {}
    agent_yaml: Dict[str, Any] = {}
    proj = find_project(a.target) or (find_project(snap.run.workspace) if snap.run.workspace else None)
    if proj is not None:
        from .config import project_settings
        try:
            settings_data = project_settings(proj)
        except Exception:
            settings_data = {}
        ay_path = proj / "agents" / ag.id / "agent.yaml"
        if ay_path.is_file():
            try:
                import yaml
                agent_yaml = yaml.safe_load(ay_path.read_text(encoding="utf-8")) or {}
            except Exception:
                agent_yaml = {}

    base_choice = sess.choice if sess.choice.backend else ag.choice
    resolved_choice = rt_models.resolve(base_choice, settings_data, agent_yaml)
    cwd = sess.cwd or ag.cwd or snap.run.workspace or "."
    try:
        sys_prompt = rt_texts.system_prompt(snap, ag.id)
    except Exception:
        sys_prompt = ""
    spec = AgentSpec(
        agent=ag.id,
        role=ag.role if isinstance(ag.role, Role) else Role(ag.role),
        choice=resolved_choice,
        cwd=cwd,
        system_prompt=sys_prompt,
        run_dir=str(run),
    )
    adapter = rt_adapters.make(resolved_choice.backend or "claude", SystemClock())
    cmd = adapter.terminal_command(spec, sess)

    open_cmd = TerminalOpen(
        agent=ag.id,
        at=time.time(),
        id=_new_command_id(),
        stop_job=bool(getattr(a, "stop_job", False)),
    )
    write_command(run, open_cmd)
    try:
        print(" ".join(shlex.quote(str(x)) for x in cmd))
        runner = getattr(a, "runner", None)
        if not getattr(a, "print_only", False) and (
            runner is not None
            or subprocess.run is not _REAL_SUBPROCESS_RUN
            or "unittest" not in sys.modules
        ):
            env = {**os.environ, **(resolved_choice.env or {})}
            (runner or subprocess.run)(cmd, cwd=cwd, env=env)
    finally:
        close_cmd = TerminalClose(agent=ag.id, at=time.time(), id=_new_command_id())
        write_command(run, close_cmd)
    return 0


def cmd_terminal(a: argparse.Namespace) -> int:
    """Open an agent's session (the manager's by default) in a terminal: Orch holds its messages meanwhile (TERM)."""
    runs = _new_runs(a)
    return _terminal_new_run(a, runs[0]) if runs else 1


def cmd_report(a: argparse.Namespace) -> int:
    runs = resolve_runs(a.target, a.monitor)
    roots = set()
    for run in runs:
        print(report.write_run_report(run))
        roots.add(run.parent.parent)
    for root in roots:
        print(report.write_index(root))
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="orch", description="Orchestrate teams of CLI coding agents.")
    ap.add_argument("--version", action="version", version=f"orch {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write an example config, or --project: a .orch folder for this project")
    p.add_argument("path", nargs="?", default="orch.yaml", help="config file, or the project root with --project")
    p.add_argument("--project", action="store_true", help="scaffold <path>/.orch with manager, critic, worker folders")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="check a config and print the resolved monitors")
    p.add_argument("config", nargs="?", default=".", help="config file or project folder (default: ./.orch)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run monitors (concurrently)")
    p.add_argument("config", nargs="?", default=".", help="config file or project folder (default: ./.orch)")
    p.add_argument("-m", "--monitor", action="append", help="only this monitor (repeatable); default: all enabled")
    p.add_argument("--resume", action="store_true", help="continue each monitor's latest unfinished run and sessions")
    p.add_argument("--serve", action="store_true", help="also start the board (local web page) for these runs")
    p.add_argument("--port", type=int, default=8765, help="board port (default 8765; the next free one is used)")
    p.add_argument("--host", default="127.0.0.1",
                   help="board address (default 127.0.0.1, this machine only; 0.0.0.0 opens it to your network, "
                        "with no password: anyone who reaches it can steer the run)")
    p.add_argument("--open", action="store_true", help="open the board in the browser")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="start the board for saved runs, without running them")
    p.add_argument("target", nargs="?", default=".", help="project folder or config (default: ./.orch), or a run dir")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="board address (default 127.0.0.1, this machine only; 0.0.0.0 opens it to your network, "
                        "with no password: anyone who reaches it can steer the run)")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("approve", help="approve the plan / milestone the run is waiting on")
    p.add_argument("target", nargs="?", default=".")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--note", help="a note for the manager along with the approval")
    p.add_argument("--revise", metavar="TEXT", help="send it back instead, with what still needs work")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("answer", help="answer an open question, e.g. orch answer . Q3 \"B\"")
    p.add_argument("target")
    p.add_argument("qid")
    p.add_argument("message")
    p.add_argument("-m", "--monitor", action="append")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("status", help="list runs")
    p.add_argument("target", help="config file or output dir")
    p.add_argument("--latest", action="store_true", help="only the latest run per monitor")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", help="print rounds/turns of a run, or one turn in full")
    p.add_argument("target", help="run dir, monitor dir, output dir or config")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--round", type=int)
    p.add_argument("--turn", help="turn id (prefix ok), e.g. r002-t01")
    p.add_argument("--width", type=int, default=90)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("stop", help="close Orch for a run (the agents carry on unless --agents)")
    p.add_argument("target")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--agents", action="store_true", help="also stop the running agents and their jobs")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("tell", help="message one agent directly, e.g. orch tell <run> critic \"focus on X\"")
    p.add_argument("target")
    p.add_argument("agent", help="agent id, e.g. critic or worker-1")
    p.add_argument("message", help="text, or - to read stdin")
    p.add_argument("-m", "--monitor", action="append")
    p.set_defaults(func=cmd_tell)

    p = sub.add_parser("audit", help="audit how the agents talk (the auditor's findings go to audit.md)")
    p.add_argument("target", nargs="?", default=".", help="project root, config file or run dir")
    p.add_argument("--monitor", action="append", help="only these monitors")
    p.add_argument("--digest", action="store_true", help="print the digest the auditor would read; no model call")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("terminal", help="open an agent's session in a terminal (Orch holds its messages meanwhile)")
    p.add_argument("target", nargs="?", default=".")
    p.add_argument("agent", nargs="?", default=None, help="agent name (default: the manager)")
    p.add_argument("--monitor", action="append")
    p.set_defaults(func=cmd_terminal)

    p = sub.add_parser("report", help="regenerate HTML reports")
    p.add_argument("target")
    p.add_argument("-m", "--monitor", action="append")
    p.set_defaults(func=cmd_report)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.func(a) or 0)
