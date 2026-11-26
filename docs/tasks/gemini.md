# Tasks for Gemini: M (config, commands, migration) and K (dashboard page)

Two independent tasks. Do M first, then K. Coordinator: the Claude session that merges branches.

## Tips (both tasks)

1. Work on your own branch: M in worktree `/Users/fancy/Documents/Orch/.worktrees/m`, branch `rd-m`; K in worktree
   `/Users/fancy/Documents/Orch/.worktrees/k`, branch `rd-k`. Change only the files the task names. If something else
   must change (a contract, another module), write it under "Requests" in your report; do not edit it.
2. Run only the related tests while you work (`python3 tests/run.py rt_m` or `rt_k`, plus `config` for M).
3. Run the full test suite (`python3 tests/run.py`) once, at the end of each task.
4. Only after all items of a task are done: commit on its branch (message ends with a blank line and
   `Co-Authored-By: Gemini <noreply@google.com>`), never push, and append the report at the end of this file
   (ids done, test counts, requests, anything unsure).

Python 3.9, standard library and PyYAML only. No live CLI, no network, never touch the user's real projects, global
CLI settings or credentials: tests use temporary folders.

---

## Task M: config, commands, migration

Files you own: `orch/runtime/migrate.py` (new), `orch/config.py`, `orch/cli.py`, `tests/test_rt_m.py` (new). The old
modes (`mode: managed` and the others) must keep working exactly as today: their existing tests must still pass. The old
code is removed later by another package (N), not by you.

Read only:
- `docs/redesign.md` rows MS-14, MOD-3, MOD-4, PRM-2 (lines ~179, 245-248, 308) and section 6 items 5 and 8
  (lines ~664-680).
- `orch/runtime/contracts.py`: the user command classes (`grep -n "(UserCommand)"`, lines ~1364-1555), `class Runtime`,
  `ENTRY_POINTS` and the "Entry points" comment (`grep -n "Entry points"`), `TextKey.NOTE_MIGRATED`.
- `orch/runtime/models.py`: `save_choice`, `with_choice`, `module(settings)` (settings file reading).
- `docs/tasks/L-engine.md` "What to build": the `Engine` you call (another agent writes it at the same time; until it
  is merged, test against a small fake with the same methods).
- `orch/config.py` (`find_project`, `_agent_folder`, `SETTINGS_FILES`) and `orch/cli.py` (`build_parser`, `cmd_run`,
  `cmd_stop`, `cmd_terminal`, `cmd_serve`, `cmd_tell`).

Items:
1. **Choosing the runtime.** A project or config with `runtime: new` (top level of the config, or in
   `.orch/settings.yaml`) runs on the new runtime; anything else runs the old code unchanged. `orch run` then builds
   `Engine(run_dir, clock, adapters, settings)` with one adapter factory per backend from `orch/runtime/adapters/`
   (`claude`, `qoder`, `qwen` -> claude.py; `codex`; `opencode`; `agy`; `command` -> oneshot.py), calls `start()`,
   `run(stop)`, and `close()` on Ctrl-C (agents keep running).
2. **`orch.config.add_worker(project, like, worker)`**: copy `agents/<like>/agent.yaml` to `agents/<worker>/agent.yaml`
   (name changed, nothing else), creating the folder; refuse if the worker exists.
3. **Commands for a new-runtime run.** `orch stop <run>` -> `StopOrch(agents=False)`; `orch stop --agents <run>` ->
   `StopOrch(agents=True)`; `orch tell <run> <agent> "text"` -> `Chat`; `orch approve` / `orch answer` -> `GateAnswer`.
   Always write `<run_dir>/commands/<time>-<id>.json` with `to_dict(command)` (atomic: write a temp file, then rename);
   a running engine reads that folder on every step, a closed one at start. Old runs keep today's behaviour.
4. **`orch terminal <project> <agent>`** for a new run: print or exec the adapter's `terminal_command(spec, session)`
   from the agent's saved session; send `TerminalOpen` first and `TerminalClose` when the command exits.
