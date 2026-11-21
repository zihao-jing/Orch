# Orch audit improvements and agreed decisions

Updated: 2026-11-21 UTC.

This is the maintained record of the audit review and subsequent design discussion. An agreed design is not an implemented fix. Update each item's decision, implementation, and validation status as work proceeds.

Audit reviewed: `.orch/agents/auditor/audit.md`, run `20261120-062003`.

## Priority and status

| Priority | Improvement | Status |
|---|---|---|
| 1 | Uniform message routing and explicit worker dispatch | Implemented in isolated source worktree; full suite passed; not deployed |
| 2 | Reinforce the auditor's required findings format | Prompt revised in source; parser unchanged; not deployed |
| 3 | Timestamp concise critic progress records and expose timing breakdowns | Implemented in source; full suite passed; not deployed |

Prompt compression is lower priority. The audit's claims of lost restart context, isolated report sessions, and improper human-answer routing were not supported by the recorded turns. The checklist HTML-comment issue is a separate previously discussed fix.

## 1. Uniform communication protocol

### Observed failure

Several workers shared checklist Step 15. The critic told individual workers that their assignments were finished, but returned `FAIL` while the shared step remained open. Orch forwarded untagged feedback to the reviewed worker and scheduled another turn. Rounds 61–66 contain six acknowledgment-only worker turns and their subsequent critic reviews.

The existing recipient of untagged text changes with the turn type: during reviews it goes to the reviewed worker, while during requested reports it goes upward. This implicit routing combines with verdict-driven scheduling to create the loop.

### Agreed replacement

Use the same communication protocol for every agent and every turn type:

| Message | Destination |
|---|---|
| Untagged text | The sender's upper level, represented as “you” in its role view |
| Explicitly addressed message | The named recipient, subject to the sender's permitted role-view addresses |
| Control tags | Their defined controller behavior; not ordinary message text |

Upper-level mapping:

- Worker → critic.
- Critic → manager.
- Manager → human.

The upper-level default must not depend on whether the turn is a review, assignment, report, check-in, or parallel-worker review. Explicit `<message to="you">…</message>` has the same upper-level destination.

For communication to a worker, the critic must explicitly address that worker. Existing explicit assignment sections such as `<to worker="ID" step="N" out="…">…</to>` must be accounted for consistently; their exact relationship to ordinary message tags remains an implementation detail to settle, without losing assignment metadata.

### Dispatch rule

- No explicit message or assignment to a worker means no new worker turn. An idle worker remains idle, regardless of `PASS` or `FAIL`.
- If a worker is already working, the absence of a new message does not stop its current work.
- Orch delivers explicitly addressed messages without judging their prose. The critic decides whether sending a message is warranted; the system must not infer “done” or “needs action” from wording or message length.
- An explicit message to an idle worker can start a turn. A normal message to a busy worker follows the existing queue rules; an urgent message follows the existing interruption rules.
- A verdict records the review result and retains its validation meaning. It must not, by itself, manufacture a worker message or trigger a worker turn.
- No separate worker-done signal is needed to resolve this acknowledgment loop under this design.
- Idling must preserve the worker's session for future explicitly assigned work.

Upward routing does not imply waking the manager for every informational note. Keep routing (recipient) distinct from existing queued/urgent delivery rules. Control tags such as questions and pauses retain their defined semantics.

### Prompt and pipeline consistency

Apply the rule to base system prompts, protocol instructions, reminders, turn templates, and message extraction/routing/dispatch code. Include single-worker and multi-worker paths. Remove instructions that send untagged review text downward or implicitly treat `FAIL` as another work assignment.

A short critic reminder supports the rule: “Send a worker-addressed message only when that worker needs to act. Otherwise, leave the worker idle; report shared progress or questions upward.” The reminder complements the routing change; it cannot replace it.

### Required validation when implemented

