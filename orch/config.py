"""Config loading, defaults merging and validation.

Supported formats: YAML (.yaml/.yml, needs PyYAML), JSON (.json), TOML (.toml, Python 3.11+).
Relative paths are resolved against the config file's directory; ``~`` and
``$VARS`` are expanded. Any of ``goal``, ``instructions``, ``system_prompt``,
``task`` and ``description`` may instead be given as ``<key>_file`` pointing at a
text file.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import reminders as R
from .templates import DEFAULT_TEMPLATES, RETIRED_TEMPLATES

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TEXT_KEYS = ("goal", "instructions", "system_prompt", "task", "description")
MODES = ("pipeline", "llm", "managed")


# Agent settings that take effect on the agent's next turn without restarting the run. Everything else
# (prompts, memory, role, working folder, name) is read at start; a change there is reported, not applied.
HOT_AGENT_FIELDS = ("backend", "backend_options", "env", "timeout", "retries", "fallback", "fallback_cooldown",
                    "safety_timeout", "check_every", "check_session", "compact_between_steps", "model_id",
                    "retry_waits")
# prompt_vars the run sets itself (not the config): kept when the config is re-read, so they never count as a change
RUN_PROMPT_VARS = ("golden_board", "checklist_files")

# B8: a failed turn (provider error, overload, rate limit, crash) is retried on the same model after each of these
# waits (seconds), then the backups take over in order. `retry_waits` in the project settings or an agent's YAML.
RETRY_WAITS = [60.0, 120.0, 240.0]
# B8: the project's model list: `.orch/settings.yaml` (a project folder) or `models:` at the top of a config file
SETTINGS_FILES = ("settings.yaml", "settings.yml", "settings.json")
SETTINGS_KEYS = ("models", "retry_waits", "fallback_cooldown", "runtime")
MODEL_KEYS = ("backend", "backend_options", "env", "retries")  # a model is shown by its id

# B5: a turn that progress checks watch (a worker's, in managed mode) may run long; only this high limit stops it
CHECK_EVERY = 20.0  # minutes between quick checks of a worker's turn or job
MANAGER_CHECK_EVERY = 60.0  # minutes between status summaries for the manager while work runs
CHECK_SESSIONS = ("own", "side")


class ConfigError(Exception):
    pass


@dataclass
class Variant:
    """One way to run an agent: its primary model (index 0) or one of its backups, in order."""
    index: int
    label: str
    backend: str
    options: Dict[str, Any]
    env: Dict[str, str]
    retries: int
    model_id: str = ""  # its id in the project's model list, if it came from there

    @property
    def session_key(self) -> str:
        """Variants with the same key can continue each other's sessions (same backend and provider endpoint);
        a switch to a different key starts a new session."""
        urls = sorted(f"{k}={v}" for k, v in self.env.items() if k.endswith("_BASE_URL") and v)
        return "|".join([self.backend] + urls)


@dataclass
class AgentConfig:
    name: str
    role: str
    description: str = ""
    system_prompt: str = ""
    task: str = ""
    backend: str = "claude"
    backend_options: Dict[str, Any] = field(default_factory=dict)
    working_dir: str = ""  # absolute
    timeout: float = 1800.0
    retries: int = 0
    env: Dict[str, str] = field(default_factory=dict)
    reviews: List[str] = field(default_factory=list)  # verifiers: which workers they review
    feedback_to: List[str] = field(default_factory=list)  # verifiers: who receives their feedback
    display_name: str = ""  # how the others refer to this agent in relayed messages (default: name)
    system_prompt_path: str = ""  # system_prompt_file: re-read before every turn, so edits apply mid-run
    memory_files: List[str] = field(default_factory=list)  # notes/preferences (files or folders of .md)
    memory_heading: str = "Things to keep in mind:"
    prompt_vars: Dict[str, str] = field(default_factory=dict)  # {{manager}}, {{critic}}, {{worker}} in prompts
    protocol_path: str = ""  # managed mode: orch's fixed signals and boundaries for this role, added after the prompt
    writing_path: str = ""  # managed mode: the project's writing rules (its `.orch/writing.md` or a path), added after it
    style_path: str = ""  # old runtime only: a style file to add after that (none since the new runtime)
    # at each milestone start (managed mode): "compact" summarises the session in place (a new session when the
    # backend can't compact), "milestone" starts a new session, "never" keeps it; "" = the role's default (the critic
    # and workers compact, the manager keeps its session)
    fresh_session: str = ""
    state_file: str = ""  # managed mode: the agent's working state (agents/<name>/state.md in a project folder)
    # backup models, tried in order when a turn still fails after its retries: each is a dict with any of
    # label, backend, backend_options, env, retries (already merged with the primary's settings when loaded)
    fallback: List[Dict[str, Any]] = field(default_factory=list)
    fallback_cooldown: float = 18000.0  # 5 h: seconds an agent stays on the backup that worked before trying its primary again
    # B5 progress checks. safety_timeout: the hard per-turn limit in seconds (None: see turn_limit). check_every:
    # minutes between quick checks while this agent works (a worker: its turns and jobs; the manager: its status
    # summaries); None = the default, 0 = off. check_session: where this agent runs the checks it does (a monitor,
    # or the critic when there is no monitor): "own" (its continuing session) or "side" (a fresh session per check)
    safety_timeout: Optional[float] = None
    check_every: Optional[float] = None
    check_session: str = ""
    # B7: orch asks the CLI to compact this agent's session between steps of the critic's plan (never mid-task);
    # compaction otherwise stays the CLI's own (e.g. Claude Code's auto-compact, `--autocompact` in extra_args)
    compact_between_steps: bool = False
    # B8: `model: <id>` from the project's model list (the primary's id; backups carry theirs in `fallback`), and the
    # waits before each retry of the same model
    model_id: str = ""
    retry_waits: List[float] = field(default_factory=lambda: list(RETRY_WAITS))

    @property
    def fresh_mode(self) -> str:
        """What a milestone start does to this agent's session in managed mode: "compact" (summarise it in place; a
        new session when the backend can't), "milestone" (a new session) or "" (never: it goes on as it is)."""
        if self.fresh_session == "never":
            return ""
        return self.fresh_session or ("compact" if self.role in ("verifier", "worker") else "")

    def turn_limit(self, watched: bool = False) -> float:
        """Seconds before stopping a turn; zero means unlimited. Watched workers have no default deadline.
        An explicit safety_timeout overrides this, including zero. Unwatched turns retain timeout."""
        if self.safety_timeout is not None:
            return float(self.safety_timeout)
        return 0.0 if watched else float(self.timeout or 0)

    def variants(self) -> List[Variant]:
        """The primary model first, then each backup."""
        out = [Variant(0, self.model_id or _variant_label(self.backend, self.backend_options), self.backend,
                       dict(self.backend_options), dict(self.env), self.retries, self.model_id)]
        for i, f in enumerate(self.fallback, 1):
            b = str(f.get("backend") or self.backend)
            opts = dict(f.get("backend_options") or {})
            out.append(Variant(i, str(f.get("label") or _variant_label(b, opts)), b, opts,
                               dict(f.get("env") or {}), int(f.get("retries", self.retries)),
                               str(f.get("model_id") or "")))
        return out

    @property
    def called(self) -> str:
        return self.display_name or f"the {self.name}"  # by role, e.g. "the critic", unless a name is given

    def current_system_prompt(self) -> str:
        """The system prompt as it is on disk right now, followed by the agent's memory notes."""
        sp = self.system_prompt
        if self.system_prompt_path:
            try:
                sp = Path(self.system_prompt_path).read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if "{{" in sp and self.prompt_vars:
            from .templates import render
            sp = render(sp, **self.prompt_vars).strip()
        notes = []
        for f in self.memory_files:
            path = Path(f)
            for x in (sorted(path.glob("*.md")) if path.is_dir() else [path]):
                try:
                    text = re.sub(r"<!--.*?-->", "", x.read_text(encoding="utf-8"), flags=re.S).strip()
                except OSError:
                    continue
                if text:
                    notes.append(text)
        if self.protocol_path:
            try:
                proto = Path(self.protocol_path).read_text(encoding="utf-8").strip()
            except OSError:
                proto = ""
            if proto:
                from .templates import render
                sp = (sp + "\n\n" if sp else "") + render(proto, **(self.prompt_vars or {})).strip()
        if self.style_path:  # the "All" rules and this role's, the same ones the style reminder repeats
            from .reminders import style_section
            from .templates import render
            try:
                style = style_section(Path(self.style_path).read_text(encoding="utf-8"), self.role)
            except OSError:
                style = ""
            if style:
                sp = (sp + "\n\n" if sp else "") + render(style, **(self.prompt_vars or {})).strip()
        if self.writing_path:  # read each time, so an edit reaches the agent with its next system prompt
            try:
                rules = re.sub(r"<!--.*?-->", "", Path(self.writing_path).read_text(encoding="utf-8"), flags=re.S).strip()
            except OSError:
                rules = ""
            if rules:
                sp = (sp + "\n\n" if sp else "") + rules
        if notes:
            sp = (sp + "\n\n" if sp else "") + (self.memory_heading + "\n\n" if self.memory_heading else "") \
                + "\n\n".join(notes)
        return sp

    def preference_path(self) -> Optional[Path]:
        """agents/<name>/preference.md, beside state.md. Not part of the prompt: the agent reads it when relevant."""
        return Path(self.state_file).parent / "preference.md" if self.state_file else None

    def public(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["env"] = sorted(self.env)  # don't leak env values into logs
        d["fallback"] = [{**f, "env": sorted(f.get("env") or {})} for f in self.fallback]
        return d


def _variant_label(backend: str, options: Dict[str, Any]) -> str:
    return f"{backend}:{options['model']}" if options.get("model") else backend


def _build_fallback(raw: Any, primary: Dict[str, Any], where: str) -> List[Dict[str, Any]]:
    """`fallback:` in an agent config: a list of backups, each a model name (same backend and settings, other
    model) or a mapping with any of label, backend, backend_options, env, retries. With the same backend as the
    primary, backend_options are merged over the primary's (null removes one) and env is kept unless given;
    with another backend, both start empty. A given env replaces the primary's whole env."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for j, item in enumerate(items):
        w = f"{where}: fallback[{j}]"
        if isinstance(item, str):
            item = {"backend_options": {"model": item}}
        if not isinstance(item, dict):
            raise ConfigError(f"{w} must be a model name or a mapping")
        unknown = set(item) - {"label", "backend", "backend_options", "env", "retries", "model_id"}
        if unknown:
            raise ConfigError(f"{w}: unknown keys: {', '.join(sorted(unknown))}")
        backend = str(item.get("backend") or primary["backend"])
        same = backend == primary["backend"]
        opts = _deep_merge(primary["backend_options"] if same else {}, dict(item.get("backend_options") or {}))
        opts = {k: v for k, v in opts.items() if v is not None}
        if "env" in item:
            env = {str(k): str(v) for k, v in (item.get("env") or {}).items() if v is not None}
        else:
            env = dict(primary["env"]) if same else {}
        f: Dict[str, Any] = {"backend": backend, "backend_options": opts, "env": env,
                             "retries": int(item.get("retries", primary["retries"]))}
        if item.get("label"):
            f["label"] = str(item["label"])
        if item.get("model_id"):
            f["model_id"] = str(item["model_id"])
        out.append(f)
    return out


def parse_models(raw: Any, where: str = "models") -> Dict[str, Dict[str, Any]]:
    """The project's model list: id -> {backend, backend_options, env, retries?}. `$VARS` in env values are
    expanded, so a key can stay in the shell's environment (`ANTHROPIC_AUTH_TOKEN: ${GLM_KEY}`)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping of id -> model settings")
    out: Dict[str, Dict[str, Any]] = {}
    for mid, entry in raw.items():
        w = f"{where}.{mid}"
        if not NAME_RE.match(str(mid)):
            raise ConfigError(f"{w}: a model id may only use letters, digits, '_', '.', '-'")
        if isinstance(entry, str):  # `opus: claude` is not meaningful; ask for the mapping
            raise ConfigError(f"{w} must be a mapping with backend and backend_options")
        if not isinstance(entry, dict):
            raise ConfigError(f"{w} must be a mapping")
        unknown = set(entry) - set(MODEL_KEYS)
        if unknown:
            raise ConfigError(f"{w}: unknown keys: {', '.join(sorted(unknown))}")
        opts = entry.get("backend_options") or {}
        env = entry.get("env") or {}
        if not isinstance(opts, dict) or not isinstance(env, dict):
            raise ConfigError(f"{w}: backend_options and env must be mappings")
        m: Dict[str, Any] = {"backend": str(entry.get("backend") or "claude"), "backend_options": dict(opts),
                             "env": {str(k): os.path.expandvars(str(v)) for k, v in env.items() if v is not None}}
        if entry.get("retries") is not None:
            m["retries"] = int(entry["retries"])
        out[str(mid)] = m
    return out


def _model(models: Dict[str, Dict[str, Any]], mid: Any, where: str) -> Dict[str, Any]:
    mid = str(mid)
    if mid not in models:
        raise ConfigError(f"{where}: unknown model id {mid!r}; the project's model list has: "
                          f"{', '.join(sorted(models)) or '(none: add `models:` to the project settings)'}")
    return models[mid]


def _resolve_models(merged: Dict[str, Any], own: Dict[str, Any], defaults: Dict[str, Any],
                    models: Dict[str, Dict[str, Any]], where: str) -> Dict[str, Any]:
    """`model: <id>`, `backup: <id>`, `backup2: <id>` in an agent's settings, resolved from the project's model list
    into the inline form (backend, backend_options, env, fallback). Options the agent itself gives are applied on top
    of its model's (and of a backup's on the same backend, except `model`); its env on top of every model's (a
    backup's own env keys win). Without these keys the inline settings are used as they are."""
    mid, b1, b2 = merged.get("model"), merged.get("backup"), merged.get("backup2")
    if not (mid or b1 or b2):
        return merged
    if (b1 or b2) and merged.get("fallback") is not None:
        raise ConfigError(f"{where}: give backups either as `backup`/`backup2` ids or as `fallback`, not both")
    if b2 and not b1:
        raise ConfigError(f"{where}: `backup2` needs a `backup`")
    out = {k: v for k, v in merged.items() if k not in ("model", "backup", "backup2")}
    own_opts = dict(own.get("backend_options") or {})
    own_env = {str(k): str(v) for k, v in (own.get("env") or {}).items()}
    def_opts = {k: v for k, v in (defaults.get("backend_options") or {}).items() if k != "model"}
    def_env = {str(k): str(v) for k, v in (defaults.get("env") or {}).items()}
    def_backend = defaults.get("backend")

    def opts_for(entry: Dict[str, Any], primary: bool, primary_backend: str) -> Dict[str, Any]:
        base = _deep_merge(def_opts if def_backend in (None, entry["backend"]) else {}, entry["backend_options"])
        mine = own_opts if primary else {k: v for k, v in own_opts.items() if k != "model"}
        if primary or entry["backend"] == primary_backend:
            base = _deep_merge(base, mine)
        return {k: v for k, v in base.items() if v is not None}

    if mid:
        e = _model(models, mid, f"{where}: model")
        if own.get("backend") and str(own["backend"]) != e["backend"]:
            raise ConfigError(f"{where}: backend {own['backend']!r} differs from model {mid!r} ({e['backend']}); "
                              "the backend comes from the model list")
        out.update(backend=e["backend"], backend_options=opts_for(e, True, e["backend"]),
                   env={**def_env, **e["env"], **own_env}, model_id=str(mid))
        if e.get("retries") is not None and "retries" not in own:
            out["retries"] = e["retries"]
    primary_backend = str(out.get("backend") or "claude")
    backups = []
    for key, bid in (("backup", b1), ("backup2", b2)):
        if not bid:
            continue
        e = _model(models, bid, f"{where}: {key}")
        f: Dict[str, Any] = {"backend": e["backend"], "backend_options": opts_for(e, False, primary_backend),
                             "env": {**def_env, **own_env, **e["env"]}, "model_id": str(bid),
                             "retries": int(e.get("retries", out.get("retries", 0)) or 0)}
        f["label"] = str(bid)  # shown on the board and in the records by its id
        backups.append(f)
    if backups:
        out["fallback"] = backups
        out["_resolved_fallback"] = True
    return out


@dataclass
class ControllerConfig:
    mode: str = "pipeline"
    instructions: str = ""
    parallel: bool = False
    require: str = "all"  # pipeline: all|any verifiers must pass
    pass_pattern: str = r"VERDICT:\s*\**\s*PASS"
    fail_pattern: str = r"VERDICT:\s*\**\s*FAIL"
    stop_on_error: bool = True  # any raw agent error (or missing verdict) stops the run
    # a line starting like this in an agent reply pauses the run for the human (status needs_human)
    human_pattern: str = r"^\W*(NEEDS[_ ]HUMAN|QUESTIONS? FOR (THE )?(HUMAN|ME|YOU))\b"
    # permission / approval prompts in stderr or the head of the reply also pause the run
    permission_pattern: str = (r"permission (denied|required|to)|requires? approval|allow[- ]rule|not allowed|auto-denied|"
                               r"requires? (confirmation|authori[sz]ation)|\[y/n\]|do you want to")
    # pipeline: a verifier PASS only counts if its reply holds a checklist ("- [x] ...") with no open "- [ ]" item
    checklist: bool = False
    # said to the verifier (as the next human message) when its PASS was not accepted
    checklist_nudge: str = ("You said it is ready, but your checklist still has open items (or I could not see it). "
                            "Please list every item again with [x] or [ ], and only pass once all are [x].")
    # pipeline: when I answer a worker's question, the reviewers' next message includes this ("" = off)
    answer_note: str = "One open question is now settled; I decided: {answer}"
    snapshot: List[str] = field(default_factory=list)  # files/dirs (relative to the workspace) saved after every turn that changes them
    max_report_chars: int = 0  # cap on an agent output embedded in another agent's prompt; 0 = no cap (the default)
    provider_retry_wait: float = 30.0  # seconds to wait before re-running a turn the provider refused (an API error)
    templates: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TEMPLATES))
    agent: Optional[AgentConfig] = None  # llm mode: the controller agent itself
    # ---- managed mode (manager -> critic -> worker)
    milestone_rounds: int = 50  # rounds without progress (no step of the critic's plan finished) before we take a look
    approve_plan: bool = True  # pause for my OK on the manager's milestone plan
    approve_milestones: bool = True  # pause for my OK after the manager accepts each milestone
    min_score: Optional[float] = None  # a manager ACCEPT below this score counts as REVISE
    protocol: bool = True  # managed mode: add orch's fixed protocol (signals, who reads what, boundaries) to each prompt
    job_ignore: List[str] = field(default_factory=list)  # regexes: commands never listed as running jobs (a status loop)
    # managed mode: older setting, the chance per turn of the protocol reminder (0 = off); now sets its schedule
    tag_reminder: Optional[float] = None
    # managed mode: short reminders at intervals (orch/reminders.py): [{id, text, to, every | random, skip}]
    reminders: List[Dict[str, Any]] = field(default_factory=lambda: R.parse(None))
    # 0 = don't look for jobs the worker left running. Otherwise the critic's review lists them and the critic decides
    # whether the step waits (<wait-job/>); progress checks watch such a wait (B5), and this caps it (seconds) only for
    # a worker whose checks are off
    job_wait: int = 21600
    checkin_every: int = 0  # every N rounds a short update for the manager: from the critic's plan when it has one (0 = off)
    # a reply holding a line like this sends questions one level up (worker -> critic -> manager -> me)
    question_pattern: str = r"^\W*QUESTIONS? FOR\b"
    # "NOTE FOR critic: ..." in a manager reply is passed on to that agent's next turn
    note_pattern: str = r"^\W*NOTES? FOR\s+([^:\n]+?)\s*:"
    accept_pattern: str = r"DECISION:\s*\**\s*ACCEPT"
    revise_pattern: str = r"DECISION:\s*\**\s*REVISE"
    # ---- the auditor (an agent with role: auditor): reads how the agents talk, never the task
    audit_every: int = 10  # rounds between audits, plus one when the run ends (0 = none of them; only `orch audit`)
    audit_points: int = 5  # at most this many points per audit
    # managed mode: an agent's session open in a terminal (orch/hooks/terminal_relay.py) holds its turns; a mark
    # with no activity for this many seconds is stale (a crashed terminal) and orch continues the session
    terminal_busy_timeout: float = 3600.0
    # managed mode: the golden board (orch/boards.py): rules, preferences and key decisions every agent reads; the
    # manager owns it. "" = `.orch/golden.md` in a project folder, else golden.md beside the monitor's runs
    golden_board: str = ""
    # managed mode: a project's writing rules (for its own kind of document), added to the manager's, critic's and
    # workers' system prompts. "" = the project's `.orch/writing.md` when there is one, else none; a path; "off" = none
    writing: str = ""
    golden_limit: int = 8000  # characters; above it the board warns and the manager is asked once to prune
    large_text: int = 50000  # B8: characters; a larger reply, input or raw stream gets a board warning (never cut)
    # B11 parallel workers (orch/parallel.py): the manager approves alone up to max_workers (the first worker counts);
    # above it I decide at a gate; in night mode the manager's number stands up to workers_hard_cap. parallel_turns caps
    # the worker turns of one parallel section (0 = only max_rounds). worker_worktrees: each extra worker gets its own
    # git worktree beside the repository (the critic merges).
    max_workers: int = 3
    workers_hard_cap: int = 6
    parallel_turns: int = 0
    worker_worktrees: bool = False


