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
    # the auditor (managed mode): the digest of the last stretch of rounds; its system prompt says what to do with it
    "audit_request": "Audit of rounds {{lo}}–{{hi}} (run {{run}}). At most {{points}} points.\n\n{{digest}}",
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
your rubric or scores. Put the instruction inside `<message to="{{critic}}">…</message>`; anything outside it is
for me. Then write your `<state>` for this part of the work: the instruction in short and the decisions that still
hold.
""",
    "manager_question": """\
{{human_messages}}

{{critic}} asks:

{{question}}

Please answer if you can, inside `<message to="{{critic}}">…</message>`. If only I can decide it, ask me in a
`<question>…</question>` (one per decision), and still give {{critic}} your best guess to work with until I reply.
If the answer changes the work, update your `<state>`.
""",
    "manager_review": """\
{{human_messages}}

{{critic}}'s report on milestone {{n}} ("{{title}}"):

{{report}}

Please score it against your rubric: a small table with a score out of 5 for each criterion, then your overall
score as `<score>x</score>` (out of 5). If the milestone is done, write me a short summary (tables welcome) and end
with `<decision>ACCEPT</decision>`. If it is not, put what still has to happen inside
`<message to="{{critic}}">…</message>`, in terms of the work and at a high level (only that is passed on, so leave
out milestones and scores), and end with `<decision>REVISE</decision>`. Either way, update your `<state>`: on
ACCEPT fold in what this milestone settled, on REVISE what still has to happen.
""",
    "manager_feedback": """\
I looked at milestone {{n}} ("{{title}}") and it is not finished yet:

{{comment}}

Please write what {{critic}} should do next, at a high level and in terms of the work (no milestones or scores),
inside `<message to="{{critic}}">…</message>`, and update your `<state>` with it.
""",
    "critic_brief": """\
{{instructions}}

{{human_messages}}

{{decisions}}

{{note}}

Please turn this into concrete instructions for {{worker}}: what to change, in what order, and what you
will check, with whatever settled matters the work must keep to ({{worker}} hears about them only from you). The
repository is at `{{cwd}}`.
""",
    "critic_review": """\
{{human_messages}}

{{worker}} sent this update:

{{worker_report}}

Please take a look at the changes. If everything that can be done here is done and the rest waits on a person,
don't tell {{worker}} to hold: propose a pause in `<pause>…</pause>` instead. End with your `<checklist>` and
`<verdict>PASS</verdict>` or `<verdict>FAIL</verdict>`.
""",
    "critic_answer": """\
About your question:

{{answer}}

If this changes what {{worker}} should do, say what. If it changes your verdict, end with a new `<verdict>`.
""",
    "critic_report": """\
{{human_messages}}

Please tell me how things stand{{limit_note}}: what was asked, what was done, what is still
open, any risks, and anything you assumed while a question was open. Tables are welcome; keep the prose short.
""",
    "worker_job_done": """\
{{human_messages}}

The background job you left running ({{jobs}}) {{state}}. Check its output and logs, finish what depended on it,
and reply with your summary as usual.
""",
    "manager_pause": """\
{{human_messages}}

{{critic}} proposes pausing the whole run, because what the agents can do is done and the rest waits on me (or
the authors):

{{proposal}}

Please decide. A pause stops the whole run (no agent works, no rounds are spent) until I continue it. If that is
right, write for me, in a few plain lines, what I need to do and what to send back, and end with
`<decision>PAUSE</decision>`. If there is still useful work meanwhile, put it inside
`<message to="{{critic}}">…</message>` and end with `<decision>CONTINUE</decision>`.
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
    "critic_urgent_checkin": """\
Please answer my message now as a brief check-in. {{worker}} is still working; this conversation leaves that
work running. Use the context and files already available, and distinguish current observations from results
that are still pending. Do not stop or restart their work, wait for it to finish, or begin a new review.
Reply directly to me inside `<message to="you">…</message>`, addressing me as "you". No worker instructions,
checklist, or verdict are needed for this check-in.
""",
    "manager_checkin": """\
{{human_messages}}

A quick update from {{critic}}:

{{update}}

No full review needed. Your reply is shown to me, not to {{critic}}: only a `<message to="{{critic}}">` is passed
on, so write one if {{critic}} should change course (and update your `<state>` if you do). If everything the agents can do is done and the rest waits on
me, write `<pause>what I need to do</pause>`: the whole run stops until I continue it. Otherwise a line for me is
enough, including anything you need me to do.
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
    "worker_fresh": "This is a fresh conversation: everything done so far is in the repository at `{{cwd}}`. "
                    "Read what you need there before you change anything.\n\n{{state}}\n\nMy instructions follow.",
    "manager_restart": """\
{{human_messages}}

We are picking the work up again in fresh conversations, so {{critic}} starts without the earlier history. Write
{{critic}} a short instruction for what to continue with now: where the current work stands, what comes next, and
what settled matters it must keep to. A few sentences; {{critic}} works out the details and keeps its own checklist.
Put it inside `<message to="{{critic}}">…</message>`; outside it, one line for me is enough. Then rewrite your
`<state>` from your notes and the decisions since: the standing instruction and what still holds, without what
no longer does.
""",
    "critic_restart": """\
{{human_messages}}

{{instruction}}

