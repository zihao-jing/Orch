"""Models (F1): the choices of an agent, the model list, choosing one on the dashboard, the smoke test (MOD-1…5, 7).

`.orch/settings.yaml` holds the list (`models:`, parsed by config.parse_models); an agent's `agent.yaml` names its model
by id (`model`, `backup`, `backup2`) or gives it inline. Keys live only in these two files: they pass through
`Choice.env` to the CLI and nowhere else (Choice._stored drops them).

What applies on top of a listed model: the agent's own `backend_options` (its own `effort` only where the model is its
main one) and its own `env`, except the variables that belong to an address of its own (`ANTHROPIC_BASE_URL` and every
other `ANTHROPIC_…`): the address and the keys of a listed model come from the list alone (no hidden override).
"""
from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from .. import config
from .contracts import (Action, Adapter, Agent, AgentSpec, ChooseModel, Choice, Clock, CommandReply, ErrorClass, Event,
                        Lifecycle, ModelInfo, ModelReported, ProcessExited, Refusal, SaveChoice, SmokeResult, Snapshot,
                        StartSwitch, Switch, SwitchReason, SwitchStep, TextKey, TextRef, TurnEnded, TurnStatus)

# fixed levels of the CLIs that list none per model (docs/probes); Codex and OpenCode list them, Qwen has none (MOD-2)
FIXED_EFFORTS: Dict[str, List[str]] = {
    "claude": ["low", "medium", "high", "xhigh", "max"],
    "agy": ["low", "medium", "high", "xhigh", "max"],
    "qoder": ["auto", "none", "low", "medium", "high", "xhigh", "max", "ultracode"],
    "qwen": [],
}
_AGY_LEVEL = re.compile(r"-(low|medium|high|xhigh|max)$")
_KEY_VARS = re.compile(r"_(BASE_URL|AUTH_TOKEN|API_KEY|TOKEN|MODEL)$")

SMOKE_ID = "x00000000a11e"  # each smoke test has its own temporary session
SMOKE_TEXT = "Reply with the single word OK."
SMOKE_WAIT = 180.0  # seconds


# ------------------------------------------------------------------------------------------------ choices


def choices(settings: Dict[str, Any], agent_yaml: Dict[str, Any]) -> List[Choice]:
    """The agent's main choice and its backups in order, complete (env and options). Raises config.ConfigError."""
    models = config.parse_models((settings or {}).get("models"))
    y = agent_yaml or {}
    main = _listed(models, str(y["model"]), y, True) if y.get("model") else _inline(y)
    out = [main]
    for key in ("backup", "backup2"):
        if y.get(key):
            out.append(_listed(models, str(y[key]), y, False, main.backend))
    if y.get("fallback") and len(out) == 1:
        primary = {"backend": main.backend, "backend_options": _with_model(main), "env": main.env, "retries": 0}
        for f in config._build_fallback(y["fallback"], primary, "fallback"):
            c = _choice(f["backend"], f["backend_options"], f["env"], str(f.get("model_id") or ""))
            c.name = str(f.get("label") or c.label)
            out.append(c)
    return out


def resolve(choice: Choice, settings: Dict[str, Any], agent_yaml: Dict[str, Any]) -> Choice:
    """The choice with its env and options, as the CLI needs it. A choice that is not in the files any more (the one of
    an old session) keeps its own model and address, with the agent's own options."""
    models = config.parse_models((settings or {}).get("models"))
    y = agent_yaml or {}
    try:
        known = choices(settings, y)
    except config.ConfigError:
        known = []
    for c in known:
        if c.same(choice):
            return c
    c: Optional[Choice] = None
    if choice.model_id in models:
        c = _listed(models, choice.model_id, y, not choice.name, known[0].backend if known else "")
        if c.session_key != choice.session_key or c.backend != choice.backend:
            c = None  # the list changed: a session is reopened where it is (REC-8)
    if c is None:
        own = _inline(y)
        same = own.backend == choice.backend and not y.get("model")
        env = own.env if same and own.session_key == choice.session_key else {**_own_env(y), **choice.env}
        c = Choice(choice.backend, choice.provider, choice.model, options=own.options if same else dict(choice.options),
                   env=dict(env))  # its own options (an executable, a permission mode) stay; keys are only in env
    c.provider, c.model, c.effort = choice.provider or c.provider, choice.model or c.model, choice.effort
    c.model_id, c.name = choice.model_id, choice.name
    return c


