# Further Orch discussion

Started: 2026-11-25. Keep each topic short, with room for comments.

Earlier issues and agreed directions: [ORCH-ISSUES.md](ORCH-ISSUES.md).

## Topics to discuss

1. Step management and checklist-based completion.
2. Worker states, background jobs, and wakeup rules.
3. Enforcing supervised execution across CLI backends.

## Discussion notes

### Agreed: steps track progress, not message routing

- **Ownership:** The critic maintains steps and their checklist items in one Markdown file.
- **Completion:** The system reads the checklist and automatically passes a step when its items are complete.
- **Display:** Show steps in a progress panel at the top; do not divide messages into step blocks.
- **Conversations:** Order messages chronologically, showing sender, recipient, timestamp, and delivery status.
- **Independence:** Neither messages nor worker assignments require a step association. Steps do not control routing or execution.
- **Traceability:** Optional links from checklist items to relevant messages may provide evidence without imposing workflow rules.
- **Context:** Step boundaries do not imply compaction; session/context management is separate.

### Manager protocol decisions

- **Critic messages:** Keep `<message to="critic">`. Remind the manager during coordination, not discussion mode.
- **Questions:** Keep `<question>` for dashboard gates with selectable options; use the single behavior defined below.
- **Pause controls:** Keep the manager's `<pause>` as a status control, distinct from question gates. Remove critic-proposed pause handling; milestone decisions remain a separate discussion.
- **Milestone review:** Retain the current rubric and score mechanisms.
- **Working state:** Retain `<state>` and its maintained state file.
- **Worker count:** Remove the manager's `<workers>` tag/control. Only the user changes worker count through the dashboard.
- **Agent preferences:** Remove per-agent preference Markdown files and the system-prompt instructions/reminders to maintain them. Preferences remain in each agent's session; the shared golden board and manager state file remain separate mechanisms.
- **Scope:** These decisions do not yet settle removal of direct worker messages, summary tags, or discussion-apply behavior.
- **Your comments:**

### Agreed: question gates do not pause execution

- **One format:** `<question>…</question>` opens a user gate with selectable options. Remove the blocking/nonblocking attribute distinction from the manager's protocol.
- **Routing:** A manager reply containing a question opens the gate and forwards nothing from that reply to the critic, even if it also contains a critic message.
- **Execution:** The question does not pause agents or jobs already running. Opening the gate does not depend on their status.
- **Answer:** The user's answer returns to the manager, which can then issue a new critic message based on that answer.
- **Scope:** This decision defines question gates; separate pause controls remain under discussion.

### Agreed: questions and pauses are separate behaviors

- **Question:** `<question>…</question>` opens a user gate. By itself it does not pause execution.
- **Pause:** Keep `<pause>…</pause>` so the manager can explicitly change the system's execution status to paused. It does not substitute for a question gate.
- **Together:** A manager reply may contain both tags: open the question gate and set paused status. Question-only behavior remains non-pausing.
- **No critic pause proposal:** Remove the manager's PAUSE/CONTINUE response path for critic-proposed pauses, since the critic no longer proposes them.
- **Routing:** A reply containing a question forwards nothing from that reply to the critic. An accompanying pause is still processed as a separate control.
- **Unresolved pause details:** Define what paused status permits for active agent turns and existing jobs, and how the user resumes it.
- **Milestone decisions:** Retain the milestone review mechanism; the meaning of milestone `<decision>PAUSE</decision>` remains to be settled.

Further topics and decisions will be added below.