- Untagged critic text goes upward in reviews, reports, and check-ins, never automatically to a worker.
- An explicit worker message reaches only its intended recipient, once.
- `FAIL` without an explicit worker message does not schedule another worker turn.
- A completed worker remains idle while another worker continues the shared step.
- Explicit later work reuses the idle worker's session.
- Queued and urgent messages retain their delivery behavior.
- Questions, pauses, verdict validation, and assignment metadata retain their intended behavior.
- When every worker is idle but work is incomplete, surface the state for the appropriate upper-level decision; do not silently invent worker turns. The exact recovery behavior needs specification during implementation.

## 2. Auditor findings-format compliance

### Verified evidence

The final audit (rounds 58–67) supplied a `<findings>` section whose entries lacked the `- ` bullet prefix. `orch/audit.py::parse_reply` only recognizes entries beginning with that prefix, so it parsed zero findings. `write_findings` treated that empty list as a valid replacement and wrote `(none)` over the maintained Findings section.

Read-only reproduction against the five saved audit responses returned 3, 5, 7, 8, and 0 parsed findings, respectively. Historical audit entries remain present; the maintained summary was cleared.

### Selected approach

The user attributes the incident to the auditor's failure to follow the required output format and selects a prompt-level improvement rather than an Orch parser change. Highlight the required `- ` prefix in the auditor system prompt or add a short explicit reminder, including a format check before replying.

Proposed wording: “Inside `<findings>`, start every finding on its own line with exactly `- ` (hyphen followed by a space). Before replying, verify that every finding has this prefix; entries without it are not recognized.”

The prompt change is implemented in the isolated source worktree and has not been deployed. It may reduce recurrence, but cannot guarantee format compliance. The observed parser behavior remains as documented above. Preserving prior findings on malformed output and accepting recognizable formatting variations are deferred robustness options, not part of the selected fix.

## 3. Delayed notes and timing diagnostics

### Evidence

Nonurgent critic notes accumulated until the manager's next regular turn. Confirmed waits include 13–31 minutes and 58–88 minutes. No resulting incorrect manager decision was established.

The 898-second critic turn `r039-t02-critic` included roughly 603 seconds before the first recorded assistant response and a 120-second shell call. The cause of the initial gap remains unresolved. Record backend waiting and tool execution separately; final-response character counts cannot establish model throughput or excessive reasoning.

### Agreed progress-note approach

- Timestamp each nonurgent critic progress note with its original creation time, including the timezone, rather than only the time it is delivered.
- Present these notes to the manager in a clearly identified “Progress records from the critic” wrapper so their historical context is explicit.
- Deliver them at the manager's next turn under the existing nonurgent delivery policy; receiving a progress note alone does not require an extra manager turn.
- Instruct the critic to keep progress notes short: describe the change since its previous update and any remaining blocker, without repeating the full review or standing instructions. This reduces the manager's context usage.
- Preserve substantive decisions and unresolved questions. Questions and urgent messages retain their existing escalation behavior rather than being downgraded to ordinary progress records.

Illustrative presentation (wording and timestamp format may be refined during implementation):

> Progress records from the critic
>
> [2026-11-20 11:08 UTC] Finance source review passed. Classroom verification is still pending.

Automatic consolidation or deletion of superseded notes is not required by this agreement; it remains a separate option. The selected change is timestamping, clear historical presentation, next-turn delivery, and concise authoring.

### Agreed timing-visibility approach

Expose the phases of a turn rather than inferring productivity from final-response length: waiting before a response, model response activity, and tool execution. Use recorded timestamps where available; label missing or uncertain intervals honestly. A gap before the first response is not by itself proof of provider failure or excessive reasoning.

Exact instrumentation and presentation remain implementation details. This agreement does not include changing timeouts based on the existing measurements.

## Lower-priority review