def _listed(models: Dict[str, Dict[str, Any]], mid: str, y: Dict[str, Any], main: bool, main_backend: str = "") -> Choice:
    e = config._model(models, mid, "model")
    own = dict(y.get("backend_options") or {})
    own.pop("model", None)
    if not main:
        own = own if e["backend"] == main_backend else {}
        if e["backend_options"].get("effort") or "#" in str(e["backend_options"].get("model") or ""):
            own.pop("effort", None)
    c = _choice(e["backend"], config._deep_merge(e["backend_options"], own), {**_own_env(y), **e["env"]}, mid)
    c.name = "" if main else mid
    return c


def _inline(y: Dict[str, Any]) -> Choice:
    return _choice(str(y.get("backend") or "claude"), dict(y.get("backend_options") or {}),
                   {str(k): str(v) for k, v in (y.get("env") or {}).items() if v is not None}, "")


def _own_env(y: Dict[str, Any]) -> Dict[str, str]:
    """The agent's env without the variables that belong to an address of its own."""
    env = {str(k): str(v) for k, v in (y.get("env") or {}).items() if v is not None}
    prefixes = tuple(k[:-len("BASE_URL")] for k in env if k.endswith("_BASE_URL"))
    return {k: v for k, v in env.items() if not (prefixes and k.startswith(prefixes))}


def _choice(backend: str, options: Dict[str, Any], env: Dict[str, str], model_id: str) -> Choice:
    """Model, effort and provider are fields of their own; `options` holds the rest."""
    opts = {k: v for k, v in options.items() if v is not None}
    model, effort, provider = str(opts.pop("model", "") or ""), str(opts.pop("effort", "") or ""), ""
    if backend == "opencode":  # provider/model#variant
        model, _, variant = model.partition("#")
        provider, _, rest = model.partition("/")
        provider, model = (provider, rest) if rest else ("", model)
        effort = effort or variant
    return Choice(backend, provider, model, effort, model_id, opts, dict(env))


def _with_model(c: Choice) -> Dict[str, Any]:
    out = dict(c.options)
    if c.model:
        out["model"] = f"{c.provider}/{c.model}" if c.provider else c.model
    if c.effort:
        out["effort"] = c.effort
    return out


def catalog(settings: Dict[str, Any], listed: Dict[str, List[ModelInfo]]) -> List[ModelInfo]:
    """The models of the settings file with their effort levels; `listed` is what the CLIs say (by backend). No keys."""
    out = []
    for mid, e in config.parse_models((settings or {}).get("models")).items():
        c = _choice(e["backend"], e["backend_options"], {}, mid)
        info = next((m for m in listed.get(c.backend, []) if m.model == c.model
                     and (not c.provider or not m.provider or m.provider == c.provider)), None)
        if c.backend in FIXED_EFFORTS:
            efforts = list(FIXED_EFFORTS[c.backend])
            if c.backend == "agy" and _AGY_LEVEL.search(c.model):
                efforts = []  # the level is in the model id: never both
        else:
            efforts = list(info.efforts) if info else []
        default = c.effort or (info.default_effort if info else "")
        out.append(ModelInfo(mid, c.backend, c.provider, c.model, mid, efforts, default if efforts else ""))
    return out


# ------------------------------------------------------------------------------------------------ the module


