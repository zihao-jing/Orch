"""Command-line interface: ``orch <command>`` (or ``python -m orch <command>``)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, is_config, load_config, validate
from .controller import MonitorRunner
from .recorder import read_json, write_json
from . import report

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
    """Scaffold <root>/.orch: one folder per agent (manager, critic, worker), prompts inherited from orch."""
    d = root.resolve() / ".orch"
    if (d / "agents").exists() and not force:
        print(f"{d / 'agents'} exists (use --force to overwrite)")
        return 1
    files = {
        ".gitignore": "runs/\n",
        "agents/manager/agent.yaml": """\
role: manager
description: Owns the milestones and the rubric; reports to me.
backend: claude
backend_options: {model: opus, effort: medium, permission_mode: auto}
timeout: 2400
retries: 1
# the run as a whole
goal_file: goal.md          # only the manager reads the goal
max_rounds: 60              # worker/critic rounds in all milestones together (safety cap); keep it well above
                            # milestone_rounds, or the run stops in the middle of a milestone
approve_plan: true          # wait for my OK on the milestone plan
approve_milestones: true    # and after each milestone the manager accepts
# min_score: 4              # an ACCEPT below this overall score counts as REVISE
""",
        "agents/manager/goal.md": "Describe what you want done, and any decisions already made.\n",
        "agents/critic/agent.yaml": """\
role: critic
description: Turns milestones into instructions and reviews every change.
backend: claude
backend_options: {model: opus, effort: medium, permission_mode: auto}
timeout: 2400
retries: 1
checklist: true             # a PASS only counts when every checklist item is [x]
milestone_rounds: 10        # rounds per milestone before the manager and I look at it anyway
checkin_every: 2            # every 2 rounds, a short update for the manager (0 = off)
# job_wait: 21600           # how long a review waits for a job the worker is waiting on (seconds; 0 = never)
""",
        "agents/worker/agent.yaml": """\
