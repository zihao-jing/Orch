"""Run the runtime's event loop and perform the store's durable intents."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, Callable, Dict, Iterable, Optional

import yaml

from . import contracts as C
from . import golden, labels, milestones, models, texts
from .adapters.base import AdapterError
from .store import Store

LOG = logging.getLogger(__name__)


class Engine:
    def __init__(self, run_dir, clock: C.Clock, adapters: Dict[str, Callable[[], C.Adapter]],
                 settings: Dict[str, Any], modules: Optional[Iterable[C.Module]] = None):
        self.run_dir, self.clock = Path(run_dir), clock
        self.factories, self.settings = adapters, settings
        self.strict = settings.get("strict_actions", modules is not None)
        if modules is None:
            modules = [import_module(f".{name}", __package__).module(settings) if name in ("models", "workers")
                       else import_module(f".{name}", __package__).module() for name in C.MODULE_ORDER]
        by_name = {m.name: m for m in modules}
        self.modules = [by_name[name] for name in C.MODULE_ORDER if name in by_name]
        self.store: Store
        self.adapters: Dict[str, C.Adapter] = {}
        self._lock = threading.RLock()
        self._commands: SimpleQueue = SimpleQueue()
        self._smoked: SimpleQueue = SimpleQueue()
        self._audited: SimpleQueue = SimpleQueue()  # (record) from the audit thread
        self._audit_stop = threading.Event()
        self._audit_rounds: Optional[int] = None  # snap.rounds when the last audit started
        self._audit_final = False  # the last audit started while the run was finished
        self._smoke_stop = threading.Event()
        self._smokes = set()
        self._pending_sends = {}
        self._attempted = set()
        self._awaiting_turn = set()
        self._last_jobs = {}
        self._closing_workers = set()
        self._opening = set()
        self._starting = True
        self._closed = True
        self._stop_requested = False
        self.update_requested = False  # UpdateOrch: the CLI takes in the new code and restarts; agents carry on
        self._last_tick = clock.now()
        self.tick_s = settings.get("tick_s", 5)
        self.jobs_check_s = settings.get("jobs_check_s", 60)

    def start(self, snap_or_none: Optional[C.Snapshot]) -> None:
        with self._lock:
            if not self._closed:
                raise RuntimeError("the engine is already open")
            options = dict(parse=labels.parse, read_checklist=milestones.read_checklist, read_board=golden.read_board)
            self.store = (Store(self.run_dir, self.clock, **options) if snap_or_none is None else
                          Store.create(self.run_dir, self.clock, snap_or_none, **options))
            self._starting, self._closed, self._stop_requested = True, False, False
            self._smoked = SimpleQueue()
            self._smoke_stop = threading.Event()
            self._smokes.clear()
            self._awaiting_turn.clear()
            self._pending_sends.clear()
            self._attempted.clear()
            self._last_jobs.clear()
            self._last_tick = self.clock.now()
            try:
                self._recover()
            except BaseException:
                self.close()
                raise
            self._fill_catalog()

    def _fill_catalog(self) -> None:
        """MOD-1, 2: the models of the settings for the page now; their effort levels as each CLI lists them come later,
        from a background thread (a CLI may take seconds to answer, or not answer)."""
        if not (self.settings or {}).get("models"):
            return
        self.store.configure(catalog=models.catalog(self.settings, {}))
        backends = sorted({str(e.get("backend") or "") for e in (self.settings.get("models") or {}).values()
                           if isinstance(e, dict)} & set(self.factories))
        if not backends or self.settings.get("list_models", True) is False:
            return

        def listing() -> None:
            listed: Dict[str, Any] = {}
            for b in backends:
                try:
                    listed[b] = list(self.factories[b]().catalog())
                except Exception as exc:  # a CLI that cannot list: its models keep the settings' effort
                    LOG.info("could not list the models of %s: %s", b, exc)
            try:
                if not self._closed:
                    self.store.configure(catalog=models.catalog(self.settings, listed))
            except Exception as exc:
                LOG.info("could not save the model list: %s", exc)
        threading.Thread(target=listing, name="orch-models", daemon=True).start()

    def _recover(self) -> None:
        # Attach first so replayed process operations can act on the saved CLI. Replay its output after pending facts.
        attached, failures = {}, {}
        for agent in self.store.snapshot().agents.values():
            if agent.proc:
                try:
                    choice = self._proc_choice(agent)
                    adapter = self._make_adapter(agent.id, choice)
                    attached[agent.id] = (adapter, agent.proc, adapter.reconnect(agent.proc))
                    self._remember_prompt(agent.id)
                except Exception as exc:
                    failures[agent.id] = (agent.proc, exc)
        if self.store.pending is not None:
            self._event(self.store.pending)
        self._perform(self.store.unfinished(), replay=True)
        started = C.OrchStarted(at=self._last_tick)
        while self.store.handled(started.key):
            started = replace(started, uid=C.new_id("engine", started))
        self._reconcile_lifecycle()
        for name, (adapter, proc, connected) in attached.items():
            agent = self.store.snapshot().agents[name]
            if self.adapters.get(name) is not adapter or agent.proc is None or agent.proc.stream != proc.stream:
                continue
            self._drain(name, adapter, catching_up=True)
            current = self.store.snapshot().agents[name].proc
            if connected and current and current.stream == proc.stream:
                cause = f"{proc.stream}/{started.key}"
                self._event(C.Reconnected(name, self._last_tick, cause))
                self._check_jobs(name, adapter, cause)
                self._check_doubt(name, adapter, replaced=False, skip_resends=True)
        for name, (proc, exc) in failures.items():
            if self.store.snapshot().agents[name].proc == proc:
                self._adapter_failed(name, exc, proc.stream)
        for agent in self.store.snapshot().agents.values():
            if agent.proc is None and self.store.snapshot().running_jobs(agent.id):
                self._event(C.JobsChecked(agent.id, started.at, started.key))
        self._event(started)
        self._starting = False
        if self.store.snapshot().run.audit_running:  # an audit cut off by the end of Orch
            self._audit_done(None)
        self._flush_sends()

    def step(self) -> bool:
        with self._lock:
            if self._closed or self._stop_requested:
                return False
            self._attempted.clear()
            if self.store.pending is not None:
                self._event(self.store.pending)
                happened = True
            else:
                happened = False
            unfinished = [i for i in self.store.unfinished()
                          if i.id not in self._smokes and i.id not in self._pending_sends]
            if unfinished:
                self._perform(unfinished, replay=True)
                happened = self._flush_sends() or happened
            happened = self._read_commands() or bool(unfinished) or happened
            if self._stop_requested:
                return True
            while True:
                try:
                    intent, result = self._smoked.get_nowait()
                except Empty:
                    break
                self._event(result)
                self.store.intent_done(intent.id)
                self._smokes.discard(intent.id)
                happened = True
            while True:
                try:
                    record = self._audited.get_nowait()
                except Empty:
                    break
                self._audit_done(record)
                happened = True
            for name, adapter in list(self.adapters.items()):
                if self.adapters.get(name) is adapter:
                    happened = self._drain(name, adapter) or happened
            happened = self._reconcile_lifecycle() or happened
            now = self.clock.now()
            self._expire_compacts(now)
            for name, adapter in list(self.adapters.items()):
                if (self.store.snapshot().running_jobs(name)
                        and now - self._last_jobs.get(name, self._last_tick) >= self.jobs_check_s):
                    happened = self._check_jobs(name, adapter, f"periodic/{now}") or happened
            if now - self._last_tick >= self.tick_s:
                self._last_tick = now
                self._event(C.Tick(at=now))
                self._auto_audit()
                happened = True
            return self._flush_sends() or happened

    def run(self, stop_event: threading.Event) -> None:
        while not (stop_event.is_set() or self._stop_requested or self._closed):
            if not self.step():
                stop_event.wait(0.2)
        if self._stop_requested:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._smoke_stop.set()
            self._audit_stop.set()
            try:
                for name in list(self.adapters):
                    self._detach(name)
            finally:
                self.store.close()

    def stop_agents(self) -> None:
        self.submit(C.StopAgents(at=self.clock.now(), id=os.urandom(16).hex()))

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return C.public_state(self.store.snapshot())

    def submit(self, command: C.UserCommand) -> Dict[str, Any]:
        future: Future = Future()
        with self._lock:
            if self._closed or self._stop_requested:
                raise RuntimeError("the engine is closed")
            self._commands.put((command, future))
            self._attempted.clear()
            self._read_commands()
        return future.result()

    def message(self, id: str) -> Optional[C.Message]:
        with self._lock:
            return self.store.message(id)

    def messages(self, before: int = 0, limit: int = 50, q: str = "", agent: str = ""):
        with self._lock:
            return self.store.messages(before, limit, q, agent)

    # Event and command handling

    def _event(self, event: C.Event) -> bool:
        ev = self.store.begin(event)
        if ev is None:
            return False
        snap = self.store.snapshot()
        prior = []
        for module in self.modules:
            for action in module.decide(snap, ev, tuple(prior)):
                if C.may_emit(module.name, action):
                    prior.append(action)
                else:
                    reason = f"{module.name} may not emit {type(action).__name__}"
                    if self.strict:
                        raise AssertionError(reason)
                    LOG.error("%s", reason)
        if isinstance(ev, C.UserCommand) and ev.handler == "engine":
            prior.extend(self._engine_command(ev, snap))
        self._perform(self.store.apply(ev, prior))
        return True

    def _command_dir(self) -> Path:
        return self.run_dir / "commands"

    def _reply_path(self, command_id: str) -> Path:
        return self._command_dir() / "replies" / (hashlib.sha256(command_id.encode()).hexdigest() + ".json")

    def _command(self, command: C.UserCommand) -> Dict[str, Any]:
        self._event(command)
        self._reconcile_lifecycle()
        self._flush_sends()
        path = self._reply_path(command.id)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return C.reply_json(C.CommandReply(command.id, ok=False), "The command produced no reply.")

    def _read_commands(self) -> bool:
        happened = False
        # names are <time>-<id>.json and sort in the order they were written (orch.cli.write_command)
        for path in sorted(self._command_dir().glob("*.json"), key=lambda p: p.name):
            try:
                command = C.from_dict(C.UserCommand, json.loads(path.read_text(encoding="utf-8")))
                if not isinstance(command, C.UserCommand):
                    raise ValueError("expected a user command")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                LOG.warning("Cannot read command %s: %s", path.name, exc)
                continue
            self._command(command)
            path.unlink(missing_ok=True)
            happened = True
            if self._stop_requested:
                break
        while True:
            try:
                command, future = self._commands.get_nowait()
            except Empty:
                break
            try:
                if self._stop_requested:
                    raise RuntimeError("the engine is closing")
                future.set_result(self._command(command))
            except Exception as exc:
                future.set_exception(exc)
            happened = True
        return happened

    def _engine_command(self, ev: C.UserCommand, snap: C.Snapshot):
        if isinstance(ev, C.StopOrch):
            return [C.CommandReply(ev.id, data={"stop_orch": True, "stop_agents": ev.agents})]
        if isinstance(ev, (C.StopAgents, C.StartAgents)):
            agents = getattr(ev, "agents", ()) or tuple(snap.agents)
            if isinstance(ev, C.StartAgents):
                return [C.RestartSession(i) for i in agents if i in snap.agents and snap.agents[i].is_open
                        and C.HoldReason.STOPPED in snap.agents[i].holds] + [
                            C.ReleaseAgent(i, C.HoldReason.STOPPED) for i in agents if i in snap.agents
                        ] + [C.CommandReply(ev.id)]
            return [C.HoldAgent(i, C.HoldReason.STOPPED) for i in agents if i in snap.agents] + [
                C.CommandReply(ev.id)]
        if isinstance(ev, C.UpdateOrch):
            return [C.CommandReply(ev.id, data={"update_orch": True})]
        if isinstance(ev, C.SetNight):
            self.store.configure(night=ev.on)
            return [C.CommandReply(ev.id)]
        if isinstance(ev, C.AuditRun):
            why = self._audit(snap)
            return [C.CommandReply(ev.id, ok=not why, data={"why": why} if why else {"audit": "started"})]
        if isinstance(ev, C.AuditNotes) and snap.run.audit_notes_path:
            path = Path(snap.run.workspace, snap.run.audit_notes_path)
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old == ev.text or hashlib.sha256(old.encode()).hexdigest() == ev.base:
                self._write(path, ev.text)
                return [C.CommandReply(ev.id)]
        return [C.CommandReply(ev.id, ok=False)]

    # Intent execution and transmission

    def _perform(self, intents: Iterable[C.Intent], replay: bool = False) -> None:
        for intent in intents:
            action = intent.action
            if isinstance(action, C.Resend) and intent.id.startswith("engine/resend-check/"):
                # This helper intent records the check without asking the CLI again.
                self.store.intent_done(intent.id)
            elif isinstance(action, (C.Send, C.Resend, C.Release, C.ReleaseAgent)):
                self._pending_sends.setdefault(intent.id, (intent, replay))
            elif intent.id not in self._smokes:
                self._effect(intent, replay)

    def _effect(self, intent: C.Intent, replay: bool) -> None:
        try:
            done = self._effect_action(intent, replay)
        except (AdapterError, OSError, ValueError) as exc:
            LOG.warning("Intent %s failed: %s", intent.id, C.redact(str(exc)))
            self.store.intent_done(intent.id, str(exc))
        else:
            if done:
                self.store.intent_done(intent.id)

    def _effect_action(self, intent: C.Intent, replay: bool) -> bool:
        a = intent.action
        if isinstance(a, C.CommandReply):
            if a.data.get("stop_orch"):
                if a.data.get("stop_agents"):
                    # StopAgents supplies the facts for the combined command.
                    self._event(C.StopAgents(at=intent.created, id=f"stop/{a.command_id}"))
                self._reconcile_lifecycle()
            path = self._reply_path(a.command_id)
            if not path.exists():
                shown = texts.words(a.text, self.store.snapshot()) if a.text else ""
                extra = self._effect_path("command", a.command_id)
                if extra.exists():
                    a = replace(a, data={**a.data, **json.loads(extra.read_text(encoding="utf-8"))})
                self._write(path, json.dumps(C.reply_json(a, shown), ensure_ascii=False))
                extra.unlink(missing_ok=True)
            if a.data.get("stop_orch"):
                self._stop_requested = True
            if a.data.get("update_orch") and not replay:
                self.update_requested = True
        elif isinstance(a, C.FailAgent):
            self._event(C.AgentFailed(a.agent, intent.created, intent.id, a.error, str(a.why)))
        elif isinstance(a, C.RestartSession):
            self._restart(a.agent, intent, replay)
        elif isinstance(a, (C.StartSwitch, C.AdvanceSwitch)):
            return self._switch(intent, a.switch, replay)
        elif isinstance(a, C.RefreshSession):
            self._refresh(intent, a, replay)
        elif isinstance(a, C.SmokeTest):
            return self._smoke(intent, a.agent, a.choice)
        elif isinstance(a, C.SaveChoice):
            models.save_choice(self._agent_yaml(a.agent), a.choice)
        elif isinstance(a, C.AddWorker):
            from .. import config
            add = getattr(config, "add_worker", None)
            if add is None:
                raise ValueError("orch.config.add_worker is unavailable")
            add(Path(self.store.snapshot().run.workspace), a.like, a.agent.id)
        elif isinstance(a, C.StopJob):
            job = self.store.snapshot().jobs.get(a.job_id)
            if job and job.agent in self.adapters and not self.adapters[job.agent].stop_job(job.id):
                raise AdapterError(f"could not stop job {job.id}")
        elif isinstance(a, C.SetLifecycle):
            self._reconcile_lifecycle()
        elif isinstance(a, C.SaveNotes):
            snap = self.store.snapshot()
            if snap.agents[a.agent].state_file:
                self._write(Path(snap.run.workspace, snap.agents[a.agent].state_file), a.text)
        elif isinstance(a, C.RecordDecision):
            self._decision(intent, a)
        elif isinstance(a, C.OpenTerminal):
            self._open_terminal(a)
        elif isinstance(a, C.CloseTerminal):
            self._close_terminal(intent, a.agent)
        return True

    def _flush_sends(self) -> bool:
        if self._starting or self._stop_requested:
            return False
        happened = False
        while self._pending_sends or self.store.to_send():
            pending, self._pending_sends = self._pending_sends, {}
            errors = {}
            for intent, replay in pending.values():
                action = intent.action
                try:
                    if isinstance(action, C.Resend):
                        self._resend(intent, action)
                    elif replay and isinstance(action, C.Send):
                        msg = self.store.message(action.message.id)
                        if msg and msg.sent_at and msg.status == C.MessageStatus.WAITING:
                            self._probe(msg, self.adapters.get(msg.recipient))
                except (AdapterError, OSError) as exc:
                    errors[intent.id] = str(exc)
                    LOG.warning("Transmission check failed: %s", C.redact(str(exc)))
            for msg in self.store.to_send():
                error = self._transmit(msg.id)
                if error is not None:
                    errors[msg.id] = error
                    happened = True
            for intent, _ in pending.values():
                error = errors.get(intent.id, "")
                if isinstance(intent.action, C.Send):
                    error = error or errors.get(intent.action.message.id, "")
                if isinstance(intent.action, C.Resend):
                    error = error or errors.get(intent.action.message_id, "")
                self.store.intent_done(intent.id, error)
            if not self._pending_sends:
                break
        return happened

    def _transmit(self, message_id: str) -> Optional[str]:
        msg = self.store.message(message_id)
        if not msg or message_id in self._attempted or not self.store.can_send(msg):
            return None
        snap = self.store.snapshot()
        agent = snap.agents[msg.recipient]
        if (agent.stopped_by_user or agent.id in self._opening or
                not agent.caps.sends_while_writing and (agent.status == C.AgentStatus.WRITING
                                                       or agent.id in self._awaiting_turn)):
            return None
        if agent.proc is None:
            if not self._open(agent.id, self._session_choice(agent), agent.session, f"send/{msg.id}/{msg.attempts}"):
                self._attempted.add(msg.id)
                return f"could not open {agent.id}'s session"
            msg = self.store.message(message_id)
            agent = self.store.snapshot().agents[agent.id]
            if not msg or not self.store.can_send(msg) or agent.proc is None:
                return None
        adapter = self.adapters.get(agent.id)
        if adapter is None:
            return None
        r = C.Rendered(msg.rendered, msg.given) if msg.attempts else texts.render(self.store.snapshot(), msg)
        if not self.store.begin_send(msg.id, r.text, r.given):
            return None
        self._attempted.add(msg.id)
        msg = self.store.message(msg.id)
        try:
            adapter.send(msg.id, r.text)
        except Exception as exc:
            self._event(C.SendFailed(agent.id, self.clock.now(), f"{msg.id}/{msg.attempts}", msg.id,
                                     self._error(exc), str(exc)))
            return str(exc) or type(exc).__name__
        if not adapter.caps.sends_while_writing:
            self._awaiting_turn.add(agent.id)
        if not adapter.caps.receipts:
            self._received(msg, taken=False)
        return ""

    def _received(self, msg: C.Message, taken: bool) -> None:
        proc = self.store.snapshot().agents[msg.recipient].proc
        self._event(C.Received(msg.recipient, self.clock.now(), proc.stream if proc else "",
                               uid=f"engine/received/{msg.id}/{msg.attempts}/{taken}", message_id=msg.id, taken=taken))

    def _probe(self, msg: C.Message, adapter: Optional[C.Adapter], *, replaced: bool = False,
               if_unknown: bool = False) -> bool:
        if adapter is None:
            return False
        try:
            arrived = adapter.arrived(msg.id)
        except Exception as exc:
            self._adapter_failed(msg.recipient, exc, f"arrived/{msg.id}/{msg.attempts}")
            raise AdapterError(str(exc), self._error(exc)) from exc
        if arrived is True or arrived is None and replaced and not msg.needs_answer and not if_unknown:
            self._received(msg, taken=replaced)
        elif arrived is False or arrived is None and if_unknown:
            self.store.not_arrived(msg.id)
            self._awaiting_turn.discard(msg.recipient)
            return True
        return False

    def _resend(self, intent: C.Intent, action: C.Resend) -> None:
        msg = self.store.message(action.message_id)
        if msg and not msg.taken_at and msg.drop is None and msg.status in (
                C.MessageStatus.WAITING, C.MessageStatus.DELIVERED):
            if msg.sent_at and not self._probe(msg, self.adapters.get(msg.recipient), if_unknown=action.if_unknown):
                return
            if msg.attempts:
                # not_arrived clears checked_at; reapply the check so the next timeout fails the agent.
                for saved in self.store.act(f"engine/resend-check/{intent.id}", [action]):
                    self.store.intent_done(saved.id)

    def _check_doubt(self, name: str, adapter: C.Adapter, *, replaced: bool, skip_resends: bool = False) -> None:
        pending = {i.action.message_id for i, _ in self._pending_sends.values() if isinstance(i.action, C.Resend)}
        pending.update(i.action.message.id for i, replay in self._pending_sends.values()
                       if replay and isinstance(i.action, C.Send))
        for msg in self.store.in_doubt(name):
            if not skip_resends or msg.id not in pending:
                try:
                    self._probe(msg, adapter, replaced=replaced)
                except AdapterError:
                    pass  # _probe has already reported the failure

    # Adapter lifecycle and facts

    @staticmethod
    def _proc_choice(agent: C.Agent) -> C.Choice:
        """The choice the saved process runs on: during a switch, a process started after the new session was made is
        the new one (whatever step was saved last), an older one is the old CLI."""
        sw, proc = agent.switch, agent.proc
        if sw and sw.step in (C.SwitchStep.PENDING, C.SwitchStep.TESTING):
            return Engine._session_choice(agent)  # nothing new is open before OPENING
        if sw and sw.new and proc and proc.started >= sw.new.started:
            return sw.new.choice or sw.target
        if sw and not sw.new and proc and (sw.step == C.SwitchStep.CONFIRMING
                                           or sw.step == C.SwitchStep.OPENING and proc.started >= sw.step_at):
            return sw.target  # OPENING saved, then the new CLI started; a crash before that leaves the old one
        return Engine._session_choice(agent)

    @staticmethod
    def _session_choice(agent: C.Agent) -> C.Choice:
        return agent.session.choice if agent.session else agent.choice

    def _agent_yaml(self, name: str) -> Path:
        snap = self.store.snapshot()
        return Path(snap.run.workspace, ".orch", "agents", name, "agent.yaml")

    def _spec(self, name: str, choice: C.Choice, session: Optional[C.SessionRef], run_dir: Path) -> C.AgentSpec:
        snap = self.store.snapshot()
        agent = snap.agents[name]
        path = self._agent_yaml(name)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        resolved = models.resolve(choice, self.settings, data or {})
        return C.AgentSpec(name, agent.role, resolved, (session.cwd if session else "") or agent.cwd,
                           texts.system_prompt(snap, name), str(run_dir))

    def _make_adapter(self, name: str, choice: C.Choice) -> C.Adapter:
        if choice.backend not in self.factories:
            raise AdapterError(f"no adapter for {choice.backend}", C.ErrorClass.MISSING_CLI)
        adapter = self.factories[choice.backend]()
        self.adapters[name] = adapter
        self.store.set_agent(name, caps=adapter.caps)
        return adapter

    def _remember_prompt(self, name: str) -> None:
        self.store.system_prompts[name] = texts.system_prompt(self.store.snapshot(), name)

    def _open(self, name: str, choice: C.Choice, session: Optional[C.SessionRef], cause: str) -> bool:
        self._opening.add(name)
        try:
            try:
                spec = self._spec(name, choice, session, self.run_dir)
                adapter = self._make_adapter(name, choice)
                proc = adapter.start(spec, session)
            except Exception as exc:
                self._detach(name)
                self._adapter_failed(name, exc, cause)
                return False
            self.store.set_agent(name, caps=adapter.caps)
            self.store.set_proc(name, proc)
            self.store.system_prompts[name] = spec.system_prompt
            self._last_jobs[name] = self.clock.now()
            self._drain(name, adapter, flush=False)
            if self.adapters.get(name) is adapter:
                self._check_doubt(name, adapter, replaced=True)
            return self.store.snapshot().agents[name].proc is not None
        finally:
            self._opening.discard(name)

    def _stop(self, name: str) -> None:
        adapter = self.adapters.get(name)
        if adapter is not None:
            try:
                adapter.stop()
            except Exception as exc:
                self._adapter_failed(name, exc, "stop")
                return
            self._drain(name, adapter, flush=False)
            if self.adapters.get(name) is adapter:
                self.store.set_proc(name, None)
                self._detach(name)
        self._awaiting_turn.discard(name)

    def _detach(self, name: str) -> None:
        adapter = self.adapters.pop(name, None)
        if adapter is not None:
            try:
                adapter.detach()
            except Exception as exc:
                LOG.warning("Could not detach %s: %s", name, C.redact(str(exc)))
        self._awaiting_turn.discard(name)

    def _reconcile_lifecycle(self) -> bool:
        changed = False
        for agent in self.store.snapshot().agents.values():
            if agent.id not in self.adapters or agent.id in self._closing_workers:
                continue
            if agent.stopped_by_user or not agent.is_open and not self.store.snapshot().running_jobs(agent.id):
                self._closing_workers.add(agent.id)
                try:
                    self._stop(agent.id)
                finally:
                    self._closing_workers.discard(agent.id)
                changed = True
        return changed

    def _drain(self, name: str, adapter: C.Adapter, *, catching_up: bool = False, flush: bool = True) -> bool:
        happened = False
        try:
            events = iter(adapter.events())
        except Exception as exc:
            self._adapter_failed(name, exc, "events")
            return True
        while True:
            try:
                ev = next(events)
            except StopIteration:
                break
            except Exception as exc:
                self._adapter_failed(name, exc, "events")
                return True
            if isinstance(ev, C.TurnEnded) and not catching_up and ev.status != C.TurnStatus.STOPPED:
                if not self.store.handled(ev.key):
                    self._check_jobs(name, adapter, ev.key)
            if isinstance(ev, (C.TurnStarted, C.TurnEnded, C.ProcessExited)):
                self._awaiting_turn.discard(name)
            happened = self._event(ev) or happened
            if isinstance(ev, C.SessionChanged) and ev.change == C.SessionChange.COMPACTED:
                self._release_compact(name, ev.key)
            if isinstance(ev, C.ProcessExited):
                agent = self.store.snapshot().agents[name]
                if agent.proc is None and self.adapters.get(name) is adapter:
                    self._detach(name)
                    self._release_compact(name, ev.key)
                    if self.store.snapshot().running_jobs(name):
                        self._event(C.JobsChecked(name, ev.at, ev.key))
            if self.adapters.get(name) is not adapter:
                break
            # Never send into a process while reading its missed turns during startup.
            if flush and not isinstance(ev, C.ProcessExited):
                happened = self._flush_sends() or happened
        return happened

    def _check_jobs(self, name: str, adapter: C.Adapter, cause: str) -> bool:
        if not adapter.caps.job_record:
            return False
        now = self.clock.now()
        self._last_jobs[name] = now
        try:
            jobs = tuple(adapter.jobs())
        except Exception as exc:
            self._adapter_failed(name, exc, f"jobs/{cause}")
            return True
        return self._event(C.JobsChecked(name, now, cause, jobs))

    @staticmethod
    def _error(exc: Exception) -> C.ErrorClass:
        return exc.error if isinstance(exc, AdapterError) else C.ErrorClass.TRANSIENT

    def _adapter_failed(self, name: str, exc: Exception, cause: str) -> None:
        self._event(C.AgentFailed(name, self.clock.now(), cause, self._error(exc), str(exc)))

    def _restarted(self, name: str, intent: C.Intent, replay: bool) -> bool:
        """Remember the old process before stopping it so replay can recognize the replacement."""
        proc = self.store.snapshot().agents[name].proc
        identity = [proc.pid, proc.started, proc.stream] if proc else None
        path = self._effect_path("restart", intent.id)
        if replay and path.exists():
            return identity is not None and identity != json.loads(path.read_text(encoding="utf-8"))
        self._write(path, json.dumps(identity))
        return False

    def _restart(self, name: str, intent: C.Intent, replay: bool) -> None:
        agent = self.store.snapshot().agents[name]
        if not agent.is_open or agent.stopped_by_user or self._restarted(name, intent, replay):
            return
        self._stop(name)
        if self.store.snapshot().agents[name].proc is not None:
            raise AdapterError(f"could not stop {name}'s process")
        if not self._open(name, self._session_choice(agent), agent.session, intent.id):
            raise AdapterError(f"could not reopen {name}'s session")

    def _switch(self, intent: C.Intent, switch: C.Switch, replay: bool) -> bool:
        agent = self.store.snapshot().agents[switch.agent]
        current = agent.switch
        if switch.step != C.SwitchStep.FAILED:
            if current is None or current.step != switch.step or not current.target.same(switch.target):
                return True  # a later step already took effect
            switch = current
        if switch.step == C.SwitchStep.TESTING:
            return self._smoke(intent, agent.id, switch.target)
        if not agent.is_open or agent.stopped_by_user:
            return True
        if switch.step == C.SwitchStep.OPENING and not self._restarted(agent.id, intent, replay):
            self._stop(agent.id)
            if self.store.snapshot().agents[agent.id].proc is None:
                session = switch.new
                choice = session.choice if session else switch.target
                self._open(agent.id, choice, session, intent.id)
        elif switch.step == C.SwitchStep.FAILED and agent.proc is None and switch.old:
            self._open(agent.id, switch.old.choice, switch.old, intent.id)
        return True

    def _refresh(self, intent: C.Intent, action: C.RefreshSession, replay: bool) -> None:
        snap = self.store.snapshot()
        agent = snap.agents[action.agent]
        if not agent.is_open or agent.stopped_by_user or not snap.settled(agent.id):
            return
        adapter = self.adapters.get(agent.id)
        if action.mode == "compact" and agent.proc and adapter and adapter.caps.compacts:
            path = self._effect_path("compact", agent.id)
            compacting = path.exists()
            if not compacting:
                self._write(path, str(self.clock.now()))
            self.store.act(f"{intent.id}/hold", [C.HoldAgent(agent.id, C.HoldReason.TURN)])
            if compacting or adapter.compact():
                return
            self._release_compact(agent.id, intent.id)
        if not self._restarted(agent.id, intent, replay):
            self._stop(agent.id)
            if self.store.snapshot().agents[agent.id].proc is not None:
                return
            if not self._open(agent.id, self._session_choice(agent), None, intent.id):
                return
        ev = C.EngineEvent(agent.id, intent.created, intent.id)
        message = C.Message(C.new_id("engine", ev), recipient=agent.id, text=C.TextRef(
            C.TextKey.SESSION_FRESH, {"state_file": agent.state_file}))
        self._perform(self.store.act(intent.id, [C.Send(message)]))

    def _release_compact(self, name: str, cause: str) -> None:
        path = self._effect_path("compact", name)
        if path.exists():
            self._perform(self.store.act(f"{cause}/compact-release", [C.ReleaseAgent(name, C.HoldReason.TURN)]))
            path.unlink(missing_ok=True)

    def _expire_compacts(self, now: float) -> None:
        snap = self.store.snapshot()
        for agent in snap.agents.values():
            if C.HoldReason.TURN in agent.holds:
                path = self._effect_path("compact", agent.id)
                if path.exists() and now - float(path.read_text(encoding="utf-8")) >= snap.settings.compact_wait:
                    self._release_compact(agent.id, f"compact-timeout/{now}")

    def _smoke(self, intent: C.Intent, name: str, choice: C.Choice) -> bool:
        if intent.id in self._smokes:
            return False
        self._smokes.add(intent.id)
        results, stop = self._smoked, self._smoke_stop
        deadline = time.monotonic() + models.SMOKE_WAIT

        def wait(seconds: float) -> None:
            if stop.wait(seconds) or time.monotonic() >= deadline:
                raise AdapterError("smoke test stopped or timed out")

        def test() -> None:
            try:
                with TemporaryDirectory(prefix="smoke-", dir=self.run_dir) as folder:
                    spec = self._spec(name, choice, None, Path(folder))
                    if spec.choice.backend not in self.factories:
                        raise AdapterError(f"no adapter for {spec.choice.backend}", C.ErrorClass.MISSING_CLI)
                    adapter = self.factories[spec.choice.backend]()
                    result = models.smoke_test(adapter, spec, self.clock, wait)
            except Exception as exc:
                result = C.SmokeResult(name, intent.created, choice=choice, why=str(exc), error=self._error(exc))
            results.put((intent, replace(result, at=intent.created, uid=intent.id)))

        threading.Thread(target=test, name=f"orch-smoke-{name}", daemon=True).start()
        return False

    # The audit (AUD): one auditor process per audit, in a thread of its own; the record comes back through step()

    def _audit(self, snap: C.Snapshot) -> str:
        """Start an audit; "" when started, else why not."""
        from . import audit
        if snap.run.audit_running:
            return "an audit is already running"
        agent = next(iter(snap.by_role(C.Role.AUDITOR)), None) or next(iter(snap.by_role(C.Role.CRITIC)), None)
        if agent is None:
            return "no auditor and no critic to take its settings from"
        choice = agent.choice
        if choice.backend not in self.factories:
            return f"no adapter for {choice.backend}"
        path = self._agent_yaml(agent.id)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        prompt = (texts.PROMPTS / "auditor.md").read_text(encoding="utf-8")
        spec = C.AgentSpec("auditor", C.Role.AUDITOR, models.resolve(choice, self.settings, data or {}),
                           str(self.run_dir / "audits"), prompt, str(self.run_dir))
        messages, _ = self.store.messages(limit=0)
        adapter = self.factories[choice.backend]()
        self.store.configure(run=replace(snap.run, audit_running=True))
        self._audit_rounds, self._audit_final = snap.rounds, snap.finished
        done, stop = self._audited, self._audit_stop
        points = int(self.settings.get("audit_points", audit.POINTS) or audit.POINTS)

        def work() -> None:
            try:
                record = audit.run_audit(self.run_dir, snap, messages, adapter, spec, stop=stop.is_set, points=points)
            except Exception as exc:
                LOG.exception("audit failed")
                record = {"status": "error", "reply": f"{type(exc).__name__}: {exc}"}
            done.put(record)

        (self.run_dir / "audits").mkdir(parents=True, exist_ok=True)
        threading.Thread(target=work, name="orch-audit", daemon=True).start()
        return ""

    def _auto_audit(self) -> None:
        """Every `audit_every` rounds, and once more when the run is finished (0: only when asked)."""
        every = int(self.settings.get("audit_every", 0) or 0)
        snap = self.store.snapshot()
        if every <= 0 or snap.run.audit_running:
            return
        if self._audit_rounds is None:
            from . import audit
            last = max(audit.records(self.run_dir), key=lambda r: int(r.get("rounds_at") or 0), default={})
            self._audit_rounds, self._audit_final = int(last.get("rounds_at") or 0), bool(last.get("finished"))
        new = snap.rounds - self._audit_rounds
        # once when the run finishes; what happens after that (a switch, a chat) waits for a full stretch
        if new >= every or (snap.finished and new > 0 and not self._audit_final):
            why = self._audit(snap)
            if why:
                LOG.info("automatic audit not started: %s", why)
                self._audit_rounds = snap.rounds  # not again before the next stretch

    def _audit_done(self, record: Optional[Dict[str, Any]]) -> None:
        run = self.store.snapshot().run
        if run.audit_running:
            self.store.configure(run=replace(run, audit_running=False))
        if record is not None:
            LOG.info("audit ended: %s", record.get("status"))

    def _open_terminal(self, action: C.OpenTerminal) -> None:
        terminal = action.terminal
        agent = self.store.snapshot().agents[terminal.agent]
        if not terminal.shared:
            self._stop(agent.id)
        if agent.session is None:
            raise ValueError(f"{agent.id} has no saved session")
        choice = agent.session.choice
        adapter = self.adapters.get(agent.id) or self.factories[choice.backend]()
        spec = self._spec(agent.id, choice, agent.session, self.run_dir)
        info = {"command": adapter.terminal_command(spec, agent.session)}
        self._write(self._effect_path("terminal", agent.id), json.dumps(C.to_dict(terminal)))
        self._write(self._effect_path("command", action.command_id), json.dumps(info))

    def _close_terminal(self, intent: C.Intent, name: str) -> None:
        agent = self.store.snapshot().agents[name]
        path = self._effect_path("terminal", name)
        terminal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        event = C.TerminalRead(name, intent.created, intent.id)
        if not self.store.handled(event.key) and agent.session:
            if not terminal.get("shared") and agent.proc is None:
                if not self._open(name, agent.session.choice, agent.session, intent.id):
                    raise AdapterError(f"could not reopen {name}'s session")
            adapter = self.adapters.get(name)
            if adapter and terminal:
                said = adapter.transcript(agent.session, terminal["opened"])
                if terminal.get("shared"):
                    said = [line for line in said if line.by == C.USER]
                event = replace(event, said=tuple(said))
        self._event(event)
        path.unlink(missing_ok=True)

    def _decision(self, intent: C.Intent, action: C.RecordDecision) -> None:
        run = self.store.snapshot().run
        if not run.decisions_path:
            return
        path = Path(run.workspace, run.decisions_path)
        marker = f"<!-- orch:{hashlib.sha256(intent.id.encode()).hexdigest()} -->"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker not in old:
            self._write(path, f"{old}\n{marker}\n- {action.by} ({action.source}): {action.text}\n")

    def _effect_path(self, kind: str, key: str) -> Path:
        return self.run_dir / "engine" / kind / hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temp = Path(f.name)
            try:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