class _Module:
    name = "models"

    def __init__(self, settings: Dict[str, Any]):
        models = config.parse_models((settings or {}).get("models"))
        self.models = {mid: _choice(e["backend"], e["backend_options"], e["env"], mid) for mid, e in models.items()}

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]:
        if isinstance(event, ChooseModel):
            return self.choose(snap, event)
        if isinstance(event, SmokeResult):
            return self.save(snap, event)
        return []

    def choose(self, snap: Snapshot, cmd: ChooseModel) -> List[Action]:
        a = snap.agents.get(cmd.agent)
        if a is None:
            return _refuse(cmd, Refusal.UNKNOWN)
        if not a.is_open:
            return _refuse(cmd, Refusal.DELETED if a.lifecycle == Lifecycle.DELETED else Refusal.CLOSED)
        target = self.target(snap, a, cmd)
        if target is None:
            return _refuse(cmd, Refusal.UNKNOWN)
        sw = a.switch
        if sw is not None and (sw.step in (SwitchStep.OPENING, SwitchStep.CONFIRMING)
                               or sw.step == SwitchStep.HANDOVER and sw.handover_by != a.id):
            return _refuse(cmd, Refusal.SWITCHING)  # the new program is open already
        new = a.sessions.get(target.session_key)
        s = Switch(a.id, target, SwitchReason.USER, cmd.force, created=snap.now, step_at=snap.now, command_id=cmd.id,
                   old=a.session, new=new, new_session=new is None and a.session is not None)
        if target.same(a.choice):
            if sw is None:
                return _refuse(cmd, Refusal.ALREADY)
            return [StartSwitch(s), CommandReply(cmd.id)]  # ends the pending one
        if cmd.force and not cmd.confirmed:
            jobs = [j.label or j.id for j in snap.running_jobs(a.id)]
            return [CommandReply(cmd.id, False, TextRef(TextKey.ASK_FORCE_SWITCH, {
                "agent": a.id, "jobs": jobs, "new_session": s.new_session}), ({"label": "force", "set": {"confirmed": True}},))]
        pending = None if cmd.force or snap.settled(a.id) else TextRef(
            TextKey.SWITCH_PENDING, {"agent": a.id, "choice": target.label})
        return [StartSwitch(s), CommandReply(cmd.id, text=pending)]

    def target(self, snap: Snapshot, a: Agent, cmd: ChooseModel) -> Optional[Choice]:
        if cmd.backup:
            return next((c for c in a.choices if c.name == cmd.backup), None)
        if not cmd.model_id:
            return a.choices[0] if a.choices else None
        base = self.models.get(cmd.model_id)
        if base is None:
            return None
        c = copy.deepcopy(base)
        if cmd.effort:
            info = next((m for m in snap.catalog if m.id == cmd.model_id), None)
            if info is not None and cmd.effort not in info.efforts:
                return None
            c.effort = cmd.effort
        return c

    def save(self, snap: Snapshot, ev: SmokeResult) -> List[Action]:
        """MOD-3: the user's choice passed its test. A backup is chosen for now only, so it is not saved."""
        a = snap.agents.get(ev.agent)
        sw = a.switch if a else None
        if (not ev.ok or a is None or sw is None or sw.reason != SwitchReason.USER or sw.step != SwitchStep.TESTING
                or not ev.choice.same(sw.target) or sw.target.name or not sw.target.model_id
                or (a.choices and a.choices[0].same(sw.target))):
            return []
        return [SaveChoice(a.id, sw.target)]


def module(settings: Dict[str, Any]) -> _Module:
    return _Module(settings)


def _refuse(cmd: ChooseModel, why: Refusal) -> List[Action]:
    return [CommandReply(cmd.id, False, TextRef(TextKey.COMMAND_REFUSED, {"why": why}))]


# ------------------------------------------------------------------------------------------------ smoke test


def smoke_test(adapter: Adapter, spec: AgentSpec, clock: Clock, wait: Callable[[float], None]) -> SmokeResult:
    """MOD-7: one tiny real turn in a new session (several CLIs accept any model name until they run one). Not ok when
    the CLI reports another model than asked for, as Qwen and Qoder silently take another. `wait(seconds)` sleeps."""
    end, reported = clock.now() + SMOKE_WAIT, ""
    done: Optional[SmokeResult] = None

    def result(ok: bool, why: str = "", error: Optional[ErrorClass] = None) -> SmokeResult:
        return SmokeResult(spec.agent, clock.now(), choice=spec.choice, ok=ok, why=why, error=error)

    try:
        adapter.start(spec, None)
        adapter.send(SMOKE_ID, SMOKE_TEXT)
        while done is None and clock.now() < end:
            for ev in adapter.events():
                if isinstance(ev, ModelReported):
                    reported = ev.model or reported
                elif isinstance(ev, TurnEnded):
                    if ev.status == TurnStatus.OK:
                        done = result(True)
                    else:
                        done = result(False, ev.detail or str(ev.status), ev.error or ErrorClass.OTHER)
                elif isinstance(ev, ProcessExited) and done is None:
                    done = result(False, ev.detail or f"exit {ev.exit_code}", ErrorClass.TRANSIENT)
            if done is None:
                wait(0.5)
    except Exception as e:  # noqa: BLE001  base.AdapterError carries its class; anything else is the CLI's too
        done = result(False, str(e), getattr(e, "error", None) or ErrorClass.OTHER)
    finally:
        try:
            adapter.stop()
        except Exception:  # noqa: BLE001  (it is gone, or never started: the result stands)
            pass
    if done is None:
        return result(False, f"{int(SMOKE_WAIT)} s", ErrorClass.TRANSIENT)
    if done.ok and reported and not same_model(spec.choice, reported):
        return result(False, f"{spec.choice.model} -> {reported}", ErrorClass.OTHER)
    return done


