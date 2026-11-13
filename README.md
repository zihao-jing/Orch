# Orch

A general-purpose orchestrator for teams of CLI coding agents (Claude Code, Codex, or anything you can call from a shell).

A project keeps its team in its own `.orch/` folder: one folder per agent, usually a **manager** (owns the goal,
the milestones and the rubric, and talks to me), a **critic** (turns each milestone into instructions and reviews
every change) and a **worker** (does the work in the repository). orch starts the agents in the project,
**keeps each agent's session alive across turns**, passes messages between them, waits for me only where I asked
to approve something, and records every round and turn under `.orch/runs/` for me to inspect, on a local board or
in the terminal.

```
              me ◀──── plan, milestone gates, questions, pauses ────┐
              │                                                      │
              ▼ approvals, answers, chat                             │
<project>/.orch/agents/                                              │
  manager/ ── milestone instruction ──▶ critic/ ── brief, review ──▶ worker/
     ▲                                    │  ▲                          │
     └──────── reports, questions ────────┘  └──── summary, questions ──┘
                                                                     ──▶ .orch/runs/<project>/<run>/
```

## Quick start

```bash
cd Orch
pip install -e .            # or: pip install pyyaml  and use `python -m orch`
python -m unittest discover tests     # mock backend, no API calls

cd ~/code/my-project
orch init . --project       # scaffolds .orch/agents/{manager,critic,worker}/
$EDITOR .orch/agents/manager/goal.md
orch validate               # resolves the folders, shows each agent's prompt, memory and settings
orch run --serve --open     # runs it, with the board at http://127.0.0.1:8765
orch run --dry-run          # the same flow with every agent replaced by the mock
```

Commands default to `./.orch`, so in the project root no path is needed. Python 3.9+ is required, and PyYAML for
the `agent.yaml` files. To use the `claude` backend, the `claude` CLI must be installed and logged in.

## Setting up a project (`.orch/`)

There is no separate project file: the team is the set of agent folders.

```
<project>/.orch/
  agents/
    manager/
      agent.yaml         role, backend, model … and the settings for the run as a whole
      goal.md            the goal (only the manager reads it)
      system_prompt.md   this agent's prompt in this project (starts as a copy of the role's default)
      memory.md          your notes and preferences for it (also memory/*.md); added to its system prompt
      state.md           its working state in the current run, kept by orch (gitignored; you can edit it)
    critic/   agent.yaml  system_prompt.md  memory.md  state.md
    worker/   agent.yaml  system_prompt.md  memory.md  state.md
  runs/                  run logs (gitignored)
```

**`memory.md` and `state.md`.** `memory.md` is yours: lasting preferences, rarely changed, added to the agent's system
prompt before every turn (it is kept stable because a changed system prompt makes the model re-read the whole
conversation at full price). `state.md` is where the agent stands in the current run, with one owner and one moment
of update for each piece:

| file | holds | written |
|---|---|---|
| `manager/state.md` | the standing instruction for the critic and the decisions that still hold | by the manager, in a `<state>` tag, whenever it writes to the critic (a milestone starts or is sent back, an answer changes the work, a restart) |
| `critic/state.md` | that standing instruction ("my standing instruction") and the critic's checklist | by orch, after every review and every change of the manager's state |
| `worker/state.md` | the worker's latest report | by orch, after every worker turn |