Please tell {{worker}} what to do next: concrete steps, somewhat more detailed than this, and what you will check.
{{worker}} also starts a fresh conversation and knows only what you tell it, so include whatever settled matters
the work must keep to. The repository is at `{{cwd}}`.
""",
    "state_edited": "I edited your working notes; they now read:\n\n{{state}}\n\nGo by them from now on.",
    "critic_state_instruction": "## My standing instruction\n\n{{state}}",
    "decisions_since": "Decided since those notes were last written:",
    "decisions_pointer": "Everything decided so far is in `{{path}}` (the later entry holds where two differ); look "
                         "one up there when you need it.",
    "decisions_intro_worker": "Decisions so far. They are binding; where two say different things, the later one holds.",
    "turn_interrupted": "Your last turn was cut off when the run was restarted, so here is the same message "
                        "again. Pick up from where you got to rather than starting over: check what you already "
                        "did (files, running jobs, logs) and don't restart a job that is still running.",
    "backend_switched": "You are taking over this work from a colleague, so this is your first message here. "
                        "Everything done so far is in the repository at `{{cwd}}`: read the current files "
                        "(and any logs or running jobs) before you change anything, and carry on from there.",
    # relayed answers (put in front of the agent's next message)
    "relay_answer_manager": "About your question ({{question}}): {{answer}}",
    "relay_answer_critic": "My answer about \"{{question}}\": {{answer}}",
    "relay_settled_critic": "Here is what I decided about \"{{question}}\":\n\n{{answer}}",
    # my conversation with the manager about one of its questions (sent from the board)
    "manager_question_chat": "About your question {{qid}} (\"{{question}}\"):\n\n{{message}}",
    "manager_question_settle": "Let's settle {{qid}} here. In a few lines, write what {{critic}} should go by "
                               "from now on: what we decided, and anything it changes in their instructions. "
                               "Write it as you would tell {{critic}}; nothing else.",
    "manager_question_settle_mine": "My answer: {{message}}\n\nLet's settle {{qid}} on that. In a few lines, write what "
                                    "{{critic}} should go by from now on: my decision, and anything it changes in their "
                                    "instructions. Go by my answer, not by your earlier recommendation; if it fits none "
                                    "of the options exactly, follow what I said. Write it as you would tell {{critic}}; "
                                    "nothing else.",
    "manager_resumed": "I ended the pause and said this (it went to {{critic}} too): \"{{message}}\"\n\n"
                       "What the pause was waiting for: {{waited}}",
    "relay_note": "{{note}}",
    "critic_reply_routing": """\
If I asked you a question or requested an explanation alongside this review or instruction, put your
complete answer inside `<message to="you">…</message>`. This sends the answer to me; address me as "you" in it.
Plain text outside that tag goes to {{worker}}, so use it only for their instructions or feedback, written as
your own request to "you". Keep any requested checklist and verdict separate. Answer my question yourself when
you can; it does not replace the ongoing review or create a new task for {{worker}}.
""",
    "manager_direct_voice": """\
When writing to {{critic}}, speak as "I" and address them as "you". Present instructions, questions and decisions
as your own. Keep my identity, our conversation and your role out of that message; do not attribute requests to
a user, manager or anyone above you. Ask for explanations as "Please explain this to me". Keep your reply to me separate
from the message you send to {{critic}}.
""",
    "urgent_resume": "I stopped you mid-turn for this, because it can't wait until you finish:\n\n{{note}}\n\n"
                     "Take it into account from here. Anything you were running in the foreground was ended; check "
                     "where it got to before you rely on it. Then finish the request you were working on, and reply "
                     "as it asked.",
    "urgent_cut_short": "(orch: I ended this turn early for an urgent message; the work above may be half done.)",
    "worker_cut_short": "(orch: I ended this turn early so {{critic}} could act on an urgent message from me at once. "
                        "The work may be half done; anything it was running in the foreground was ended.)",
    "worker_after_cut": "I ended your last turn early to change course; anything it was running in the foreground was "
                        "ended. What to do now follows.",
    "tag_reminder_manager": "A reminder, in case it helps (not a request to add anything): what is for {{critic}} goes in "
                            "<message to=\"{{critic}}\">…</message>; a decision only I can make in <question>…</question> "
                            "(one per question); stopping the whole run until I act in <pause>…</pause>; in reviews "
                            "<score>…</score> and <decision>…</decision>; your working state in <state>…</state> when it changes. "
                            "Everything outside tags is read by me.",
    "tag_reminder_critic": "A reminder, in case it helps (not a request to add anything): text outside tags reaches "
                           "{{worker}}; what is only for me goes in <message to=\"you\">…</message>; "
                           "questions for me in <question>…</question>; proposing to stop the whole run until "
                           "a person acts in <pause>…</pause>; each review ends with <checklist>…</checklist> and "
                           "<verdict>PASS</verdict> or <verdict>FAIL</verdict>.",
    "tag_reminder_worker": "A reminder, in case it helps (not a request to add anything): a question for me goes in "
                           "<question>…</question>; jobs you start detached are noticed automatically, and "
                           "<no-wait pid=\"…\"/> marks one my review should not wait for.",
    "session_reset": """\
This is a fresh conversation: your earlier one was cleared to keep turns quick, and the work carries on from where it
is. Here is what you need from before.

{{state}}

Read the files and logs you need before you act, and carry on from there.""",
    "report_checklist": "{{critic}}'s working checklist at its last review ({{done}} of {{total}} done):\n\n{{checklist}}",
    "relay_note_up": "{{critic}} mentioned: {{note}}",
}
