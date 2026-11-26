# Task L: the engine (`orch/runtime/engine.py`)

For: one coding agent (Codex). Coordinator: the Claude session that merges branches.

## Tips

1. Work on your own branch: worktree `/Users/fancy/Documents/Orch/.worktrees/l`, branch `rd-l` (made from `rd-int`).
   Change only `orch/runtime/engine.py` and `tests/test_rt_l*.py`. If something elsewhere must change (a contract, a
   module), stop and write it under "Requests" in your report; do not edit other files.
2. Run only the related tests while you work: `python3 tests/run.py rt_l` (and `rt_b` if you rely on store behaviour).
3. Run the full test suite (`python3 tests/run.py`) once, at the end.
4. Only after all items below are done: commit on `rd-l` (message ends with a blank line and
   `Co-Authored-By: Codex <noreply@openai.com>`), never push, and write the report (at the end of this file).

Python 3.9, standard library and PyYAML only. No live CLI, no network: tests use `orch/runtime/adapters/mock.py`
(`MockCLI`, `FakeClock`) and `tests/rt_helpers.py`.

## What to read (and only this)

- `orch/runtime/contracts.py`: the header (lines 1-75: how one event is handled, ids, holds); `MODULE_ORDER`,
  `ENTRY_POINTS`; `class Intent`, `Clock`, `Module`, `AgentSpec`, `Adapter`, `Runtime`; the event classes
  (`grep -n "^class .*Event)\|^class .*(AgentEvent)\|^class .*(EngineEvent)"`); and the "Engine:" line in each action's
  docstring (`grep -n "Engine:"`): that line is exactly what you perform for the action.
- `orch/runtime/store.py`: the public methods of `class Store` (line ~858 on): `create`, `begin`, `apply`, `act`,
  `unfinished`, `intent_done`, `to_send`, `can_send`, `begin_send`, `in_doubt`, `not_arrived`, `set_proc`, `snapshot`,
  `add_agent`, `set_agent`, `configure`, `close`, `message`, `messages`.
- `orch/runtime/adapters/mock.py` and `tests/rt_helpers.py`: how tests script a CLI.
- `docs/redesign.md`: 3.1-3.5 (lines 347-441) and the rows MSG-1, MSG-2, JOB-4, REC-7, REC-9.
- Each logic module exposes `module()` (models: `module(settings)`) returning an object with `name` and
  `decide(snap, event, prior)`. Texts: `texts.render(snap, message)`, `texts.words(ref, snap)`,
  `texts.system_prompt(snap, agent)`. Do not read their insides unless a test fails on them.

## What to build

`class Engine` (also usable as the `Runtime` protocol for the dashboard):

```python
Engine(run_dir, clock, adapters: Dict[str, Callable[[], Adapter]], settings: Dict, modules=None)
#   adapters: backend name -> factory of a fresh adapter. modules: default = one per MODULE_ORDER name.
engine.start(snap_or_none)   # open or load the store, recover (item 5), emit OrchStarted
engine.step() -> bool         # one pass of the loop (items 2-4); True if anything happened. Tests call this.
engine.run(stop_event)        # loop: step(); when idle, wait ~0.2 s; a Tick every few seconds (settings tick_s, default 5)
engine.close()                # detach every adapter (REC-9: CLIs and jobs keep running), close the store
engine.stop_agents()          # the "stop agents" button: adapter.stop() for each agent
engine.state() / submit(command) / message(id) / messages(...)   # the Runtime protocol
```

1. **One event, exactly as the contracts header says.** For each event:
   `ev = store.begin(event)`; if None (already handled) skip. `snap = store.snapshot()`. `prior = []`; for each module
   in MODULE_ORDER: `acts = m.decide(snap, ev, tuple(prior))`; check each with the action's `may_emit(m.name)` (raise
   in tests, log and drop in production); `prior += acts`. Then `intents = store.apply(ev, prior)` (one save). Then
   perform the intents (item 3). Never decide with a snapshot taken after some actions were performed.
2. **Event sources, in this order per step:** first any unfinished intents from a crash (item 5); then user commands
   (queue filled by `submit` and by files in `<run_dir>/commands/*.json`, oldest first, file deleted after handling);
   then for every agent `adapter.events()` until empty (it never blocks); then a `Tick` when `tick_s` passed since the
   last one. Events of one agent keep their order. An adapter that raises gives `AgentFailed` (see its class).
