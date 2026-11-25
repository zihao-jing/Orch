# Orch usage issues

Updated: 2026-11-25. Working list for discussion; proposed fixes are not yet implemented.

## 1. Messages do not reliably activate workers

- **Issue:** Assignments appeared sent, but worker-2 stayed blocked. Malformed tags and a stuck urgent message both caused missed handoffs.
- **Proposed fix:** Critic tags worker messages; its untagged output goes upward to the hidden manager. Validate recipients, deliver promptly, and separate delivery from activation; track receipt and deduplicate retries.
- **Your comments:**

## 2. Dashboard status does not clearly reflect execution

- **Issue:** Writing indicators persisted, resumed work appeared under old steps, and background-job waits were unclear.
- **Proposed fix:** Derive status from backend and job events; show writing, waiting, blocked, and failed states with last activity and the current assignment.
- **Your comments:**

## 3. Workers can bypass the intended review loop

- **Issue:** Batch execution and internal iteration drifted from the required rollout → evidence-based rubric → critic review process.
- **Proposed fix:** Require critic review after every completed query attempt; record acceptance in its checklist before advancement, and communicate revisions through worker messages. Allow logged technical retries separately.
- **Your comments:**

## 4. Messaging reminders should match the current event

- **Issue:** Generic routing instructions leave the critic to infer recipients and syntax.
- **Proposed fix:** Include short, exact tag examples in every critic prompt for relevant active workers (e.g. worker-2 after its event). Never expose the manager as a recipient to the critic.
- **Your comments:**

## 5. Worker identity and lifecycle need explicit controls

- **Issue:** Retired workers and stale session mappings make routing and recovery confusing.
- **Proposed fix:** Keep a backend registry of worker identity, session, assignment, and lifecycle. Add dashboard Add/Close/Reopen controls; reject delivery to closed workers and preserve history. Distinguish closing from cancelling running jobs.
- **Your comments:**

## Agreed critic protocol redesign

Planning record for one coordinated update; no runtime changes made yet.

1. **One communication tag:** `<message to="worker-2">…</message>` sends instructions or answers to a worker. No separate step-assignment tag.
2. **Upward communication:** Untagged critic text goes to the hidden manager. Questions and blockers use ordinary prose; the manager replies or escalates to the user.
3. **Remove critic controls:** Remove `<question>`, `<pause>`, `<wait-job>`, `<verdict>`, `<workers>`, and `<to worker="…" step="…">` from its protocol. Worker `<wait-job>` is removed too.
4. **Checklist-based completion:** Prompt the critic to maintain its checklist strictly. The system automatically passes a step when its checklist is complete; no verdict tag or manager approval for that step. Broader step management remains for later discussion.
5. **User-controlled worker count:** No default on-demand allowance. Only the user sets or changes the number of workers through the dashboard. The critic messages available workers using the same message format; it cannot increase the worker count or create workers beyond the user-configured pool.
6. **Dashboard control:** Only the user can add or close workers and change the pool size. Preserve closed-worker history and prevent accidental revival by late messages.
7. **Backend registry:** Map stable worker identities to sessions, lifecycle states, and current work. Validate recipients and record delivery acknowledgments without duplicates.
8. **Every-turn reminders:** Show relevant workers' statuses in one line each, plus exact message-tag examples. Include available IDs from the user-configured pool; never expose a manager address to the critic.
9. **Job lifecycle deferred:** Remove model-written waiting tags. Design worker writing, background-job waiting, completion, and monitor-triggered wakeups separately; removal alone does not define replacement behavior.

### Deferred discussion

- Broader step management and worker job/wakeup lifecycle remain to be designed.

## Agreed worker question handling

- **Reminder:** If an answer is required to proceed, ask the critic in plain text and end the turn. Do not guess or continue dependent work.
- **Delivery:** The system passes the completed reply to the critic. Its worker message answers the question and starts the next turn in the same session.
- **No question tag:** No worker-written question control tag is required. Independent work may continue when an answer is not required for it.
- **Job waiting:** Still under discussion; the reminder above does not define background-job behavior.

## Worker job tracking proposal

- **Direction:** Replace heuristic process scanning with system-managed job tracking. This concerns job detection, not removing all progress or output-quality checks.
- **Enforcement:** Route shell execution through an Orch-owned supervisor and disable bypass execution paths where supported. A reminder to use an optional job tool is insufficient.
- **Registration:** Assign stable job IDs and record worker/session ownership, logs, running/completed/failed states, and exit status automatically. Persist records and reconcile live jobs after restarts.
- **Wakeup:** Separate model activity from job activity. After a worker turn ends, unfinished task jobs keep it waiting; completion or a new message can wake it without duplicate turns. Persistent servers and monitors must not block task completion.
- **Limit:** Reliable tracking covers supervised execution. Remote jobs, escaped descendants, and native CLI tools require backend-specific integration; no universal 100% guarantee.
- **Migration:** Verify enforcement and lifecycle recovery for each supported backend before removing the existing detector. Exact execution interface and job classification remain to be designed.
- **Your comments:**
