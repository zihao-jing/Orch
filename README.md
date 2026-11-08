# Orch

A general-purpose orchestrator for teams of CLI coding agents (Claude Code, Codex, or anything you can call from a shell).

One config file declares any number of **monitors**. Each monitor is independent: it has its own workspace and working directory, an overall goal, a **controller** with instructions, and a roster of **agents** (workers, verifiers, or any custom role), each with its own system prompt and backend. The controller starts the agents in the workspace, **keeps each agent's session alive across turns**, routes tasks and feedback between them, and records every round and turn to an **output directory** for humans to inspect.

```
           ┌──────────── monitor "api-feature" (thread) ─────────────┐
config ──▶ │ controller ──task──▶ implementer ─┐                     │ ──▶ orch-runs/api-feature/<run>/
           │     ▲                docs ────────┼─reports─▶ reviewer  │       state.json  events.jsonl
           │     └──────────── feedback (FAIL) ◀───────────┘         │       progress.md report.html
           └──────────────────────────────────────────────────────────┘       rounds/r001/t01-implementer.{json,input.md,output.md}
           ┌──────────── monitor "research-notes" (thread) ──────────┐
           │ LLM controller ──JSON directive──▶ reader / writer / critic … │
           └──────────────────────────────────────────────────────────┘
```

## Quick start

```bash
cd Orch
pip install -e .            # or: pip install pyyaml  and use `python -m orch`
python -m unittest discover tests                      # 17 tests, mock backend, no API calls
orch run examples/mock-demo.yaml                       # dry demo, then open examples/demo-runs/index.html

orch init my.yaml           # annotated example config
orch validate my.yaml       # resolves paths, checks roles/routing, prints the plan
orch run my.yaml            # runs all enabled monitors concurrently
orch run my.yaml --dry-run  # same flow with every agent replaced by the mock
```

Python 3.9+ is required. PyYAML is only needed for YAML configs; JSON works with the standard library alone, and TOML works on Python 3.11+. To use the `claude` backend, the `claude` CLI must be installed and logged in.

## Setting up a new project

All settings go in one YAML file. Run `orch init my-project.yaml` to get an annotated starting point, edit it, then run `orch validate my-project.yaml` and `orch run my-project.yaml`.

