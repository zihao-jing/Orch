# Insights from auditing a long multi-agent run

What we learned from orch's auditor and from checking its findings against the raw run records. The run was a
real research project: manager ↔ critic ↔ worker, 110 rounds, 446 turns over five days, with the worker moved to a
cheaper model on a third-party Anthropic-compatible endpoint partway through.

Every point below was checked against the turn files (`rounds/*/t*.json`), `events.jsonl` and the inbox. Where the
auditor was wrong, that is a lesson too; see "Auditing the system" at the end. Each entry says what we saw, the rule
we took from it, and where orch stands.

## Recording what happened

### 1. A cost meter is only as good as its price table
**Seen.** The worker ran a non-Claude model through Claude Code pointed at another provider's endpoint. Claude Code
priced those tokens with its own Claude rates: $565 of the run's reported $701 was this estimate, not what the
provider charged. Every downstream judgement inherited the error: "$15 worker turns", "phase spend over the cap",
a "39× cost spread" between similar turns.

**Rule.** Record per turn the model that actually answered, the endpoint (host only), and token counts. Label each
cost as *reported* or *estimate*, and never compare or cap spend on estimates. Tokens are the figure that holds
whatever the endpoint charges.

**Status.** Done: turns record `model`, `endpoint`, `cost_basis` and `meta.usage` / `meta.models`; the board's cost
tile shows the estimated share and turn headers mark it with `~`; `state.json` keeps `cost_estimate_usd` apart.

### 2. Config snapshots go stale; record facts on the event, not at the start
**Seen.** The run's `config.json` still said the worker used `opus` after 70 rounds on a different model. Anyone
reading the snapshot (a person, the board, the auditor) was misled.

**Rule.** A snapshot taken at the start describes the start. Anything that can change mid-run (model, backend,
endpoint, prompts) belongs on each turn record.

**Status.** Done for model and endpoint (see 1). System prompts were already recorded per turn.

## Messages between agents

### 3. "Sent" is not "received": check delivery against the recipient's prompt
**Seen.** Two manager instructions to the critic (a five-step plan in one case) never appeared in any of the
critic's next 128 prompts. Both times the manager wrote a REVISE review, the milestone round limit opened a gate
for the human, and after the human's "revise" a newer manager message went to the critic in its place. Nothing
logged the drop.

**Rule.** At every control-flow junction (gates, retries, restarts, phase changes), a message waiting to be
delivered must be delivered, merged, or explicitly superseded, and the outcome logged. The honest test of
delivery is whether the text appears in a later prompt of the recipient (or was cut into its running turn), not
whether the sender produced it.

**Status.** Fixed: a message held at a gate is shown to the manager to carry over, and a `message_superseded` event
is logged. The board works out each `<message>`'s fate from the recipient's prompts: read in turn X, waiting,
superseded, not delivered (with the reason), or not found.

### 4. Show the delivery status where the message was written
**Seen.** The board replaced each passed-on message with "→ passed to critic (shown as that agent's message)". It
gave neither the text nor whether it arrived, so the human could not answer the one question they had.

**Rule.** Fold, don't remove. Keep the message where it was written, with its status and a link to the turn that
read it. Link the recipient's side back to the source.

**Status.** Done: folded cards with status, header chips (`✉ critic ✓`), and "Its prompt carried the message from …".

### 5. Agents mis-address messages; validate the address and report it
**Seen.** In four turns the critic sent its instructions to "The critic", that is, to itself. In another case a
note was meant for an agent that takes no messages.

**Rule.** Resolve every address against the roster. Report the ones that resolve to the sender, nobody, or a
recipient that can't take messages, rather than dropping them or guessing.

**Status.** Done (`tags.routes`, `route_problem` events, shown on the board).

## Turn structure and cost

### 6. Long turns are the root of several problems
**Seen.** Worker turns ran 30–85 minutes. Non-urgent notes waited up to two hours for them to end. One 85-minute
turn ended in a provider error with no output. Recovery was cheap only because the session resumed with its
context intact.

**Rule.** Prefer smaller units of work: have the critic hand out the task step by step, not all at once. Short
turns make notes timely, failures cheap and progress visible.

**Status.** Open (a prompt and protocol change for the critic). Urgent notes already cut into a running turn.

### 7. Don't wake a higher level unless it has something to decide
**Seen.** Every check-in (a 120-character ping to the critic) was followed by a manager turn. In rounds 89–98
there were five check-ins, and four of the manager turns told the human "nothing needed from you". The critic's
replies were usually short (340–490 characters), but some earlier ones were 4–6k reports.

**Rule.** A check-in asks for a status line, not a report. A status update for the human doesn't need a model
turn. Wake the manager only when there is a question or a decision for it.

**Status.** Open.

### 8. Shared context grows without bound unless something prunes it
**Seen.** By milestone 4 the decisions register held 64 entries, 17.7k characters, and was the bulk of the kickoff
prompts for both manager and critic. A fixed reminder of 555 characters went into every critic verify turn,
about a quarter of a typical one.

**Rule.** Send what is in force and relevant to the current work, and point to the file for the rest. Send fixed
reminders once, or when they change.

**Status.** Open.

### 9. Know which errors are worth retrying
**Seen.** A provider's "balance must be greater than 0" refusal was retried within the turn before orch stopped
the run. Backup models only help if the running process was started with them configured; this one wasn't.

**Rule.** Classify provider errors: transient (overload, rate limit) → retry or fall back; account or billing →
stop and tell the human at once. Check that the process actually in use has the fallbacks you think it has.

**Status.** Partly: orch stops the run on a failed turn and supports backup models. Error classification is open.

## Auditing the system

### 10. An auditor is only as good as its digest
**Seen.** Four of the auditor's twelve standing findings were false, each traceable to something the digest did
not say:

| The auditor reported | What had happened | What the digest was missing |
|---|---|---|
| critic → worker relays cut to 16–65% | The worker got 100% of the critic's text; the rest was the critic's checklist, which the worker never gets on purpose | what orch withholds by design |
| a worker turn sent 17 chars, "the task was lost" | The turn resumed the same session, so the worker had its full context | session continuity |
| the human's raw chat reached the worker | It was a note the critic passed on, correctly | the note's real sender |
| spend far over the cap | Built on Claude Code's estimates (see 1) | how reliable each cost is |

**Rule.** A digest for an auditor has to encode the system's design intent, not just sizes and overlaps: what is
withheld on purpose, which sessions carry context, who really sent each message, and which numbers are estimates.
Otherwise the auditor reports the design as a defect.

**Status.** Done in `orch/audit.py`: relay shares count only what orch passes on and list what is withheld by
design; the turn table has session, model and `~` for estimated cost; notes are labelled "note from X".

### 11. Verify audit findings before acting on them
**Seen.** The auditor wrote true and false findings with the same confidence and specific-looking numbers. The
most important problems in the run (items 1 and 3) were not in its list at all: one came from checking its cost
figures, the other from building a delivery check for the board.

**Rule.** Treat an automated audit as a set of leads. Check each against the raw records before changing the
system, and look for what the auditor cannot see, such as dropped messages and mispriced costs, as well as what
it reports.