def same_model(choice: Choice, reported: str) -> bool:
    """Whether the reported model is the one asked for. Aliases: Claude's names ("opus" for claude-opus-5-5, also the
    ones its env redirects), a date at the end, the provider in front, Antigravity's level at the end, letter case."""
    want, got = _plain(choice.model), _plain(reported)
    if not want or not got or want in ("default", "auto") or want == got:
        return True
    if re.fullmatch(re.escape(want) + r"-(\d{8}|\d{4}-\d{2}-\d{2}|latest)", got):
        return True
    if choice.backend == "agy":
        return bool(choice.effort) and got == f"{want}-{choice.effort.lower()}"
    if choice.backend == "opencode":
        full = f"{choice.provider.lower()}/{want}" if choice.provider else want
        return got == full or full.endswith("/" + got)
    if choice.backend == "claude":
        if any(k.startswith("ANTHROPIC_") and k.endswith("MODEL") and _plain(v) == got for k, v in choice.env.items()):
            return True
        return f"-{want}-" in f"-{got}-" or (want == "opusplan" and ("opus" in got or "sonnet" in got))
    return False


def _plain(model: str) -> str:
    """Lower case, without a context size ("[1m]") or a variant ("#high")."""
    return re.sub(r"\[[^\]]*\]$", "", (model or "").strip().lower().partition("#")[0])


# ------------------------------------------------------------------------------------------------ the files


def save_choice(agent_yaml: Path, choice: Choice) -> None:
    """MOD-3: the choice replaces the old model and effort lines; everything else in the file stays as written."""
    path = Path(agent_yaml).resolve()
    _write(path, with_choice(path.read_text(encoding="utf-8"), choice.model_id, choice.effort))


def with_choice(text: str, model_id: str, effort: str = "", backups: Sequence[str] = (),
                drop_env: Sequence[str] = (), drop_options: Sequence[str] = ()) -> str:
    """The text of an agent.yaml that refers to `model_id` (and, for MOD-4, to `backups` instead of inline ones)."""
    if not model_id:
        raise config.ConfigError("a choice needs an id of the model list")
    want = yaml.safe_load(text) or {}
    text = _set(text, "model", model_id, before="backend")
    want["model"] = model_id
    for key in ("backend", "model_id") + (("fallback",) if backups else ()):
        text = _remove(text, [key])
        want.pop(key, None)
    for i, b in enumerate(backups):
        key = "backup" if i == 0 else f"backup{i + 1}"
        text = _set(text, key, b, after="model" if i == 0 else "backup")
        want[key] = b
    had = isinstance(want.get("backend_options"), dict)
    opts = dict(want.get("backend_options") or {})
    if effort and "effort" in opts:
        text = _set_child(text, "backend_options", "effort", effort)
        opts["effort"] = effort
    for key in ("model",) + (() if effort else ("effort",)) + tuple(drop_options):
        if key in opts:
            text = _remove(text, ["backend_options", key])
            opts.pop(key)
    if effort and "effort" not in opts:
        text = _set_child(text, "backend_options", "effort", effort)
        opts["effort"] = effort
    if opts:
        want["backend_options"] = opts
    elif had or "backend_options" in want:
        if "backend_options" in (yaml.safe_load(text) or {}):
            text = _remove(text, ["backend_options"])
        want.pop("backend_options", None)
    env = dict(want.get("env") or {})
    for key in drop_env:
        if key in env:
            text = _remove(text, ["env", key])
            env.pop(key)
    if drop_env and "env" in want:
        if env:
            want["env"] = env
        else:
            want.pop("env")
            if "env" in (yaml.safe_load(text) or {}):
                text = _remove(text, ["env"])
    if (yaml.safe_load(text) or {}) != want:  # never write a file that says something else
        raise config.ConfigError("agent.yaml could not be edited safely; change its model lines by hand")
    return text