@dataclass
class MonitorConfig:
    name: str
    workspace: str
    working_dir: str
    goal: str
    max_rounds: int
    controller: ControllerConfig
    agents: List[AgentConfig]
    output_dir: str
    enabled: bool = True
    source: str = ""  # the config file or .orch folder this monitor came from: re-read before each agent turn
    runtime: str = ""

    def agent(self, name: str) -> AgentConfig:
        for a in self.agents:
            if a.name == name:
                return a
        if self.controller.agent and self.controller.agent.name == name:
            return self.controller.agent
        raise KeyError(name)

    def by_role(self, role: str) -> List[AgentConfig]:
        return [a for a in self.agents if a.role == role]

    def snapshot(self) -> Dict[str, Any]:
        c = dict(self.controller.__dict__)
        c["agent"] = self.controller.agent.public() if self.controller.agent else None
        return {
            "name": self.name,
            "workspace": self.workspace,
            "working_dir": self.working_dir,
            "goal": self.goal,
            "max_rounds": self.max_rounds,
            "output_dir": self.output_dir,
            "controller": c,
            "agents": [a.public() for a in self.agents],
        }


@dataclass
class Config:
    path: str
    output_dir: str
    monitors: List[MonitorConfig]
    runtime: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)

    def monitor(self, name: str) -> MonitorConfig:
        for m in self.monitors:
            if m.name == name:
                return m
        raise ConfigError(f"no monitor named {name!r}; have: {', '.join(m.name for m in self.monitors)}")


