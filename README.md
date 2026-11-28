# Orch

An orchestrator for teams of CLI coding agents (Claude Code, Codex, OpenCode, Qoder, and other shell CLIs).

A project keeps its team in `.orch/`: a **manager**, a **critic**, one or more **workers**, a **monitor**, and an
**auditor**. Orch keeps each agent's CLI session open across turns, delivers messages immediately, routes finished
work through the critic, pauses at gates only where you asked to decide or approve, and records the full run under
`.orch/runs/` with a live web dashboard.

```
              you ◀──── plan & milestone gates, questions, pauses ───┐
               │                                                     │
               ▼ approvals, answers, discussions, direct chat        │
<project>/.orch/agents/                                              │
  manager/ ── milestone instructions ──▶ critic/ ── tasks & review ──▶ worker(s)/
     ▲                                     │  ▲                          │
     └────────── reports, questions ───────┘  └─── replies, questions ───┘
                                              ▲
                                         monitor/ (unhealthy checks)
```

## Roles

Each agent knows only its own upper level (`worker → critic → manager → you`), which it sees as its user:

- **manager**: reads the goal (`goal.md`), proposes the numbered milestone plan, sets each milestone's rubric
  (`<rubric>`; Orch keeps it in the run folder as `rubric-<N>.md` and never shows it to the critic), reviews each
  completed milestone (`<score>`, `<decision>ACCEPT|REVISE|PAUSE</decision>`), maintains the golden board
  (`.orch/golden.md`), and talks to you (`<summary>`, `<question>`, `<pause>`).
- **critic** (`verifier` in code): keeps the steps and checklist of the manager's current instruction in the run
  folder (`checklist-<N>.md`), assigns work to workers (`<message to="worker-1">`, or `to="worker-1, worker-2"` for
  the same text to several), checks finished work, and reports to the manager. It ends a report with `<done/>` when
  the work it was given is finished and checked, and adds `<silent/>` when nothing needs the manager. It never sees
  the golden board: the manager writes the rules that matter into its instructions.
- **worker** (one or more): makes changes in the repository, starts long commands with its CLI's own background
  option, asks the critic in plain text when blocked, and sends its whole reply to the critic.
- **monitor**: watches long-running worker turns and background jobs, reporting only unhealthy checks (`quiet`,
  `off-track`, `stopped`) with evidence (never verdicts, at most 3 lines) to the critic.
- **auditor**: sits outside the team; every `audit_every` rounds (and at the end of a run) it reads a digest of how
  the agents communicate and updates `agents/auditor/audit.md` without judging the task or messaging any agent.

## Install and quick start

Python 3.9+ and PyYAML are required, plus the CLI(s) you configure in `.orch/settings.yaml`.

```bash
cd Orch
pip install -e .                      # or: pip install pyyaml  and use `python3 -m orch`
python3 tests/run.py                  # run the test suite

cd ~/code/my-project
orch init . --project                 # scaffolds .orch/settings.yaml and .orch/agents/*
$EDITOR .orch/agents/manager/goal.md
$EDITOR .orch/settings.yaml
orch validate                         # checks the project and prints the resolved agents
orch run --serve --open               # starts the run and opens http://127.0.0.1:8765
```

Commands default to `./.orch`, so no path is needed when you run them from the project root.

## Project layout and configuration (`.orch/`)

`orch init --project` creates:

```
<project>/.orch/
  settings.yaml                shared model list, retry_waits, fallback_cooldown
  golden.md                    (created during the run) rules, preferences and key decisions kept by the manager
  writing.md                   (optional) this project's own writing rules (papers, tables, …)
  audit-decisions.md           (optional) your notes on audit findings
  runs/                        run state, messages, turns, checklists and rubrics (gitignored)
  agents/
    manager/   agent.yaml  goal.md
    critic/    agent.yaml
    worker/    agent.yaml
    monitor/   agent.yaml
    auditor/   agent.yaml  audit.md (written by the auditor)
```

### `.orch/settings.yaml`

