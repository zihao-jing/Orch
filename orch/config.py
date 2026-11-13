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

from .templates import DEFAULT_TEMPLATES

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TEXT_KEYS = ("goal", "instructions", "system_prompt", "task", "description")
MODES = ("pipeline", "llm", "managed")


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
    fresh_session: str = ""  # "milestone": start a new session when each milestone starts (managed mode)
    state_file: str = ""  # managed mode: the agent's working state (agents/<name>/state.md in a project folder)
    # backup models, tried in order when a turn still fails after its retries: each is a dict with any of
    # label, backend, backend_options, env, retries (already merged with the primary's settings when loaded)
    fallback: List[Dict[str, Any]] = field(default_factory=list)
    fallback_cooldown: float = 900.0  # seconds an agent stays on the backup that worked before trying its primary again

    def variants(self) -> List[Variant]:
        """The primary model first, then each backup."""
        out = [Variant(0, _variant_label(self.backend, self.backend_options), self.backend,
                       dict(self.backend_options), dict(self.env), self.retries)]
        for i, f in enumerate(self.fallback, 1):
            b = str(f.get("backend") or self.backend)
            opts = dict(f.get("backend_options") or {})
            out.append(Variant(i, str(f.get("label") or _variant_label(b, opts)), b, opts,
                               dict(f.get("env") or {}), int(f.get("retries", self.retries))))
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
        unknown = set(item) - {"label", "backend", "backend_options", "env", "retries"}
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
        out.append(f)
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
    milestone_rounds: int = 40  # worker/critic rounds per milestone before the manager and I take a look
    approve_plan: bool = True  # pause for my OK on the manager's milestone plan
    approve_milestones: bool = True  # pause for my OK after the manager accepts each milestone
    min_score: Optional[float] = None  # a manager ACCEPT below this score counts as REVISE
    protocol: bool = True  # managed mode: add orch's fixed protocol (signals, who reads what, boundaries) to each prompt
    job_ignore: List[str] = field(default_factory=list)  # regexes: commands never waited on (e.g. a status loop)
    tag_reminder: float = 0.2  # managed mode: chance per turn of a short reminder of the tags (0 = never)
    job_wait: int = 21600  # seconds the critic waits for a job the worker says it is waiting on (0 = never wait)
    checkin_every: int = 0  # every N worker/critic rounds the critic sends the manager a short update (0 = off)
    # a reply holding a line like this sends questions one level up (worker -> critic -> manager -> me)
    question_pattern: str = r"^\W*QUESTIONS? FOR\b"
    # "NOTE FOR critic: ..." in a manager reply is passed on to that agent's next turn
    note_pattern: str = r"^\W*NOTES? FOR\s+([^:\n]+?)\s*:"
    accept_pattern: str = r"DECISION:\s*\**\s*ACCEPT"
    revise_pattern: str = r"DECISION:\s*\**\s*REVISE"
    # ---- the auditor (an agent with role: auditor): reads how the agents talk, never the task
    audit_every: int = 10  # rounds between audits (0 = only `orch audit`); one more when the run ends
    audit_points: int = 5  # at most this many points per audit


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


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _build_agent(raw: Dict[str, Any], defaults: Dict[str, Any], base: Path, mon_wd: str, where: str) -> AgentConfig:
    sp_path = _expand(raw["system_prompt_file"], base) if raw.get("system_prompt_file") else ""
    raw = _load_text_files(raw, base, where)
    merged = _deep_merge(defaults, raw)
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
             "memory_files", "memory_heading", "fresh_session", "state_file", "fallback", "fallback_cooldown"}
    unknown = set(merged) - known
    if unknown:
        raise ConfigError(f"{where} ({name}): unknown keys: {', '.join(sorted(unknown))}")
    fresh = str(merged.get("fresh_session", "") or "").strip().lower()
    if fresh not in ("", "never", "milestone"):
        raise ConfigError(f"{where} ({name}): fresh_session must be 'milestone' or 'never'")
    primary = {"backend": str(merged.get("backend", "claude")),
               "backend_options": dict(merged.get("backend_options") or {}),
               "env": {str(k): str(v) for k, v in (merged.get("env") or {}).items()},
               "retries": int(merged.get("retries", 0))}
    fallback = _build_fallback(merged.get("fallback"), primary, f"{where} ({name})")
    return AgentConfig(
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
        fresh_session="" if fresh == "never" else fresh,
        state_file=_expand(merged["state_file"], base) if merged.get("state_file") else "",
    )