| You want to set… | Where in the YAML |
|---|---|
| **Where results are recorded** | top-level `output_dir:` (relative to the YAML file), or a per-monitor `output_dir:` override |
| Which project the agents work on | `monitors[].workspace` (project root) and `monitors[].working_dir` (agents' cwd, relative to workspace) |
| **The shared goal every agent sees** | `monitors[].goal` (or `goal_file: goal.md`) |
| Rules for the whole team | `monitors[].controller.instructions` — included in every worker's/verifier's first prompt and the LLM controller's |
| **Each agent's own prompt** | `agents[].system_prompt` (persona/rules, or `system_prompt_file:`), `agents[].task` (its round-1 job), `agents[].description` |
| The LLM controller's own prompt | `controller.agent.system_prompt` (added after the built-in controller protocol) |
| **Model and effort, for everyone** | top-level `defaults.backend_options.model` / `.effort` |
| **Model and effort, per agent** | `agents[].backend_options: {model: opus, effort: high}` — overrides the defaults for that agent only |
| Model and effort for the LLM controller | `controller.agent.backend_options` |
| Tool permissions | `backend_options.permission_mode` and `allowed_tools` (defaults or per agent) |
| The exact wording of routed prompts | `controller.templates` (see `orch/templates.py` for the names and `{{placeholders}}`) |

Settings merge in this order, with later ones winning: top-level `defaults:` → the monitor's `defaults:` → the agent's own keys. `backend_options` dictionaries are merged key by key, so an agent that sets only `effort` still inherits `model` from the defaults.

With the `claude` backend, `model` is any value `claude --model` accepts (`haiku`, `sonnet`, `opus`, or a full model ID), and `effort` is `low`, `medium`, `high`, `xhigh` or `max`.

A minimal new project:

```yaml
output_dir: ~/orch-logs/my-app            # where every round/turn is recorded

defaults:
  backend: claude
  backend_options: { model: sonnet, effort: medium, permission_mode: acceptEdits,
                     allowed_tools: [Read, Edit, Write, Glob, Grep, Bash] }

monitors:
  - name: my-app
    workspace: ~/code/my-app
    goal: Add CSV export to the reports page, with tests.
    max_rounds: 4
    controller:
      mode: pipeline
      instructions: Follow the existing code style. Run `npm test` before reporting.
    agents:
      - name: coder
        role: worker
        system_prompt: You are a senior TypeScript engineer.
        task: Implement CSV export and its tests.
        backend_options: { model: opus, effort: high }
      - name: reviewer
        role: verifier
        system_prompt: You are a strict reviewer. Run the tests yourself. Never edit files.
        backend_options: { effort: low, allowed_tools: [Read, Glob, Grep, Bash] }
```

## Controller modes

| | `pipeline` (deterministic) | `llm` (an agent decides) |
|---|---|---|
| Round 1 | every worker gets `goal + instructions + its task` | controller gets goal, instructions, roster |
| Each round | workers work → every verifier inspects the workspace → `VERDICT: PASS/FAIL` | controller replies with JSON `{"dispatch":[{"agent","message"}], "done", "parallel"}` → those turns run → results go back to the controller |
| Routing | FAIL feedback goes to `feedback_to` (default: the workers the verifier `reviews`, default: all). Workers with no new feedback skip the round. | Decided by the controller. Invalid JSON or unknown agents get one automatic repair turn. |
| Done when | verifiers pass (`require: all` or `any`) | controller sets `done: true` (and writes `final_report`) |
| Stops at | `max_rounds` | `max_rounds` |

Every agent, including the LLM controller, keeps **one session for the whole run**. Later turns resume it (`claude --resume <id>`), so agents remember earlier work and feedback.

### Project folders (`.orch/`)

A project keeps its agents in `<project>/.orch/agents/`, one folder per agent; there is no separate project file
(`orch init <root> --project` scaffolds a manager, a critic and a worker):

```
.orch/
  agents/<name>/
    agent.yaml       role (manager | critic | worker), display_name, backend, backend_options, timeout …
                     plus the run settings that belong to this agent, e.g.
                       manager: goal_file, max_rounds, approve_plan, approve_milestones, min_score, protocol, tag_reminder
                       critic:  checklist, checklist_nudge, milestone_rounds, checkin_every, job_wait, job_ignore
                       worker:  snapshot, fresh_session
    goal.md          manager: the goal (only the manager reads it)
    memory.md, memory/*.md   this project's notes and preferences, added after the system prompt
                     ("Things to keep in mind:"; HTML comments are ignored)
    system_prompt.md this agent's prompt in this project: starts as a copy of the role's default
                     (orch/prompts/<role>.md) and can be adapted freely
  runs/              run logs (gitignored)
```

Each role has a generic default prompt in orch (`orch/prompts/manager.md`, `critic.md`, `worker.md`);
`orch init --project` copies it into every agent folder, where the project can adapt it (if the file is missing,
the default is used). `{{manager}}`, `{{critic}}` and `{{worker}}` are filled in from the display names. Prompt and memory files are re-read before every turn, so edits reach a running run (Codex agents read
them once, at the start of their session). Workers get no task file: their instructions come from the critic.
`orch validate` shows which prompt and memory each agent uses; a setting given by two agents with different
values is an error. Commands default to `./.orch`.

### `managed` mode: me ↔ manager ↔ critic ↔ worker

Three layers of review, so each layer above reads less. Agents: exactly one `role: manager`, one `role: verifier` (the critic), one or more `role: worker`. Give them `display_name`s; relayed messages use them ("Alex wrote: …"), and no prompt mentions a workflow.

| step | who | what |
|---|---|---|
| plan | manager | splits the goal into a numbered milestone list + a scored rubric → **I approve** (`approve_plan`) |
| kickoff | manager → critic | a high-level note for the milestone |
| brief | critic → worker | concrete instructions; the critic starts its own checklist |
| rounds | worker ↔ critic | work, then review with `<checklist>` + `<verdict>`, up to `milestone_rounds` |
| report | critic → manager | milestone report (tables first) |
| review | manager | scores per criterion + `<score>` + `<decision>ACCEPT/REVISE</decision>` (`min_score` optional); REVISE → guidance to the critic |
| gate | me | the manager's summary → **I approve** or send it back (`approve_milestones`) |

Questions go up one level at a time and never stop the run: a worker's `<question>` is answered by the critic; a critic's is answered by the manager straight away; a manager's is labelled (Q1, Q2 …) for me while the asker continues with a stated assumption. My answer is relayed to the manager and the critic on their next turn. A `<message to="…">` in a manager reply (also in my chat with it) is passed to that agent.

**The protocol.** In managed mode orch adds a fixed section, *How this team works*, to every agent's system prompt
(`orch/prompts/protocol/<role>.md`), after the project's own prompt and before its memory notes. The role prompts
describe the job and the working style; the protocol is what the framework itself depends on, so it stays the same
however a project edits its prompts (`protocol: false` turns it off). Signals are XML-style tags with a start and an
end, so orch never guesses where a block stops (`orch/tags.py`); tags quoted in `code` are ignored, and a tag left
open runs to the next tag or the end of the reply. Text outside the tags is for whoever the agent is replying to:
the critic's review goes to the worker, the manager's text to me.

| tag | written by | what orch does |
|---|---|---|
| `<message to="…">…</message>` | anyone | delivers the block to that agent only (the manager's instructions and answers to the critic, the critic's notes to the manager) |
| `<question>…</question>` | worker → critic, critic → manager, manager → me | one decision per tag; recorded (Q1, Q2 …) and passed up one level; the run does not wait |
| `<pause>…</pause>` | critic (a proposal) / manager (a pause) | the critic's goes to the manager; the manager's stops the whole run until I continue it |
| `<checklist>…</checklist>` + `<verdict>PASS\|FAIL</verdict>` | critic | a PASS counts only when every item is `[x]` |
| `<score>x</score>` + `<decision>ACCEPT\|REVISE</decision>` | manager, milestone review | accepts the milestone or sends it back |
| `<decision>PAUSE\|CONTINUE</decision>` | manager, on a proposed pause | stops the run, or sends the critic what to do meanwhile |
| `<no-wait pid="…"/>` | worker | the review does not wait for that process (orch finds the worker's detached jobs itself) |

The boundaries: the manager doesn't edit files and keeps milestones, rubric and scores to itself; the critic only
reviews (no edits, no fixes written for the worker, no wider scope); the worker changes only what the work needs,
commits nothing unless asked, writes no notes or status files, caps and pilots anything that spends, never starts
a job twice and never keeps its turn open to wait (orch waits for its jobs between turns).

**Tag reminders.** With probability `tag_reminder` (default 0.2) a turn's message ends with a short
`<system-reminder>` listing that role's tags, worded as a reminder rather than a request, so an agent deep in a long
session keeps the protocol in view without being pushed to add tags it doesn't need. The board marks those turns.

**Fresh sessions mid-run.** `orch reset-sessions <project> [--agent NAME]` (with the run stopped) clears the agents'
conversations and keeps everything else: the phase, milestone, rounds, questions, decisions and records. Each agent
starts a new session on its next turn with a handover: the manager gets the goal, its approved plan and rubric, the
milestone table and the critic's latest report; the critic gets its current instruction and its latest review with
the checklist; the worker its latest report; all get the decisions register and anything the run is paused on. The
board chat follows the new session.

**Decisions register.** Everything settled during the run is kept in one list, `decisions.md` in the run folder: my
answers and notes, messages I add at a gate, and the manager's answers to the critic, each with who decided and
when (S1, S2 …; where two say different things, the later one holds). The manager's kickoff, the critic's brief
and a fresh worker session get the whole list at the start of every milestone, so decisions don't have to survive
a chain of relayed notes. The board shows it under **Decisions**. A run started before the register existed has it
rebuilt from its history on resume.

**Fresh worker sessions.** `fresh_session: milestone` on an agent starts it in a new session when each milestone
starts; its work is in the files, and the critic's brief plus the register tell it what it needs. A long session
re-reads its whole history on every step, so this keeps worker turns from getting slower and dearer as a run goes
on. The manager and critic are best left in one session: they are cheap, and they need to remember the decisions.

**Pausing the whole run.** A pause stops the pipeline itself: no agent works and no rounds are spent until I
continue it. It is for the point where everything the agents can do is done and the rest waits on people
(labelling, a dry run). The critic proposes one with `<pause>…</pause>`; the manager decides with
`<decision>PAUSE</decision>` or `<decision>CONTINUE</decision>` (then with a message for the critic on what to do
meanwhile). The manager can also call one itself, in any reply or in the board chat, with its own `<pause>`; it
takes effect before the next round. While paused, the board shows what I need to do under *Needs you*, with a
**Done, carry on** button and a message box; the message goes into the decisions register and to the critic, which
then tells the worker what to do next.

**Waiting for the worker's own jobs.** orch finds them itself: a process that started during the worker's turn, is
still running after the turn ends, runs inside the workspace and was detached from the agent (`nohup … &`, so its
parent is the system's init process) is a job; processes of mine, an editor or a terminal never count. While a job
runs, the critic's review is held (no review spent on unfinished work, and none of it counts against the worker's
turn time limit); when it exits, the worker gets the turn back to finish, and the critic then reviews both replies
together. `job_wait` (seconds, default 21600; 0 = off) caps the wait, after which the worker is asked for progress;
`job_ignore` (regexes on the command line) and a worker's `<no-wait pid="…"/>` exclude long-lived helpers.
**Review now** on the board skips the wait.

**The board** — `orch run cfg.yaml --serve [--open]`, or `orch serve cfg.yaml` any time: a local page (stdlib server, http://127.0.0.1:8765) with what needs me (approvals, my questions), milestones, the critic's checklist progress, the manager's scores and reports, the manager↔critic thread, every worker↔critic round with live streaming, and a chat with the manager (it resumes the manager's own session). Without the board: `orch approve <run|cfg> [--note … | --revise …]`, `orch answer <run|cfg> Q3 "B"`, or type `Q3: B` / press Enter at a gate in the terminal.

## Config reference

```yaml
output_dir: ./orch-runs
defaults: {backend: claude, timeout: 1800, retries: 1, backend_options: {...}}  # inherited by all agents
monitors:
  - name: my-monitor
    workspace: ~/code/repo          # project root
    working_dir: services/api       # agents' cwd (relative to workspace); per-agent override allowed
    goal: | ...                     # or goal_file: goal.md
    max_rounds: 5
    enabled: true
    output_dir: ...                 # optional per-monitor override
    defaults: {...}                 # monitor-level defaults, merged over the top-level ones
    controller:
      mode: pipeline | llm
      instructions: | ...           # or instructions_file:
      parallel: false               # run agents in the same stage concurrently
      require: all | any            # pipeline
      pass_pattern / fail_pattern   # verdict regexes (default VERDICT: PASS / FAIL; last match wins)
      stop_on_error: false
      max_report_chars: 8000        # how much of an agent's output is embedded when routing it
      templates: {worker_feedback: "...", ...}   # override any prompt template (orch/templates.py)
      agent: {backend:..., system_prompt:..., backend_options:...}   # llm mode: the controller agent
    agents:
      - name: implementer
        role: worker                # pipeline uses worker/verifier; llm mode accepts any role name
        description: ...
        system_prompt: | ...        # or system_prompt_file:
        task: ...                   # pipeline: the worker's round-1 task
        reviews: [..]               # verifier only
        feedback_to: [..]           # verifier only
        backend: claude | codex | command | mock
        backend_options: {...}
        working_dir: ...
        timeout: 1800
        retries: 1
        env: {KEY: value}           # recorded by key name only
```

### Backends

* **claude**: runs `claude -p --output-format json` with the prompt on stdin. The first turn uses `--session-id <uuid>`, and later turns use `--resume <uuid>`. The system prompt is passed with `--append-system-prompt`, or with `--system-prompt` if you set `system_prompt_mode: replace`. Options: `model`, `effort`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `add_dirs`, `max_turns`, `mcp_config`, `extra_args`, `executable`. Cost, number of turns and error flags are recorded for each turn.
  Agents run with no one watching, so set `permission_mode` (for example `acceptEdits`) and `allowed_tools`. Use `bypassPermissions` only in a sandbox.
* **codex**: runs `codex exec --json` and continues sessions with `codex exec resume <id>`. Options: `model`, `effort` (sent as `model_reasoning_effort`), `sandbox`, `full_auto`, `extra_args`. This backend is experimental: Codex flags change between versions, so check them with `codex exec --help`.
* **command**: wraps any CLI. Options:
  * `command` and an optional `resume_command`, with the placeholders `{session_id} {cwd} {agent} {prompt} {prompt_file} {system_prompt} {system_prompt_file}`
  * `prompt_via: stdin|arg|file`
  * `output_format: text|json` together with `output_field` and `session_field`, or `session_regex`
* **mock**: a deterministic fake for dry runs and tests. Options: `delay`, `script`, `pass_after`, `done_after`, `fail_turns`, `write_file`.

## What gets recorded

```
<output_dir>/index.html                       every monitor and run (auto-refreshes while running)
<output_dir>/<monitor>/<run-id>/
  config.json        resolved config snapshot (env values omitted)
  state.json         status, current round, sessions {agent: session_id, turns}, round summaries,
                     pending feedback, turn counts, total cost
  events.jsonl       append-only event log (run/round/turn started/finished, human messages, errors)
  progress.md        human-readable running log with verdicts and decisions
  report.html        full viewer: every round → every turn's input, output, system prompt, stderr,
                     command line, session id, timing, exit code, attempts
  rounds/r002/round.json                      decision, verdicts / controller directive, routing
  rounds/r002/t01-implementer.json            full turn record (written as "running" first, then final)
  rounds/r002/t01-implementer.input.md / .output.md
  inbox/             human messages waiting for the next round (consumed → inbox/consumed/)
```

## Steering and inspecting a live run

```bash
orch status orch-runs --latest                 # one line per monitor
orch show orch-runs -m api-feature             # rounds, verdicts, one line per turn
orch show orch-runs/api-feature --turn r002-t01   # one turn in full: input, output, stderr
orch inject orch-runs/api-feature "Use pytest, not unittest" --to implementer
orch stop orch-runs/api-feature                # stops after terminating the current turn (resume re-sends it)
orch run my.yaml -m api-feature --resume       # continue the same run, same agent sessions
orch report orch-runs                          # regenerate HTML
```

Pressing Ctrl-C in `orch run` stops every monitor cleanly: running agent processes are terminated and each run is marked `stopped`, so `--resume` can pick it up later.

## Running a long project: lessons from real runs

- **Two round limits.** `milestone_rounds` (critic) is per milestone: at the limit the manager reviews anyway.
  `max_rounds` (manager) counts every round of the whole run; keep it well above `milestone_rounds`, or the run
  stops in the middle of a milestone (orch warns about this, and the stop message says which limit was hit).
  Raising either takes effect on `orch run --resume`.
- **Changing models or backends mid-run** (for cost, or Codex ↔ Claude): edit the agent's `agent.yaml` and resume.
  A changed backend can't resume the old session, so the agent starts a new one with a handover note.
- **Restarting.** Ctrl-C (or `orch stop`) ends the current turn, and `--resume` sends it again in the same session,
  so a restart costs at most one repeated turn. The cheapest moment is just after a worker turn ends with nothing
  waiting in the run's `inbox/` (only `consumed*` and `merged` folders). Notes queued for an agent survive a restart.
- **Cost.** Long sessions get dearer per turn because each turn re-reads the history: use `fresh_session:
  milestone` for workers, a cheaper model for the worker, and a low effort for the critic if the reviews stay good.
- **Idle rounds.** If the work waits on people, make sure the pause gets used (the board's *Needs you* shows it);
  a worker that keeps answering "holding" is spending rounds.

## Layout

```
orch/config.py      schema, defaults merging, *_file includes, validation
orch/backends.py    claude / codex / command / mock + process runner (timeouts, stop, kill process group)
orch/controller.py  MonitorRunner: pipeline + llm modes, sessions, routing, retries, parallel stages
orch/recorder.py    run directory, state, events, turn and round records, inbox / STOP control files
orch/report.py      static HTML viewers
orch/templates.py   all default prompts ({{goal}}, {{feedback}}, {{worker_reports}}, …)
orch/cli.py         orch init | validate | run | status | show | stop | inject | report
```