Models are defined once in `.orch/settings.yaml` and referenced by ID in each `agent.yaml`:

```yaml
models:
  opus:
    backend: claude
    backend_options: {model: opus, effort: medium, permission_mode: auto, extra_args: ["--autocompact", "400k"]}
  gpt-6.2-sol:
    backend: codex
    backend_options: {model: gpt-6.2-sol, effort: medium}
  claude-haiku:
    backend: claude
    backend_options: {model: haiku, effort: medium, permission_mode: auto}
  qwen4-max-preview:
    backend: opencode
    backend_options: {model: alibaba-token-plan-cn/qwen4-max-preview}
    env: {DASHSCOPE_API_KEY: "${DASHSCOPE_API_KEY}"}
    retries: 1

retry_waits: [60, 120, 240]     # seconds before each retry on the same model (up to 5 tries)
fallback_cooldown: 18000        # seconds (5 h) on a working backup before returning to the primary model
```

- `models`: each entry accepts `backend`, `backend_options`, `env` (`$VARS` are expanded from your shell), and
  `retries`.
- `retry_waits`: wait durations before retrying a transient API failure on the same model, before trying the
  agent's backups in order. Billing errors and missing CLIs skip straight to a backup that avoids them.
- `fallback_cooldown`: how long an agent stays on a backup that succeeded before automatically returning to its main
  model (when the session can continue).

### `.orch/agents/<name>/agent.yaml`

Each agent file selects its role and models from `settings.yaml`:

```yaml
# .orch/agents/manager/agent.yaml
role: manager
description: Owns the milestones and the rubric; reports to me.
model: opus
backup: codex-astra
goal_file: goal.md          # only the manager reads the goal
max_rounds: 400             # worker replies across the run (safety cap)
approve_plan: true          # wait for my OK on the milestone plan
approve_milestones: true    # and after each milestone the manager accepts
# min_score: 4.5            # the pass score: an ACCEPT below it goes back to the critic (default 4.5)
# golden_board: golden.md   # path to the golden board
# golden_limit: 8000        # character warning threshold for the golden board
```

```yaml
# .orch/agents/critic/agent.yaml
role: critic
description: Turns milestones into instructions and reviews every change.
model: gpt-6.2-sol
backup: claude-haiku, qwen4-max-preview   # backups, tried in order
```

```yaml
# .orch/agents/worker/agent.yaml
role: worker
description: Does the work in the repository.
model: claude-haiku
backup: deepseek-v4-pro, glm-5.3
fresh_session: compact      # each milestone: summarise in place (milestone = new session; never = keep)
```

Keys read from `agents/<name>/agent.yaml`:

- **Every agent**: `role`, `description`, `model`, `backup` (one or more model ids, comma-separated, tried in order), `fresh_session` (`compact`, `milestone`, or
  `never`), `working_dir`, `env`.
- **Manager**: `goal_file`, `approve_plan`, `approve_milestones`, `min_score`, `max_rounds` (a safety cap on worker
  replies delivered to the critic), `golden_board`, `golden_limit`.
- **Auditor**: `audit_every` (rounds between automatic audits, plus one at the end; `0` = only `orch audit`),
  `audit_points` (maximum points added per audit).

**Prompts.** Orch always uses its own built-in prompts for `manager`, `critic`, `monitor`, and `auditor`. If a
project has a hand-edited `system_prompt.md` for one of those roles, Orch moves it to `system_prompt.md.bak`. For a
`worker`, `agents/<worker>/system_prompt.md` (if present) becomes its task text. You steer the project through
`agents/manager/goal.md`, `.orch/golden.md` (maintained by the manager, read by the manager only), and optional
`.orch/writing.md`: your project's own writing rules, added at the end of the manager's, critic's and workers'
system prompts (read again for each new session; HTML comments are left out). `writing:` in the manager's
`agent.yaml` names another file, or `off`.

When an existing project without `.orch/settings.yaml` is started, Orch extracts its agent models into
`.orch/settings.yaml` and keeps the original files until you confirm the migration on the dashboard.