ROLE_ALIASES = {"critic": "verifier", "reviewer": "verifier", "writer": "worker"}
PROMPTS_DIR = Path(__file__).with_name("prompts")
DEFAULT_PROMPT = {"manager": "manager.md", "verifier": "critic.md", "worker": "worker.md", "auditor": "auditor.md"}
# workflow settings an agent.yaml may carry (usually: the manager's goal and approvals, the critic's checklist
# and rounds, the worker's snapshot). Two agents setting the same one differently is an error.
MONITOR_KEYS = ("goal", "goal_file", "max_rounds", "output_dir", "workspace")
CTRL_KEYS = ("mode", "instructions", "instructions_file", "parallel", "require", "pass_pattern", "fail_pattern",
             "stop_on_error", "max_report_chars", "provider_retry_wait", "templates", "human_pattern", "permission_pattern", "snapshot",
             "checklist", "checklist_nudge", "answer_note", "milestone_rounds", "approve_plan",
             "approve_milestones", "min_score", "question_pattern", "note_pattern", "accept_pattern", "revise_pattern",
             "checkin_every", "job_wait", "job_ignore", "protocol", "tag_reminder", "audit_every", "audit_points")


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
            raw["system_prompt_file"] = str(PROMPTS_DIR / DEFAULT_PROMPT[role])
    elif raw.get("system_prompt_file"):
        raw["system_prompt_file"] = _expand(raw["system_prompt_file"], d)
    if role == "manager" and "goal" not in raw and "goal_file" not in raw and (d / "goal.md").is_file():
        raw["goal_file"] = str(d / "goal.md")
    raw.setdefault("memory_files", [])  # notes read into the prompt; preference.md is read on demand instead
    raw.setdefault("state_file", str(d / "state.md"))
    return raw