# --------------------------------------------------------------------------- loading


def _read_raw(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ConfigError("YAML configs need PyYAML: pip install pyyaml (or use a .json config)") from e
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    elif suffix == ".toml":
        try:
            import tomllib  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ConfigError("TOML configs need Python 3.11+ (or use .yaml/.json)") from e
        data = tomllib.loads(text)
    else:
        raise ConfigError(f"unsupported config extension {suffix!r} (use .yaml, .json or .toml)")
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    return data


def _expand(p: str, base: Path) -> str:
    p = os.path.expandvars(os.path.expanduser(str(p)))
    path = Path(p)
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_text_files(d: Dict[str, Any], base: Path, where: str) -> Dict[str, Any]:
    d = dict(d)
    for key in TEXT_KEYS:
        fk = f"{key}_file"
        if fk in d:
            if key in d:
                raise ConfigError(f"{where}: give either {key} or {fk}, not both")
            fp = Path(_expand(d.pop(fk), base))
            if not fp.is_file():
                raise ConfigError(f"{where}: {fk} not found: {fp}")
            d[key] = fp.read_text(encoding="utf-8")
    return d


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _build_agent(raw: Dict[str, Any], defaults: Dict[str, Any], base: Path, mon_wd: str, where: str,
                 models: Optional[Dict[str, Dict[str, Any]]] = None) -> AgentConfig:
    sp_path = _expand(raw["system_prompt_file"], base) if raw.get("system_prompt_file") else ""
    raw = _load_text_files(raw, base, where)
    merged = _deep_merge(defaults, raw)
    merged = _resolve_models(merged, raw, defaults, models or {}, f"{where} ({raw.get('name', '?')})")
    resolved_fallback = bool(merged.pop("_resolved_fallback", False))
    name = str(merged.get("name", "")).strip()
    if not name:
        raise ConfigError(f"{where}: agent needs a name")
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: agent name {name!r} may only use letters, digits, '_', '.', '-'")
    role = str(merged.get("role", "worker")).strip()
    role = ROLE_ALIASES.get(role, role)
    wd = merged.get("working_dir")
    working_dir = _expand(wd, Path(mon_wd)) if wd else mon_wd
    known = {"name", "role", "description", "system_prompt", "task", "backend", "backend_options",
             "working_dir", "timeout", "retries", "env", "reviews", "feedback_to", "display_name",
             "memory_files", "memory_heading", "fresh_session", "state_file", "fallback", "fallback_cooldown",
             "safety_timeout", "check_every", "check_session", "compact_between_steps", "model_id",
             "retry_waits"}
    unknown = set(merged) - known
    if unknown:
        raise ConfigError(f"{where} ({name}): unknown keys: {', '.join(sorted(unknown))}")
    fresh = str(merged.get("fresh_session", "") or "").strip().lower()
    if fresh not in ("", "never", "milestone", "compact"):
        raise ConfigError(f"{where} ({name}): fresh_session must be 'compact', 'milestone' or 'never'")
    session = str(merged.get("check_session", "") or "").strip().lower()
    if session and session not in CHECK_SESSIONS:
        raise ConfigError(f"{where} ({name}): check_session must be 'own' or 'side'")
    try:
        safety = float(merged["safety_timeout"]) if merged.get("safety_timeout") is not None else None
        every = float(merged["check_every"]) if merged.get("check_every") is not None else None
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where} ({name}): safety_timeout and check_every must be numbers") from e
    if (safety is not None and safety < 0) or (every is not None and every < 0):
        raise ConfigError(f"{where} ({name}): safety_timeout and check_every must be >= 0")
    primary = {"backend": str(merged.get("backend", "claude")),
               "backend_options": dict(merged.get("backend_options") or {}),
               "env": {str(k): str(v) for k, v in (merged.get("env") or {}).items()},
               "retries": int(merged.get("retries", 0))}
    fallback = (list(merged["fallback"]) if resolved_fallback
                else _build_fallback(merged.get("fallback"), primary, f"{where} ({name})"))
    waits = merged.get("retry_waits", RETRY_WAITS)
    try:
        waits = [float(x) for x in (waits if isinstance(waits, list) else [waits])] if waits is not None else []
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where} ({name}): retry_waits must be a list of seconds, e.g. [60, 120, 240]") from e
    if any(x < 0 for x in waits):
        raise ConfigError(f"{where} ({name}): retry_waits must be >= 0")
    return AgentConfig(
        model_id=str(merged.get("model_id") or ""),
        retry_waits=waits,
        fallback=fallback,
        fallback_cooldown=float(merged.get("fallback_cooldown", AgentConfig.fallback_cooldown) or 0),
        name=name,
        role=role,
        description=str(merged.get("description", "") or "").strip(),
        system_prompt=str(merged.get("system_prompt", "") or "").strip(),
        task=str(merged.get("task", "") or "").strip(),
        backend=str(merged.get("backend", "claude")),
        backend_options=dict(merged.get("backend_options") or {}),
        working_dir=working_dir,
        timeout=float(merged.get("timeout", 1800)),
        retries=int(merged.get("retries", 0)),
        env={str(k): str(v) for k, v in (merged.get("env") or {}).items()},
        reviews=_as_list(merged.get("reviews")),
        feedback_to=_as_list(merged.get("feedback_to")),
        display_name=str(merged.get("display_name", "") or "").strip(),
        system_prompt_path=sp_path,
        memory_files=[_expand(x, base) for x in _as_list(merged.get("memory_files"))],
        memory_heading=str(merged.get("memory_heading", AgentConfig.memory_heading) or ""),
        fresh_session=fresh,
        state_file=_expand(merged["state_file"], base) if merged.get("state_file") else "",
        safety_timeout=safety,
        check_every=every,
        check_session=session,
        compact_between_steps=_as_bool(merged.get("compact_between_steps", False)),
    )


