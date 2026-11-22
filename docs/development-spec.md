# orch development spec (for agents)

The agent-facing version of `development.md` (the user's short review copy). Same batches, with the context needed
to implement them. Background and evidence: `lessons.md`. Rules for prompt text: `collaboration-preferences.md`,
`writing-preferences.md`.

## Context

- orch (`orch/`) runs CLI coding agents (Claude Code or Codex) as a team: **manager**, **critic**, **worker**, plus
  an **auditor** that reviews run records. Managed mode is in `managed.py`; turns are run by `backends.py`
  (`codex_app_server.py` for Codex); records by `recorder.py`; the web board by `board.py` + `board.html`; tags by
  `tags.py`; settings by `config.py` (agent YAML files, hot-reloaded before each turn); background jobs by
  `jobs.py`.
- Prompts: role prompts in `orch/prompts/<role>.md` (per project, editable); the fixed protocol every project
  shares in `orch/prompts/protocol/<role>.md`.
- Agents talk through tags in their replies: `<message to=…>`, `<question>`, `<pause>`, `<checklist>`,
  `<verdict>`, `<plan>`, `<state>`. Board actions arrive as control files in the run's `inbox/`
  (`recorder.take_control`).
- Gates: plan, milestone, wait (`managed.wait_gate`, `ph_wait`). Night mode auto-approves gates and lets the manager
  settle questions alone.
- Run records: `.orch/runs/<project>/<run>/` with `state.json`, `chat.jsonl`, `rounds/rNNN/tNN-<agent>.{input.md,
  output.md,json}`, `audits/`, `inbox/`.

## Design principles (apply to every batch)

1. **Default user view.** Each agent has exactly one "user": manager → the real user; critic → the manager;
   worker → the critic. It talks to that user as to a person.
2. **Role view.** The agents it may address: manager → critic (worker only in rare cases); critic → its
   worker(s); worker → none. A monitor (B11) reports only to the critic.
3. **No layer above is ever revealed.** The critic doesn't know a manager or real user exists; the worker doesn't
   know a critic exists. Nobody writes "worker", "manager", "critic" or "orch" to an agent below. Any message to an
   agent, whatever its source (board chat, the real user), arrives as from its usual user.
4. **Conciseness comes from the user framing plus short reminders, not caps.** Never truncate messages. Warn on
   the board when a size is out of range.
5. **Agents edit boards; they don't re-output them.** Long-lived state lives in files.
6. **The board shows what is happening now.** Every retry, model switch, check and warning is visible there.

## B1. Role views and protocol

Goal: every agent knows its user and whom it may address, and never leaks the layer above.

- Rewrite `orch/prompts/protocol/*.md` to state the agent's user and role view explicitly, with the exact
  addresses it may use. Remove any text that describes the whole team to the critic or worker.
- Critic protocol: write to the worker as "I", as its user. Never mention the manager, orch, or the word "worker".
- Manager protocol: write to the critic as its user. Messaging the worker directly is possible but not suggested.
- Board chat (`board.py`, the direct-chat prompt around the `system-reminder` "talking to you directly"): don't say
  the message comes from the board or the real user; frame it as from the agent's usual user.
- Stop manager–user chat text from reaching critic prompts (lessons §1: up to 45% of a critic input).
- Done when: no critic or worker input in a test run contains manager–user chat or role names of layers above;
  invalid addresses are rejected with a short correction to the sender.

## B2. Message routing

Goal: a message is delivered as soon as it is written, from any source, and its status is always true.

- Scan every new agent reply for message tags, not only round turns: board chat replies from any agent, and the
  manager's interactive terminal session. For the terminal, use a Claude Code hook (Stop hook, or similar) that
  hands new assistant text to orch; keep the hook fast (hooks that slow every turn were removed before).
- Delivery = activation. If the recipient is idle, start its turn. If it is mid-turn, queue the message until the
  turn ends; a message marked urgent interrupts.
- While a terminal session on the manager's session is open, orch must not resume that session (two writers split
  its history). The hook marks the session busy; orch holds manager turns and only routes the session's tags.