role: worker
description: Does the work in the repository.
backend: claude
backend_options: {model: sonnet, effort: medium, permission_mode: auto}
# Codex instead: backend: codex, backend_options: {model: <model>, sandbox: workspace-write,
#                extra_args: ["-c", "sandbox_workspace_write.network_access=true"]}
timeout: 7200               # per turn; long jobs run in the background and are waited on with WAITING ON JOB
retries: 1
fresh_session: milestone    # a new session for each milestone: turns stay fast and cheap; the files carry the state
# snapshot: [docs]          # folders whose every version is kept in the run log
""",
    }
    from .config import PROMPTS_DIR
    for name in ("manager", "critic", "worker"):
        files[f"agents/{name}/memory.md"] = MEMORY_STUB
        # the role's default prompt, copied so this project can adapt it
        files[f"agents/{name}/system_prompt.md"] = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists() and not force:
            continue
        f.write_text(text, encoding="utf-8")
    print(f"created {d}\nWrite the goal in agents/manager/goal.md; each agents/<name>/system_prompt.md starts "
          f"as orch's default for that role; adapt it for this project. Then: orch validate {root} && orch run {root} --serve")
    return 0


MEMORY_STUB = """\
<!-- Notes and preferences for this agent in this project. Everything outside these comment marks is added to
     the end of its system prompt (re-read before every turn). Files in memory/*.md next to this one are added too. -->
"""


def _load(path: str, check_paths: bool) -> Config:
    try:
        cfg = load_config(path)
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


def cmd_run(a: argparse.Namespace) -> int:
    cfg = _load(a.config, check_paths=True)
    selected = [m for m in cfg.monitors if (m.name in a.monitor if a.monitor else m.enabled)]
    if a.monitor:
        missing = set(a.monitor) - {m.name for m in cfg.monitors}
        if missing:
            raise SystemExit(f"unknown monitor(s): {', '.join(sorted(missing))}")
    if not selected:
        raise SystemExit("no monitors selected")
    if a.dry_run:
        def mockify(ag):
            return dataclasses.replace(ag, backend="mock", backend_options={"delay": 0.2})
        selected = [dataclasses.replace(
            m, agents=[mockify(x) for x in m.agents],
            controller=dataclasses.replace(m.controller, agent=mockify(m.controller.agent) if m.controller.agent else None),
        ) for m in selected]
        print("dry run: every agent uses the mock backend")
    if a.max_rounds:
        selected = [dataclasses.replace(m, max_rounds=a.max_rounds) for m in selected]

    stop = threading.Event()
    lock = threading.Lock()

    def log(s: str) -> None:
        with lock:
            print(time.strftime("%H:%M:%S ") + s, flush=True)

    def on_sigint(signum, frame):
        if stop.is_set():
            raise SystemExit(130)
        log("stopping: current turns are being terminated (Ctrl-C again to force)")
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        signal.signal(signal.SIGTERM, on_sigint)
    except (ValueError, OSError):
        pass

    results = {}
    runners = [MonitorRunner(m, stop, resume=a.resume, log=log) for m in selected]
    if a.serve:
        from .board import Board, serve
        boards = {r.mon.name: Board(r.mon.name, (lambda r=r: getattr(r, "rec", None) and r.rec.run_dir), r.mon)
                  for r in runners}
        _, url = serve(boards, port=a.port)
        log(f"board: {url}  (live progress, your questions and approvals, chat with the manager)")
        if a.open:
            import webbrowser
            webbrowser.open(url)
    threads = []
    for r in runners:
        t = threading.Thread(target=lambda r=r: results.__setitem__(r.mon.name, r.run()), name=r.mon.name, daemon=True)
        t.start()
        threads.append(t)
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=0.3)
    print("\nsummary:")
    for r in runners:
        rd = getattr(r, "rec", None)
        print(f"  {r.mon.name:<20} {results.get(r.mon.name, '?'):<20} {rd.run_dir / 'report.html' if rd else ''}")
    if runners and getattr(runners[0], "rec", None):
        print(f"index: {Path(selected[0].output_dir) / 'index.html'}")
    if a.serve and not stop.is_set():
        print(f"the board stays up at {url} (Ctrl-C to quit)", flush=True)
        try:
            while not stop.is_set():
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            pass
    return 0 if all(v == "completed" for v in results.values()) else 2


def cmd_serve(a: argparse.Namespace) -> int:
    """Board for existing runs: a config (chat enabled, latest run per monitor) or a run folder (read-only)."""
    from .board import Board, serve
    p = Path(a.target).expanduser().resolve()
    boards = {}
    if is_config(str(p)):
        cfg = _load(str(p), check_paths=False)
        for m in cfg.monitors:
            if a.monitor and m.name not in a.monitor:
                continue
            md = Path(m.output_dir) / m.name

            def latest(md=md):
                runs = _runs_in(md) if md.is_dir() else []
                return runs[-1] if runs else None
            boards[m.name] = Board(m.name, latest, m)
    else:
        for run in resolve_runs(a.target, a.monitor):
            boards[run.parent.name] = Board(run.parent.name, (lambda run=run: run), None)
    if not boards:
        raise SystemExit("nothing to show")
    _, url = serve(boards, port=a.port)
    print(f"board: {url}   (Ctrl-C to quit)", flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def _control(a: argparse.Namespace, data: dict) -> int:
    runs = resolve_runs(a.target, a.monitor)
    if not runs:
        print("no runs found")
        return 1
    for run in runs:
        inbox = run / "inbox"
        inbox.mkdir(exist_ok=True)
        p = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{data['kind']}.json"
        write_json(p, data)
        print(f"queued: {p}")
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    if a.revise is not None:
        return _control(a, {"kind": "gate", "decision": "revise", "message": a.revise})
    return _control(a, {"kind": "gate", "decision": "approve", "message": a.note or ""})


def cmd_answer(a: argparse.Namespace) -> int:
    return _control(a, {"kind": "answer", "qid": a.qid.upper(), "message": a.message})


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
    runs = resolve_runs(a.target, a.monitor)
    if not runs:
        print("no runs found")
        return 1
    for run in runs:
        (run / "STOP").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        print(f"stop requested: {run}")
    return 0


def cmd_inject(a: argparse.Namespace) -> int:
    runs = resolve_runs(a.target, a.monitor)
    if not runs:
        print("no runs found")
        return 1
    text = a.message if a.message != "-" else sys.stdin.read()
    for run in runs:
        inbox = run / "inbox"
        inbox.mkdir(exist_ok=True)
        p = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json"
        write_json(p, {"to": a.to or None, "message": text})
        print(f"queued for next round: {p}")
    return 0


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
    p.add_argument("--dry-run", action="store_true", help="replace every backend with the mock backend")
    p.add_argument("--max-rounds", type=int, help="override max_rounds for this invocation")
    p.add_argument("--serve", action="store_true", help="also start the board (local web page) for these runs")
    p.add_argument("--port", type=int, default=8765, help="board port (default 8765; the next free one is used)")
    p.add_argument("--open", action="store_true", help="open the board in the browser")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="start the board for existing runs (config: chat with the manager enabled)")
    p.add_argument("target", nargs="?", default=".", help="project folder or config (default: ./.orch), or a run dir")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("approve", help="managed mode: approve the plan / milestone the run is waiting on")
    p.add_argument("target", nargs="?", default=".")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--note", help="a note for the manager along with the approval")
    p.add_argument("--revise", metavar="TEXT", help="send it back instead, with what still needs work")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("answer", help="managed mode: answer an open question, e.g. orch answer cfg.yaml Q3 \"B\"")
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

    p = sub.add_parser("stop", help="ask a running monitor to stop after its current turn")
    p.add_argument("target")
    p.add_argument("-m", "--monitor", action="append")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("inject", help="send a human message into the next round")
    p.add_argument("target")
    p.add_argument("message", help="text, or - to read stdin")
    p.add_argument("-m", "--monitor", action="append")
    p.add_argument("--to", action="append", help="only these agents (pipeline); annotated for the llm controller")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("report", help="regenerate HTML reports")
    p.add_argument("target")
    p.add_argument("-m", "--monitor", action="append")
    p.set_defaults(func=cmd_report)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.func(a) or 0)