A single YAML file with top-level `models:` and `monitors:` (see `orch init orch.yaml` / `orch/example_config.yaml`)
is also supported for running one or more managed teams from a single config file.

## Running and resuming

```bash
orch run --serve --open               # start a new run (or resume an unfinished one) with the dashboard
orch run --resume --serve             # continue the latest run and reconnect to open agent sessions
orch stop .                           # close Orch; running agents and background jobs keep running
orch stop . --agents                  # close Orch and stop all running agents and their jobs
```

- **Live sessions and immediate delivery.** Orch keeps each agent's CLI open and delivers messages immediately,
  including into a running turn when the CLI supports live steering (Claude Code, Codex app-server, OpenCode) or as
  soon as the turn finishes (Qoder).
- **Background jobs.** Workers start long commands using their CLI's native background option. When a turn ends with
  a job still running, Orch marks the reply *intermediate* (`waiting for job`) and forwards it to the critic; when
  the job finishes, the worker continues and its final reply goes to the critic for review.
- **Milestone review.** When the critic ends a report with `<done/>`, no worker is writing and no message is
  waiting, Orch asks the manager to review the milestone against its rubric (`rubric-<N>.md`). An ACCEPT below the
  pass score (`min_score`, 4.5 by default) is not accepted; the manager writes the critic what to fix. After an
  accepted milestone Orch waits for your approval (except in night mode). After the last milestone is approved, the
  run stays open as `finished` with sessions preserved so you can add further milestones.
- **Clean restart.** Closing Orch (Ctrl-C or `orch stop`; the dashboard has no close button) leaves running agents and background jobs alive.
  `orch run --resume` reconnects to the running processes, catches up on any output written while Orch was closed,
  and continues without duplicating messages.

## The dashboard

Start the dashboard with `orch run --serve [--open]` or inspect saved runs any time with `orch serve [--open]`
(default `http://127.0.0.1:8765`; pass `--host 0.0.0.0` on a trusted network or tunnel via SSH):

- **Status board and messages**: shows each agent's live state (`idle`, `writing`, `waiting for job`, `failed`,
  `closed`) from CLI events, the actual model reported by the CLI, running jobs, last activity, current assignment,
  and waiting messages. Milestone cards are at the top; the picked milestone's details and checklist fold below them.
  Messages appear newest first, one line per agent output (opened, it lists each message that output sent, with its
  recipient and status), filtered by one chip per agent.
- **Chat and discussions**:
  - Chatting with the **manager** or **critic** opens a **discussion**: while it is open the agent talks only with
    you, and what it writes to others is not sent. When you click **Apply**, it writes the others what you agreed;
    you can also end the discussion without applying.
  - Chatting with a **worker** is instant, and the conversation stays between you and the worker.
- **Gates**: plan approval, milestone approval, milestone changes (started with **Change milestones**, showing old
  and new lists side by side), manager questions (with selectable options and free text), pauses, and operational
  gates (opened on repeated silence or model failure). When multiple answer gates are open, Orch collects your
  answers and sends them to the manager together once the last gate closes.
- **Add and close workers**: add, close, reopen, or delete workers on the dashboard. Reopening a worker resumes its
  saved session. Closing or deleting a worker with a running job asks whether to let the job finish or stop it.
- **Model switch**: open the model window for any agent to pick a backend, provider, model, and effort from
  `.orch/settings.yaml`, or switch to one of its backups. Orch smoke-tests the choice in a temporary session first,
  switches when the agent is idle (or forces a switch on request), updates `agent.yaml`, and either continues the
  session (when `backend` and `*_BASE_URL` match) or writes a short handover into a new session.
- **Terminal take-over**: copies `orch terminal <project> <agent>` to open the agent's live session in your terminal
  with its model, effort, and system prompt. Orch holds incoming messages while the terminal is open, shows the
  waiting message count on the dashboard, and provides a take-back button. Taking over the manager or critic opens a
  discussion that you apply on the dashboard when done.
