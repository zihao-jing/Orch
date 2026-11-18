# orch development plan

Batches of changes, from `lessons.md`. Round-1 comments folded in (original: `development-commented.md`).
✔ = decided. 💬 = new discussion point: please comment.

Suggested order: B1 + B2 + B4 (protocol) → B3 → B5 + B7 → B8 → B6 + B9 → B10 → B11 (needs boards and the monitor).

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
- 💬 Terminal session: if you talk to the manager in a terminal while orch also resumes the same session, the two
  write one session at once and its history splits. Proposal: while a terminal session is open, orch holds the
  manager's turns (hook marks it busy) and routes its tags. [OK]

## B3. Boards (files agents edit, not re-output)

- ✔ **Golden board**: one project file of rules, preferences and key decisions for all agents. The manager owns
  and prunes it. Replaces the decisions register.
- **Preference** per agent (keep).
- **Step checklist** (critic, `.md`): one checklist per step; PASS reads it; edited in place.
- Agents output only what changed.
- 💬 Golden board size: a warning on the board above a size (e.g. 8k characters) so pruning happens before it
  fills every prompt. [OK, and above a size, You can send a reminder to the manager to manage it.]

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
- 💬 Keep a high safety limit (e.g. 8 h, configurable) instead of none. If checks fail silently, a stuck turn
  otherwise runs forever. [OK]
- 💬 The B11 monitor could also do these checks, even with one worker: cheaper, and the critic's context stays clean. [Good]

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
- 💬 Who triggers the compact: orch at a size threshold, or the critic between steps? Proposal: orch, between steps
  only, never mid-task. [The compact is the cli's Operation, And we can Configure the context length in the agent YAML. Besides, it's auto compact. You can add a compact between steps. Orch Doesn't have to count the context length]

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

[I think the UI could be revised as well. Currently, it's hard to locate the exact message on the first page. And the outside level should be folded as well. We need a clear classification of the messages and show them in the blocks. And then we don't need the log page. I prefer that all messages be shown on the dashboard. And load it whenever it is unfolded with cache. think of the design when update the UI.]

## B10. Auditor

- Design logic in its prompt; timestamps in its digest; strip reminders before checks.

## B11. Parallel workers

- ✔ Critic or manager proposes N workers; manager approves within budget, else the user.
- ✔ Work board: who does what, status, outputs. The critic reads it instead of remembering.
- ✔ A **monitor** per worker when there are several: a small model that reads the step's checklist, checks the
  worker's output, and reports to the critic briefly (each item done or not, one reason). The critic decides what
  happens next.
- Each worker sees only the critic as its user; workers never talk to each other.
- 💬 Monitor reports evidence, not verdicts: "item 3: done, `results/x.json` has 120 rows", not "PASS". A small
  model's PASS invites the critic to trust it, the same failure as trusting worker reports. The critic spot-checks
  at least one item per step. [Yes, the monitor is not allowed to give verdict.]
- 💬 Workspaces: parallel workers in one folder overwrite each other's files. Proposal: each worker writes only to
  its own output folder (or a git worktree for code); the critic merges. Shared limits (GPU, API rate) are listed
  on the work board.[Generally good. When the critic Assign the job, And should consider the conflicts of writing files. And use work tree and separate output folder, preferred. But not strictly.]
- 💬 Who writes to whom: the critic → workers by id (`<message to="worker-2">`); monitor → critic only; a worker's
  question goes to the critic, not the monitor. [Correct.]
- 💬 Board display: one lane per worker (status line, current step, monitor's last report); messages folded under
  the lane. [Sounds good.]