- Repeated critic verification template: *reversed 2026-11-21: fix implemented.* The full review instructions go out once per session and after a compaction; later reviews get a short line. With `checklist: true` the checklist file decides the verdict (no tag needed; an explicit FAIL still holds the work back).
- Manager review prompt repetition: *reversed 2026-11-21: fix implemented.* The review, kickoff, question and feedback templates no longer repeat protocol rules; a re-review in the same session says "Rubric unchanged."; a finished plan is one line with the file path. The audit's total “Orch-originated” share includes useful checklist and rubric data and is not a measure of waste.
- Broad critic feedback during individual worker reviews: user confirmed this is covered by the agreed routing and concise progress-note changes; no separate mechanism planned. In `r060-t02-critic`, the critic wrote a 3,318-character response including a five-case completion overview while reviewing one worker. Worker-directed feedback should focus on that worker's assignment, with a concise overall update routed upward.

All 17 audit items have now been reviewed and their independently supported improvements discussed. Remaining claims concern intentional milestone session resets, supposed restart/report context loss contradicted by session records, permitted human-answer routing, or latency diagnoses unsupported by response length. They do not establish additional fixes. The checklist HTML-comment issue remains separate from this audit list.

## Auditor improvement proposals

The prompt and digest changes below are implemented in the isolated source worktree. They are not deployed; model-based replay evaluation remains unperformed.

### Keep the audit aligned with the running design

Supply a concise active-protocol description with each audit, tied to the version used for the audited turns. Distinguish the approved target design from implemented behavior. Historical turns must be judged under their historical rules. Where a window spans a protocol change, identify the boundary rather than applying the new rule retroactively.

When priority 1 is deployed, update the auditor alongside the agent prompts and routing code: untagged text goes upward; explicitly addressed messages go to their recipients; no explicit worker message or assignment means no new worker dispatch; verdicts alone do not dispatch workers. Nonurgent progress records wait for the manager's next turn.

The current auditor prompt's “Each addresses only the agent below it” wording needs clarification: it describes role views ambiguously and can conflict with permitted upward communication. State permitted recipients and explicit routing rules directly. Distinguish raw human/manager conversation from a deliberately relayed binding decision; human-origin text is not itself leakage.

### Require evidence before diagnoses

Each reported issue should identify the observed event and turn IDs, the relevant active design rule, the actual deviation or concrete inefficiency, and the impact supported by the digest. Mark uncertainty rather than inventing a cause. If the digest lacks the data needed to decide, request that missing evidence rather than asserting a defect. Intended behavior may still merit a design improvement when concrete adverse effects are shown; label that as a tradeoff rather than a protocol violation.

### Explain measurement limitations explicitly

- Session turn numbers are not session identities. A short continuation within a resumed session does not establish lost context.
- Provenance describes where text originated, not necessarily its immediate sender or its permitted route.
- “Orch-originated” includes dynamic checklists and rubrics; it is not synonymous with wasted boilerplate. Scheduled reminders are already excluded from the digest's relevant counts.
- Paragraph tracing omits text shorter than 60 characters. A missing worker contribution in the provenance table does not establish an empty worker message.
- Equal input or output lengths do not establish identical content.
- Recipient-specific relay percentages must be interpreted against intended recipients; withheld control tags and messages for others are not lost information.
- Final-response length divided by elapsed time is not model throughput. Tool activity and pre-response delays require separate evidence.

### Respect reviewed findings and output format

Include the maintained human decisions in the auditor's input: resolved false alarms with the reasons they were rejected, the two repetition optimizations marked “won't fix,” and merged concerns linked to their existing findings. Reopen only on new contradictory evidence or a material change, not merely another occurrence of already accepted behavior. Incorporate the priority-2 bullet-format reminder.

### Evaluate the revised auditor

Replay representative historical audit inputs with their matching historical design rules. Check that the auditor retains the acknowledgment-loop finding, avoids the demonstrated session/routing/provenance false alarms, distinguishes unknown latency causes, preserves reviewed statuses, and emits valid findings bullets. Do not judge improvement only by producing fewer findings: include a concrete message-delivery failure example so the audit must still detect real failures. This evaluation is proposed, not yet run.