def _write(path: Path, text: str) -> None:
    """Replace the file in one step, with its permissions."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _find(text: str, keys: Sequence[str]) -> Optional[Tuple[yaml.Node, yaml.Node, yaml.Node]]:
    """(mapping, key node, value node) at this path, or None."""
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    found = None
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            return None
        pair = next(((k, v) for k, v in node.value if k.value == key), None)
        if pair is None:
            return None
        found, node = (node, pair[0], pair[1]), pair[1]
    return found


def _scalar(value: str) -> str:
    return yaml.safe_dump(value, width=10 ** 6).splitlines()[0]


def _line(text: str, at: int) -> Tuple[int, int]:
    """Start and end (after the newline) of the line that holds index `at`."""
    start, end = text.rfind("\n", 0, at) + 1, text.find("\n", at)
    return start, len(text) if end < 0 else end + 1


def _set(text: str, key: str, value: str, before: str = "", after: str = "") -> str:
    """A top-level `key: value`: replaced where it is, else put before or after another key, else in front."""
    got = _find(text, [key])
    if got:
        v = got[2]
        return text[:v.start_mark.index] + _scalar(value) + text[v.end_mark.index:]
    root = yaml.compose(text, Loader=yaml.SafeLoader) if text.strip() else None
    if root is not None and (not isinstance(root, yaml.MappingNode) or root.flow_style):
        raise config.ConfigError("agent.yaml is not a plain block of `key: value` lines")
    new = f"{key}: {_scalar(value)}\n"
    other = _find(text, [before or after])
    if other and before:
        at = _line(text, other[1].start_mark.index)[0]
    elif other:
        at = _line(text, other[2].end_mark.index)[1]
        if text[:at] and not text[:at].endswith("\n"):
            new = "\n" + new
    else:
        at = _line(text, root.value[0][0].start_mark.index)[0] if root is not None else len(text)
    return text[:at] + new + text[at:]


def _set_child(text: str, parent: str, key: str, value: str) -> str:
    """`key: value` inside the top-level mapping `parent` (made if missing)."""
    got = _find(text, [parent, key])
    if got:
        v = got[2]
        return text[:v.start_mark.index] + _scalar(value) + text[v.end_mark.index:]
    up = _find(text, [parent])
    if up is None:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        at = _line(text, root.value[0][1].end_mark.index)[1]  # after the first line: the model
        nl = "" if text[:at].endswith("\n") else "\n"
        return text[:at] + f"{nl}{parent}: {{{key}: {_scalar(value)}}}\n" + text[at:]
    v = up[2]
    if isinstance(v, yaml.MappingNode) and v.flow_style:
        at = v.start_mark.index + 1
        return text[:at] + f"{key}: {_scalar(value)}" + (", " if v.value else "") + text[at:]
    if isinstance(v, yaml.MappingNode) and v.value:
        first = v.value[0][0].start_mark
        return text[:first.index] + f"{key}: {_scalar(value)}\n" + " " * first.column + text[first.index:]
    if not v.value:  # empty: `parent:`, `parent: {}` or null
        colon = text.index(":", up[1].end_mark.index) + 1
        return text[:colon] + f" {{{key}: {_scalar(value)}}}" + text[max(colon, v.end_mark.index):]
    raise config.ConfigError(f"{parent} is not a mapping")


def _remove(text: str, keys: Sequence[str]) -> str:
    got = _find(text, keys)
    if got is None:
        return text
    mapping, k, v = got
    if mapping.flow_style:  # {a: 1, b: 2}: with the comma after it, or before it
        start, end = k.start_mark.index, v.end_mark.index
        tail = re.match(r"\s*,\s*", text[end:])
        if tail:
            return text[:start] + text[end + tail.end():]
        head = re.search(r",\s*$", text[:start])
        return text[:head.start() if head else start] + text[end:]
    start = _line(text, k.start_mark.index)[0]
    if isinstance(v, yaml.ScalarNode) or v.flow_style:
        end = _line(text, max(v.end_mark.index, k.end_mark.index))[1] if not _block_scalar(v) else v.end_mark.index
        if v.value == "" and isinstance(v, yaml.ScalarNode) and not v.style:
            end = _line(text, k.end_mark.index)[1]
        return text[:start] + text[end:]
    last = _last(v)  # a block: up to its last value, so that what follows keeps its comments
    end = last.end_mark.index if _block_scalar(last) else _line(text, last.end_mark.index)[1]
    return text[:start] + text[end:]


def _last(node: yaml.Node) -> yaml.Node:
    while isinstance(node, yaml.CollectionNode) and not node.flow_style and node.value:
        x = node.value[-1]
        node = x[1] if isinstance(x, tuple) else x
    return node


def _block_scalar(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.style in ("|", ">")


# MOD-4: an existing project's inline models become the list. Package M writes the files (and keeps the old ones).


def list_models(settings: Dict[str, Any], agents: Dict[str, Dict[str, Any]]
                ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """(models to add to the list, per agent the keywords of with_choice). Agents that refer to the list already, or
    have more inline backups than agent.yaml can refer to (2), keep what they have. Holds keys: only for the files."""
    models = {str(k): dict(v) for k, v in ((settings or {}).get("models") or {}).items()}
    known, added, refs = dict(models), {}, {}

    def add(c: Choice, options: Dict[str, Any], env: Dict[str, str], label: str) -> str:
        entry: Dict[str, Any] = {"backend": c.backend}
        if options:
            entry["backend_options"] = options
        if env:
            entry["env"] = env
        for mid, e in known.items():
            if e == entry:
                return mid
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-.") or c.backend
        mid, n = base, 2
        while mid in known:
            mid, n = f"{base}-{n}", n + 1
        known[mid] = added[mid] = entry
        return mid

    for name, y in agents.items():
        if y.get("model") or y.get("backup"):
            continue
        every = choices(settings, y)
        main, backups = every[0], every[1:]
        if len(backups) > 2:
            continue
        prefixes = tuple(k[:-len("BASE_URL")] for k in main.env if k.endswith("_BASE_URL"))
        moved = [k for k in main.env if _KEY_VARS.search(k) or (prefixes and k.startswith(prefixes))]
        ref: Dict[str, Any] = {"model_id": add(main, {k: v for k, v in _with_model(main).items() if k in ("model", "effort")},
                                               {k: main.env[k] for k in moved}, main.model or main.backend),
                               "drop_env": moved}
        if backups:
            ref["backups"] = [add(b, _with_model(b) if b.backend != main.backend else
                                  {k: v for k, v in _with_model(b).items()
                                   if k in ("model", "effort") or main.options.get(k) != v},
                                  {k: v for k, v in b.env.items() if k in moved or main.env.get(k) != v}, b.name)
                              for b in backups]
        refs[name] = ref
    return added, refs


def with_models(text: str, models: Dict[str, Dict[str, Any]]) -> str:
    """The text of a settings.yaml with these models added to its list."""
    if not models:
        return text
    want = yaml.safe_load(text) or {}
    want["models"] = {**(want.get("models") or {}), **models}
    got = _find(text, ["models"])
    if got and (not isinstance(got[2], yaml.MappingNode) or got[2].flow_style or not got[2].value):
        text, got = _remove(text, ["models"]), None
        models = want["models"]
    if got:
        first = got[2].value[0][0].start_mark
        block = "".join(" " * first.column + ln + "\n" for ln in _dump(models).splitlines())
        at = _line(text, first.index)[0]
        text = text[:at] + block + text[at:]
    else:
        block = "models:\n" + "".join("  " + ln + "\n" for ln in _dump(models).splitlines())
        text = text + ("" if not text or text.endswith("\n") else "\n") + block
    if yaml.safe_load(text) != want:
        raise config.ConfigError("settings.yaml could not be edited safely; add the models by hand")
    return text


def _dump(models: Dict[str, Dict[str, Any]]) -> str:
    return yaml.safe_dump(models, sort_keys=False, allow_unicode=True, width=10 ** 6)
