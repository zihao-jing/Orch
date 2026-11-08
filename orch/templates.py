"""Default prompt templates and a tiny renderer.

Templates use ``{{name}}`` placeholders. Unknown placeholders are left as-is so
user text containing braces is never mangled. Every template can be overridden
per monitor under ``controller.templates``.
"""

from __future__ import annotations

import re
from typing import Any, Dict

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(template: str, **values: Any) -> str:
    def sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key in values and values[key] is not None:
            v = str(values[key])
            before = m.string[:m.start()].rstrip(" \t")
            if key in ("manager", "critic", "worker") and v.startswith("the ") and (not before or before.endswith((".", "!", "?", "\n", ":", '"'))):
                v = "T" + v[1:]  # "the critic" at the start of a sentence
            return v
        return m.group(0)

    out = _VAR.sub(sub, template)
    # collapse runs of 3+ blank lines left by empty sections
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


DEFAULT_TEMPLATES: Dict[str, str] = {
    # ---------------- pipeline mode ----------------
    "worker_initial": """\
# Overall goal
{{goal}}

# Controller instructions
{{instructions}}

# Your role
You are **{{agent}}** ({{role}}). {{description}}

# Your task (round {{round}})
{{task}}

{{human_messages}}

Work directly in the workspace (current directory: `{{cwd}}`).
When you finish, reply with a concise report: what you changed, how you checked it, and anything still open.
""",
    "worker_feedback": """\
# Round {{round}} — feedback from verifiers
Your previous work was reviewed and has not passed yet.

{{feedback}}

{{human_messages}}

Address every point in the workspace, then reply with a concise report of what you changed and how you checked it.
""",
    "verifier_initial": """\
# Overall goal
{{goal}}

# Controller instructions
{{instructions}}

# Your role
You are **{{agent}}** ({{role}}). {{description}}

# Round {{round}} — reports from the workers you review
{{worker_reports}}

{{human_messages}}

Inspect the actual state of the workspace (current directory: `{{cwd}}`); do not trust the reports alone.
If anything is wrong or missing, list concrete, actionable feedback addressed to the workers.
Finish with exactly one final line: `VERDICT: PASS` or `VERDICT: FAIL`.
""",
    "verifier_followup": """\
# Round {{round}} — workers revised their work
{{worker_reports}}

{{human_messages}}

Re-verify the workspace from scratch against the goal and your earlier feedback.
Finish with exactly one final line: `VERDICT: PASS` or `VERDICT: FAIL`.
""",
    # ---------------- llm-controller mode ----------------
    "controller_system": """\
You are the CONTROLLER of a team of AI agents working toward a shared goal in one workspace.
You do not do the work yourself. Each round you decide which agents get which messages, read
their reports, route feedback between them, and decide when the goal is met.

Reply EVERY time with one JSON object inside a ```json fenced block, shaped like:

```json
{
  "summary": "one or two sentences on the current state and why you chose this step",
  "dispatch": [
    {"agent": "<agent name>", "message": "<complete task / feedback for that agent>"}
  ],
  "parallel": false,
  "done": false,
  "final_report": ""
}
```

Rules:
- `agent` must be one of the roster names exactly.
- Each agent only sees the messages you send it (plus its own history), so include all context it needs,
  including feedback from other agents when routing it.
- Set `parallel` to true only when the dispatched agents will not edit the same files.
- When the goal is fully met and verified, set `done` to true, leave `dispatch` empty and write `final_report`.
""",
    "controller_initial": """\
# Overall goal
{{goal}}

# Controller instructions
{{instructions}}

# Roster
{{roster}}

# Workspace
Agents work in `{{cwd}}`. Full transcripts of every turn are saved under `{{run_dir}}` if you need to read them.

{{human_messages}}

This is round {{round}} of at most {{max_rounds}}. Reply with your first JSON directive.
""",
    "controller_update": """\
# Round {{round}} of at most {{max_rounds}} — results of your last dispatch
{{results}}

{{human_messages}}

Reply with your next JSON directive.
""",
    "agent_initial": """\
# Overall goal
{{goal}}

# Your role
You are **{{agent}}** ({{role}}). {{description}}

# Message from the controller (round {{round}})
{{message}}

Work in the workspace (current directory: `{{cwd}}`). Reply with a concise report when done.
""",
    "agent_followup": """\
# Message from the controller (round {{round}})
{{message}}

Reply with a concise report when done.
""",
    # ---------------- managed mode (manager -> critic -> worker) ----------------
    # Messages read like a normal conversation: nobody is told about a workflow. {{manager}}, {{critic}}
    # and {{worker}} are the agents' display names.
    "manager_plan": """\
{{goal}}

{{instructions}}

{{human_messages}}

Could you split this into milestones? Please list them in the order the work should happen, as a numbered
list with one milestone per line: "1. Short title — what has to be true when it is done". Then tell me how
you will judge whether a milestone is finished: a small table of criteria with what a 5 out of 5 looks like,
and the score you need before you accept one. {{critic}} oversees the hands-on work and will report to you when
a milestone is ready.
""",
    "manager_plan_revise": """\
{{comment}}

Please send me the updated milestone list in the same form.
""",
    "manager_kickoff": """\
{{human_messages}}

{{decisions}}

Milestone {{n}} of {{total}} is next: {{milestone}}

Please write the instruction that gets {{critic}} started on it: what is needed, what matters most and what to
watch out for. Describe the work itself and keep it high level; {{critic}} does not hear about milestones,
your rubric or scores. I will pass on your reply as it is.
""",
    "manager_question": """\
{{human_messages}}

{{critic}} asks:

{{question}}

Please answer if you can. If only I can decide it, start your reply with a line `QUESTIONS FOR YOU:`
followed by the question(s), and still give {{critic}} your best guess to work with until I reply.
""",
    "manager_review": """\
{{human_messages}}

{{critic}}'s report on milestone {{n}} ("{{title}}"):

{{report}}

Please score it against your rubric: a small table with a score out of 5 for each criterion, then a line
"Overall score: x/5". If the milestone is done, write me a short summary (tables welcome) and end with the
line `DECISION: ACCEPT`. If it is not, add a line `For {{critic}}:` followed by what still has to happen,
in terms of the work and at a high level (only that part is passed on, so leave out milestones and scores),
and end with the line `DECISION: REVISE`.
""",
    "manager_feedback": """\
I looked at milestone {{n}} ("{{title}}") and it is not finished yet:

{{comment}}

Please write what {{critic}} should do next, at a high level and in terms of the work (no milestones or scores);
I will pass on your reply as it is.
""",
    "critic_brief": """\
{{instructions}}

{{human_messages}}

{{decisions}}

{{note}}

Please turn this into concrete instructions for {{worker}}: what to change, in what order, and what you
will check. The repository is at `{{cwd}}`.
""",
    "critic_review": """\
{{human_messages}}

{{worker}} sent this update:

{{worker_report}}

Please take a look at the changes. If {{worker}} has nothing useful left to do until a task of mine or the
authors' is done, don't tell {{worker}} to hold: propose a pause with a `WAITING FOR YOU:` paragraph instead.
End with one final line: `VERDICT: PASS` or `VERDICT: FAIL`.
""",
    "critic_answer": """\
About your question:

{{answer}}

If this changes what {{worker}} should do, say what. If it changes your verdict, end with a new
`VERDICT:` line.
""",
    "critic_report": """\
{{human_messages}}

Please tell me how things stand{{limit_note}}: what was asked, what was done, what is still
open, any risks, and anything you assumed while a question was open. Tables are welcome; keep the prose short.
""",
    "worker_job_done": """\
{{human_messages}}

The job you were waiting on ({{jobs}}) {{state}}. Check its output and logs, finish what depended on it, and reply
with your summary as usual. If you start another job and the rest depends on it, end with the same line again.
""",
    "manager_pause": """\
{{human_messages}}

{{critic}} proposes pausing the work, because what comes next is waiting on me (or the authors):

{{proposal}}

Please decide. If nothing useful can be done until that is finished, write for me, in a few plain lines, what I need
to do and what to send back, and end with the line `DECISION: PAUSE`; the work then waits, without spending rounds,
until I say it is done. If there is still useful work meanwhile, write `For {{critic}}:` followed by what to do in
the meantime, and end with the line `DECISION: CONTINUE`.
""",
    "critic_continue": """\
{{human_messages}}

About your proposal to pause: not yet. {{guidance}}

Please tell {{worker}} what to do next.
""",
    "critic_resume": """\
{{human_messages}}

The work was paused while waiting for this:

{{waited}}

That is done now. My message: {{message}}

Please check where things stand and tell {{worker}} what to do next.
""",
    "critic_guidance": """\
{{human_messages}}

I read your report, and more is needed:

{{guidance}}

Please add what is needed to your checklist and tell {{worker}} what to do next.
""",
    "critic_checkin": """\
{{human_messages}}

A quick update for me, please: two or three lines on how it is going and anything I should know. No full
report needed.
""",
    "manager_checkin": """\
{{human_messages}}

A quick update from {{critic}}:

{{update}}

No full review needed. Your reply is shown to me, not to {{critic}}: only a paragraph starting with
`NOTE FOR {{critic}}:` is passed on, so write one if {{critic}} should change course. If nothing useful can be done
until I finish something, write a paragraph starting `WAITING FOR YOU:` saying what I need to do; the work then
pauses, without spending rounds, until I say it is done. Otherwise a line for me is enough, including anything you
need me to do.
""",
    "worker_start": """\
{{task}}

{{feedback}}

{{decisions}}

{{human_messages}}

The repository is at `{{cwd}}`.
""",
    "worker_next": """\
{{human_messages}}

{{feedback}}
""",
    "worker_timeout": "Your last turn ran past the time limit and was stopped. Check where things stand (running "
                      "jobs, logs, partial results) before you continue.",
    "milestone_fresh": "We are starting a new part of the work, so this is a fresh conversation. Everything done so "
                       "far is in the repository at `{{cwd}}`: read the relevant files, logs and any running jobs "
                       "before you change anything, and carry on from there.",
    # the decisions register: everything settled so far, given in full when a milestone starts
    "decisions_intro": "Decisions so far. They are binding; where two say different things, the later one holds. "
                       "The list is kept up to date in `{{path}}`.",
    "turn_interrupted": "Your last turn was cut off when the run was restarted, so here is the same message "
                        "again. Pick up from where you got to rather than starting over: check what you already "
                        "did (files, running jobs, logs) and don't restart a job that is still running.",
    "backend_switched": "You are taking over this work from a colleague, so this is your first message here. "
                        "Everything done so far is in the repository at `{{cwd}}`: read the current files "
                        "(and any logs or running jobs) before you change anything, and carry on from there.",
    # relayed answers (put in front of the agent's next message)
    "relay_answer_manager": "About your question ({{question}}): {{answer}}",
    "relay_answer_critic": "I got an answer on \"{{question}}\": {{answer}}",
    "relay_settled_critic": "I talked \"{{question}}\" over with the authors. Here is where we landed:\n\n{{answer}}",
    # my conversation with the manager about one of its questions (sent from the board)
    "manager_question_chat": "About your question {{qid}} (\"{{question}}\"):\n\n{{message}}",
    "manager_question_settle": "Let's settle {{qid}} here. In a few lines, write what {{critic}} should go by "
                               "from now on: what we decided, and anything it changes in their instructions. "
                               "Write it as you would tell {{critic}}; nothing else.",
    "relay_note": "{{note}}",
    "relay_note_up": "{{critic}} mentioned: {{note}}",
}