## Decision history

- Rejected relying on a short-output threshold or semantic interpretation of “you are done” to suppress worker calls.
- Initially considered separate worker-done/wait signals; superseded for this loop by explicit-message-driven dispatch.
- Agreed to replace turn-dependent default recipients with the uniform upper-level default.
- Agreed that no worker-addressed message means no new worker turn, regardless of verdict.
- Agreed to combine the protocol/pipeline change with a concise critic reminder.
- Selected an explicit auditor-format reminder for priority 2; parser hardening deferred. The user considers this an auditor behavior/performance issue rather than an Orch bug.
- Agreed to timestamp concise critic progress records, wrap them clearly as historical progress, and deliver them on the manager's next turn. Agreed to improve timing visibility; automatic note consolidation and timeout changes are not included.
- 2026-11-21 (ICLR2027-rebuttal-mining audit review): reversed the won't-fix on the repeated critic review template; the verdict is read from the checklist file when no tag is given.
- 2026-11-21: reversed the won't-fix on manager review repetition; the critic's final review is now also its milestone report (no separate report turn).


## Implementation record — 2026-11-21

Source worktree: `/chatgpt_nas/jingzihao/lctx_agent/Orch-protocol-audit`, branch
`fix/uniform-protocol-audit`, based on main `ff284da`. The installed checkout at
`/chatgpt_nas/jingzihao/lctx_agent/Orch` and its running process were not changed or restarted.

- Managed-mode routing and explicit worker dispatch are implemented across normal turns, board chats,
  terminal relays, and parallel assignments. Pipeline mode remains a separate workflow.
- A worker with no new explicit instruction stays idle. Parallel worker sessions remain available while
  waiting for manager direction. If all assigned work stops unfinished, the existing manager pause/direction
  flow runs once; another empty continuation waits for the human. A rejected parallel proposal requests one
  explicit single-worker assignment from the critic before that escalation.
- `<to worker="ID" step="N" out="…">` remains an explicit assignment with its step/output metadata.
  Ordinary messages retain the existing queued, urgent, and additive/replacement rules. Reviews with an explicit
  message can dispatch work even when the verdict is PASS. Amendments apply only to their addressed workers.
- Nonurgent critic progress records carry their original UTC timestamp and wait for the manager's next turn.
  Concision is instructed in the prompt; records are not automatically summarized or deleted.
- Recorded turn events expose first activity/text, text-response window, measured tool intervals and unknown
  time. Tool interval overlap is counted once. OpenCode tool durations and Codex app-server boundaries are
  instrumented; unsupported/missing timing remains unknown. No timeout policy was changed.
- Auditor input now includes per-turn protocol versions, historical-rule fallback, timing evidence and the
  optional human-reviewed `audit-decisions.md` beside `audit.md`. The prompt reinforces evidence requirements,
  known measurement limits, reviewed dispositions and the required `- ` findings prefix. Parser behavior is unchanged.
- The project's `.orch/agents/auditor/audit-decisions.md` records the dispositions from this discussion.
  Existing project-specific system prompts have not been overwritten; deployment must reconcile any customized
  prompts with the revised source defaults.

Validation: the initial full suite recorded **416 tests, 388 passed, 28 failed/errored**, with all 11 new
regression tests passing. After the user authorized continued verification, a targeted rerun passed 25 of the
28 former failures. Three remaining stale assertions were corrected, then the final full repository suite
(`python tests/run.py -j 8 --slow 5`) passed **all 416 tests in 33.8 seconds, with zero failures, errors or skips**.
The final run includes the job-ownership, declined-proposal, explicit-dispatch, session, routing, timing and
auditor-digest coverage. Static compilation and whitespace checks also pass. See
`Orch-protocol-audit/docs/protocol-audit-validation.md` for the verification history and corrections.
Historical model-based auditor replay and live-provider/deployment validation have not been run.