3. **Performing intents.** First every intent that is not a transmission, then the transmissions (`Send` of a message
   that `store.can_send` allows). For a transmission: `r = texts.render(snap, msg)`;
   `store.begin_send(id, r.text, r.given)` (saved before sending); then `adapter.send(id, r.text)`; on an exception
   emit `SendFailed(message_id, error class)` as a new event. If the adapter's `caps.receipts` is False, emit `Received` yourself when
   `send` returns. Copy `adapter.caps` to `Agent.caps` when an agent's adapter is made. Mark every intent with `store.intent_done(id, error)`.
   Actions that start, stop, restart or switch an agent use the adapter factory and `store.set_proc`; the system
   prompt comes from `texts.system_prompt`. Messages that wait (holds, closed agent, busy CLI without
   caps.sends_while_writing) are not sent; `store.to_send()` gives what may go now, check it after every event (MSG-1, 2).
4. **Engine events.** `OrchStarted` once after recovery; `Reconnected` per agent after a successful reconnect;
   `JobsChecked` from `adapter.jobs()` every `jobs_check_s` (default 60) per agent with a running job;
   `SmokeResult` from `models.smoke_test` when an action asks for one (run it in a thread; feed the result back as an
   event, never block the loop). Use `contracts.new_id`-style keys so a replay gives the same key.
5. **Start and recovery (REC-7, 8, MSG-6).** On start: load the store; perform `store.unfinished()` intents again,
   but for a transmission first call `adapter.arrived(id)`: True -> mark done; False -> `store.not_arrived(id)` and send
   again; None -> send again only if the action says so (read its Engine: line). For each agent with a saved ProcRef:
   `adapter.reconnect(proc)`; True -> `Reconnected`; False -> drain `events()` (it ends with ProcessExited), then let
   the modules decide (recovery restarts it). Transmit nothing before the actions of OrchStarted are done.
6. **JOB-4.** Nothing special in the engine: a job end arrives as events from the adapter (wake-up turn or JobEnded);
   the supervisor and routing decide. Test it end to end with MockCLI.job_end(wake=...) and without wake (Codex, Qwen
   caps), where the supervisor's wakeup message must go out.
7. **REC-9.** `close()` calls `detach()` on every adapter, never `stop()`; after a new `Engine.start()` on the same
   run_dir the agents reconnect and nothing is sent twice.

## Tests (`tests/test_rt_l.py`, each requirement id in a test name or docstring)

- MSG-1: a message to an agent that is writing goes out at once when caps.sends_while_writing; with
  `sends_while_writing=False` it goes out right after TurnEnded.
- MSG-2: a held message is not sent; released later, it goes out once.
- MSG-6 / REC-8: crash after `begin_send` but before the intent is marked done (simulate by building a new Engine on the
  same run_dir): arrived True -> not sent again; False -> sent once more; never twice in the mock's received list.
- A crash after `store.apply` but before performing: the intents run on restart; a crash before `apply`: the event is
  decided again with the same ids (no duplicates).
- REC-9: close and start again; MockCLI keeps running; nothing re-sent; Reconnected emitted.
- JOB-4: both variants of item 6.
- SendFailed: the mock refuses a send; the message ends up retried or failed as the modules decide, not lost.
- Commands: a file in `commands/` is handled once, then deleted; `submit` returns the CommandReply json.
- Order: the module order and `prior` content are as in MODULE_ORDER (use a fake module list).

## Report (append below)

Ids done, test results (rt_l and full suite counts), requests for contract or module changes, anything unsure.

- Implemented the event loop, durable intent replay, command replies, process lifecycle, job polling, and background smoke tests. The mock integration coverage includes MSG-1, MSG-2, MSG-5, MSG-6, REC-7, REC-8, REC-9, and JOB-4. It also covers crashes around `Store.apply`, send failures, command files, and module order.
- The Task L selection (`python3 tests/run.py test_rt_l.`) passed all 13 tests. The store selection passed all 59 tests in the combined store and engine run. The literal `rt_l` filter also selected unrelated adapter cases, with failures in process reuse and sandboxed Unix socket binding in the initial run.

- The full suite discovered 1,063 tests. The run showed unrelated failures and errors, including denied process inspection and local socket binding. A legacy waiter-loop test stalled until its temporary shell was stopped. Other workers remained stalled, so the runner was interrupted and did not print final pass, fail, or skip counts.
- Implementation and tests are in `4b8b0c7` on `rd-l`. This report was appended after the commit.

### Requests

- Provide `orch.config.add_worker(project, like, worker)` as declared in the contracts. The engine calls it when present. This checkout has no helper to copy the source worker's configuration.
- Expose terminal process ownership and shutdown through the adapter contract so take-back can terminate an external terminal. The engine can already stop Orch's own CLI, generate the launch command, read the transcript, and reopen the saved session.
- Specify the update and audit execution hooks for `UpdateOrch` and `AuditRun`. The command contracts are placeholders and the engine returns unsuccessful replies for those commands.
- Let the supervisor decide wakeup from job-end or job-check events if JOB-4 must act before the next scheduled tick. The current supervisor schedules fallback on Tick.
- Add a Store operation to reset an in-doubt transmission while preserving `checked_at`. The engine currently reapplies the recorded check through `Store.act`.
