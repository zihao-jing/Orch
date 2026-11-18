# orch development plan

Batches of changes, from `lessons.md` and your comments in `lessons-commented.md`. 💬 = discussion point: please
comment. Order is a suggestion.

## B1. Role views and protocol

- Protocol prompt states each agent's user and role view (lessons §1); lists who it may address.
- No agent mentions the layer above or orch roles. Critic writes to the worker as "I"; never "worker", "manager".
- Manager → worker allowed, but not prompted.
- Board chat to any agent arrives as from its usual user.
- 💬 How far to hide: the manager knows the worker exists; does the critic know a manager exists (vs "the user")? [No, it doesn't. I said that for the critic, The manager is a default view. Default view is the user.]

## B2. Message routing

- orch scans every new reply for message tags: round turns, board chat replies, and the manager's terminal session
  (via a Claude Code hook, if not slow). Delivery happens at once, as an activation.
- Status labels: sent → held / delivered.
- Held-by-design blocks logged with the reason; delivery log matches the recipient's prompt.
- Questions never stripped; protocol fix.
- Corrections: the critic says what is replaced ("drop X; do Y now").
- 💬 Activation on delivery while the recipient is mid-turn: queue until turn end, or interrupt? Proposal: queue
  unless marked urgent. [Proposal accepted.]

## B3. Boards (files agents edit, not re-output)

- **Golden board** (project): rules and key decisions for all agents; manager owns and prunes it. Decisions
  register merges into it.
- **Preference** per agent (exists; keep).
- **Step checklist** (critic, `.md`): the critic splits the milestone into steps; each step's checklist lives here;
  PASS reads it. Edited in place.
- Agents output only what changed.
- 💬 Golden board vs project preference: one file or two? [One]

## B4. System reminders

- Dynamic reminders, short: conciseness, protocol, golden board.
- Interval per reminder (e.g. conciseness every 3 turns) or random 3–10; configurable.
- Fixed per-turn reminders removed.
- Report length guide in the worker's reminders.

## B5. Progress checks (replace job wait and flat timeout)

- Critic quick check every N min (config, default 20): reads logs, runs status commands; no full review, no message
  unless quiet, off track or watcher down.
- Prompt like "Quick check: how is the run going?"; short output.
- Manager checks at its own, longer interval.
- Critic may propose a pause; manager decides.
- Live status line per agent on the board.
- Remove the 2 h turn limit; rely on checks.
- 💬 Check runs in the critic's session (context grows) or a side session (no memory of checks)? [Good point. It's better that I can choose that. Same for all of the agents.]

## B6. The manager's job

- Limited view by default: logs, launch scripts, reports, markdown, critic's checklist vs its milestones; reads
  deeper only when something looks wrong. Not the worker's intermediate scripts.
- Rubrics per milestone, task-specific, 3–8, set by the manager.
- Research preference in its prompt: diagnose before adding work; look for hidden causes.
- Discussion mode: while on, nothing goes to the critic; on close, one batch; the critic decides whether to
  interrupt the worker.
- Milestone fallback: board can move a milestone back (display + checklist), no extra prompt.
- 💬 Discussion mode: explicit switch on the board, or detected from the user's words? [Explicit.]

## B7. Sessions and context

- Milestone start: system prompt + short instruction, no pasted history. Manager keeps its session.
- After a cut-off: "continue" only.
- Record each session reset and why (model change, refresh).
- Turn cost attributed per turn, board-chat turns as their own entries.
- 💬 Worker reset per passed step, decided by the critic: what carries over (golden board + step brief)? [I think we should let the worker continue instead of starting a new session at each step. I think it's better not to restart. Can do a compact.]

## B8. Models and reliability

- Project-level model list; each agent picks a model id, backup and second backup.
- Retry the same model at 1, 2, 4 min, then switch; shown on the board.
- Model change: the critic is told and adjusts its instruction briefly.
- Save the final message only (raw-stream bug); size warnings on the board, no caps.
- Less cost focus in prompts; keep meters simple.

## B9. Gates and UI

- "Send back" on every gate, including the final pause.
- Switches can't flip night mode in the middle of a question.
- Outside night mode, a manager question to the user pauses dependent work; in night mode the manager acts on its
  recommendation.
- Gate summary: what changed, what was checked, where to look.
- "Review file" button: the manager writes a concise, self-contained Markdown review to the review rules; the user
  comments in it.

## B10. Auditor

- Design logic in its prompt; timestamps in its digest; strip reminders before checks.

## B11. Parallel workers 💬

- Critic or manager proposes N workers; manager approves within budget, else the user. [OK]
- A work board records who does what; the critic reads it instead of remembering. [Yes, that's the intent.]
- Open: critic load; one-to-many messages; board display. Maybe a sub-critic per worker group. [Yes, I think for each worker group, we can set up a smaller model. Which is even tiny. And it is giving critical results to the real critic. The A real critic can send back the task and decide what to do next. So for each worker, we can add a new role. It's like a monitor. A small model, a lightweight model which can read the rubrics for the worker and check the worker's result, and briefly updated to the critic.Of course, the update should also be concise. For example, it can pass to the real critic: the checklist that is being finished, and a reason for each checklist point. Like a small judge. And only if multiple workers could trigger this kind of monitor.]