ROLE_ALIASES = {"critic": "verifier", "reviewer": "verifier", "writer": "worker"}
PROMPTS_DIR = Path(__file__).with_name("prompts")
DEFAULT_PROMPT = {"manager": "manager.md", "verifier": "critic.md", "worker": "worker.md", "auditor": "auditor.md",
                  "monitor": "monitor.md"}
# workflow settings an agent.yaml may carry (usually: the manager's goal and approvals, the critic's checklist
# and rounds, the worker's snapshot). Two agents setting the same one differently is an error.
MONITOR_KEYS = ("goal", "goal_file", "max_rounds", "output_dir", "workspace", "runtime")
CTRL_KEYS = ("mode", "instructions", "instructions_file", "parallel", "require", "pass_pattern", "fail_pattern",
             "stop_on_error", "max_report_chars", "provider_retry_wait", "templates", "human_pattern", "permission_pattern", "snapshot",
             "checklist", "checklist_nudge", "answer_note", "milestone_rounds", "approve_plan",
             "approve_milestones", "min_score", "question_pattern", "note_pattern", "accept_pattern", "revise_pattern",
             "checkin_every", "job_wait", "job_ignore", "protocol", "tag_reminder", "reminders", "audit_every", "audit_points",
             "terminal_busy_timeout", "golden_board", "writing", "golden_limit", "large_text", "max_workers", "workers_hard_cap",
             "parallel_turns", "worker_worktrees")


