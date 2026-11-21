# Human-reviewed audit decisions

Scope: review of x_case_factory run 20261120-062003, agreed 2026-11-21.
These are review dispositions, not evidence that code changes are deployed.

- [rejected] Restart context loss: r020-t01-worker and r020-t02-worker used the same resumed session; `continue` retained the delivered task.
- [rejected] Isolated critic report sessions: r022 and r028 verification/report turns shared a session; consecutive session turn numbers did not identify new sessions.
- [rejected] Human-answer leakage: the cited Q3/Q10 decisions were deliberately forwarded with role-view wrapping. Provenance alone does not establish a forbidden route.
- [accepted design] Milestone session resets and their handovers are intentional. No context-loss failure was established in the cited reset.
- [won't fix] Repeated critic verification template: user chose to leave it unchanged.
- [won't fix] Repeated manager review instructions: user chose to leave them unchanged. Dynamic checklist and rubric text is useful review context.
- [confirmed; fix agreed] Worker acknowledgment loop: untagged completion prose plus FAIL repeatedly dispatched finished workers. The replacement protocol requires explicit worker messages; check the recorded protocol version before evaluating later turns.
- [merged] Broad critic feedback and supposed identical tiny reviews: covered by explicit message routing and concise progress records. The cited tiny inputs included different worker ids and acknowledgments; equal lengths were not equal content.
- [fix agreed] Delayed progress notes: timestamp and identify short historical critic progress records, delivered at the manager's next turn. Delays alone do not violate the nonurgent policy; no incorrect manager decision was established.
- [unproven cause] Slow worker/critic turns: short final reports do not establish degradation or excessive reasoning. r039 had a roughly 603-second pre-response gap and a 120-second shell call; the gap's cause is unresolved.
- [prompt improvement agreed] Auditor omitted required findings bullet prefixes. Highlight `- ` and check formatting before replying. Parser hardening was deferred by the user.
