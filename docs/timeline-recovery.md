# Recovering historical step groups

The timeline normally groups messages using the critic's saved checklist snapshots. Parallel worker turns use
their recorded `par_step`; a parallel review uses the assignment of the worker it reviewed in that round.
This keeps concurrent workers on their own steps instead of putting them all under one shared step number.

If an older run lost checklist snapshots, a reviewed `timeline-steps.json` in that run's directory can restore
the missing transitions:

```json
{
  "run_id": "20261120-062003",
  "transitions": [
    {
      "turn": "r036-t02-critic",
      "when": "after",
      "step": 3,
      "evidence": "This reply explicitly hands the worker Step 3."
    }
  ]
}
```

`step` is the one-based position in the milestone's checklist. The referenced turn supplies the milestone and
timestamp. `after` (the default) starts the step at that turn's end; `start` includes that turn in the new step,
for example when a guidance turn introduces a new assignment. Use saved handoffs or assignment metadata as
evidence, not an arbitrary mention of a step in a message. Leave uncertain transitions uncorrected.

The file applies only when its `run_id` matches. Later checklist snapshots take over, and explicit parallel
assignments take precedence for individual worker turns and reviews. This changes the board's grouping only;
it does not rewrite messages, mark checklist items complete, or change the scheduler's state.

Critic review labels use the scheduler's verdict in the saved `round.json`, including checklist-based decisions
and cases where the scheduler overruled a reply's verdict. Until that round result is available, completed
sequential reviews can use their saved checklist snapshot. Running turns are not given a checklist verdict.
