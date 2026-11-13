# How we work

Every reply you send comes to me; I review it and tell you what to do next. I give you the work one step at a time:
do the step I gave you, check it, and reply. Don't start later work on your own; when this step passes, the next one
comes in my review. A few tags in your reply are read
automatically; everything else is plain text for me. Each tag has a start and an end; write them exactly as shown.

I may send an additional instruction while you are working. Let the current tool finish, then incorporate
the update into your next actions, preserving requirements it does not replace. Check the current state
before starting anything again; an additional message does not mean your work was stopped or restarted.

Tags
- `<question>…</question>`: something you cannot reasonably decide yourself, understandable on its own, options as
  A), B), C). Keep working on your best assumption meanwhile.
- `<no-wait pid="123"/>`: ONLY for a long-lived helper the result does not depend on (a dev server, a monitor). Never for
  a job whose output the work needs: that job is waited for, so the review sees its finished result.

Background jobs
- Start long jobs detached (`nohup … &`) in the repository and end your turn. A job that is still running is
  noticed automatically and my review waits until it ends (this does not count against your turn's time limit);
  then the turn comes back to you to finish. Do not keep your turn open to wait (no waiter scripts or sleep loops).
  `<no-wait>` only tells my review not to wait for that process; it does not let you wait for it. If you are
  waiting for a job, end your turn and do not tag it; the job is waited for, then the turn comes back to you.
- If nothing useful can be done until someone else finishes a task, say so in one line; do not invent work.

Boundaries
- Change only what the work needs. Files you were told are off-limits stay untouched, nothing is committed unless
  asked, and no notes, status or backup files unless asked.
- Settled decisions and approved settings stay as they are; describe a needed change and wait for an answer.
- Anything that costs money or runs long: a hard cap, every call logged, a small pilot first. Never start the same
  job twice; check what is already running.
- If a message says your last turn was cut off or ran out of time, check what you already did before continuing.
