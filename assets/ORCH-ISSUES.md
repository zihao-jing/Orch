# Orch usage issues

Updated: 2026-11-25. Discussion record; agreed fixes are not yet implemented. The organized requirements, design and work plan are in `docs/redesign.md`.

## 1. Messages do not reliably activate workers

- **Issue:** Assignments appeared sent, but worker-2 stayed blocked. Malformed tags and a stuck urgent message both caused missed handoffs.
- **Agreed fix:** The critic messages workers with one label, checked by Orch; a broken label is returned once, and an unknown or closed worker is rejected with the available IDs. Messages are sent at once. Message status (waiting, delivered, answered, failed) is kept separate from agent status, and an unanswered message is checked after 5 minutes and sent again once only if it did not arrive. See *Agreed immediate messaging* and *Agent status board*.
- **Your comments:**

## 2. Dashboard status does not clearly reflect execution

- **Issue:** Writing indicators persisted, resumed work appeared under old steps, and background-job waits were unclear.
- **Agreed fix:** The status board uses the CLI's own events: idle, writing, waiting for job, failed or closed, with each agent's jobs, last activity and current assignment. Messages appear in time order and steps are shown only in a progress panel. See *Agent status board* and *Agreed step management*.
- **Your comments:**

## 3. Workers can bypass the intended review loop

- **Issue:** Batch execution and internal iteration drifted from the required rollout → evidence-based rubric → critic review process.
- **Agreed fix:** Finished work always goes to the critic. The critic's prompt tells it to assign one query at a time and to keep its checklist strictly; nothing blocks a worker that does several in one turn. See *Agreed worker review*.
- **Your comments:**

## 4. Messaging reminders should match the current event

- **Issue:** Generic routing instructions leave the critic to infer recipients and syntax.
- **Agreed fix:** Every critic reminder shows each worker's one-line status, the available IDs and exact label examples, including `<silent/>`, and never a manager address. See *Agreed critic protocol redesign* and *Agreed critic silence*.
- **Your comments:**

## 5. Worker identity and lifecycle need explicit controls

- **Issue:** Retired workers and stale session mappings make routing and recovery confusing.
- **Agreed fix:** A backend registry keeps each worker's identity, session, status and current work. Only the user adds, closes, reopens or deletes workers on the dashboard; history is kept, and closing or deleting a worker with a running job asks the user first. See *Agreed critic protocol redesign* and *Agent status board*.
- **Your comments:**

## Agreed critic protocol redesign

Planning record for one coordinated update; no runtime changes made yet.

1. **Two critic labels:** `<message to="worker-2">…</message>` sends instructions or answers to a worker; `<silent/>` keeps the rest of the reply from the manager (see *Agreed critic silence*). No separate step-assignment tag.
2. **Upward communication:** Untagged critic text goes to the hidden manager. Questions and blockers use ordinary prose; the manager replies or escalates to the user.
3. **Remove critic controls:** Remove `<question>`, `<pause>`, `<wait-job>`, `<verdict>`, `<workers>`, and `<to worker="…" step="…">` from its protocol. Worker `<wait-job>` is removed too.
4. **Checklist-based completion:** Prompt the critic to maintain its checklist strictly. The system automatically passes a step when its checklist is complete; no verdict tag or manager approval for that step. Steps are defined in *Agreed step management*.
5. **User-controlled worker count:** No default on-demand allowance. Only the user sets or changes the number of workers through the dashboard. The critic messages available workers using the same message format; it cannot increase the worker count or create workers beyond the user-configured pool.
6. **Dashboard control:** Only the user can manage workers on the dashboard: add a new one, close, reopen an old one, or delete, and change the pool size. A closed worker can be reopened by the user only. Preserve closed-worker history and prevent accidental revival by late messages.
7. **Backend registry:** Map stable worker identities to sessions, lifecycle states, and current work. Validate recipients and record delivery acknowledgments without duplicates.
8. **Every-turn reminders:** Show relevant workers' statuses in one line each, plus exact message-tag examples. Include available IDs from the user-configured pool; never expose a manager address to the critic.
9. **Job lifecycle:** Remove model-written waiting tags. The replacement is defined in *Agreed worker job tracking* below.

## Agreed critic silence

- **Label:** `<silent/>` means there is nothing to bother the manager or the user with. A reply that contains it is silent; it does not have to be the whole reply.
- **Prompts:** Clearly describe how to use it in both the critic's system prompt and its reminders: use it when there is nothing to bother the manager or the user with, for example when guiding a worker, responding to a worker, or when nothing is needed while a job runs. Do not use it when the reply has a question, blocker or decision for the manager or the user. Reminders refer to this without exposing a manager address.
- **Example:** `<message to="worker-2">Use the validation split, then continue.</message> <silent/>`
- **With worker messages:** A reply can contain both labels: worker messages are still delivered and only the untagged text is kept from the manager. Silenced text stays visible on the dashboard.
- **All silent:** If the critic is silent and nothing else is active (no worker writing, no running job, no waiting message, no pending user decision), and the system is not paused, Orch asks the manager to check. If everything is still silent after three tries, Orch pauses, which opens a pause gate for the user. The count restarts once work restarts.
- **Not silence:** The check must not fire while a message has not yet reached its recipient (manager, critic or worker), or while the recipient has received it but has not started answering. A job that just ended and has not yet woken its agent counts the same way.
- **Message never answered:** It must not block the check forever. If the recipient has not started answering within the tolerance, Orch checks whether the message arrived and sends it again once if not; if it still has not started, Orch treats the agent as failed and uses the retry rule (case 31). The tolerance is 5 minutes.

### Agent status board