5. **MOD-4 settings.** A new project (`orch init --project`) gets `.orch/settings.yaml` (the model list) and agent.yaml
   files that refer to it by model id, never with inline backend options. For an existing project without it,
   `migrate.write_settings(project)` writes settings.yaml from the agents' backend/model/effort lines, keeps every old
   file, and records the old files in `.orch/migration.json`; the run shows `NOTE_MIGRATED`; `ConfirmMigration`
   (handled by `migrate.confirm(project)`) then moves the old lines/files to `.orch/backup/<date>/`.
6. **MOD-3.** Model saving uses `models.save_choice`; check with a test that comments and other keys of agent.yaml stay.
7. **PRM-2.** Orch's own prompts are always used: a project's `system_prompt.md` (or `system_prompt:` key) for a
   manager, critic or monitor in a new-runtime run is ignored; if its text differs from Orch's prompt it is moved to
   `agents/<name>/system_prompt.md.bak` once, with a notice. Workers' task text stays.
8. **Old runs (section 6 item 8, MS-14).** `migrate.migrate_run(run_dir)`: a run whose state.json has no `"runtime"`
   key and is not running is converted when started with the new runtime: sessions kept (per agent), waiting messages
   become new `waiting` messages, milestones get ids (`m1`, `m2`, … in order), the old state kept as
   `state.old.json`. Refuse while its process is alive. A finished run can be reopened: it starts with status
   finished and accepts added milestones (`ChangeMilestones`).

Tests (`tests/test_rt_m.py`, each id in a test name or docstring): items 1-8 with temporary projects; old-mode configs
still load (run the existing `config` tests).

---

## Task K: finish the dashboard page

Worktree `.worktrees/k`, branch `rd-k`. An earlier author stopped mid-way: `orch/runtime/page/app.js` has an
uncommitted change (a `flat()` helper) and `tests/test_rt_k.py` is new and uncommitted. Keep both and continue. First:
`git merge rd-int` (live items now carry `"n"`; `/api/live` may answer `"reset": true`).
Files you own: `orch/runtime/page/*`, `tests/test_rt_k.py`.
Read: the page files, the test, `docs/redesign.md` rows UI-1…UI-6 (line ~293), and the "/api/" comment block near the
end of `orch/runtime/contracts.py` (`grep -n "/api/"`). The tests run the page in `node` with a small fake DOM
(`dom.js`, written by the test).

`rt_k` has 7 failing smoke tests. For each, decide whether the page or the fake DOM is wrong and fix that side; never
weaken an assertion that checks real behaviour.

1. `test_UI_1_status_board`: "Last activity: 20833 d ago" for agents with no activity. A missing or 0 time shows "–".
2. `test_UI_3_enter_sends`: `e.preventDefault is not a function`: the fake key event lacks it.
3. `test_UI_3_model_window`: the model window lists the catalog names (e.g. "Alpha") and a "Backups" part.
4. `test_UI_3_pending_switch`: textContent of undefined: find the missing element (page or fake).
5. `test_UI_3_terminal_notice_take_back_and_notices`: notices say "A terminal is open on <display name>".
6. `test_UI_5_UI_2_message_details`: each message has a details button.
7. `test_UI_6_old_runs_are_read_only`: agents show their display name ("Worker 1") as on the live board.
8. Live view: keep the last `n` per agent; after an SSE reconnect fetch `/api/live?agent=&from=<last n + 1>`; on
   `"reset": true` clear that agent's lines and show the returned ones. Add one smoke test.

---

## Report

### Task M (`rd-m`, worktree `.worktrees/m`)

