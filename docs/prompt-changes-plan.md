# Prompt changes: your review comments, organised

Source: your comments in [ ] in docs/prompts-review.md. Each line: what you asked → what I will change. "Code" means a change in Orch's logic; the others are text only. Items marked (Q) wait for your answer at the end.

## A. Changes in Orch's logic

A1 Manager notes. Remove <state>, the notes file, its reminder (rem_state_edited) and the code behind them. The manager keeps its own context.
A2 Wait after each milestone. After an accepted milestone Orch waits for you before the next one starts (a gate with "Start next milestone"), except in night mode. Today it waits only after the last one.
A3 Milestone review not based on the critic's checklist. The manager reviews against its own rubric and the work, never against the critic's checklist. The checklist is the critic's own working file: lighter rules, and Orch stops reading it closely. What then triggers the review (Q1).
A4 Rubric. The <rubric> label stays, so that Orch can check each criterion is scored. Orch also saves it to a file in the run folder, and the review message gives that path instead of the whole rubric.
A5 Your chats with a worker stay between you and it. A worker's reply to you is no longer sent on to the critic (worker_to_critic), and the critic is not told you wrote to it (direct_message_notice).
A6 Stopped or closed workers. No message to the critic (jobs_stopped). Instead, the critic's reminder block always shows each worker's state (working, job running, idle, closed), and its system prompt says where to find it.
A7 Discussion. While a discussion is open, Orch holds back the manager's messages to others instead of keeping them as drafts. When you apply the discussion, the manager writes the messages then. The "drafts" wording and the "discussion over, not applied" reminder go.
A8 Silence check. Orch pauses the work after the last check itself; the message to the manager no longer mentions it.
A9 Model switch. Today the old model writes a handover and the critic (or manager) also writes an instruction, so the new session gets two texts. One is enough (Q2).
A10 An interrupted message sent again goes to the new model as it is, without the "Your last turn was stopped…" line.
A11 Session texts. session_fresh and the handover_instruction preface go. session_return becomes short: "Work went on without this conversation since <time>, by <who>." plus the path of the session that went on, when there is one.
A12 Golden board words the critic must not see (Q3).

## B. System prompts

Manager
B1 Start of a milestone: the instruction to the critic gives what is needed, what matters most and what to watch out for, plus the golden-board rules and your decisions that matter for this task only. "Open items" goes (the manager has no context for it).
B2 Guiding. "Step in when the work is inefficient, drifts from my goal, or waits for an answer from me." Replace "Don't go looking for progress" with: keep a general view of the progress; leave the details to the critic and focus on decisions.
B3 Messages: the manager writes to the critic when it has received something from it that needs an answer or a change. It never checks in on its own, sets timers or starts monitoring unless I ask.
B4 Answering the critic: by my goal, preferences and the golden board's rules. Drop "use the same names and numbers… check them before you write".
B5 My decisions: pass each one on once, completely; if it replaces an earlier one, say which. Drop "They are binding".
B6 Judging: against the milestone, its rubric and the project goal; mainly from the Markdown results the critic and workers wrote, then logs and files. Read deeper when something looks wrong or not good enough. No mention of the critic's report or checklist.
B7 Asking me: preferences, money beyond the approved budget, or a change to something I approved. Drop "time". "Make it self-contained".
B8 Golden board: relative path; no example rule; "write the board's rules that matter for the current task into your instruction to the critic".
B9 Files and the run: drop "you edit no files" and "run none of the work" (no rule either way). It may stop a process when something is clearly wrong.
B10 "How your replies are read" becomes "How to communicate".
B11 <question>: say plainly what happens: I answer, my answer comes to you, then you write to the critic with what it means for the work.
B12 Writing to the critic: keep to the role boundary: no headings such as "Instructions" or "Critic", nothing about you or me.
B13 Writing to me: self-contained, don't assume I have the context, no code or experiment names I would have to look up; if code is needed, explain it first.