def find_project(path: str) -> Optional[Path]:
    """The .orch folder of a project root (or the .orch folder itself): it holds agents/<name>/agent.yaml."""
    p = Path(path).expanduser().resolve()
    for d in (p / ".orch", p):
        if (d / "agents").is_dir() and any((x / "agent.yaml").is_file() for x in (d / "agents").iterdir()):
            return d
    return None


def is_config(path: str) -> bool:
    p = Path(path).expanduser()
    return p.is_file() or find_project(str(p)) is not None


class _WorkerExistsError(ConfigError, FileExistsError, ValueError):
    pass


def add_worker(project: Any, like: str, worker: str) -> None:
    """Copy agents/<like>/agent.yaml to agents/<worker>/agent.yaml (name changed, nothing else),
    creating the folder; refuse if the worker exists."""
    from .runtime import models as rt_models

    p = Path(project).expanduser().resolve()
    orch_dir = find_project(str(p)) or (p / ".orch" if (p / ".orch").is_dir() else p)
    agents_dir = orch_dir / "agents"
    wname = str(worker or "").strip()
    if not wname or not NAME_RE.match(wname):
        raise ConfigError(f"invalid worker name {worker!r}")
    dst_dir = agents_dir / wname
    dst = dst_dir / "agent.yaml"
    if dst_dir.exists() or dst.exists():
        raise _WorkerExistsError(f"worker {wname!r} already exists: {dst_dir}")
    lname = str(like or "").strip()
    if lname:
        src = agents_dir / lname / "agent.yaml"
    else:
        src = next(
            (
                d / "agent.yaml"
                for d in sorted(agents_dir.iterdir())
                if (d / "agent.yaml").is_file()
                and ROLE_ALIASES.get(str(_read_raw(d / "agent.yaml").get("role", "worker")), "worker") == "worker"
            ),
            agents_dir / "worker" / "agent.yaml",
        )
    if not src.is_file():
        raise ConfigError(f"worker {like!r} not found: {src}")
    text = src.read_text(encoding="utf-8")
    if rt_models._find(text, ["name"]) is not None:
        text = rt_models._set(text, "name", wname)
    dst_dir.mkdir(parents=True, exist_ok=True)
    rt_models._write(dst, text)