- **Agreed:** The system keeps a board of each agent's status and its waiting jobs. The silence check reads this board.
- **Separate statuses:** Message status and agent status are separate.
- **Message:** waiting (not yet with the recipient, including held for gates), delivered, answered (the recipient has started answering), failed.
- **No answer needed:** Some messages need no answer, such as messages to the user. They end at delivered and never count against silence.
- **Agent:** idle, writing, waiting for job, failed, closed. Each waiting job is listed under its agent.
- **Reopen and delete:** Reopening continues the worker's saved session with its history. Deleting removes the worker from the list for good, so it cannot be reopened, but its past messages stay in the record.
- **Silence:** No message that needs an answer is waiting or unanswered, and no agent is writing or waiting for a job. The other conditions in *All silent* and case 5 also apply.
- **Signals for active:** Codex app-server sends turn-started and active/idle events; OpenCode reports delivery and lists active sessions; Claude can echo a received message (`--replay-user-messages`), and any output shows it is writing, until its result event. Qoder is expected to be similar; its receipt echo is unchecked. The process being alive is only a backup signal, since it stays open when idle.
- **Measured on both projects' past runs (468 turns, one process per turn):** time from a turn's first log entry to its first output was typically 5 to 13 seconds; 2 turns, both a worker's, took over 2 minutes and the longest took 187 seconds. The longest quiet period inside a turn was 3.6 minutes. Receipt latency in the new always-open mode is not measured.

## Agreed step management

Steps track progress; they do not route messages.

- **Ownership:** The critic maintains steps and their checklist items in one Markdown file.
- **Completion:** The system reads the checklist and automatically passes a step when its items are complete.
- **Display:** Show steps in a progress panel at the top; do not divide messages into step blocks.
- **Conversations:** Order messages chronologically, showing sender, recipient, timestamp, and delivery status.
- **Independence:** Neither messages nor worker assignments require a step association. Steps do not control routing or execution.
- **Traceability:** Optional links from checklist items to relevant messages may provide evidence without imposing workflow rules.
- **Context:** Step boundaries do not imply compaction; session/context management is separate.

## Agreed manager protocol

- **Critic messages:** Keep `<message to="critic">`. Remind the manager during coordination, not discussion mode.
- **Questions and pauses:** Keep `<question>` and `<pause>` as separate controls; see *Agreed question gates and pauses*.
- **Milestone review:** Retain the current rubric and score mechanisms.
- **Working state:** Retain `<state>` and its maintained state file.
- **Worker count:** Remove the manager's `<workers>` tag/control. Only the user changes worker count through the dashboard.
- **Agent preferences:** Remove per-agent preference Markdown files and the system-prompt instructions/reminders to maintain them. Preferences remain in each agent's session; the shared golden board and manager state file remain separate mechanisms.
- **Direct worker messages:** Allowed, but never prompt the manager to use them.
- **Summary:** Keep `<summary>`; it is harmless.
- **Discussion mode:** Only for the manager and critic. The agent under discussion communicates only with the user and sends nothing to other agents. It acts only after the user applies the discussion, with one consolidated message per recipient. This prevents sending the critic a message in every discussion round, or several at once afterwards.
- **Milestone pause:** `<decision>PAUSE</decision>` works like `<pause>`, including its pause gate, set after the manager reviews the milestone.
- **Your comments:**

## Agreed question gates and pauses

- **Question:** `<question>…</question>` opens a dashboard gate with selectable options. Remove the blocking/nonblocking attribute distinction from the manager's protocol. By itself a question does not pause agents or jobs already running, and opening the gate does not depend on their status.
- **Routing:** A manager reply containing a question forwards nothing from that reply to the critic, even if it also contains a critic message.
- **Answer:** The user's answer returns to the manager, which can then issue a new critic message based on that answer.
- **Several gates:** When several questions or gates are open, the user selects an option or enters text for each, and Orch records the answers. Work waiting on them continues only when the user has closed all gates, with all answers sent to the manager in one message. This prevents repeated messages to the manager.
- **Pause:** Keep `<pause>…</pause>` so the manager can explicitly set the system's execution status to paused. Every pause opens a pause gate, so the system is never paused without an open gate. A pause gate is separate from a question gate and does not substitute for it.
- **Effect of pause:** Running agents and jobs continue. Pause changes one thing: when everything is silent, that state is allowed and Orch does not ask the manager to check.
- **Resuming:** A pause ends when the user closes all open gates, including the pause gate.
- **Together:** A reply may contain both tags: open the question gate and set paused status. The pause is still processed although nothing is forwarded to the critic.
- **No critic pause proposal:** Remove critic-proposed pause handling, including the manager's PAUSE/CONTINUE response path.

## Agreed worker question handling

- **Reminder:** If an answer is required to proceed, ask the critic in plain text and end the turn. Do not guess or continue dependent work.
- **Delivery:** The system passes the completed reply to the critic. Its worker message answers the question and starts the next turn in the same session.
- **No question tag:** No worker-written question control tag is required. Independent work may continue when an answer is not required for it.
- **Job waiting:** See *Agreed worker job tracking*.

## Agreed worker review

- **Routing:** When a worker finishes with no job running, its work always goes to the critic for review.
- **One query at a time:** The critic's prompt tells it to assign one query at a time, so each is reviewed before the next. Prompt only; the system does not block a worker that does several within one turn.

## Agreed worker job tracking

Discussed 2026-11-25; not yet implemented. Replaces the earlier Orch-owned supervisor proposal.