def _project_raw(orch_dir: Path) -> Dict[str, Any]:
    """Turn a .orch folder (one workflow made of agent folders) into the structure of a config file."""
    mon: Dict[str, Any] = {}
    owner: Dict[str, str] = {}
    ctrl: Dict[str, Any] = {}
    agents = []
    order = {"manager": 0, "verifier": 1, "worker": 2}
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
        agents.append(raw)
    agents.sort(key=lambda a: order.get(ROLE_ALIASES.get(str(a.get("role")), str(a.get("role"))), 3))
    roles = {ROLE_ALIASES.get(str(a.get("role")), str(a.get("role"))) for a in agents}
    ctrl.setdefault("mode", "managed" if "manager" in roles else "pipeline")
    root = orch_dir.parent if orch_dir.name == ".orch" else orch_dir
    mon.setdefault("name", re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-.") or "project")
    mon.setdefault("workspace", str(root))
    mon.update(controller=ctrl, agents=agents)
    return {"output_dir": mon.pop("output_dir", "runs"), "monitors": [mon]}


def load_config(path: str) -> Config:
    p = Path(path).expanduser().resolve()
    proj = None if p.is_file() else find_project(str(p))
    cfg_path = proj or p
    if not proj and not cfg_path.is_file():
        raise ConfigError(f"config not found: {cfg_path} (and no .orch/agents/*/agent.yaml there)")
    base = proj or cfg_path.parent
    raw = _project_raw(proj) if proj else _read_raw(cfg_path)

    output_dir = _expand(raw.get("output_dir", "./orch-runs"), base)
    top_defaults = raw.get("defaults") or {}
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
            if k not in DEFAULT_TEMPLATES:
                raise ConfigError(f"{where}.controller.templates: unknown template {k!r}; "
                                  f"known: {', '.join(DEFAULT_TEMPLATES)}")
            templates[k] = str(v)
        ctrl_agent = None
        if mode == "llm":
            araw = dict(craw.get("agent") or {})
            araw.setdefault("name", "controller")
            araw.setdefault("role", "controller")
            ctrl_agent = _build_agent(araw, defaults, base, working_dir, f"{where}.controller.agent")
        ctrl_known = {"mode", "instructions", "parallel", "require", "pass_pattern", "fail_pattern",
                      "stop_on_error", "max_report_chars", "provider_retry_wait", "templates", "agent",
                      "human_pattern", "permission_pattern", "snapshot", "checklist", "checklist_nudge", "answer_note",
                      "milestone_rounds", "approve_plan", "approve_milestones", "min_score", "question_pattern",
                      "note_pattern", "accept_pattern", "revise_pattern", "checkin_every", "job_wait", "job_ignore", "protocol", "tag_reminder",
                      "audit_every", "audit_points"}
        unknown = set(craw) - ctrl_known
        if unknown:
            raise ConfigError(f"{where}.controller: unknown keys: {', '.join(sorted(unknown))}")
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
            milestone_rounds=int(craw.get("milestone_rounds", 40)),
            approve_plan=bool(craw.get("approve_plan", True)),
            approve_milestones=bool(craw.get("approve_milestones", True)),
            min_score=float(craw["min_score"]) if craw.get("min_score") is not None else None,
            checkin_every=int(craw.get("checkin_every", 0) or 0),
            job_wait=int(craw.get("job_wait", ControllerConfig.job_wait) or 0),
            protocol=bool(craw.get("protocol", True)),
            tag_reminder=float(craw.get("tag_reminder", ControllerConfig.tag_reminder) or 0),
            job_ignore=[str(x) for x in (craw.get("job_ignore") or [])],
            audit_every=int(craw.get("audit_every", ControllerConfig.audit_every) or 0),
            audit_points=int(craw.get("audit_points", ControllerConfig.audit_points) or 5),
            **{k: str(craw[k]) for k in ("question_pattern", "note_pattern", "accept_pattern", "revise_pattern")
               if craw.get(k)},
        )
        agents = [
            _build_agent(a, defaults, base, working_dir, f"{where}.agents[{j}]")
            for j, a in enumerate(mraw.get("agents") or [])
        ]
        team = {key: " and ".join(a.called for a in agents if a.role == role) or key
                for key, role in (("manager", "manager"), ("critic", "verifier"), ("worker", "worker"))}
        for a in agents:
            a.prompt_vars = dict(team)
            proto = {"manager": "manager", "verifier": "critic", "worker": "worker"}.get(a.role)
            if controller.mode == "managed" and controller.protocol and proto:
                a.protocol_path = str(PROMPTS_DIR / "protocol" / f"{proto}.md")
        m_known = {"name", "workspace", "working_dir", "goal", "max_rounds", "controller", "agents",
                   "defaults", "output_dir", "enabled"}
        unknown = set(mraw) - m_known
        if unknown:
            raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        monitors.append(MonitorConfig(
            name=name,
            workspace=workspace,
            working_dir=working_dir,
            goal=str(mraw.get("goal", "") or "").strip(),
            max_rounds=int(mraw.get("max_rounds", 50)),
            controller=controller,
            agents=agents,
            output_dir=_expand(mraw["output_dir"], base) if mraw.get("output_dir") else output_dir,
            enabled=bool(mraw.get("enabled", True)),
        ))

    cfg = Config(path=str(cfg_path), output_dir=output_dir, monitors=monitors)
    validate(cfg)
    return cfg


# --------------------------------------------------------------------------- validation


def validate(cfg: Config, check_paths: bool = False) -> List[str]:
    """Raise ConfigError on structural problems; return a list of warnings.

    With ``check_paths`` the workspace / working directories must exist.
    """
    from .backends import BACKENDS

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
            others = [a.name for a in m.agents if a.role not in ("worker", "verifier", "manager", "auditor")]
            if others:
                warnings.append(f"{where}: managed mode ignores agents with custom roles: {', '.join(others)}")
            if len(m.by_role("auditor")) > 1:
                raise ConfigError(f"{where}: at most one agent with role: auditor")
            if c.audit_every < 0 or c.audit_points < 1:
                raise ConfigError(f"{where}: controller.audit_every must be >= 0 and audit_points >= 1")
        if check_paths:
            if not Path(m.workspace).is_dir():
                raise ConfigError(f"{where}: workspace does not exist: {m.workspace}")
            if not Path(m.working_dir).is_dir():
                raise ConfigError(f"{where}: working_dir does not exist: {m.working_dir}")
    return warnings