def _agent_folder(d: Path) -> Dict[str, Any]:
    """agents/<name>/: agent.yaml, memory.md + memory/*.md, optionally system_prompt.md (else the role default)."""
    raw = _read_raw(d / "agent.yaml")
    raw.setdefault("name", d.name)
    role = ROLE_ALIASES.get(str(raw.get("role", "worker")), str(raw.get("role", "worker")))
    for k in ("goal_file", "instructions_file"):  # files next to the agent.yaml
        if raw.get(k):
            raw[k] = _expand(raw[k], d)
    if "system_prompt" not in raw and "system_prompt_file" not in raw:
        own = d / "system_prompt.md"
        if own.is_file():
            raw["system_prompt_file"] = str(own)
        elif role in DEFAULT_PROMPT:
            raw["system_prompt_file"] = str(PROMPTS_DIR.parent / "runtime" / "prompts" / DEFAULT_PROMPT[role])
    elif raw.get("system_prompt_file"):
        raw["system_prompt_file"] = _expand(raw["system_prompt_file"], d)
    if role == "manager" and "goal" not in raw and "goal_file" not in raw and (d / "goal.md").is_file():
        raw["goal_file"] = str(d / "goal.md")
    raw.setdefault("memory_files", [])  # notes read into the prompt; preference.md is read on demand instead
    raw.setdefault("state_file", str(d / "state.md"))
    return raw


def _writing_file(value: Any, base: Path, proj: Optional[Path]) -> str:
    """The writing rules file: "off" for none, a path as given, else the project's `.orch/writing.md` ('' without one:
    orch's general rules are in runtime/prompts/style.md, and the writing file is for a project's own kind of document)."""
    v = str(value or "").strip()
    if v.lower() in ("off", "none", "false"):
        return ""
    if v:
        return _expand(v, base)
    own = proj / "writing.md" if proj else None
    return str(own) if own and own.is_file() else ""