- **No separate tool:** Workers start long jobs with their CLI's own background option. Orch reads the CLI's job record instead of scanning processes.
- **Replies during a job:** Orch passes them to the critic at once, marked "job still running, not ready for review". The critic may message the worker, whose job keeps running, or reply `<silent/>`. It does not check off items that depend on the job until it ends.
- **Messages during a job:** Orch can send a worker a message while its job runs. Tested on Claude and Qoder with input kept open: both answered while the job ran and still woke when it ended, with the message sent from a different process than the launcher. Codex and OpenCode untested for this case.
- **No `nohup` or similar:** Orch rejects them, because these jobs may be missing from the CLI's record. How each CLI enforces this is untested.
- **Old detector:** Remove it; the new job tracking replaces it.
- **Two kinds of ending:** When an agent ends its turn, Orch checks the job record. *Intermediate:* a job is still running; the worker shows as waiting and will wake again. *Final:* nothing is running; the work is ready for critic review. Orch does not tell these apart today.
- **Self wakeup:** When a job ends, the CLI wakes its own session and Orch catches the response.
- **Wakeup fallback:** If a job ended but no new turn starts within about one minute, Orch messages the same session ("your job finished, continue"), or resumes the saved session if the process has exited. It does this once per job, after checking that no new turn has started, and shows it on the dashboard.
- **Unreported endings:** The monitor's regular check covers a job whose end is never reported.
- **Restart:** Agents and their jobs keep running when Orch closes or restarts. Orch writes each agent's output to a file, leaves agents running when it closes, and on reopening reconnects and reads what it missed. Messages sent while Orch is closed wait until it reopens.
- **Stopping agents:** Closing Orch no longer stops agents; a separate stop-agents button does.
- **Limit:** Agents and jobs still stop if the computer shuts down.
- **Tested on Claude:** Today's stop kills the agent and its job; on resume the agent is told the job did not finish, and the job does not restart by itself. With output going to a file and the launcher closed, the agent and job kept running, the agent woke itself, and a new process read the full output.

### CLI support (checked 2026-11-25)

| CLI | Job record | Wakes itself | Checked by |
|---|---|---|---|
| Claude 2.1.332 | Stream events with status and exit code | Yes | Live, successful job |
| Qoder 1.1.97 | Stream events with status and exit code | Yes | Live, successful job |
| OpenCode 2.3.2 | Job list API (experimental) | Yes, with exit code | Live, job with exit code 3 |
| Codex 0.171.0 | App-server job list (experimental) and exit code | Not tested | Live, job with exit code 3 |

### Still open

- **Restart on other CLIs:** Qoder should work like Claude. Codex and OpenCode can run as servers Orch could reconnect to. None of the three is tested.
- **Codex:** Orch closes the app-server process when a turn ends; it must stay open while a job runs. Test self wakeup.
- **To test:** Failed jobs on Claude and Qoder, several jobs at once, jobs lasting hours (Claude documents a time limit for background commands), and other backends.
- **Your comments:**

## Agreed immediate messaging

- **No queue:** Each agent's CLI stays open and Orch sends every message at once, including while the agent is working. Remove today's waiting for the next turn, replacement of waiting messages, and the `additive` and `urgent` options.
- **Critic busy:** When a second worker finishes while the critic is working on another, send its result at once too; each message names its worker.
- **Only exceptions:** Orch keeps a message while the user's gate answers are incomplete, a terminal is open on the agent's session, when Orch is closed, or when a send fails. In a discussion, what the agent sends others is a draft until it is applied; what it receives is not held.
- **Record:** Every message and its delivery status are still recorded.
- **Depends on a test:** Claude and Qoder accepted a message while waiting for a job; messages while they are writing are untested. Orch already has code for this on Codex app-server and OpenCode.
- **Keeping the CLI open:** Orch starts each agent's CLI once, in a mode that stays open, and sends messages into that process: `--input-format stream-json` for Claude and Qoder, `codex app-server` for Codex, `opencode serve` for OpenCode. Orch's code must change to keep the process open and reconnect to it; today it starts a new process for each turn.
- **Codex:** Always use app-server mode. Today's default is `codex exec`, which both current projects' critics use and which cannot take messages while running.
- **`command` programs:** They cannot take messages while running, so messages wait until the current turn ends. This is the only remaining queue.
- **Qwen and Antigravity:** Their help lists a `stream-json` input mode that accepts several messages; untested.

## Agreed monitor rule

- **Healthy worker:** The monitor sends nothing to the critic when a worker's status is good. Results stay on the dashboard.
- **Unhealthy worker:** Quiet, off-track or stopped checks go to the critic.
- **Change needed:** Today the critic is also woken on every second healthy check.

## Communication map

Summary of the agreed rules.

| From | To | How |
|---|---|---|
| User | Manager | Chat, gate answers (all together once every gate is closed) |
| Manager | User | Untagged text, `<summary>`, `<question>` gate, `<pause>` gate |
| Manager | Critic | `<message to="critic">`; nothing if the same reply has a question |
| Manager | Worker | Allowed, never prompted |
| Critic | Manager (hidden) | Untagged text, which wakes the manager at once; nothing if the reply has `<silent/>` |
| Critic | Worker | `<message to="worker-2">`, also while its job runs |
| Worker | Critic | Whole reply: final (ready for review) or intermediate (job running) |
| Monitor | Critic | Only when a worker is unhealthy |
| Orch | Worker | Wakeup fallback after a job ends |
| Orch | Manager | Check request when everything is silent and the system is not paused |
| Manager or critic in discussion | User only | Others receive nothing until the user applies it |

## Case handling

Agreed on 2026-11-25.

### Conflicts and loops

1. **Milestone end:** Verdicts are gone and steps do not control execution, so nothing starts the milestone review; a `<silent/>` final reply could hide it too. → See *When the manager reviews a milestone*. A silent reply does not hide it, and running jobs do not block it but are shown to the manager.
2. **Never-ending job:** A server never ends, so the worker never reaches a final ending and the all-silent rule never fires. → The critic may tick items that do not depend on it. The monitor reports a long job with no progress to the critic.
3. **Manager wakeup:** Untagged critic text wakes the manager at once, unless the reply has `<silent/>`, in which case the manager is not woken.
4. **Endless exchange:** No exchange limit. The critic ends an exchange with `<silent/>` and the manager by sending no message; the manager's prompt tells it not to send messages that only acknowledge. Instead of step or milestone round limits, use one very large safety limit of 5000 rounds in total across the critic and workers, since there is no fixed workflow to count. One round is one worker reply delivered to the critic, and reaching the limit opens a pause gate.
5. **All-silent definition:** Also count as active: the manager or critic writing, a discussion, any open gate, an agent retrying or waiting for a usage limit, and agents stopped by the user. The check applies whenever nothing is active, whether or not the critic used `<silent/>`.

