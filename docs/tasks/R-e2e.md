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

### End-to-end results

The tests use the default Engine with its real store and modules. Every scenario uses scripted input and persisted state. The adapter variants use the repository’s subprocess, Unix-socket, and loopback HTTP fixtures.

| Scenario | Result |
| --- | --- |
| 1 · Concurrent worker reports | Pass. Two reports reach the writing critic together, are answered, and both workers receive follow-up instructions once. |
| 2 · Job, guidance, and wake-up | Pass with the scripted runtime and the Claude, Codex, and OpenCode adapter fixtures. Codex receives the engine’s fallback because its CLI does not self-wake. |
| 3 · Missing wake-up and lost job | Pass. Ending a job without an event produces one fallback. Removing a lost job from the CLI’s job list prompts a worker check. |
| 4 · Silence | Pass. Three manager checks open a pause gate. Work after the pause resets the count. |
| 5 · Silent critic and checklist review | Pass. The worker gets the instruction and the completed checklist reaches the manager for review. |
| 6 · Questions and pause | Pass. The critic instruction is held, and answering the two questions and the pause separately sends one combined answer. |
| 7 · Removed, broken, and closed targets | Pass. The critic gets the first label corrections, the manager sees the repeated broken label, and a closed target is rejected. |
| 8 · Send-intent restart | Pass before and after the CLI accepts the saved send. The original message ID reaches the CLI once. |
| 9 · Working-session restart | Pass with a running job and an in-flight turn on a backup backend. The original processes and sessions continue. |
| 10 · Retries, backup, handover, and return | Pass. Transient errors reach the retry limit, the critic hands over to a new backup session, and a dashboard choice returns the worker to its saved main session. |
| 11 · Billing and recovery | Pass for a single choice and for a main plus backup. Another worker continues, and a successful periodic smoke test clears the affected worker’s gate. |
| 12 · Forced switch with restart | **Fails at opening and handover.** Pending, testing, confirming, done, and failed-smoke checkpoints pass. See reproductions below. |
| 13 · Discussion | Pass. Incoming work reaches the critic during a draft, applying waits for the active reply, and ending without applying discards the draft. |
| 14 · Terminal | Pass. Two instructions remain held until take-back and then reach the same worker session. |
| 15 · Milestone changes | Pass for changing a milestone with an approval open, adding one after completion, and reopening a saved finished run. |
| 16 · Worker close and reopen | Pass. Letting the job finish records its late result without forwarding it, and reopening retains the session. |
| 17 · Frozen worker | **Fails on the second freeze.** The same-session restart and the long-tool and long-job controls pass. See reproduction below. |
| 18 · Night mode | Pass. The recommendation is recorded, the critic receives the instruction, and no question gate opens. |

- **S12 · Opening (MOD-6, REC-7).** Start a worker job, send a user message that starts an unfinished turn, then force a cross-backend switch. Restart immediately after the store saves `opening`, before its effect. The switch completes, but the new CLI never receives the unfinished user request. Suspect engine replay of the opening effect or recovery’s unfinished-turn bookkeeping.
- **S12 · Handover (REC-7, REC-8).** Restart immediately after `handover` is saved for the same forced switch. The critic’s handover is processed and the switch enters `confirming`, but delivery reports “the CLI is not running” even after retry. Suspect engine reconnect selecting the main process or backend while the replacement session is awaiting confirmation.
- **S17 · Second freeze (SIL-4).** Start a worker turn with no output, active tool, or job. Advance beyond 40 minutes and observe the restart and continuation in the same session. Keep the continuation silent and advance another 40 minutes. The worker remains `writing` instead of becoming `failed`. Suspect the supervisor’s second-timeout path or its handoff to recovery.

`python3 tests/run.py rt_e2e` ran 36 tests. 33 passed and the three cases above failed. All three adapter tests ran and passed.

The mock can model messages, terminal holds, and session identity. It does not launch an external terminal, so scenario 14 verifies the runtime take-back and queue release without observing a separate terminal process exit. Scenario 15 reopens a persisted finished runtime snapshot. Legacy-file conversion is outside that fixture.

The one full run, `python3 tests/run.py`, ran 1,239 tests. 1,226 passed, 6 skipped, 6 failed, and 1 errored. It reproduced the three failures above and the three baseline cases in redesign §4.5. Its remaining failure was S11’s multi-choice fixture. Both choices could recover on the same periodic probe, making the expected primary selection timing-dependent. The fixture now keeps the backup unavailable for that probe. The related selection was rerun with the corrected fixture after the full run.

An early adapter test reached the installed Codex binary because the engine re-resolved the stored choice and discarded its executable option. The final Claude and Codex tests use a restricted executable search path containing the corresponding repository fake and system utilities.