Whenever an agent starts a fresh session (`orch reset-sessions`, a changed backend, or `fresh_session: milestone`
for the manager or critic), its notes are built from its `state.md` and what happened since it was written (for the
manager: the decisions made since), at the moment its next turn starts. The agents are not asked to read the files
themselves: orch puts what they need into the message. You can edit any of them between turns: orch notices, and the agent's
next turn starts with "I edited your working notes; they now read: …" (an edit of the manager's also updates the
critic's). Each file names its run; a new run moves the previous run's files into that run's folder
(`runs/<project>/<run>/agent_state/`). The full history stays in the run's `decisions.md` and turn records.

Each `agent.yaml` holds that agent's own settings plus the run settings that belong to its role:

```yaml
# .orch/agents/manager/agent.yaml
role: manager
backend: claude
backend_options: {model: opus, effort: medium, permission_mode: auto}
timeout: 2400
goal_file: goal.md
max_rounds: 100             # rounds in all milestones together; keep it well above milestone_rounds
approve_plan: true          # wait for my OK on the milestone plan
approve_milestones: true    # and after each milestone the manager accepts
```

```yaml
# .orch/agents/critic/agent.yaml
role: critic
backend: claude
backend_options: {model: opus, effort: medium, permission_mode: auto}
checklist: true             # a PASS only counts when every checklist item is [x]
milestone_rounds: 25        # rounds without a step of the critic's plan finishing before the manager looks
checkin_every: 2            # a status line for the manager every 2 rounds (0 = off)
```

```yaml
# .orch/agents/worker/agent.yaml
role: worker
backend: claude
backend_options: {model: sonnet, effort: medium, permission_mode: auto}
timeout: 7200               # per turn; long jobs run in the background, and orch waits for them between turns
fresh_session: milestone    # a new session for each milestone
```

| You want to set… | Where |
|---|---|
| **The goal** | `manager/goal.md` (`goal_file:` in `manager/agent.yaml`) |
| **Model, effort, backend of one agent** | `backend`, `backend_options: {model, effort}` in that agent's `agent.yaml` |
| Tool permissions | `backend_options.permission_mode` / `allowed_tools` in that agent's `agent.yaml` |
| **An agent's prompt** | its `system_prompt.md` (missing: the role default in `orch/prompts/<role>.md`) |
| Project notes and preferences | its `memory.md` and `memory/*.md`, added after the prompt ("Things to keep in mind:"; HTML comments are ignored) |
| Display names used in messages | `display_name:` in each `agent.yaml`; fills `{{manager}}`, `{{critic}}`, `{{worker}}` in the prompts |
| Run-wide limit, approvals, minimum score | `manager/agent.yaml`: `max_rounds`, `approve_plan`, `approve_milestones`, `min_score`, `protocol`, `tag_reminder` |
| Rounds per milestone, checklist, check-ins, job waiting | `critic/agent.yaml`: `milestone_rounds`, `checklist`, `checklist_nudge`, `checkin_every`, `job_wait`, `job_ignore` |
| Fresh sessions, saved versions of files | `worker/agent.yaml`: `fresh_session`, `snapshot` |
| Where runs are recorded | `output_dir:` in any `agent.yaml` (default `.orch/runs`) |
| The agents' working directory | `working_dir:` per agent (relative to the project root) |
| The exact wording of routed prompts | `templates:` (see `orch/templates.py` for the names and `{{placeholders}}`) |

A run setting may sit in any agent's file, but it is set once: two agents giving it different values is an error.
With the `claude` backend, `model` is any value `claude --model` accepts (`haiku`, `sonnet`, `opus`, or a full
model ID), and `effort` is `low`, `medium`, `high`, `xhigh` or `max`.

`orch init --project` copies each role's default prompt into the agent folder, where the project can adapt it
freely. Prompt and memory files are re-read before every turn, so edits reach a running run (Codex agents read them
once, at the start of their session); `agent.yaml` changes take effect on `orch run --resume`. Workers get no task
file: their instructions come from the critic. `orch validate` shows which prompt and memory each agent uses.
A folder without a manager runs in `pipeline` mode (see *Other setups* below).

## How the team works (`managed` mode): me ↔ manager ↔ critic ↔ worker

Three layers of review, so each layer above reads less. Agents: exactly one `role: manager`, one `role: critic` (or `verifier`), one or more `role: worker`. Give them `display_name`s; relayed messages use them ("Alex wrote: …"), and no prompt mentions a workflow.

| step | who | what |
|---|---|---|
| plan | manager | splits the goal into a numbered milestone list + a scored rubric → **I approve** (`approve_plan`) |
| kickoff | manager → critic | a high-level note for the milestone |
| brief | critic → worker | the critic breaks the note into a `<plan>` of steps and gives the worker the first one, with a checklist for it |
| rounds | worker ↔ critic | one step of the critic's `<plan>` at a time; each review ends with `<plan>` + `<checklist>` (current step) + `<verdict>`; up to `milestone_rounds` rounds without a step finishing |
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

When the manager asks a side question during a critic review, the critic puts its answer inside
`<message to="you">…</message>` and keeps worker feedback outside it. For a critic reply, `you` routes to the
manager without exposing that role in the prompt; explicit agent names remain supported. The manager speaks
as "I" to the critic, and the critic addresses it as "you". Downstream messages present decisions as the
sender's own, without naming an upstream user. Use neutral display names to keep role names out of prompts.
With the protocol enabled, every
critic review/instruction turn includes this routing reminder, including resumed sessions; it is independent
of `tag_reminder`. Tagged answers are queued for the manager's next run turn, without creating an immediate
board-chat response. Dedicated critic reports and check-ins still address the manager directly.

| tag | written by | what orch does |
|---|---|---|
| `<message to="…">…</message>` | anyone | delivers the block to that agent only (the manager's instructions and answers to the critic, the critic's notes to the manager) |
| `<question>…</question>` | worker → critic, critic → manager, manager → me | one decision per tag; recorded (Q1, Q2 …) and passed up one level; the run does not wait |
| `<pause>…</pause>` | critic (a proposal) / manager (a pause) | the critic's goes to the manager; the manager's stops the whole run until I continue it |
| `<state>…</state>` | manager | saved as `manager/state.md` (and the critic's standing instruction); a fresh session starts from it |
| `<plan>…</plan>` | critic | its steps for the current work, `- [x]` done / `- [ ]` not yet. The worker gets one step at a time and never sees the plan. A step ticked is progress: the `milestone_rounds` count starts again, the board shows "step k of n", and a check-in is one status line from the plan (no model turn). Kept across fresh sessions; a new milestone starts without one |
| `<checklist>…</checklist>` + `<verdict>PASS\|FAIL</verdict>` | critic | the checklist covers the current step and standing rules; a PASS counts only when every item and every step of the plan is `[x]` (a PASS with steps open goes back with "hand out the next step"). The list is the critic's own: the worker never gets it (also when the critic forgets the tag), and the manager sees the latest plan and list with each milestone report |
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
conversations and keeps everything else: the phase, milestone, rounds, questions, decisions and records. When the
manager is reset in the middle of a milestone (the default, all three), the work restarts top-down: the manager gets
the goal, its approved plan and rubric, the milestone table, its `state.md`, the critic's newest message and the
decisions made since its state was written, and writes the critic a short instruction on what to continue with (and
a fresh `<state>`); the critic, with its checklist and a pointer to the decisions file, turns that into concrete
steps for the worker; the worker starts with only that. A cut-off turn is replaced by this, not sent again. Resetting one agent alone gives it
a handover instead, from its `state.md`: the critic its standing instruction, checklist and latest review in short;
the worker its latest report in short. The worker never gets the decisions list or anything about the people above the critic:
what the work must keep to reaches it in the critic's instructions. The notes are written when each agent's next turn starts, not at the reset, so they show where the run is then; until that turn, the agent's queue in `state.json` (`fwd`) holds a placeholder. The manager's notes include the critic's newest message of any kind with its checklist count, not just its last report. The board chat follows the new session.

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

**The board** — `orch run --serve [--open]`, or `orch serve` any time: a local page (stdlib server, http://127.0.0.1:8765) with what needs me (approvals, my questions), milestones, the critic's checklist progress, the manager's scores and reports, the manager↔critic thread, every worker↔critic round with live streaming, and a chat with the manager (it resumes the manager's own session). The **📜 Log** button opens the full log at `/log/` on the same server: every run, and for each run every turn's input, output, system prompt and command, grouped by milestone and round with everything folded except the newest round, a one-line preview on each folded turn, filters by agent and a search box. Files under `.orch/runs/` are served read-only at their path below `/log/`; nothing outside that folder is. To look from another machine, start it with `--host 0.0.0.0` (it then prints your network address). There is no password, so anyone who can reach the port can also steer and stop the run: use it on a network you trust, or keep the default and tunnel in (`ssh -L 8765:127.0.0.1:8765 you@this-machine`). Without the board: `orch approve [--note … | --revise …]`, `orch answer . Q3 "B"`, or type `Q3: B` / press Enter at a gate in the terminal.

## Steering and inspecting a live run

**Night mode** (🌙 on the board): while it is on, the manager answers its own questions for you and plan and milestone
gates pass without waiting (a milestone the manager did not accept goes back to the critic with the manager's guidance).
Its choices are recorded in Questions (🌙 manager decided) and in the decisions register, by "manager (night mode)";
answer any of them later to overrule it. A pause the manager calls for a task of yours still waits, and `max_rounds`
still caps the run.

```bash
orch status .orch/runs --latest                # one line per run
orch show .                                    # rounds, verdicts, one line per turn
orch show . --turn r002-t01                    # one turn in full: input, output, stderr
orch approve . --note "…"                      # approve the plan / milestone it waits on (or --revise "…")
orch answer . Q3 "B"                           # answer a question to me
orch stop .                                    # stops after terminating the current turn (resume re-sends it)
orch run --resume --serve                      # continue the same run, same agent sessions
orch reset-sessions .                          # with the run stopped: fresh conversations, same run state
orch audit .                                   # how the agents talk: findings in agents/auditor/audit.md
orch report .orch/runs                         # regenerate HTML
```

Pressing Ctrl-C in `orch run` stops cleanly: running agent processes are terminated and the run is marked
`stopped`, so `--resume` can pick it up later.

Only one `orch run` drives a run at a time: a second `--resume` on a run another process is driving says which
process holds it and does nothing else (with `--serve` it still shows the board). Stop the first one to take over.

## The auditor: how the agents talk, not what they do

An agent with `role: auditor` (scaffolded by `orch init --project` in `agents/auditor/`) sits outside the team. Every
`audit_every` rounds (default 10), and once more when the run ends, it reads a digest of what was passed between the
agents and keeps a short list of findings in `agents/auditor/audit.md`: repetition, unneeded context, messages that
crossed a boundary (leakage), turns that achieved nothing, roles doing each other's jobs. It never judges the task,
and nothing it writes reaches the agents; the agents are not told it exists.

The digest holds only the messages, already compared by orch: each message's size, the part that repeats what that
agent was already sent, where each paragraph came from (another agent, the human, the manager's chat replies, or
orch's own text), how much of each relayed output arrived, how long notes waited, and flags (short replies, bursts,
large messages). System prompts appear on the first audit and again only when they change. About 20k characters
per 10 rounds; the auditor gets no tools.

Each audit adds at most `audit_points` points (default 5) under "Audits" and rewrites the "Findings" list (at most 12
lines, each with a count and a status). Mark one `fixed` or `won't fix` and the auditor keeps it. The full exchange
of each audit is in `<run>/audits/`.

```bash
orch audit . --digest --rounds 35-44   # what the auditor would read; no model call
orch audit .                           # audit the last audit_every rounds now (uses the critic's backend if
                                       # the project has no auditor)
```

## What gets recorded

```
.orch/runs/index.html                          every run (auto-refreshes while running)
.orch/runs/<project>/<run-id>/
  config.json        resolved config snapshot (env values omitted)
  state.json         status, phase, milestone, rounds, sessions {agent: session_id, turns}, questions, cost
  events.jsonl       append-only event log (run/round/turn started/finished, human messages, errors)
  progress.md        human-readable running log with verdicts and decisions
  decisions.md       the decisions register (S1, S2 …)
  report.html        full viewer: every round → every turn's input, output, system prompt, stderr, timing …
  rounds/r002/round.json                       decision, verdicts, routing
  rounds/r002/t01-worker.json                  full turn record (written as "running" first, then final)
  rounds/r002/t01-worker.input.md / .output.md
  inbox/             messages waiting for the next turn (consumed → inbox/consumed/)
```

## Running a long project: lessons from real runs

- **Two round limits.** `milestone_rounds` (critic) counts rounds without progress: a step of the critic's plan
  finishing starts the count again, and at the limit the manager reviews anyway (a critic that writes no plan
  counts every round of the milestone).
  `max_rounds` (manager) counts every round of the whole run; keep it well above `milestone_rounds`, or the run
  stops in the middle of a milestone (orch warns about this, and the stop message says which limit was hit).
  Raising either takes effect on `orch run --resume`. A run keeps its own `max_rounds` (in `state.json`): a limit
  raised with `--max-rounds` stays when you resume without the flag; pass `--max-rounds` again to lower it.
- **Changing models or backends mid-run** (for cost, or Codex ↔ Claude): edit the agent's `agent.yaml` and resume.
  A changed backend can't resume the old session, so the agent starts a new one with a handover note.
- **Restarting.** Ctrl-C (or `orch stop`) ends the current turn, and `--resume` sends it again in the same session,
  so a restart costs at most one repeated turn. The cheapest moment is just after a worker turn ends with nothing
  waiting in the run's `inbox/` (only `consumed*` and `merged` folders). Notes queued for an agent survive a restart.
- **Cost.** Long sessions get dearer per turn because each turn re-reads the history: use `fresh_session:
  milestone` for workers, a cheaper model for the worker, and a low effort for the critic if the reviews stay good.
- **Idle rounds.** If the work waits on people, make sure the pause gets used (the board's *Needs you* shows it);
  a worker that keeps answering "holding" is spending rounds.
- **Insight: each agent should know only the one it talks to.** Agents that are told about the whole team start
  writing about it: a worker that knows a critic reviews it addresses "the critic", reasons about who will decide,
  and pads its reply for the next level up; a critic that knows the manager passes on "the manager decided…" and the
  manager's question numbers. That is noise for the reader and a source of confusion. So the worker's prompts,
  handover and decision list mention no other role, no framework and no pause: it works with "me", who is in fact the
  critic. The critic treats the manager as "me" and gives the worker instructions and decisions as its own, without
  their origin. The manager talks about the work to the critic, not about the worker or how the team is run. If you
  write your own prompts, keep to the same rule.
- **Insight: ask for brevity explicitly.** Without it, every message grows: instructions restated, earlier turns
  recapped, a preamble, praise. Each default prompt now says to say each point once, skip the recap and keep
  "nothing changed" to a few lines. Long messages cost twice: tokens for the reader, and attention for the next
  agent, who has to find what is new.

More, from auditing a 110-round run and checking every finding against the records (cost meters behind other
providers' endpoints, messages dropped at gates, what an auditor's digest must say): see
[`docs/insights-from-audits.md`](docs/insights-from-audits.md).

## Other setups: one config file, `pipeline` and `llm` modes

Instead of a `.orch/` folder, a single YAML (or JSON, or TOML on Python 3.11+) file can declare any number of
independent **monitors**, each with its own workspace, goal, controller and agents, run concurrently. This is
useful for running several small workflows together, or for the simpler `pipeline` and `llm` controllers.
`orch init my.yaml` writes an annotated example, and every command takes the file in place of the project folder
(`orch validate my.yaml`, `orch run my.yaml`, `orch run examples/mock-demo.yaml` for a dry demo).

| | `pipeline` (deterministic) | `llm` (an agent decides) |
|---|---|---|
| Round 1 | every worker gets `goal + instructions + its task` | controller gets goal, instructions, roster |
| Each round | workers work → every verifier inspects the workspace → `VERDICT: PASS/FAIL` | controller replies with JSON `{"dispatch":[{"agent","message"}], "done", "parallel"}` → those turns run → results go back to the controller |
| Routing | FAIL feedback goes to `feedback_to` (default: the workers the verifier `reviews`, default: all). Workers with no new feedback skip the round. | Decided by the controller. Invalid JSON or unknown agents get one automatic repair turn. |
| Done when | verifiers pass (`require: all` or `any`) | controller sets `done: true` (and writes `final_report`) |
| Stops at | `max_rounds` | `max_rounds` |

Settings merge in this order, with later ones winning: top-level `defaults:` → the monitor's `defaults:` → the
agent's own keys. `backend_options` dictionaries are merged key by key, so an agent that sets only `effort` still
inherits `model` from the defaults.

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
      mode: pipeline | llm | managed
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
        backend: claude | codex | agy | command | mock
        backend_options: {...}
        working_dir: ...
        timeout: 1800
        retries: 1
        env: {KEY: value}           # recorded by key name only
```

With a file, the steering commands take the output directory and a monitor name, e.g.
`orch show orch-runs -m api-feature`, `orch inject orch-runs/api-feature "Use pytest" --to implementer`,
`orch run my.yaml -m api-feature --resume`.

### Urgent messages

A message normally waits for its agent's next turn. An urgent one is delivered immediately:

* `orch inject <project> "Stop the provenance checks" --to worker --urgent`: if the worker is mid-turn, that turn is
  stopped and resumed in the same session with the message first; the agent then finishes the turn.
* Managed mode, `--to critic --urgent` while the worker is mid-turn: the critic answers in a separate check-in
  while the worker continues its current turn and session. The reply goes directly to the manager. It does not
  become worker feedback, trigger a review, or release a background-job wait. Explicit **Review now** still
  releases a job wait.
* In a board chat, the manager can request this check-in with `<message to="critic" urgent="yes">`. If the critic
  itself is already mid-turn, it receives the message through the usual same-session interruption.

During a check-in, the critic can also send an explicit `<message to="worker">…</message>` with changed
instructions. With the worker's Codex `transport: app-server`, Orch submits it using `turn/steer` into the
same active turn. Current tools keep running; Codex incorporates the update at its next input boundary.
Only the critic's addressed message reaches the worker; the manager conversation and the critic's reply
to the manager stay separate. Status-only check-ins do not change worker instructions.

The board's **Updates to running workers** section shows each message, its source and destination turns,
and whether it is queued, accepted, deferred, or unconfirmed. Acceptance means the backend accepted the
input, not that the requested work has been performed. If live input is unsupported or explicitly rejected
(including a turn that just finished), the message stays available for the next worker turn. An unacknowledged
send is marked unconfirmed and is not automatically resent, including after a restart. A background-job
wait is not released by these updates. Direct `orch inject --to worker --urgent` retains its interrupting behavior.

The turn record lists each interruption (`interruptions`, `cut_short`), and the board's progress log marks it with ⚡.

## Backends

* **claude**: runs `claude -p --output-format json` with the prompt on stdin. The first turn uses `--session-id <uuid>`, and later turns use `--resume <uuid>`. The system prompt is passed with `--append-system-prompt`, or with `--system-prompt` if you set `system_prompt_mode: replace`. Options: `model`, `effort`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `add_dirs`, `max_turns`, `mcp_config`, `extra_args`, `executable`. Cost, number of turns and error flags are recorded for each turn.
  Agents run with no one watching, so set `permission_mode` (for example `acceptEdits`) and `allowed_tools`. Use `bypassPermissions` only in a sandbox.
* **codex**: runs `codex exec --json` and continues sessions with `codex exec resume <id>`. Options: `model`, `effort` (sent as `model_reasoning_effort`), `sandbox`, `full_auto`, `extra_args`. This backend is experimental: Codex flags change between versions, so check them with `codex exec --help`.
  For incremental text on the board, set `backend_options.transport: app-server` (tested with Codex CLI 0.167.0).
  This uses `codex app-server` with `thread/start`, `thread/resume` and `turn/start`; text deltas and tool activity
  appear while the turn runs. Sessions remain resumable, and stop/urgent-message handling still applies.
  The board's **Prompt** section shows the turn input immediately, with system instructions expandable separately.
  How finely text streams depends on the provider: a gateway may send a whole reply as one delta.
  The default transport remains `exec`. Both transports accept `model`, `effort`, `sandbox` and `executable`.
  With `app-server`, use `app_server_args` for CLI config overrides (`-c key=value`); when absent, it uses
  `extra_args`. Exec-only flags such as `--ignore-user-config` and `--full-auto` are not supported by app-server.
  App-server reads normal Codex configuration plus these overrides, uses `approval_policy="never"`, and defaults
  to `workspace-write` if `sandbox` is omitted. It does not automatically approve tool requests.
* **agy**: the Antigravity CLI, for Gemini models. Runs `agy --output-format json -p <prompt>` and continues sessions with `--conversation <id>` (the id is read from the JSON output). There is no system-prompt flag, so the system prompt goes in front of a session's first message. Options: `model`, `effort`, `mode` (`accept-edits|auto|plan`), `dangerously_skip_permissions`, `sandbox`, `add_dirs`, `agent`, `project`, `settings`, `print_timeout`, `prompt_via` (`arg`, the default, or `stdin`), `extra_args`, `executable`. Sign in once by running `agy` interactively; `agy models` lists the model names. Experimental: the JSON field names are read leniently (`result`/`response`/`text`, `conversation_id`/`session_id`).
* **command**: wraps any CLI. Options:
  * `command` and an optional `resume_command`, with the placeholders `{session_id} {cwd} {agent} {prompt} {prompt_file} {system_prompt} {system_prompt_file}`
  * `prompt_via: stdin|arg|file`
  * `output_format: text|json` together with `output_field` and `session_field`, or `session_regex`
* **mock**: a deterministic fake for dry runs and tests. Options: `delay`, `script`, `pass_after`, `done_after`, `fail_turns`, `write_file`.

### Backup models (`fallback`)

Each agent can list backup models, tried in order when a turn still fails after its `retries` (an API error,
a refusal from the provider, a crash; not a timeout, a stop or a question):

```yaml
backend: claude
backend_options: {model: opus, effort: medium, permission_mode: auto}
retries: 1
fallback:
  - sonnet                                   # same backend and settings, another model
  - {label: deepseek, backend_options: {model: deepseek/deepseek-v4.1-flash},
     env: {ANTHROPIC_BASE_URL: https://zenmux.ai/api/anthropic, ANTHROPIC_AUTH_TOKEN: ...}}
  - {backend: agy, backend_options: {model: gemini-3.6-pro, mode: auto}}
fallback_cooldown: 900                       # seconds to stay on a backup that worked (0 = back to the primary next turn)
```

* A model name is shorthand for "same backend, same settings, this model".
* With the same backend, a backup's `backend_options` are merged over the primary's (`null` removes one) and it
  keeps the primary's `env` unless it gives its own (a given `env` replaces the whole of it). With another
  backend, both start empty. Each backup may set its own `retries`.
* A backup on the same backend and endpoint (the same `*_BASE_URL` env values) continues the agent's session.
  One on another backend or endpoint starts a new session, with the handover notes in front of the message
  (managed mode) or a note to pick up from the workspace.
* After a backup works, the agent stays on it for `fallback_cooldown` seconds, then tries its primary again.
* The turn record lists the model of every attempt (`attempts[].model`, `model`) and each switch (`fallbacks`);
  the progress log marks a switch with ↪. The auditor and board chats also try the backups (a chat only the
  ones that can continue its session).

## Layout

```
orch/config.py      schema, defaults merging, *_file includes, validation
orch/backends.py    claude / codex / agy / command / mock + process runner (timeouts, stop, kill process group)
orch/controller.py  MonitorRunner: pipeline + llm modes, sessions, routing, retries, parallel stages
orch/recorder.py    run directory, state, events, turn and round records, inbox / STOP control files
orch/report.py      static HTML viewers
orch/templates.py   all default prompts ({{goal}}, {{feedback}}, {{worker_reports}}, …)
orch/audit.py       the auditor's digest (origins, repeats, relays, flags), its findings file, one audit
orch/cli.py         orch init | validate | run | status | show | stop | inject | audit | report
```