- **IDs & Items Done:**
  - Items 1–8: `runtime: new` selection (top-level config or `.orch/settings.yaml`) building `Engine(run_dir, clock, adapters, settings)` with adapter factories per backend (`claude`, `qoder`, `qwen`, `codex`, `opencode`, `agy`, `command`) and calling `start()`, `run(stop)`, and `close()` on Ctrl-C (`REC-9`).
  - `orch.config.add_worker(project, like, worker)` (`WRK-2`).
  - CLI commands for new-runtime runs (`orch stop`, `orch stop --agents`, `orch tell`, `orch approve`, `orch answer`) atomically writing `<run_dir>/commands/<time>-<id>.json` (`REC-9`, `GATE-3`, `DISC-6`).
  - `orch terminal <project> <agent>` (`TERM-1`) sending `TerminalOpen`, printing/executing `adapter.terminal_command(spec, session)`, and sending `TerminalClose` on exit.
  - `MOD-4`: `orch init --project` creating `.orch/settings.yaml` and `agent.yaml` files referencing model IDs without inline backend options; `migrate.write_settings(project)`, `.orch/migration.json`, `TextKey.NOTE_MIGRATED`, and `migrate.confirm(project)` moving old lines/files to `.orch/backup/<date>/`.
  - `MOD-3`: `models.save_choice` preserving comments and other keys in `agent.yaml`.
  - `PRM-2`: ignoring project `system_prompt.md` / `system_prompt:` for manager, critic, and monitor in new-runtime runs, moving hand-edited prompts to `agents/<name>/system_prompt.md.bak` once with a notice, and keeping workers' task text.
  - `MS-14` & Section 6 item 8: `migrate.migrate_run(run_dir)` converting old stopped/finished runs (`state.old.json`, keeping sessions per agent, converting waiting messages to `waiting`, assigning `m1`, `m2`, … milestone IDs, refusing while process is alive, reopening finished runs with `finished=True` accepting `ChangeMilestones`).
- **Test Counts:**
  - `rt_m` (`tests/test_rt_m.py`): 10 ok, 0 failed, 0 errors.
  - `config`: 28 ok, 0 failed, 0 errors.
  - Full suite on `rd-m`: 1063 tests run — 1053 ok, 5 skipped, 4 failed, 1 error (the 3 pre-existing baseline failures in `test_b9b`, `test_board_background`, and `test_reminders` from section 4.5, plus 2 old `test_orch.TestProjectFolder` assertions noted below).
- **Requests:**
  - In `tests/test_orch.py` (`TestProjectFolder`), two legacy assertions check pre-`MOD-4` `orch init --project` layout (`self.assertFalse(list(d.glob("*.yaml")))` which forbids `.orch/settings.yaml`, and `re.sub(r"(?m)^backend: \w+", "backend: mock", text)` on `agent.yaml` which now uses `model: <id>`). Package N should update or remove `TestProjectFolder` during cutover.
- **Unsure:** None.

### Task K (`rd-k`, worktree `.worktrees/k`)

- **IDs & Items Done:**
  - Merged `rd-int` into `rd-k`.
  - Items 1–8 (`UI-1`, `UI-2`, `UI-3`, `UI-4`, `UI-5`, `UI-6`):
    1. `test_UI_1_status_board`: missing or `0` `last_output` displays `"–"` (`L.ago` and `app.js`), and fixture agents without activity have `last_output = 0.0`.
    2. `test_UI_3_enter_sends`: added `preventDefault() {}` to fake DOM event in `DOM_JS`.
    3. `test_UI_3_model_window`: `L.agentName` resolves default display strings (`"worker-1"`, `"the critic"`, `"the manager"`) to plain display names (`"Worker 1"`, `"Critic"`, `"Manager"`) while keeping custom display names (e.g. `"Ada"`), so `Worker 1`'s model window opens and shows catalog names (`"Alpha"`) and `"Backups"`.
    4. `test_UI_3_pending_switch`: `Worker 1` card is matched by its display name and shows `"switch pending"` and `"Switch now"`.
    5. `test_UI_3_terminal_notice_take_back_and_notices`: open terminal banner displays `"A terminal is open on Critic"`.
    6. `test_UI_5_UI_2_message_details`: every message row has `"Show the text as sent"` and `"Show the whole reply"` detail buttons.
    7. `test_UI_6_old_runs_are_read_only`: old runs and live board show agent display names (`"Worker 1"`, `"Manager"`).
    8. Live view (`UI-1`): tracks last `n` per agent (`liveN`), fetches `/api/live?agent=<id>&from=<last n + 1>` after SSE reconnect, clears agent lines on `"reset": true`, and added smoke test `test_UI_1_live_reconnect_and_reset`.
- **Test Counts:**
  - `rt_k` (`tests/test_rt_k.py`): 56 ok, 0 skipped, 0 failed, 0 errors.
  - Full suite on `rd-k`: 1189 tests run — 1180 ok, 6 skipped, 2 failed, 1 error (only the 3 pre-existing baseline failures from section 4.5).
- **Requests:** None.
- **Unsure:** None.

