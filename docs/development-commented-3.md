# orch development plan

Batches of changes, from `lessons.md`. Comment rounds folded in (originals: `development-commented.md`, `-commented-2.md`).
✔ = decided. 💬 = new discussion point: please comment.

Suggested order: B1 + B2 + B4 (protocol) → B3 → B5 + B7 → B8 → B6 + B9 + B9b → B10 → B11 (needs boards and the monitor).

## B1. Role views and protocol

- Protocol prompt states each agent's user and role view; lists who it may address.
- ✔ Each agent sees only its user, never the layer above. The critic doesn't know a manager or a real user exists;
  the worker doesn't know a critic exists.
- No agent mentions orch roles to its role view. The critic writes to the worker as "I"; never "worker", "manager".
- Manager → worker allowed, but not prompted.
- Board chat to any agent arrives as from its usual user.

## B2. Message routing

- orch scans every new reply for message tags: round turns, board chat replies, the manager's terminal session (via
  a Claude Code hook, if not slow).
- ✔ Delivery is an activation. If the recipient is mid-turn: queue until its turn ends, unless marked urgent.
- Status labels: sent → held / delivered.
- Held-by-design blocks logged with the reason; delivery log matches the recipient's prompt.
- Questions never stripped.
- Corrections say what they replace ("drop X; do Y now").
- ✔ Terminal session: if you talk to the manager in a terminal while orch also resumes the same session, the two
  write one session at once and its history splits. Proposal: while a terminal session is open, orch holds the
  manager's turns (hook marks it busy) and routes its tags.

## B3. Boards (files agents edit, not re-output)

- ✔ **Golden board**: one project file of rules, preferences and key decisions for all agents. The manager owns
  and prunes it. Replaces the decisions register.
- **Preference** per agent (keep).
- **Step checklist** (critic, `.md`): one checklist per step; PASS reads it; edited in place.
- Agents output only what changed.
- ✔ Golden board size: above a size (e.g. 8k characters), a board warning and a reminder to the manager to prune.

## B4. System reminders

- Short dynamic reminders: conciseness, protocol, golden board.
- Interval per reminder (fixed or random 3–10), configurable. No fixed per-turn reminders.
- Report length guide in the worker's reminders.

## B5. Progress checks (replace job wait and flat timeout)

- Quick check every N min (config, default 20): reads logs, runs status commands; no full review; messages the
  worker only if quiet, off track or watcher down.
- Prompt like "Quick check: how is the run going?"; short output.
- Manager checks at its own, longer interval. Critic may propose a pause; manager decides.
- Live status line per agent on the board.
- ✔ Per agent setting: check in the agent's own session or in a side session.
- ✔ Keep a high safety limit (e.g. 8 h, configurable) instead of none. If checks fail silently, a stuck turn
  otherwise runs forever.
- ✔ The monitor (B11) does these checks, even with one worker; the critic's context stays clean.

## B6. The manager's job

- Limited view by default: logs, launch scripts, reports, markdown, critic's checklist vs its milestones; reads
  deeper only when something looks wrong.
- Task-specific rubrics per milestone, 3–8, set by the manager.
- Research guidance in its prompt (`collaboration-preferences.md`).
- ✔ Discussion mode: an explicit board switch. While on, nothing goes to the critic; on close, one batch; the
  critic decides whether to interrupt the worker.
- Milestone fallback on the board (display + checklist), no extra prompt.

## B7. Sessions and context

- Milestone start: system prompt + short instruction, no pasted history. Manager keeps its session.
- ✔ The worker keeps its session across steps, compacting when long. New session per milestone (as now).
- After a cut-off: "continue" only.
- Record each session reset and why.
- Turn cost per turn; board-chat turns as their own entries.
- ✔ Compaction stays the CLI's (auto-compact; context length set in agent YAML). orch adds an explicit compact
  between steps only.

## B8. Models and reliability

- Project-level model list; each agent picks a model id, backup and second backup.
- Retry the same model at 1, 2, 4 min, then switch; shown on the board.
- Model change: the critic is told and adjusts its instruction briefly.
- Save the final message only; size warnings on the board, no caps.
- Less cost focus in prompts; keep meters simple.

## B9. Gates and UI

- "Send back" on every gate, including the final pause.
- Switches can't flip night mode in the middle of a question.
- Outside night mode, a manager question to the user pauses dependent work; in night mode the manager acts on its
  recommendation.
- Gate summary: what changed, what was checked, where to look.
- "Review file" button: the manager writes a concise Markdown review; the user comments in it.

## B9b. Board redesign

- ✔ All messages on the dashboard; no separate log page. Folded levels; full text loaded on unfold, cached.
- 💬 Levels: milestone → step (or round) → items. Everything above the current step is folded. [What means above, up or down here?]
- 💬 Message kinds, each with its own colour and filter: needs you (questions, gates), decisions, messages between
  agents, agent turns (full output), progress checks, jobs, system events (retry, model switch, warnings).
[I felt I hardly ever see the decisions. It's better to replace it with the golden board. Messages between agents, I prefer to show them in different blocks, Each block holder a level of communication (For example, manager and me are one level, and manager with critics are one leve, Critic with worker is one level, The monitor should be a separate tiny one, It only observes the work and Briefly tell the critic) Other things are OK, I think. And do you think one block is one agent's message is better, or one block is one level of communication?]
- 💬 Top of the page: "needs you" pinned, then one status line per agent (and per worker lane). Each links to its
  message in the timeline. [I guess most of the agents are idle. So the status only showing who is working is fine.Several workers are working. You can see a list of working agents. ]
- 💬 Search box and agent / kind filters; the URL keeps the position, so a link opens the exact message. [I only review messages on the dashboard. Not to see the local files.]

## B10. Auditor

- Design logic in its prompt; timestamps in its digest; strip reminders before checks.

## B11. Parallel workers

- ✔ Critic or manager proposes N workers; manager approves within budget, else the user.
- ✔ Work board: who does what, status, outputs. The critic reads it instead of remembering.
- ✔ A **monitor** per worker (also with one worker, for the B5 checks): a small model that reads the step's checklist, checks the
  worker's output, and reports to the critic briefly (each item done or not, one reason). The critic decides what
  happens next.
- Each worker sees only the critic as its user; workers never talk to each other.
- ✔ Monitor reports evidence, not verdicts: "item 3: done, `results/x.json` has 120 rows", not "PASS". A small
  model's PASS invites the critic to trust it, the same failure as trusting worker reports. The critic spot-checks
  at least one item per step.
- ✔ Workspaces: the critic considers file conflicts when assigning work. Preferred, not strict: a separate output
  folder per worker, a git worktree for code; the critic merges. Shared limits (GPU, API rate) are listed
  on the work board.
- ✔ Who writes to whom: the critic → workers by id (`<message to="worker-2">`); monitor → critic only; a worker's
  question goes to the critic, not the monitor.
- ✔ Board display: one lane per worker (status line, current step, monitor's last report); messages folded under
  the lane.