- **Night mode**: when enabled, the manager answers its own questions and plan/milestone gates advance
  automatically, while explicit pauses for user tasks and the round limit still hold.
- **Update button**: checks `origin/main` (or locally pulled commits), runs `orch validate` on your config, and
  restarts Orch in place on the same port while agents and jobs keep running.
- **Audit page**: displays the auditor's findings alongside an editor for your notes in `audit-decisions.md`.

## CLI commands

| Command | Description |
|---|---|
| `orch init [path] [--project] [--force]` | Write an example config file (`orch.yaml`), or `--project` to scaffold `<path>/.orch/` |
| `orch validate [config]` | Check a project folder or config file and print the resolved monitors and agents |
| `orch run [config] [-m MONITOR] [--resume] [--serve] [--port 8765] [--host 127.0.0.1] [--open]` | Run enabled monitors concurrently, optionally serving and opening the dashboard |
| `orch serve [target] [-m MONITOR] [--port 8765] [--host 127.0.0.1] [--open]` | Start the dashboard for saved runs without running them |
| `orch approve [target] [-m MONITOR] [--note TEXT] [--revise TEXT]` | Approve the open plan/milestone gate (with an optional note), or `--revise` to send it back |
| `orch answer <target> <qid> <message> [-m MONITOR]` | Answer an open question gate (e.g. `orch answer . Q1 "Option A"`) |
| `orch status <target> [--latest]` | List runs under an output folder or config (`--latest` shows only the latest per monitor) |
| `orch show <target> [-m MONITOR] [--round N] [--turn ID] [--width 90]` | Print rounds and turns of a run, or `--turn` for one turn's full input, output, and stderr |
| `orch stop <target> [-m MONITOR] [--agents]` | Close Orch for a run (`--agents` also stops the running agents and their jobs) |
| `orch tell <target> <agent> <message> [-m MONITOR]` | Send a critic instruction to a worker; replies return to the critic. Use dashboard Talk for human messages. Pass `-` to read stdin. |
| `orch audit [target] [--monitor MONITOR] [--digest]` | Queue an immediate audit on the running Orch, or `--digest` to print the digest without a model call |
| `orch terminal [target] [agent] [--monitor MONITOR]` | Open an agent's session (default: manager) in your terminal while Orch holds its messages |
| `orch report <target> [-m MONITOR]` | Regenerate static HTML reports (`report.html` and `index.html`) |

## Old runs

Runs recorded before the current runtime can be inspected in `orch serve` and `orch show` in read-only mode. Running
`orch run --resume` on a stopped or completed old run migrates its `state.json` (saving `state.old.json`), preserves
each agent's session IDs and waiting messages, assigns milestone IDs `m1, m2, …`, and reopens completed runs so new
milestones can be added.

## Supported CLIs

- **Claude Code (`claude`)**: runs `claude` with stream-json input/output, continues sessions with
  `--session-id` / `--resume`, steers new messages directly into a running turn, compacts sessions with `/compact`,
  and tracks native background jobs. Effort levels: `low`, `medium`, `high`, `xhigh`, `max`.
- **Codex (`codex`)**: runs `codex app-server` (`thread/start`, `thread/resume`, `turn/start`), streams text and tool
  activity live, steers messages into an active turn with `turn/steer`, compacts with `thread/compact/start`, and
  reads available effort levels via `model/list`.
- **OpenCode (`opencode`)**: runs against a private `opencode serve` per agent with live event streaming, live
  message steering into running turns, session compaction, and `provider/model#variant` effort variants.
- **Qoder (`qoder`)**: runs `qodercli` with stream-json output and `--session-id` / `--resume`. Qoder is always
  invoked with an explicit `--model` (and `--reasoning-effort` when effort is set); messages sent while a turn is
  running are delivered as soon as the turn ends.

Additional adapters available in `settings.yaml`: `qwen` (Qwen Code CLI), `agy` (Antigravity CLI), `command` (wraps
any custom CLI template), and `mock` (deterministic fake for tests).
