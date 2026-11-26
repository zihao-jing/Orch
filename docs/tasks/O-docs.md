# Task O: the docs for the new runtime

For: Gemini. Coordinator: the Claude session that merges branches.

## Tips

1. Work on your own branch: worktree `/Users/fancy/Documents/Orch/.worktrees/o`, branch `rd-o` (made from `rd-int`).
   Change only the files listed under "What to change".
2. Run only the related tests while you work: `python3 tests/run.py test_orch`.
3. Run the full test suite (`python3 tests/run.py`) once, at the end. Expected: every test passes.
4. Only after all items below are done: commit on `rd-o` (message ends with a blank line and
   `Co-Authored-By: Gemini <noreply@google.com>`), never push, and append a short report at the end of this file.

## The point of this task

Orch now has one runtime (`orch/runtime/`). The old one was removed (commit 29a2e04: read its message first,
`git show --stat 29a2e04`). The README and the example files still describe the old one. Make them describe what Orch
does now, briefly and correctly. Remove what is gone; do not describe the removal itself ("no longer", "used to").

## What to read (and only this)

- `docs/redesign.md`: section 1 (what Orch is), 2.x headings and requirement ids you cite, 2.15 (removals).
- `orch/cli.py` `build_parser()`: the commands and options that exist. Document exactly these.
- `orch/config.py` `SETTINGS_KEYS` and `orch/runtime/migrate.py` `build_snapshot()`: the settings the runtime reads.

What the runtime reads from a project (`.orch/`):
- `settings.yaml`: `models` (each: `backend`, `backend_options`, `env`, `retries`), `retry_waits`, `fallback_cooldown`.
- Each `agents/<name>/agent.yaml`: `role`, `description`, `model`, `backup`/`backup2` (model ids from settings.yaml),
  `fresh_session`, `working_dir`, `env`; the manager's also `goal_file`, `approve_plan`, `approve_milestones`,
  `min_score`, `max_rounds` (a safety cap; keep it), and the golden board keys `golden_board`, `golden_limit`.
- Orch's own prompts are always used for manager, critic and monitor; a project copy is moved to `system_prompt.md.bak`.
  A worker's `system_prompt.md` becomes its task text.
Keys outside these lists are accepted but do nothing: remove them from the examples (for instance `checkin_every`,
`milestone_rounds`, `check_session`, `job_wait`, `job_ignore`, `compact_between_steps`, `safety_timeout`,
`check_every`, `timeout`, `retries` in agent.yaml). Keep a key you are unsure about, and list it in the report.

## What to change

1. `README.md`: rewrite for the new runtime. Keep it under about 300 lines. Cover: what Orch does (roles: manager,
   critic, workers, monitor, auditor), install, `orch init --project`, the settings and agent files (as above), running
   (`orch run`, `--resume`, `--serve`), the dashboard (chat, gates, add/close workers, model switch, terminal take-over,
   night mode, Update button), the other commands from `build_parser()`, old runs (readable in `orch serve`, read-only),
   and the supported CLIs (Claude Code, Codex app-server, OpenCode, Qoder; Qoder always gets an explicit model).
   Remove: pipeline and controller modes, `orch inject`, `orch reset-sessions`, `--dry-run`, `--plan`, `--max-rounds`,
   urgent messages, the review file, preference files, prompt copies, the terminal hook, the parallel mode.
2. `orch/example_config.yaml`: one managed example (manager, critic, one worker) with only keys from the list above.
   `python3 -m orch validate orch/example_config.yaml` must pass.
3. `orch/cli.py`: only the string constants `SETTINGS_YAML`, `AUDITOR_YAML`, `MONITOR_YAML` and the agent.yaml texts in
   `init_project()`: drop the keys that do nothing, keep comments short. Do not change any code.
4. `tests/test_orch.py` class `TestProjectFolder` only: update an assertion if a value you removed from the init
   texts was asserted (say which in the report).

Do not change `docs/redesign.md`, `docs/probes/`, `docs/tasks/` (except your report), or the user's notes in `docs/`
(preferences, lessons, development*, writing*, collaboration*, audit-decisions-example).

## Report (append below)

Files changed, keys you removed or kept-unsure, test counts.
