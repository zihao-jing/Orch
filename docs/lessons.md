# Lessons from building and running orch

General lessons for a manager–critic–worker system, grouped by theme so each point appears once. Example: ICLR
2027 rebuttal run (`ICLR2027/20261108-153043`: 272 rounds, 967 turns, Nov 8–16 2026). Sources and your comments:
`lessons-commented.md`. Fix plan: `development.md`.

## 1. Roles are views, not just functions

- **Each agent has one "user" and a set of agents it can address.**
  - Default user view: manager → the real user; critic → the manager; worker → the critic.
  - Role view: manager → critic (worker rarely); critic → its worker(s); worker → none.
- **Never reveal the layer above.** Manager–user chat leaked into critic prompts (up to 45% of one input); the
  critic wrote "worker" in instructions.
- **All sources count as one user.** When the real user talks to the critic from the board, the critic treats it as
  its usual user.
- **The manager's job is the user's judgement**, not relaying: progress and pace against the user's goal and
  preferences. "You didn't hold the gate for me, it's your job." Some send-backs are normal alignment (7 of 13).
- **The critic exists to find the worker's problems.** Trusting reports (1,667 calls vs 1,180 planned, unexplained)
  defeats it.
- **Workers do the task, not police it.** Re-checking hashes, timestamps and cost wasted turns.

## 2. Message passing

- **A message is an activation, like a turn.** Notes waited for the next turn (98–103 min, including "machine
  restarted, resume"). Agents promised actions orch's turn order couldn't deliver.
- **Tags with start and end; validate addresses**; the protocol prompt lists whom each agent may address. The
  critic once sent instructions to itself 4 times.
- **Sent ≠ received.** Track delivery; label held messages as held.
- **Some blocks are held by design** (checklist kept from the worker). The log must say so and match what the agent
  saw ("0% delivered" for delivered text misled the auditor).
- **A question must always reach its addressee.**
- **Corrections say what they replace**; the worker got both versions.
- **Every junction (gate, retry, restart) delivers or supersedes what is queued.**

## 3. Length

- **Messages grow when agents don't write as to a person.** 10.9k manager text → 6k critic → 3.8k worker in 4 min.
  Find why; don't cap. Caps that cut instructions to 27–65% were worse.
- **Answers dwarf questions** (10–25× the critic's question). Escalating routine questions is fine; length isn't.
- **Reports need a length guide**: 803-character instruction → 7,195-character report.
- **Prompts alone don't keep models brief**; short repeated reminders help.
- **The user's style suits every agent's user**: self-contained, plain names, no internal codes, assume the reader
  forgot.

## 4. Context and memory

- **Each kind of memory has its home**: golden rules (project, all agents), each agent's preferences, decisions,
  the critic's checklist. Preferences passed down the chain lost detail.
- **Write it down.** The manager's session reset on 11-13 lost unwritten knowledge. orch's preference files ≠
  Claude Code's own memory.
- **Edit, don't re-output.** The critic rewrote plan and checklist every turn: 45–75% of its output.
- **Shared lists only grow** (decisions: 64 entries, 17.7k by milestone 4; checklists ~60 items). Someone owns
  pruning.
- **Fixed reminders every turn are waste** (555–1,010 characters per critic review).
- **New sessions start clean.** A milestone start pasted 22.9k characters of history; resume resent what the session
  had. Fresh worker sessions: handover 31k → 0.2k.
- **Keep the system prompt stable, and attribute cost correctly.** $2.11 vs $0.12 for similar manager turns was not
  a cache miss: turn cost = change in the session's running total, so board-chat turns in that session were charged
  to the next orch turn. *(checked: r204 after 21:10–21:50 chat; cache writes 1.9k tokens)*

## 5. Progress and monitoring

- **Long turns are fine if someone watches.** Worker turns ran 30–85 min; one 51 min with no signal. A watcher
  stopped silently after 10 min (Claude Code limit).
- **Flat timeouts and job waits misfire.** A turn ran the full 2 h to reply "Leftover timer"; five rounds of "still
  running"; system processes taken for worker jobs.
- **Restart points need design.** A turn started from a 17-character message after a pause or answer.
- **Work continued while a question waited for the user.**
- **Parallel by default when the user wants speed**; asked twice.
- **The board should always show what is happening now**; status was asked ~20 times.

## 6. Research behaviour

- **Diagnose before adding experiments.** "Endless experiments … find reason first." Read real outputs and logs;
  suspect the result first.
- **Look for hidden causes behind any result**, but don't over-check favourable ones without reason.
- **Gates check substance** (does the table support the claim), not cell matching. The user's spot check: 2 of 2
  tables flawed.
- **Generic rubrics fit no task**; the 5 fixed milestone rubrics were too general.

## 7. The human interface

- **The burden is understanding, not deciding.** The final gate asked for three full files and six night decisions.
- **Questions stand alone**, yet "What does the current question mean?" still happened.
- **Switches respond at once**, but not in the middle of a question.
- **Every gate needs "send back".**
- **Side channels lose agreements**: a terminal `claude --resume` doesn't reach other agents.
- **Night mode works**: 22 decisions, 19 questions settled.

## 8. Reliability

- **Retry the same model with backoff before switching.**
- **Save the final answer, not the event stream** (5.7M and 6.5M characters stored).
- **Warn, don't cap**, when sizes are out of range.
- **Cost precision matters less than progress.** Agents spent time on cost accounting.
- **Survived a machine restart** from records and sessions. One controller per run; settings hot-reload.

## 9. Auditing

- **Tell the auditor the design**, or it reports design as bugs (4 of 12 false; acceptable).
- **Check findings before acting.**
- **Timestamps** make timing claims checkable.
- **Strip orch's own reminders** before checking a reply.

## 10. Prompts, reminders, code

- **Rules broken repeatedly as prompts** (general LLM knowledge worth keeping):
  - be short with the user (asked 5+ times);
  - don't message the critic while the user is discussing;
  - restate preferences in every instruction;
  - don't reveal the layer above;
  - don't re-check trusted inputs.
- **Escalate: prompt → short reminder at intervals → code.**
- **Needed code**: checklist tag and PASS check, address check, delivery tracking, job detection, pause control,
  preference files, check-ins from the plan.

## Baseline (ICLR run)

| | value |
|---|---|
| Rounds / turns | 272 / 967 (20 interrupted, 19 stopped, 4 errors) |
| Agent spend (orch meter) | $1,596, of which $552 estimated |
| Experiment spend | $122.52 |
| Decisions | 182: user 108, manager 52, night mode 22 |
| Questions | 170: critic → manager 66, worker → critic 65, manager → user 39 |
| Milestones sent back after manager accepted | 7 / 13 |
| User chat messages | 241 |