Critic
B14 "so that I never have to follow the details" → "so that I don't have to follow the details".
B15 "They say what is needed now" → "My instructions say what is needed now".
B16 My decisions: when one replaces an earlier one, tell the workers what changed. No "binding".
B17 Ask first only for a change to the scope or method I approved, or money beyond the budget. No examples, no "time".
B18 Golden board: read it; write the rules that matter for a task into the instruction to the worker; never pass the board itself to a worker.
B19 Checklist: its path is in the system prompt. Lighter rules: steps let you and the workers go step by step; the size of a step is a range that depends on the work (no "about 25 items").
B20 Drop "It is the only file you edit". Say nothing to forbid other files.
B21 Drop the line about several workers; the manager decides that in its instruction.
B22 Instruction to a worker: what to do, what it must keep to, and what "done" means for it (not your checklist). Drop "A result from someone else is just a file to read."
B23 "Write as I to you" → "Write to the worker directly, as its lead."
B24 Reviewing: "Check the files, logs and results: do the numbers add up…" (drop "not only the report").
B25 Job still running: align with the message the critic gets (C1). Drop the server sentence (left over from before).
B26 Progress checks: "A progress check says what the monitor saw. Look yourself before you step in."
B27 Telling me: length fits the situation; reports concise; questions without a recommendation (the manager decides).
B28 Protocol: a positive heading ("How to communicate"); drop "a reply may hold several" and "A report that needs no answer gets none"; "[a decision] for me to make"; drop the unclear "also if it concerns only one of several workers".
B29 Drop the `.orch/` line. Add the role boundary: you plan, hand out and check; workers do the hands-on work.

Worker (much shorter)
B30 Keep only: the ask-and-end rule, the background-job lines for its program (the job line of 1.5 replaces the fixed Bash line), the "never nohup…" line, and "How to write". Everything else goes.

Monitor
B31 "Only look, with read-only commands such as tail, ls -l, wc -l, ps, git status": the list is examples, not a limit. Drop the `.orch/` line.

How to write (style.md)
B32 Drop "Use the terms the work already uses…" for every role. Drop "Give the conclusion, not the story" for the worker.

## C. Messages to agents

C1 worker_intermediate: a clear first line: "worker-1's job is still running (train.py). Not ready for review." The critic's reminder block also lists running jobs, so none is forgotten.
C2 worker_failed: the reason and assignment are the real ones at run time (the review file shows a sample).
C3 monitor_report: in plain words, as one sentence, and matching the four states the monitor uses.
C4 handover_request_*: drop the reason ("a billing problem"); "its latest report" → "the previous worker's latest report". One message (Q2).
C5 milestone_start: say the rubric is for you only, never for the critic. Replace "your instruction needs no path" with: "Orch tells the critic where its checklist is."
C6 checklist_complete → milestone review: rubric as a path; no checklist; no milestone name where it adds nothing. Ends with what to do if the critic says it is done but the work is not ready (Q1).
C7 review_score_low: see Q4.
C8 silence_check: no pause wording (A8); "find out why: the critic's checklist, its last message, the files, and the state of Orch's agents and jobs".
C9 golden_leak: Q3.
C10 discussion_apply: no drafts (A7): "The discussion is applied. Write to the others now what we agreed."
C11 label_returned and label_broken_passed: show a complete example, e.g. <message to="worker-1">…</message>.
C12 job_wakeup: "Your background job has ended: train.py (exit code 1). Check the result and continue."
C13 nobody_watches: "Look yourself when a run takes longer than 40 minutes" (the setting's value).
C14 monitor_check: drop the checklist. Name the live file plainly: "The output of its current run is in <path> (written as it works)." Answer to your question: Orch writes that file itself from the worker's program output, so the path is exact.
C15 checklist_unreadable: already the real path at run time; the review file shows a sample.

## D. Reminders

D1 rem_message_critic 60% → 30%.
D2 rem_golden_size: once when over the limit, then 25% while it stays over.
D3 rem_state_edited: removed (A1).
D4 rem_discussion 25% → 40%, with the new wording (A7); rem_discussion_over removed.
D5 rem_workers: "N workers are available." only, no advice on splitting.
D6 rem_golden_path: "…so read and maintain it." rem_golden_use: "…into your instruction to the critic." rem_golden_keep: "…keep it clean and brief."
D7 rem_ask_and_end: "ask me concisely in plain text and end your turn".
D8 New for the critic (A6, C1): one line per worker with its state and running job, replacing rem_worker_line.

## Your answers

Q1 The critic ends a report with <done/> when it thinks the milestone is finished; Orch then asks the manager to review. The checklist is the critic's own file only.
Q2 The old model's handover first; when the old model cannot write one (it failed, e.g. billing), the lead (the critic for a worker, the manager for the critic) writes the instruction.
Q3 The critic never sees the golden board. The manager restates the rules that matter (the same wording is fine) in its instruction. The word check and golden_leak go. So B18 becomes: no golden board for the critic; rules come in the manager's instruction, and the critic passes the ones that matter on to workers.
Q4 When the plan names no pass score, Orch uses 4.5.