### Gates, pauses and reviews

6. **Open gates:** May a later manager reply without a question still message the critic? → Yes, when it is unrelated to the open question. The manager is reminded of open gates and does not ask the same question again.
7. **Question plus worker message:** → Hold direct worker messages too, not only critic messages.
8. **Pause plus critic message:** → Deliver the message, since a pause only allows silence.
9. **Question in a milestone review:** → Hold ACCEPT or REVISE until all gates are closed, then the manager decides again. After a milestone pause the review also runs again.
10. **Which gates count:** → Plan and milestone approvals count as gates too.
11. **User chat while gates are open:** → Goes to the manager as normal; it does not close a gate.

### Delivery

12. **Malformed label:** A broken worker message would go to the manager as plain text. → Return it to the critic once to fix; if it fails again, send it to the manager marked as broken.
13. **Unknown or closed worker:** → Reject, and tell the sender which workers are available. With no workers, the manager asks the user.
14. **Busy recipient:** → Send at once; the recipient's CLI takes the message in while it works. See *Agreed immediate messaging*.
15. **Replacing and urgent messages:** → Remove `additive` and `urgent`; every message is sent at once and nothing waits to be replaced.
16. **Unanswered worker:** A question is missed while other workers keep the system active. → Each critic reminder shows which workers are idle and since when.
17. **Direct message to a worker:** The critic does not know what the manager or user told a worker. → Show it to the critic as its own user's instruction.
18. **Discussion:** Messages arriving for that agent, or its job ending, during a discussion. → Delivered into the discussion at once, not held; its replies go only to the user until the discussion is applied. Ending without applying drops drafts, not incoming messages.

### Failures and lifecycle

19. **Agent failure:** → Retry, then tell the critic that the worker failed (not as work to review). If the critic or manager fails, open a pause gate.
20. **Lost job:** A job ends without a result after a crash, reboot or stop. → Mark it lost and wake the worker to check, unless the user pressed stop.
21. **Closing or deleting a worker with a job:** → Ask the user whether to let it finish or stop it. Late results are recorded but not routed.
22. **Adding or reopening a worker:** → Shown in the critic's next reminder; it does not wake the critic.
23. **Reopening Orch:** → Process collected replies in order; merge outdated intermediate ones into the latest.
24. **Monitor failure:** → Show it on the dashboard; after two failures, tell the critic that nobody is watching.
25. **Missing or unreadable checklist:** → Treat as not complete and ask the critic to fix it.
26. **Repeated unhealthy checks:** → Tell the critic once per change of status, then at most hourly.

### Forgetful models: dead ends and loops

Covered by the agreed rules: a forgotten or wrong label, an unticked checklist, a plain-text question without a gate, or a manager that does nothing after a check all end quiet, so the all-silent rule brings in the manager and then the user. The auditor only reads messages and never sends any, so it adds no case.

Agreed on 2026-11-25:

27. **Frozen manager or critic:** If either produces no output for 40 minutes while marked writing, Orch restarts it in the same session once, then opens a pause gate. This does not apply to the monitor.
28. **Busy without progress:** No extra rule; the 5000-round limit stays.
29. **Repeated rescue:** No extra rule beyond the three tries in *All silent*, which cover work that does not restart. The manager works out the problem and gets the system going again; if it decides to pause, the gate opens automatically.
30. **Repeated milestone review:** No hard rule. See *When the manager reviews a milestone*.
31. **Repeated crash:** Up to 5 tries for API errors, using the existing retry logic, then backup models if set, and otherwise a message to the user. A setup problem (such as a missing CLI) or billing problem is not retried; it goes straight to a backup that can avoid it (see *Recovery review*). Today's default is 3 waits (1, 2 and 4 minutes) before backups, and missing-CLI and billing errors already stop at once.
32. **Orch itself stops:** No automatic restart.

### Manager prompt: when everything is silent

- When Orch reports silence, the manager should work out the cause and message the critic to continue the pipeline. It should not simply ask the user. It asks the user or pauses only when the work really needs the user.

### When the manager reviews a milestone

- **Agreed:** The manager reviews a milestone only when it should. A critic message that is a question or anything else is an ordinary message, not a milestone report.
- **Today:** Ordinary critic messages do not start a review, but two cases do wrongly: once the checklist is complete, whatever the critic wrote in that turn becomes the report, even a question; and the 50-round no-progress limit starts a full review of unfinished work.
- **Trigger:** When the checklist becomes complete, and no worker is writing and no message is waiting, Orch tells the manager once and its prompt lets it choose: review the milestone, or first answer the critic. Orch tells it again only after the checklist changes and is complete again. Remove the no-progress review.
- **After REVISE:** The manager's message tells the critic what to fix and to update its checklist (untick or add items), without mentioning milestones or the review. The critic's prompt says the same for any requested change. This prevents repeated review triggers.

### Prompt conflict found

- The worker prompt currently tells workers to use `nohup … &`. It must change to the CLI's background option.
- **Your comments:**

## Agreed dashboard model selection

Discussed 2026-11-25; not yet implemented.