def _project_raw(orch_dir: Path) -> Dict[str, Any]:
    """Turn a .orch folder (one workflow made of agent folders) into the structure of a config file."""
    ps = project_settings(orch_dir)
    from .runtime import migrate as rt_migrate
    rt_migrate.backup_prompts(orch_dir)  # one runtime now (N): Orch's prompts, a project's own copy kept as .bak
    mon: Dict[str, Any] = {}
    owner: Dict[str, str] = {}
    ctrl: Dict[str, Any] = {}
    agents = []
    order = {"manager": 0, "verifier": 1, "worker": 2, "monitor": 3}
    folders = [x for x in sorted((orch_dir / "agents").iterdir()) if (x / "agent.yaml").is_file()]
    for d in folders:
        raw = _agent_folder(d)
        for k in list(raw):
            if k not in MONITOR_KEYS and k not in CTRL_KEYS:
                continue
            v = raw.pop(k)
            if k in owner and (mon.get(k, ctrl.get(k)) != v):
                raise ConfigError(f"{k!r} is set differently in agents/{owner[k]} and agents/{d.name}")
            owner[k] = d.name
            (mon if k in MONITOR_KEYS else ctrl)[k] = v
        role = ROLE_ALIASES.get(str(raw.get("role", "worker")), str(raw.get("role", "worker")))
        if role in ("manager", "verifier", "monitor"):
            raw.pop("system_prompt", None)
            rt_name = "critic" if role == "verifier" else role
            raw["system_prompt_file"] = str(Path(__file__).with_name("runtime") / "prompts" / f"{rt_name}.md")
        elif role == "worker" and not raw.get("task") and (d / "system_prompt.md").is_file():
            raw["task"] = (d / "system_prompt.md").read_text(encoding="utf-8").strip()
        agents.append(raw)
    agents.sort(key=lambda a: order.get(ROLE_ALIASES.get(str(a.get("role")), str(a.get("role"))), 3))
    roles = {ROLE_ALIASES.get(str(a.get("role")), str(a.get("role"))) for a in agents}
    ctrl.setdefault("mode", "managed" if "manager" in roles else "pipeline")
    root = orch_dir.parent if orch_dir.name == ".orch" else orch_dir
    mon.setdefault("name", re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-.") or "project")
    mon.setdefault("workspace", str(root))
    mon.update(controller=ctrl, agents=agents)
    out = {"output_dir": mon.pop("output_dir", "runs"), "monitors": [mon]}
    if "runtime" in mon and "runtime" not in ps:
        out["runtime"] = mon["runtime"]
    out.update(ps)
    return out


def settings_path(orch_dir: Path) -> Optional[Path]:
    """The project's general settings file (`.orch/settings.yaml`), if there is one."""
    return next((orch_dir / n for n in SETTINGS_FILES if (orch_dir / n).is_file()), None)


def project_settings(orch_dir: Path) -> Dict[str, Any]:
    """`.orch/settings.yaml`: the model list (`models:`) and the project-wide `retry_waits` and `fallback_cooldown`
    (defaults every agent can override). Re-read with the agent files, so edits apply from the next turn."""
    p = settings_path(orch_dir)
    if p is None:
        return {}
    raw = _read_raw(p)
    unknown = set(raw) - set(SETTINGS_KEYS)
    if unknown:
        raise ConfigError(f"{p.name}: unknown keys: {', '.join(sorted(unknown))} (known: {', '.join(SETTINGS_KEYS)})")
    return raw


def load_config(path: str) -> Config:
    p = Path(path).expanduser().resolve()
    proj = None if p.is_file() else find_project(str(p))
    cfg_path = proj or p
    if not proj and not cfg_path.is_file():
        raise ConfigError(f"config not found: {cfg_path} (and no .orch/agents/*/agent.yaml there)")
    base = proj or cfg_path.parent
    raw = _project_raw(proj) if proj else _read_raw(cfg_path)

    output_dir = _expand(raw.get("output_dir", "./orch-runs"), base)
    top_defaults = dict(raw.get("defaults") or {})
    models = parse_models(raw.get("models"))
    for k in ("retry_waits", "fallback_cooldown"):  # project-wide settings: defaults for every agent
        if raw.get(k) is not None:
            top_defaults.setdefault(k, raw[k])
    monitors_raw = raw.get("monitors")
    if not monitors_raw or not isinstance(monitors_raw, list):
        raise ConfigError("config needs a non-empty `monitors:` list")

    monitors: List[MonitorConfig] = []
    for i, mraw in enumerate(monitors_raw):
        where = f"monitors[{i}]"
        if not isinstance(mraw, dict):
            raise ConfigError(f"{where}: must be a mapping")
        mraw = _load_text_files(mraw, base, where)
        name = str(mraw.get("name", "")).strip()
        if not name or not NAME_RE.match(name):
            raise ConfigError(f"{where}: monitor needs a name using letters, digits, '_', '.', '-'")
        where = f"monitor {name!r}"
        if "workspace" not in mraw:
            raise ConfigError(f"{where}: `workspace` is required")
        workspace = _expand(mraw["workspace"], base)
        working_dir = _expand(mraw.get("working_dir", "."), Path(workspace))
        defaults = _deep_merge(top_defaults, mraw.get("defaults") or {})

        craw = dict(mraw.get("controller") or {})
        for k in ("instructions", "instructions_file"):  # also allowed at the top of a workflow
            if k in mraw:
                craw.setdefault(k, mraw.pop(k))
        craw = _load_text_files(craw, base, f"{where}.controller")
        mode = str(craw.get("mode", "pipeline"))
        templates = dict(DEFAULT_TEMPLATES)
        for k, v in (craw.get("templates") or {}).items():
            if k in RETIRED_TEMPLATES:  # replaced by the reminder registry: accepted, not used
                continue
            if k not in DEFAULT_TEMPLATES:
                raise ConfigError(f"{where}.controller.templates: unknown template {k!r}; "
                                  f"known: {', '.join(DEFAULT_TEMPLATES)}")
            templates[k] = str(v)
        ctrl_agent = None
        if mode == "llm":
            araw = dict(craw.get("agent") or {})
            araw.setdefault("name", "controller")
            araw.setdefault("role", "controller")
            ctrl_agent = _build_agent(araw, defaults, base, working_dir, f"{where}.controller.agent", models)
        ctrl_known = {"mode", "instructions", "parallel", "require", "pass_pattern", "fail_pattern",
                      "stop_on_error", "max_report_chars", "provider_retry_wait", "templates", "agent",
                      "human_pattern", "permission_pattern", "snapshot", "checklist", "checklist_nudge", "answer_note",
                      "milestone_rounds", "approve_plan", "approve_milestones", "min_score", "question_pattern",
                      "note_pattern", "accept_pattern", "revise_pattern", "checkin_every", "job_wait", "job_ignore", "protocol", "tag_reminder",
                      "reminders", "audit_every", "audit_points", "terminal_busy_timeout", "golden_board", "writing",
                      "golden_limit", "large_text", "max_workers", "workers_hard_cap", "parallel_turns",
                      "worker_worktrees"}
        unknown = set(craw) - ctrl_known
        if unknown:
            raise ConfigError(f"{where}.controller: unknown keys: {', '.join(sorted(unknown))}")
        tag_reminder = float(craw["tag_reminder"] or 0) if craw.get("tag_reminder") is not None else None
        try:
            reminders = R.parse(craw.get("reminders"), tag_reminder, f"{where}.controller.reminders")
        except (R.ReminderError, TypeError, ValueError) as e:
            raise ConfigError(str(e)) from e
        controller = ControllerConfig(
            mode=mode,
            instructions=str(craw.get("instructions", "") or "").strip(),
            parallel=bool(craw.get("parallel", False)),
            require=str(craw.get("require", "all")),
            pass_pattern=str(craw.get("pass_pattern", ControllerConfig.pass_pattern)),
            fail_pattern=str(craw.get("fail_pattern", ControllerConfig.fail_pattern)),
            stop_on_error=bool(craw.get("stop_on_error", True)),
            human_pattern=str(craw.get("human_pattern", ControllerConfig.human_pattern)),
            permission_pattern=str(craw.get("permission_pattern", ControllerConfig.permission_pattern)),
            snapshot=[str(x) for x in (craw.get("snapshot") or [])],
            checklist=bool(craw.get("checklist", False)),
            answer_note=str(craw.get("answer_note", ControllerConfig.answer_note) or "").strip(),
            checklist_nudge=str(craw.get("checklist_nudge", ControllerConfig.checklist_nudge)).strip(),
            max_report_chars=int(craw.get("max_report_chars", 0)),
            provider_retry_wait=float(craw.get("provider_retry_wait", 30.0)),
            templates=templates,
            agent=ctrl_agent,
            milestone_rounds=int(craw.get("milestone_rounds", 50)),
            approve_plan=bool(craw.get("approve_plan", True)),
            approve_milestones=bool(craw.get("approve_milestones", True)),
            min_score=float(craw["min_score"]) if craw.get("min_score") is not None else None,
            checkin_every=int(craw.get("checkin_every", 0) or 0),
            job_wait=int(craw.get("job_wait", ControllerConfig.job_wait) or 0),
            protocol=bool(craw.get("protocol", True)),
            tag_reminder=tag_reminder,
            reminders=reminders,
            job_ignore=[str(x) for x in (craw.get("job_ignore") or [])],
            audit_every=int(craw.get("audit_every", ControllerConfig.audit_every) or 0),
            audit_points=int(craw.get("audit_points", ControllerConfig.audit_points) or 5),
            terminal_busy_timeout=float(craw.get("terminal_busy_timeout", ControllerConfig.terminal_busy_timeout) or 0),
            golden_board=(_expand(craw["golden_board"], base) if craw.get("golden_board")
                          else str(proj / "golden.md") if proj else ""),
            writing=_writing_file(craw.get("writing"), base, proj),
            golden_limit=int(craw.get("golden_limit", ControllerConfig.golden_limit) or 0),
            large_text=int(craw.get("large_text", ControllerConfig.large_text) or 0),
            max_workers=int(craw.get("max_workers", ControllerConfig.max_workers) or 1),
            workers_hard_cap=int(craw.get("workers_hard_cap", ControllerConfig.workers_hard_cap) or 1),
            parallel_turns=int(craw.get("parallel_turns", 0) or 0),
            worker_worktrees=bool(craw.get("worker_worktrees", False)),
            **{k: str(craw[k]) for k in ("question_pattern", "note_pattern", "accept_pattern", "revise_pattern")
               if craw.get(k)},
        )
        agents = [
            _build_agent(a, defaults, base, working_dir, f"{where}.agents[{j}]", models)
            for j, a in enumerate(mraw.get("agents") or [])
        ]
        team = {key: " and ".join(a.called for a in agents if a.role == role) or key
                for key, role in (("manager", "manager"), ("critic", "verifier"), ("worker", "worker"))}
        for a in agents:
            a.prompt_vars = dict(team)
            proto = {"manager": "manager", "verifier": "critic", "worker": "worker", "monitor": "monitor"}.get(a.role)
            if controller.mode == "managed" and controller.protocol and proto:
                a.protocol_path = str(PROMPTS_DIR / "protocol" / f"{proto}.md")
            if controller.mode == "managed" and controller.writing and a.role in ("manager", "verifier", "worker"):
                a.writing_path = controller.writing
        m_runtime = "new"  # the only runtime (N); a `runtime:` key is still accepted
        from .runtime import migrate as rt_migrate
        for a in agents:
            if a.role in ("manager", "verifier", "monitor"):
                defaults_sp = rt_migrate._orch_prompts_for(a.role)
                if a.system_prompt_path:
                    sp_p = Path(a.system_prompt_path)
                    if sp_p.is_file() and PROMPTS_DIR not in sp_p.parents and rt_migrate.RUNTIME_PROMPTS_DIR not in sp_p.parents:
                        txt = sp_p.read_text(encoding="utf-8").strip()
                        if txt and txt not in defaults_sp:
                            os.replace(sp_p, Path(str(sp_p) + ".bak"))
                a.system_prompt = ""
                rt_name = "critic" if a.role == "verifier" else a.role
                a.system_prompt_path = str(rt_migrate.RUNTIME_PROMPTS_DIR / f"{rt_name}.md")
        m_known = {"name", "workspace", "working_dir", "goal", "max_rounds", "controller", "agents",
                   "defaults", "output_dir", "enabled", "runtime"}
        unknown = set(mraw) - m_known
        if unknown:
            raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        monitors.append(MonitorConfig(
            name=name,
            workspace=workspace,
            working_dir=working_dir,
            goal=str(mraw.get("goal", "") or "").strip(),
            max_rounds=int(mraw.get("max_rounds", 400)),
            controller=controller,
            agents=agents,
            output_dir=_expand(mraw["output_dir"], base) if mraw.get("output_dir") else output_dir,
            enabled=bool(mraw.get("enabled", True)),
            source=str(cfg_path),
            runtime=m_runtime,
        ))

    top_runtime = "new"
    settings_dict = {k: raw[k] for k in SETTINGS_KEYS if k in raw}
    cfg = Config(path=str(cfg_path), output_dir=output_dir, monitors=monitors, runtime=top_runtime, settings=settings_dict)
    validate(cfg)
    return cfg


# --------------------------------------------------------------------------- validation


def require_managed(cfg: Config) -> None:
    """DEL: the pipeline and controller modes were removed; `orch run` and `orch validate` refuse them (a config file
    still loads, so its settings can be read)."""
    for m in cfg.monitors:
        if m.controller.mode != "managed":
            raise ConfigError(f"monitor {m.name!r}: the {m.controller.mode} mode was removed: a run needs a manager, a "
                              f"critic and workers (orch init --project writes one)")


def validate(cfg: Config, check_paths: bool = False) -> List[str]:
    """Raise ConfigError on structural problems; return a list of warnings.

    With ``check_paths`` the workspace / working directories must exist.
    """
    from .runtime.adapters import ADAPTERS as BACKENDS

    warnings: List[str] = []
    seen = set()
    for m in cfg.monitors:
        if m.name in seen:
            raise ConfigError(f"duplicate monitor name {m.name!r}")
        seen.add(m.name)
        where = f"monitor {m.name!r}"
        c = m.controller
        if c.mode not in MODES:
            raise ConfigError(f"{where}: controller.mode must be one of {MODES}")
        if c.require not in ("all", "any"):
            raise ConfigError(f"{where}: controller.require must be 'all' or 'any'")
        for pat in (c.pass_pattern, c.fail_pattern, c.question_pattern, c.note_pattern,
                    c.accept_pattern, c.revise_pattern):
            try:
                re.compile(pat)
            except re.error as e:
                raise ConfigError(f"{where}: bad regex {pat!r}: {e}")
        if m.max_rounds < 1:
            raise ConfigError(f"{where}: max_rounds must be >= 1")
        if not m.goal:
            warnings.append(f"{where}: no goal set")
        if not m.agents:
            raise ConfigError(f"{where}: needs at least one agent")
        names = [a.name for a in m.agents] + ([c.agent.name] if c.agent else [])
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ConfigError(f"{where}: duplicate agent names: {', '.join(sorted(dup))}")
        for a in m.agents + ([c.agent] if c.agent else []):
            if a.backend not in BACKENDS:
                raise ConfigError(f"{where}: agent {a.name!r} uses unknown backend {a.backend!r}; "
                                  f"available: {', '.join(BACKENDS)}")
            for j, f in enumerate(a.fallback):
                if f["backend"] not in BACKENDS:
                    raise ConfigError(f"{where}: agent {a.name!r} fallback[{j}] uses unknown backend "
                                      f"{f['backend']!r}; available: {', '.join(BACKENDS)}")
            if check_paths and not Path(a.working_dir).is_dir():
                raise ConfigError(f"{where}: agent {a.name!r} working_dir does not exist: {a.working_dir}")
        if c.mode == "pipeline":
            workers = [a.name for a in m.by_role("worker")]
            if not workers:
                raise ConfigError(f"{where}: pipeline mode needs at least one agent with role: worker")
            others = [a.name for a in m.agents if a.role not in ("worker", "verifier")]
            if others:
                warnings.append(f"{where}: pipeline mode ignores agents with custom roles: {', '.join(others)}")
            if not m.by_role("verifier"):
                warnings.append(f"{where}: no verifiers — the pipeline will stop after one round")
            for v in m.by_role("verifier"):
                for ref in v.reviews + v.feedback_to:
                    if ref not in workers:
                        raise ConfigError(f"{where}: verifier {v.name!r} references unknown worker {ref!r}")
        if c.mode == "managed":
            mgr, ver, wrk = m.by_role("manager"), m.by_role("verifier"), m.by_role("worker")
            if len(mgr) != 1 or len(ver) != 1 or not wrk:
                raise ConfigError(f"{where}: managed mode needs exactly one agent with role: manager, one with "
                                  f"role: verifier (the critic) and at least one with role: worker")
            if c.milestone_rounds < 1:
                raise ConfigError(f"{where}: controller.milestone_rounds must be >= 1")
            if m.max_rounds <= c.milestone_rounds:
                warnings.append(f"{where}: max_rounds ({m.max_rounds}) counts rounds over all milestones; at or below "
                                f"milestone_rounds ({c.milestone_rounds}) the run can stop in the middle of a milestone")
            others = [a.name for a in m.agents if a.role not in ("worker", "verifier", "manager", "auditor", "monitor")]
            if others:
                warnings.append(f"{where}: managed mode ignores agents with custom roles: {', '.join(others)}")
            if len(m.by_role("auditor")) > 1:
                raise ConfigError(f"{where}: at most one agent with role: auditor")
            if len(m.by_role("monitor")) > 1:
                warnings.append(f"{where}: several agents with role: monitor; only "
                                f"{m.by_role('monitor')[0].name!r} watches the work (each extra worker gets its own copy of it)")
            if c.max_workers < 1 or c.workers_hard_cap < c.max_workers or c.parallel_turns < 0:
                raise ConfigError(f"{where}: controller.max_workers must be >= 1, workers_hard_cap >= max_workers and "
                                  f"parallel_turns >= 0")
            if c.audit_every < 0 or c.audit_points < 1:
                raise ConfigError(f"{where}: controller.audit_every must be >= 0 and audit_points >= 1")
        if check_paths:
            if not Path(m.workspace).is_dir():
                raise ConfigError(f"{where}: workspace does not exist: {m.workspace}")
            if not Path(m.working_dir).is_dir():
                raise ConfigError(f"{where}: working_dir does not exist: {m.working_dir}")
    return warnings
