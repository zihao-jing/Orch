# Task R: end-to-end tests of the new runtime

For: one coding agent (Codex). Coordinator: the Claude session that merges branches.

## Tips

1. Work on your own branch: worktree `/Users/fancy/Documents/Orch/.worktrees/r`, branch `rd-r` (made from `rd-int`).
   Create and change only `tests/test_rt_e2e_*.py` (and, if you need shared helpers, `tests/rt_e2e_helpers.py`).
   Do not change any file under `orch/`.
2. Run only the related tests while you work: `python3 tests/run.py rt_e2e`.
3. Run the full test suite (`python3 tests/run.py`) once, at the end.
4. Only after all items below are done: commit on `rd-r` (message ends with a blank line and
   `Co-Authored-By: Codex <noreply@openai.com>`), never push, and append the report at the end of this file.

Python 3.9, standard library and PyYAML only. No live CLI, no network.

## The point of this task

Every module has unit tests on hand-made snapshots. Nobody has yet run the modules TOGETHER through the real engine.
Your tests do that: the real `Engine` (`orch/runtime/engine.py`), the real store and all real modules, with scripted
CLIs (`MockCLI` / `MockAdapter` from `orch/runtime/adapters/mock.py`) and a `FakeClock`. You find where the pieces do
not fit. **When a test fails because the runtime is wrong, do not fix the runtime and do not weaken the test**: leave it
failing, and list it in the report with the smallest reproduction and your guess at the module at fault. Fix a test
only when the test itself is wrong.

## What to read (and only this)

- `docs/redesign.md` section 4.4 (the 18 scenarios, line ~607) and, for each scenario, the requirement rows it names
  (grep the id, e.g. `grep -n "SIL-4" docs/redesign.md`).
- `tests/test_rt_l.py`: how it builds an `Engine` on a temp run dir with `MockCLI`s and steps it; reuse that pattern.
- `orch/runtime/adapters/mock.py`: `MockCLI.script`, `emit`, `job_start`, `job_end(wake=...)`, `exit`, `refuse_sends`,
  `lose_session`, `duplicate`; `FakeClock.advance`.
- `tests/rt_helpers.py`: `snapshot`, `agent`, `milestone`, builders for the starting state.
- `orch/runtime/contracts.py` only for the classes you touch (`grep -n "^class <Name>"`); the user commands
  (`grep -n "(UserCommand)"`) to drive the dashboard side through `engine.submit(...)`.
- How replies are labelled: `orch/runtime/prompts/protocol/*.md` (the tags a manager, critic or worker writes).

## What to build

One test (or a few) per scenario 1-18 of 4.4, in files grouped by theme, for example:
`test_rt_e2e_messages.py` (1, 2, 5, 6, 7), `test_rt_e2e_jobs.py` (2, 3, 16, 17), `test_rt_e2e_silence.py` (4, 18),
`test_rt_e2e_restart.py` (8, 9, 12), `test_rt_e2e_models.py` (10, 11, 12), `test_rt_e2e_discussion.py` (13, 14),
`test_rt_e2e_milestones.py` (15).

Each test:
- starts from a small snapshot (manager, critic, one or two workers, one current milestone), the scripted replies, and
  steps the engine with the fake clock until the state settles (bounded loop, e.g. at most 200 steps);
- asserts on what the CLIs RECEIVED (each mock's received list: who got which message, how many times) and on the
  final state (message statuses, gates, holds, agent status), never on internal action lists;
- for the restart scenarios (8, 9, 12): close the engine (or simulate a crash by dropping the object without close,
  after the store saved), build a new `Engine` on the same run dir, `start(None)`, continue; assert nothing was sent
  twice and nothing was lost;
- has the scenario number and the requirement ids in its name or docstring (`test_S08_MSG_6_...`).

Then, for the real adapters, one scenario each (scenario 2: a job, a message, the wake-up reply) through the engine
using the fakes their own tests already have: Claude (`tests/test_rt_x1.py`), Codex (`tests/test_rt_x2_fake.py`),
OpenCode (`tests/test_rt_x3.py`). If one of those fakes cannot be reused without changing its file, skip that adapter
and say so.

## Report (append below)

For each scenario: passing, or failing with reproduction and suspected module. Test counts (rt_e2e and the full suite).
Anything in 4.4 that you could not express with the mock (say what the mock lacks).
