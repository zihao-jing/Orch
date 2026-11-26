# Live acceptance (S)

One small real project, run on 2026-11-26 with the new runtime, with every CLI the user works with in one team.
I drove the checks by hand through the dashboard API. Fixes found along the way are committed on `rd-int`.

## Setup

- Project: a scratch folder whose goal is `hello.py` with `greet(name)`, unittest tests, and the tests run as a
  background job (`sleep 90 && python3 -m unittest …`).
- Team: manager claude-haiku (Claude Code), critic gpt-6.2-sol (Codex app-server), worker claude-haiku (Claude Code)
  with OpenCode backups deepseek-v4-pro and glm-5.3, monitor qwen3.8-flash (OpenCode), auditor on OpenCode.
- Orch ran as `orch run . --serve`, then again as `orch run . --resume --serve`. Each agent server was Orch's own: the
  user's `opencode serve --service` and Codex daemon were never used or touched.

## Checks

| Check | Result |
|---|---|
| Plan gate | Opened from the manager's plan; approval started milestone 1. |
| Pause gate | Opened and closed; a plan approval waited until the pause was closed, as designed. |
| Claude background job (worker) | Intermediate reply, the job ended (exit 0), the worker woke, its final reply went to the critic and then to the manager's review. |
| Codex job (critic) | `rg --files` ran as a job and was listed until it ended (about 6 minutes, exit 0); the critic woke after. |
| Milestone review and gate | After the fixes, the manager's ACCEPT (4.8 of 5) opened the milestone gate; approving it marked m1 done and the run finished. |
| Question gate | The manager's question opened a gate; the answer reached it in one turn. |
| Restart | Orch stopped with Ctrl-C; the agents' programs kept running and `--resume` reconnected to the same pids and sessions. |
| Model switch | Worker claude-haiku → deepseek-v4-pro on OpenCode: pending, testing, handover, confirming, done; the new model read the handover and checked the work. |
| Terminal | Open returned `claude --resume <the manager's session> …` and closed Orch's copy; close reopened the same session. Talking inside a real terminal was not exercised. |
| Audit | The automatic final audit ran with one auditor process, wrote `audits/<stamp>.json` and `.md` and `audit.md`, kept 3 points and dropped 2 it could not verify; the auditor's server stopped afterwards. |

## Found and fixed

1. The manager added a `<pause>` next to its plan. The protocol now says a plan or milestone waits for approval anyway.
2. A plan approval and the milestone start arrived as two turns. A bare approval that starts a milestone is now said in
   the start message ("I approved the plan.").
3. The manager's answer to "no rubric" was ignored, so its ACCEPT never opened the milestone gate. Answers to "no
   rubric" and "score too low" now count as the review; both name the milestone.
4. Silence checks came after 5 seconds of quiet, so the manager wrote the same idle status five times. The wait is now
   2 minutes per try.
5. The manager offered to stop Orch's own process to "end the run". Its prompt now says the run and its processes are
   not its to start or stop.
6. The audit called notes to the user "(empty)" because they have no body. It now shows their words.
7. The manager sent the checklist path in a second message. The milestone start now says Orch gives the critic the path.
8. A model switch after the run finished started a second full audit. There is now one audit at the finish; after that
   only a full `audit_every` stretch starts another.

Full suite after the fixes: 773 tests, 767 pass, 6 skipped, none fail.

## Not covered, possible follow-ups

- A dashboard command to stop one job: the hung-looking Codex `rg` could only be waited out.
- A model choice in the add-worker dialog.
- Codex: taking back a terminal opened outside Orch; a store helper for in-doubt resends.
- `orch init <name>` without `--project` still writes the old pipeline example config (part of the O docs task).
- Only one CLI combination was run; the other projects' combinations use the same three CLIs in other roles.