- **Window:** A dashboard window lets the user select each agent's backend, provider (for OpenCode), model and reasoning effort from a configured list.
- **Model list:** Kept in one file at the root of `.orch` (`.orch/settings.yaml`, which Orch already supports), edited by hand because it can hold API keys. Keys are never shown on the dashboard.
- **Saving:** A choice is saved in the agent's `agent.yaml`, whose changes already apply on the agent's next turn. Keep the file's other settings and comments.
- **New projects:** They start with the model list in `.orch/settings.yaml`, and the agents refer to it.
- **When to switch:** When the agent is idle with no running job; show "switch pending" until then. The user can force an immediate switch, for example when the original model failed.
- **Different backend:** When the session cannot continue, warn that a new conversation starts, with a handover (below).
- **Effort:** List the levels from the correct interface for each CLI; hide the choice when there are none.
    - Codex: app-server `model/list` gives each model's levels (checked live).
    - OpenCode: its model API has variants, chosen as `provider/model#variant`.
    - Claude and Antigravity: fixed levels in their help (low to max).
    - Qoder: `--reasoning-effort`; levels and source not yet found.
    - Qwen: no effort option in its help.
- **Same backend:** When the new model can continue the old session, the history stays and no handover is needed. The handover below applies only when it cannot, such as a different backend.
    - Claude with a different API address (`*_BASE_URL`) starts a new session, with the handover.
    - OpenCode and Qoder: the model is chosen inside the CLI, so switching is expected to keep the session (OpenCode's API has a switch-session-model call). Not tested live, and past runs have no example.
- **Session cannot continue after a switch:** If Orch detects this, it starts a new session with the handover. No such handling was found in today's code.
    - **Detection:** Check it on the first message after a switch: the CLI rejects the old session or its history, or it returns a different session ID without an error. Rate limits, overload, network, billing and missing-CLI errors are not treated as this and follow the retry rule.
    - **No loop:** Try a new session once per switch. If the new session also fails, the model itself is treated as failed and follows the retry rule.
- **Handover:** On a normal switch the old model writes a short handover before it closes. If it cannot and the switch needs a new session, Orch tells the critic that the worker's model is changing, and the critic writes the new model a short instruction.
    - The critic's instruction replaces only the critic's own unfinished message, so the worker does not get the task twice; messages from others are sent.
    - If the critic's own model fails, the manager writes the instruction for the new critic.
    - The one who writes it is prompted to read the current progress first (for example the checklist, files and latest report), then write.
    - If the manager's model fails, the new manager gets the system prompt and a pointer to the previous session's history (the turns Orch saved, newest first), so it learns its role, how to collaborate and the progress. Nothing else is copied.
    - If the one who should write it does not answer, Orch opens a gate to warn the user (so it counts as a pause and the manager is not prompted), and the user replaces models from the upper level down. No copied text is used.
- **Forced switch:** Smoke-test the new choice first; if it fails, stop with a warning. Warn that the switch stops the agent's current work and any running job, then send the unfinished message again to the new model, except where an upper-level instruction replaces it (see *Handover*).
- **Existing projects:** Create their `.orch/settings.yaml` from the models already in their `agent.yaml` files; keep the old files until the user confirms.
- **No hidden override:** Settings in `agent.yaml` apply on top of the chosen model, so a choice replaces the old model and effort lines.
- **To test:** Switching OpenCode providers and Qoder models within one session; each CLI's error when a session cannot continue; where Qoder lists effort levels; OpenCode's variant list.

## Recovery review: model switching and restart

Reviewed 2026-11-25 against the code and the agreed rules.

Found in today's code:

- **Wrong session after a restart:** Orch checks the cut-off session against the agent's main backend, not the one it was using. Reproduced with a worker on an OpenCode backup: Orch reports its session as lost and does not mark the message as already received, so the task can be sent twice, although the saved session is still there. Fix: use the saved session's own backend and model.
- **A chosen model is ignored while on a backup:** For up to 5 hours after a backup worked, turns start on the backup by its position in the list, and reloading the settings does not clear it. Fix: the user's choice clears it, and backups are remembered by name.
- **One saved session per agent:** Moving to a backup on another backend overwrites the main model's session, so each move back starts another new conversation.
- **One agent's billing or missing-CLI problem stops the whole run** (a billing problem even when backups are set).
- Already handled: only one Orch can control a run, and the state file is saved safely.

Agreed rules:

1. **Failure while a job runs:** Retry in the open CLI without restarting it. Do not switch automatically to a different backend until the job ends, since closing the CLI would kill the job. The user can still force it.
2. **Going back to the main model:** Automatic only when the session continues; otherwise stay on the backup until the user switches. Going back follows the same logic as any switch and first tries to keep the session. The model window also shows each agent's backups and lets the user switch to them and back.
3. **All models fail:** Open a gate, test all models set for that agent every 30 minutes, and close the gate when one works and is switched to.
4. **What a model choice is:** A combination of backend, provider, model and effort. A billing problem or missing CLI is not retried on the same choice; it goes straight to a backup that can avoid it (another provider or CLI), and only with none does that agent stop and a gate open. Other agents keep running.
5. **Session at reconnect:** After a restart or CLI update, always try the old session with the same choice first. A new session starts only if the CLI rejects or loses the old one.
6. **Unanswered messages at a switch:** Messages sent to a failed agent are sent after the switch, again only if they may not have reached its current session.
7. **Smoke test:** Before switching, and in the 30-minute checks, send a tiny request with the new choice in a separate temporary session, never the agent's own. If it fails, do not switch and show why. A forced switch is tested too.

Also recorded as robustness fixes:

- **Finding the right session:** Save each agent's session ID, backend, model, process (ID and start time), output file and last-read position. Save that a message is being sent before sending it, each with its own ID; after a restart, check whether it arrived before sending it again. Never open a second CLI on a session that is still open.
- **Restart in the middle of a switch:** Save each step of a switch, keep the old session until the new one has answered, and continue from the last step.
- **Actual model:** Show the model the CLI reports using, not only the chosen one.

Further agreed rules:

- **Reopening an old session:** When an agent returns to a backend it used before, reopen that old session and tell it what happened since, instead of starting another new conversation. Each agent then keeps one saved session per backend and provider where they cannot share one.
- **Upper-level instruction and unanswered messages:** On a new session, the upper-level instruction replaces only that level's own unanswered message; everyone else's are sent.

## Parallel workers

Discussed 2026-11-25.

- **Today:** A temporary mode. The critic proposes it and the manager or user approves; copies of the worker get their own session, monitor and output folder, and a work board shows who does what. It ends with one worker again. No past run has used it. Its 21 tests pass.
- **Weak points today:** If one worker's turn ends in an error after its retries and backups, the whole run stops (`stop_on_error` is on by default). Workers share one folder unless `worker_worktrees` is set, and separate output folders are only requested by prompt. Reviews run one at a time.
- **Already agreed and replacing this mode:** A user-controlled worker pool, every-turn critic reminders with each worker's status, IDs and label examples, immediate messages, and, if a worker fails, the critic is told (case 19).

Agreed rules:

1. **When to use several workers:** The critic's prompt and reminder say how many workers are available, and to split work only when the parts do not depend on each other and can run in parallel. Using one is fine. Nothing more about parallel work is asked of the critic.
2. **Reminder:** Orch also shows each worker's current assignment, and whether its report still awaits the critic's reply.
3. **Worker failure:** Failure means a worker still fails after its retries and model switch. The others continue. The critic may hand the work to an idle worker, after checking what the failed one left.
4. **Files:** No hard prevention and no warnings. Workers may share files if they have to; a worker may use a separate development tree (git worktree) if necessary.
5. **Shared limits:** No shared-limits note; the critic does not keep one.

Further agreed rules:

- **Separate tree:** The worker's prompt says that files may change while it works, not to undo changes it did not make, and to use a separate tree if its work would clash. It merges its own work back and removes the tree; the critic does not. Nothing in it names other workers, as workers do not know about each other.
- **Old parallel mode:** Remove its separate-tree setting, work board and assignment tag along with the mode.

## Discussion and simultaneous messages

Discussed 2026-11-25.

Agreed:

- **One conversation per agent:** Each agent has a single conversation. Messages arriving together enter it in order; the labels in a reply decide who receives which part, so one reply can answer several workers.
- **Several workers to the critic:** Sent at once, each naming its worker. Today they are reviewed one at a time. The reminder shows any report still awaiting the critic's reply.
- **Messages during a discussion:** Delivered into the agent's conversation at once, not held, for the manager and critic. Discussion mode covers only these two; a chat with a worker or another agent is an instant conversation (see *Terminal command*). The agent replies only to the user, so nothing leaves until the discussion is applied. Applying then covers what arrived, so no separate rule is needed for it. This replaces the earlier rule of holding them.
- **Job ending during a chat with a worker:** Handled as usual, since a worker has no discussion mode. This replaces the earlier rule that kept it with the user.

Further agreed rules:

- **Starting a discussion while the agent is writing:** What it is writing to other agents becomes a draft and is not sent.
- **`<silent/>` with several workers:** It hides all untagged text in a reply from the manager, so it could hide a problem the critic passes up for one worker. The critic's prompt says to use it only when nothing in the reply needs the manager.

### Terminal command

- **Agreed:** The discussion panel has a command the user can copy into a terminal to talk to that agent's session in its CLI.
- **Today:** `orch terminal` prints a command, but always `claude --resume`, even for a Codex or OpenCode agent, and without the agent's system prompt, model or permission settings. Routing the replies needs a hook that works only for Claude.
- **Found in the CLIs' help (attaching untested):** Codex can connect a terminal to an app-server (`codex resume --remote`) and OpenCode to a server (`opencode --server … --session …`), so the terminal can share the open session. Claude's `attach` covers sessions started with `--bg`; none was found for a process Orch keeps open in `stream-json` mode, and none for a local Qoder session.

Agreed:

- **One command:** The panel shows one short Orch command (such as `orch terminal <project> <agent>`) that opens the right CLI with the agent's model, effort, system prompt and settings.
- **Manager and critic:** Opening it starts a discussion, so nothing reaches other agents until the user applies it on the dashboard. Orch reads the session for what was said; no hook is needed.
- **Workers and other agents:** No discussion mode. It is an instant conversation: the agent continues its own work and can still send messages.
- **While a terminal is open:** Messages to that agent wait until it closes, for all CLIs. This is an exception to immediate delivery. Where the terminal can connect to the open session (Codex, OpenCode), Orch need not close its copy; otherwise Orch closes it and reopens it afterwards, since two CLIs must not use one session.
- **Running job:** Where opening the terminal would end the agent's job, Orch asks first.
- **Critic discussion:** Nothing the critic writes in it goes to the manager either; its untagged text goes only to the user, and worker messages are drafts.
- **Forgotten terminal:** The dashboard shows that a terminal is open and how many messages wait, with a button to take the session back. The agent counts as active, so the silence check does not fire.
- **Worker's replies in a chat:** No label for workers. Orch decides by what started the reply: one started by the critic, a job or other work goes to the critic as usual (also when the user wrote while the worker was working); one started only by the user's message goes to the user, with a *Send to critic* button. The critic sees the user's message as its own user's instruction (case 17), so it can ask about the work.
- **Applying a critic discussion:** The critic's one consolidated reply then follows the normal rules: labelled parts go to workers and untagged text goes to the manager, so the manager learns what changed.

## Changing milestones during a run

Discussed 2026-11-25.

- **Agreed idea:** Guided by the user, the manager can modify the current and later milestones, redesign from the current one to the last, or add milestones after the last one is finished.
- **Today:** The milestone list is written once, in the plan; after it is approved only a finished milestone can be reopened. After the last one, the run is completed and cannot be resumed. Milestones and their checklist files are numbered by position, so inserting or removing one would mix them up.

Agreed:

1. **Who starts it:** Only the user, by asking the manager to propose a change, which the user then approves and applies. The manager's normal prompt does not say that milestones can be changed, and it must not use the change format until the user asks.
2. **Approval:** One gate shows the old and new list side by side; nothing changes before it. Applying a discussion that changes milestones opens the same gate. The content of earlier milestones must be handled correctly: no milestone is deleted, and each has its own ID.
3. **Limits:** Finished milestones stay as they are (they can be reopened); the current and later ones can change.
4. **No forced message:** Changing milestones sends nothing to the critic. The manager messages the critic separately, as it decides.
5. **Review in progress:** An open review or approval gate for the changed milestone is withdrawn, and its rubric is written again.
6. **After the last one:** The run stays open as finished, with sessions kept and no silence check. The user can ask the manager for more milestones. Completed old runs can be reopened the same way.
7. **Night mode:** No special rule, since only the user starts a change.

Today the list is kept in the run's `state.json` (not a Markdown file); the plan text is also kept there as the manager wrote it.

Further agreed rules:

- **Format:** The existing numbered list, with each milestone's ID; no new format. Only the manager's reply to a change request is read as a change. The request comes from a *Change milestones* button, which adds the current list with IDs to that one message.
- **Editing the current milestone:** The milestone and its rubric (for the manager) are rewritten in place under the same ID. The critic's checklist is not touched, and no start message is sent.
- **Removed later milestones:** Marked dropped, with their ID and text kept; IDs are not reused.
- **Added after the last one:** When approved, the new milestone starts like any other, with the manager's usual first message to the critic.
- **Storage:** `state.json` stays the only source; no Markdown copy.

## Clean and short prompts

Discussed 2026-11-25. Measured on the two past runs (494 turns, characters sent each turn, without the system prompt).

- **Several tags:** 83 turns had two or more `<system-reminder>` tags, at most 4 in one. Each reminder is wrapped on its own (`orch/reminders.py`).
- **What cost context (already fixed on main):** Before commit `eea8d25` the board was sent in full every 10 turns: in NIW-orch, full boards were 42% of worker input and 18% of critic input. After the first turn using that commit's board format (round 64, 2026-11-24) there were none in 42 worker and 49 critic turns. Current code gives the manager a path, the critic the full text in a new or compacted session and then changes, and the worker only the board's `## Boundaries` section. The earlier statement that full resends still happen was wrong. Other reminders are only 1–2% of input over all measured turns.
- **Contradiction:** A reminder tells the critic that text outside tags in a review goes to the worker; its protocol prompt says it goes to the manager.
- **Repeated rules:** "Be brief" appears in the role prompts, the protocol prompts, the style rules and the `concise` reminder. The label rules are in both the role prompt and the protocol prompt. System prompts in the last turns are about 11,000–13,000 characters for the manager and critic.

Agreed:

1. **One reminder tag:** All reminders of a message go into one tag at its end, one short line each, without duplicates.
2. **Golden board:** Not sent to workers; workers do not know it exists. When the manager or critic assigns work, it picks the rules that relate to the job and writes them into the instruction, instead of passing on the whole board. Remaining change: stop sending workers the `## Boundaries` section too, and replace the critic's pasted copy (see below).
3. **Critic's every-turn reminder:** One line per worker (status, assignment, unanswered report) and one line of label examples including `<silent/>`.
4. **Monitor:** Send the step's checklist once per session, then only items that changed.
5. **Frozen worker:** The 40-minute rule of case 27 covers workers too, but a worker waiting for a job or background terminal, or in a tool call, is not frozen. Frozen → restart in the same session once, then treat as a failed worker (retry, switch, tell the critic). Today only a high per-turn limit stops one (8 hours in the local NIW-orch project).
6. **How the critic gets the golden board:** Like the manager today: Orch gives the file's path when a session is new or compacted, or when the board changed, and the critic reads it when it assigns work. The critic only reads it; the manager maintains it. The current check that keeps manager-only text from reaching the critic must also cover the file.
7. **Using the golden board:** The manager's and critic's system prompt, or a reminder, tells them to use the board's related rules when instructing the current task.
8. **No limits on the worker's file reading:** Orch does not mention the golden board to the worker, but the worker explores files freely; nothing blocks it from opening any file.
9. **One place per rule:** During development, first organize all requirements and the new design; then rewrite each system prompt from the new design. Each rule stays in one place (role prompt = what the agent does; protocol = labels; reminders = only what is easily forgotten), and contradictions are removed.
10. **Writing rules:** Several writing rules may stay, inside the single reminder tag; duplicate "be brief" lines are reduced to one.
11. **Checking against the board:** Without the `## Boundaries` section a worker knows a rule only if the critic included it. It is the critic's duty to also check finished work against the board's related rules.

### Review of all prompt templates

Every file in `orch/prompts/` and every template in `orch/templates.py` was read (2026-11-25), and the last 99 turns of NIW-orch were measured (rounds 64–106). These messages are already short: the critic's review turns average 1,900 characters and the worker's 1,000, mostly the agents' own words. The room left is in the system prompts (role plus protocol files: about 12,500 characters for the critic, 10,500 for the manager) and in the built-in texts.

Follows from the new design, so done with the prompt rewrite without asking (the user, 2026-11-25: prompt changes based on the design need no question; ask only about uncertainty in reminders):

1. **Out of date:** About 24 of 123 templates serve controls the design removes (verdicts, `<wait-job>`, critic pauses, `<workers>`, `<to>`, urgent messages, replaced messages, separate check-in turns). Delete them with their prompt sections. The prompts also still mention `preference.md`, `nohup`, and the golden board being sent to every agent. `orch/prompts/auditor.md` describes the old design and is rewritten too.
2. **Built-in texts repeat the protocol:** 25 templates name `<message to=…>` and several restate the tag rules. Keep them in the protocol prompt; messages carry the content and at most one line on what is needed. The review texts already follow this (full once per session, then a short line).
3. **Contradictions:** The monitor's protocol asks for at most 8 lines, one per item; its check message asks for at most 3 and to omit unchanged items. Keep 3. The critic is told both that untagged text is for the manager and to reply "inside `<message to="you">`". Drop the second.
4. **Codex citation codes:** The critic's reports contain `:codex-file-citation{…}` codes, passed to the manager as they are (3 of 7 manager turns, 10 codes). Turn them into plain paths.
5. **Unnatural wording:** For example "(or the authors)" in the pause text (left over from one project), "Reply target: **worker** (only if not healthy)", and the JSON reply for closing a discussion. Rewrite in plain language when the prompts are rewritten.

Agreed on reminders and modes:

1. **When to remind every turn:** Only for a very important point that is easily ignored and harmful if ignored, such as the critic's message labels. Everything else is a random pick, each reminder item with its own probability.
2. **Golden board line:** The system prompt tells the critic and manager to use the board's related rules; a one-line reminder repeats it every few turns (random) and when the board changes.
3. **Manager's reminder:** Not every turn, since the manager usually runs a stronger model. Random pick; as the manager has fewer turns, with a higher probability (about every one or two turns). Covers how to message the critic and, when there are any, open gates; not in discussion mode.
4. **Worker's reminder:** The "ask in plain text and end the turn" line is a low-probability random pick, since frequent reminders make workers ask instead of work.
5. **Old modes:** Remove the pipeline and controller modes and their 9 templates.
6. **Managing the golden board:** One of the manager's tasks, so a random-pick item in its reminder too: add the user's new standing preferences and decisions, merge overlapping entries, remove what no longer holds. Applies in discussion mode as well, where the user often states them. The once-per-growth size warning stays.
7. **Research rules:** The ten "Research guidance" rules leave the default manager prompt (`orch/prompts/manager.md`, commit `b81d9a1`) and are kept in an `assets/` folder in the repository. Orch does not decide which project is a research project and adds them to none; the user, or the manager when the user asks, adds them to a project's golden board.
8. **Prompt updates reach existing projects:** When Orch's system prompts change, every project uses the new ones. Today each project copies the role prompts when created, so updates do not reach it. Seen now: the critic copies in NIW-orch and ICLR2027-rebuttal-mining are the old default of commit `056726d`, which says to write to the worker directly, against the current protocol.
9. **No prompt copies:** Projects stop copying the prompts and always use Orch's own. The user does not manage any agent's system prompt; anything special to one project goes in its golden board (edited by the user or through the manager) or its goal. A prompt the user edited by hand, such as ICLR2027's (its three match no default), is moved to a backup file, not deleted.

Reference: audit files of another project were read and have since been removed at the user's request. From them:

- **Correction:** `audit-decisions.md` says the critic's verification template and the manager's review instructions stay unchanged, but `docs/audit-decisions-example.md` in this repository records that decision as reversed on 2026-11-21: full instructions once per session, then a short line, and an unchanged rubric is not resent. Recent NIW-orch turns show the short line. The checklist and rubric in the manager's review stay, as useful context; the idea to replace the pasted checklist with a path is dropped.
- Already covered by the agreed rules: a message to an unknown worker held without notice, a user message waiting for hours, questions left open for days, acknowledgment loops, re-sent worker reports, healthy monitor checks waking the critic, and large handovers.
- Likely false alarms: pause turns said to generate nothing (they had over 1,000 characters of output), short reviews of short work, and slow turns whose cause is unproven.

## Removing the review file

Agreed 2026-11-25. The user finds it unnecessary.

- **Dashboard:** Remove the "Review file" button (`reviewbtn`), the "Review files" card, and the "Review file to comment" request (`orch/board.html`), with `/api/review`, `/api/review_comments` and `_review_files` (`orch/board.py`).
- **Run:** Remove the manager's review file turn, the comments sent back as a diff and their state, events and timeline entries (`orch/managed.py`, `orch/recorder.py`, `orch/timeline.py`).
- **Prompts:** Remove the templates `review_file_request` and `review_comments`, and the two mentions in `orch/prompts/protocol/manager.md`.
- **Tests and docs:** Remove `TestReviewFile` and adapt the test in `tests/test_e2e_fixes4.py` that uses review comments; drop it from README and the current docs.
- **Kept:** Milestone reviews, their gates and the "Discuss" chat are separate and unchanged. Review files already written in a project stay on disk; old runs still open.

## Updating the audit

Agreed 2026-11-25: after this batch of development, update the audit for the new design, to reduce false alarms. Likely causes seen in the reference audit files:

- **A misleading number:** "text response window: 0s" is read as "nothing was written" (one finding counted 26 times), though those turns had over 1,000 characters of output. A reply that arrives in one piece has a zero window. The digest should not show it as zero, or should say what it means.
- **Already decided points come back:** The finding on Q13 and M199 was raised again and again after the user had marked them fixed, although not deployed. The digest should say which fixes are in the running version.
- **Guesses from sizes and times:** Long turns, short replies and slow starts are reported as provider problems or bad work without proof.
- **Expected behavior reported as a fault:** The auditor does not know well enough what is meant to happen.

Agreed:

1. **New design:** Rewrite the auditor's description of intended behavior from the final design, when the prompts are rewritten. Run no audit across the change of design.
2. **Facts from Orch, not the model:** What can be checked exactly (undelivered or unanswered messages, a missing gate, a job without an end) is checked by Orch; the model only judges what cannot be.
3. **Second check:** Before a new finding is reported, a check against the actual turns and the user's notes; if it cannot be confirmed, it is not listed.
4. **The user's notes:** No marks on findings. The dashboard's audit page has a place for the user's own notes on auditing. Assumed: they are saved in the project's `audit-decisions.md`, which the auditor already reads, so a point the user has settled there is not raised again without new evidence.