- Status per message: sent → held (with reason) / delivered (seen in the recipient's prompt). Blocks held by design
  (e.g. the critic's checklist is never passed to the worker) are logged as such, not as lost. The delivery log
  must match the recipient's actual prompt.
- Never strip a `<question>` addressed to the recipient, including when nested in other blocks.
- Corrections: protocol tells the critic to say what a new instruction replaces ("Drop X; do Y now"); orch marks the
  replaced queued message as superseded instead of delivering both.
- Done when: a tagged message written in board chat or the terminal reaches its recipient without waiting for a
  round; delivery status on the board matches the prompts.

## B3. Boards

Goal: long-lived state lives in files that agents edit in place.

- **Golden board**: one project file (`.orch/golden.md` or similar) holding rules, the user's preferences and key
  decisions. Every agent sees it (system prompt or periodic reminder, B4). The manager owns it: adds, merges,
  prunes. It replaces the decisions register as the source agents read; keep the register as a record if useful.
- Size: above a threshold (configurable, ~8k characters) show a board warning and send the manager a reminder to
  prune.
- **Agent preference** files: keep as now (`.orch/agents/<agent>/preference.md`).
- **Step checklist**: a Markdown file per milestone owned by the critic. The critic splits the milestone into steps;
  each step has its checklist items. The critic edits the file; PASS is computed from it (every item of the step
  ticked). Replaces the `<checklist>` block in every output.
- Agents output only changes to plan / state / checklist (lessons §4: full rewrites were 45–75% of critic output).
- Done when: critic outputs contain no full checklist or plan; PASS reads the checklist file.

## B4. System reminders

Goal: short reminders at intervals keep rules alive without a fixed block every turn.

- A reminder registry in config: each reminder has short text (one or two sentences), a target (agent or all) and
  an interval (every N turns, or random in a range, e.g. 3–10). Extends the existing `tag_reminder` chance.
- Initial reminders: conciseness ("Think at length; write briefly. Your user reads every word."); protocol (whom you
  may address, tag form); golden board (point to it or include it); worker report length ("Short report; details in
  files.").
- Remove the fixed per-turn reminders (555–1,010 characters on every critic review).
- Strip orch's reminders from replies before automatic checks read them (lessons §9).
- Reminders go at the end of the user message, so they don't break the prompt cache.

## B5. Progress checks

Goal: long turns are allowed and watched, instead of job waits and flat timeouts.

- A quick check every N minutes (agent config, default 20) while a worker turn or job runs. It reads logs and runs
  status commands. No full review, short output. It messages the worker only if the run is quiet, off track, or a
  watcher has died.
- The check is done by the worker's monitor (B11), also when there is only one worker, and reported briefly to the
  critic. The manager checks at a longer interval (config).
- Per agent setting: run checks in the agent's own session, or in a side session.
- Check prompt style: "Quick check: how is the run going?"
- The critic may propose a pause; the manager decides.
- Replace the job wait (`job_wait`, polling rounds of "still running") with these checks.
- Replace the 2 h turn timeout with a high safety limit (configurable, e.g. 8 h).
- Board: a live status line from the latest check for each working agent.
- Done when: no rounds are spent only on "still running"; a quiet run is noticed within one interval.

## B6. The manager's job

Goal: the manager reviews as the user would, from a limited view.

- Manager prompt: default view is logs, launch scripts, reports, Markdown files, and the critic's step checklist
  compared with the manager's own milestones and the goal. It reads deeper (scripts, raw outputs) only when
  something looks wrong. It doesn't review the worker's intermediate code.
- Per milestone, the manager sets 3–8 task-specific rubrics (e.g. "each reviewer concern is strongly supported"),
  replacing the fixed 5 generic ones.
- Add the research guidance from `collaboration-preferences.md` to the manager prompt.
- Discussion mode: the manager's directions stay held until the user clicks **Apply discussion**. That action ends
  discussion and makes one manager turn use the existing conversation to settle covered questions and the current
  gate (including a human-task wait), then sends consolidated directions. Unanswered questions and ambiguous gate
  decisions stay open. Gate handlers reuse this reply rather than requesting another summary.
  **End discussion** offers **Apply discussion**, **End only**, and **Cancel**. End only makes no manager turn,
  preserves existing gates without creating one, drops unsent discussion directions, and releases the discussion
  hold so existing work can continue. The apply button disappears when no discussion is pending. Applying shows
  queued, running (with live manager activity), or paused status, suppresses gate
  inputs, and waits for accepted manager chats to finish. Invalid resolutions release nothing; failures are shown
  for an explicit retry. Request IDs prevent duplicate applications.
- Milestone fallback: the board can move a milestone back to open (display and checklist state). No extra prompt to
  the manager.

## B7. Sessions and context

Goal: no resent or pasted history; sessions continue where sensible.

- Milestone start: the critic and worker get their system prompt and a short natural instruction, not pasted past
  rounds (one start carried 22.9k characters). The manager keeps its session.
- The worker keeps its session across steps within a milestone; new session per milestone (as now).
- Compaction is the CLI's job (auto-compact; context length set in the agent YAML). orch only adds an explicit
  compact between steps, never mid-task. orch doesn't count context length.
- After a cut-off turn, resume with "continue" only; don't resend the last message.
- Record every session reset with its reason (model change, refresh, compact) in the turn record.
- Cost attribution bug: turn cost is the change in the session's running total, so board-chat turns in the same
  session are charged to the next orch turn. Record board-chat turns as their own entries.

## B8. Models and reliability

- Project-level model list in the general settings (id → backend, model, endpoint, key name). Each agent YAML picks
  a model id, a backup id and a second backup id, without repeating model settings. Hot reload must pick up changes
  to both the list and the agent files.
- On failure: retry the same model after 1, 2, 4 minutes; then switch to the backup, then the second backup. Show
  each retry and switch on the board.
- When the worker's model changes, tell the critic in one line; the critic adjusts its instruction briefly if
  needed.
- Save only the agent's final message as the reply; the raw event stream was stored twice (5.7M and 6.5M
  characters). Board warning on out-of-range sizes; no caps.
- Prompts: drop cost accounting from agents' tasks; keep cost meters simple (experiment spend caps still apply).

## B9. Gates

- "Send back" with notes on every gate, including the wait gate (currently only "Done, carry on", `board.html`
  wait-gate UI and `wait_gate`).
- Night mode can't switch while a question is open.
- Outside night mode, a manager question to the user pauses work that depends on it. In night mode the manager acts
  on its own recommendation and records the decision.
- Gate summary: what changed, what was checked, where to look.
- "Review file" button: the manager writes a concise, self-contained Markdown review to the review rules; the user
  comments in it and sends it back.

## B9b. Board redesign

- All messages live on the dashboard; remove the separate log page. Full text loads on unfold and is cached.
- Time levels: milestone → step → messages. Past milestones and steps are folded; the current one is open.
- One block per level of communication:
  - user ↔ manager;
  - manager ↔ critic;
  - critic ↔ worker (one lane per worker when there are several);
  - monitor → critic: a small block.
- In a block: messages in order, one line each, unfolding to full text. Questions and gates that need the user are
  highlighted and pinned at the top of the page.
- Show the golden board instead of the decisions list.
- Progress checks, jobs and system events (retries, model switches, warnings) are small tagged lines in their
  block, with a filter to hide them.
- Status area: only agents currently working ("worker-2: step 3, running 14 min").
- A search box over all messages.

## B10. Auditor

- Put orch's design logic (role views, held-by-design blocks, check-ins) in the auditor prompt so it doesn't report
  design as bugs.
- Add timestamps to its digest: turn start, how long each message waited, when questions opened and closed.
- Strip orch's reminders before its checks.

## B11. Parallel workers

Goal: several workers on independent steps, without overloading the critic.

- The critic or manager proposes N workers for independent steps; the manager approves within budget, otherwise the
  user decides.
- Work board (file): who does what, status, output location, shared limits (GPU, API rate). The critic reads it
  instead of remembering each worker.
- Each worker sees only the critic as its user, as if it were alone. Workers never talk to each other. The critic
  addresses them by id (`<message to="worker-2">`).
- A **monitor** per worker: a small, cheap model. It reads the step's checklist, watches the worker's progress
  (B5) and output, and reports briefly to the critic: each item done or not, with evidence ("item 3: done,
  `results/x.json` has 120 rows"). It never gives a verdict; the critic decides and spot-checks at least one item
  per step. It writes only to the critic. A worker's questions go to the critic, not the monitor.
- Workspaces: the critic considers file conflicts when assigning steps. Preferred, not strict: a separate output
  folder per worker, a git worktree for code; the critic merges.
- Board: one lane per worker in the critic ↔ worker block (status, current step, monitor's last report).

## Build order

B1 + B2 + B4 (protocol) → B3 → B5 + B7 → B8 → B6 + B9 + B9b → B10 → B11.
