"""Write docs/prompts-review.md: every prompt and text Orch's new runtime gives an agent or the user, filled in. Run: python3 tools/prompts_review.py"""
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from rt_helpers import C, agent, snapshot  # noqa: E402
from test_rt_h import values  # noqa: E402
from orch.runtime import reminders, texts  # noqa: E402
from orch.runtime.contracts import TextKey as K  # noqa: E402

P = ROOT / "orch/runtime/prompts"

# who reads each text, and when Orch sends it (one line each)
GROUPS = [
    ("To the critic", "critic", [
        (K.WORKER_FINAL, "A worker ends a turn with no job running: its reply goes to the critic for review."),
        (K.WORKER_INTERMEDIATE, "A worker ends a turn while a job it started still runs: passed on, not for review."),
        (K.WORKER_TO_CRITIC, "No longer sent: a worker's reply to the user's chat stays with the user."),
        (K.WORKER_FAILED, "A worker's turn failed for good (after retries and backups)."),
        (K.DIRECT_MESSAGE_NOTICE, "The manager wrote to a worker: the critic is told (the user's chats are not)."),
        (K.LABEL_BROKEN_PASSED, "A message with a broken tag was returned once and came back broken: passed on, marked."),
        (K.UNKNOWN_WORKER, "The critic wrote to a worker that does not exist (or is closed)."),
        (K.CHECKLIST_UNREADABLE, "The critic's checklist file cannot be read or parsed."),
        (K.NOBODY_WATCHES, "The progress checks on a worker failed twice: nobody watches its runs."),
        (K.JOB_NO_PROGRESS, "A worker's job has shown no progress for the set time."),
        (K.JOBS_STOPPED, "No longer sent: the critic's reminder shows a worker as stopped."),
        (K.MONITOR_REPORT, "The monitor found a worker's run off track or stopped (once per change, then hourly)."),
        (K.HANDOVER_REQUEST_CRITIC, "A worker moves to a new model or session: the critic writes its instruction."),
    ]),
    ("To the manager", "manager", [
        (K.CRITIC_REPORT, "The critic wrote text outside tags: it reaches the manager as the critic's report."),
        (K.PLAN_REQUEST, "The run starts with a goal and no plan."),
        (K.PLAN_LIST_MISSING, "The manager's plan has no milestone list Orch can read."),
        (K.MILESTONE_START, "The plan (or the previous milestone) is approved: the next milestone starts."),
        (K.CHECKLIST_COMPLETE, "Every item on the critic's checklist is ticked: the manager reviews the milestone."),
        (K.REVIEW_RUBRIC_MISSING, "The manager reviewed a milestone that has no rubric."),
        (K.REVIEW_SCORE_LOW, "The manager's ACCEPT scored below the pass score."),
        (K.GATE_ANSWERS, "The user answered one or more gates: all answers in one message."),
        (K.MILESTONE_CHANGE_REQUEST, "The user asked to change the milestones."),
        (K.MILESTONE_CHANGE_UNREADABLE, "The manager's changed milestone list cannot be read."),
        (K.SILENCE_CHECK, "Nothing has happened for the silence time (2 min per try) and no one is writing."),
        (K.NO_WORKERS, "The critic wrote to a worker but there is none: the manager should ask the user."),
        (K.HANDOVER_REQUEST_MANAGER, "The critic moves to a new model or session: the manager writes its instruction."),
        (K.MANAGER_TAKEOVER, "A new manager session starts: where the earlier turns are saved."),
        (K.DISCUSSION_APPLY, "The user ends a discussion and applies it: the drafts become real messages."),
    ]),
    ("To any agent, about its own turn or session", "worker-1", [
        (K.LABEL_RETURNED, "The agent wrote a broken or unknown tag: returned to it once to fix."),
        (K.JOB_WAKEUP, "A job the agent started has ended and its program did not wake it itself."),
        (K.JOB_LOST, "Orch lost track of a job (e.g. after a restart); it may still be running."),
        (K.RETRY_CONTINUE, "A turn failed and is tried again in the same session."),
        (K.RESTART_CONTINUE, "The agent's program was restarted (silence or failure) in the same session."),
        (K.MESSAGE_AGAIN, "After a model switch: the unfinished message again, to the new model."),
        (K.SESSION_RETURN, "An earlier session is reopened: what happened since."),
        (K.SESSION_FRESH, "A new session without a handover (e.g. the old one was lost)."),
        (K.HANDOVER_WRITE, "Before a switch: the old model writes a handover for its successor."),
        (K.HANDOVER_INSTRUCTION, "The first message of the new session: the instruction and the handover."),
    ]),
    ("To the monitor", "monitor", [
        (K.MONITOR_CHECK, "Every `check_every` minutes while a worker writes or waits for a job."),
    ]),
    ("Gates (shown to the user, with buttons)", None, [
        (K.GATE_PLAN, "The manager wrote a plan: approve it to start milestone 1."),
        (K.GATE_MILESTONE, "The manager accepted a milestone."),
        (K.GATE_MILESTONE_CHANGE, "The manager proposes changed milestones."),
        (K.GATE_SILENCE, "Silence checks went unanswered."),
        (K.GATE_FROZEN, "An agent writes but shows no output for long."),
        (K.GATE_ROUND_LIMIT, "The run reached its round limit."),
        (K.GATE_BILLING, "A provider refused for billing or quota."),
        (K.GATE_MISSING_CLI, "An agent's program is not installed."),
        (K.GATE_ALL_FAILED, "Every model of an agent failed."),
        (K.GATE_AGENT_FAILED, "One agent failed for good."),
        (K.GATE_HANDOVER, "A handover could not be written."),
    ]),
    ("Questions and answers to a dashboard command (user)", None, [
        (K.ASK_JOBS_CLOSE, "Closing a worker whose jobs still run."),
        (K.ASK_JOBS_DELETE, "Deleting a worker whose jobs still run."),
        (K.ASK_JOB_TERMINAL, "Opening a terminal on an agent that is writing or has jobs."),
        (K.ASK_FORCE_SWITCH, "Switching the model of an agent that is busy."),
        (K.SWITCH_PENDING, "A switch waits for the agent's turn to end."),
        (K.SMOKE_TEST, "The short real turn that tests a new model before a switch (sent to that model)."),
        (K.SMOKE_FAILED, "That test failed."),
        (K.COMMAND_REFUSED, "Any command Orch refuses, with the reason."),
    ]),
    ("Notices to the user (in the messages)", None, [
        (K.NOTE_AGENT_FAILED, "An agent failed."),
        (K.NOTE_WAKEUP_SENT, "Orch woke an agent after its job ended."),
        (K.NOTE_MONITOR_FAILED, "The monitor's checks fail."),
        (K.NOTE_SWITCHED, "A model switch finished."),
        (K.NOTE_REVIEW_WITHDRAWN, "A milestone review was withdrawn (the work changed)."),
        (K.NOTE_FINISHED, "All milestones are done."),
        (K.NOTE_MIGRATED, "An old project was upgraded to the new runtime."),
    ]),
]

REMINDERS = [
    # key, to whom, when
    (K.REM_WORKER_LINE, "critic", "Every message: one line per worker."),
    (K.REM_LABELS, "critic", "Every message: the tags it may use, with the worker ids."),
    (K.REM_WORKERS, "critic", "When the pool of workers changes; else 15% (only with 2+ workers)."),
    (K.REM_UNWATCHED, "critic", "25%, while a worker's checks have failed."),
    (K.REM_CHECKLIST, "critic, manager", "Once per session, and again when the checklist path changes."),
    (K.REM_GOLDEN_PATH, "manager (critic only when no golden words)", "When the board is new to the session or changed."),
    (K.REM_GOLDEN_USE, "manager (critic only when no golden words)", "With the line above, else 25%."),
    (K.REM_GOLDEN_KEEP, "manager", "25%."),
    (K.REM_GOLDEN_SIZE, "manager", "Once when the board grows over the limit, then 25% while it stays over."),
    (K.REM_MESSAGE_CRITIC, "manager", "30%, not during a discussion."),
    (K.REM_OPEN_GATES, "manager", "With the line above, when gates are open."),
    (K.REM_NIGHT, "manager", "When night mode starts, then 25%."),
    (K.REM_NIGHT_OVER, "manager", "Once when night mode ends."),
    (K.REM_DISCUSSION, "critic, manager", "When a discussion opens, then 40%."),
    (K.REM_DISCUSSION_OVER, "critic, manager", "No longer sent."),
    (K.REM_ASK_AND_END, "worker", "10%."),
    (K.REM_WRITING, "manager, critic, worker", "25%: the first writing rule plus one at random."),
]


BODY = {K.WORKER_FINAL: "«the worker's reply»", K.WORKER_INTERMEDIATE: "«the worker's reply»",
        K.WORKER_TO_CRITIC: "«the worker's reply»", K.DIRECT_MESSAGE_NOTICE: "«what the user wrote»",
        K.LABEL_BROKEN_PASSED: "«the sender's reply»", K.MONITOR_REPORT: "«the monitor's reply»",
        K.MESSAGE_AGAIN: "«the unfinished message»", K.HANDOVER_INSTRUCTION: "«the instruction and the handover»",
        K.CRITIC_REPORT: "«the critic's plain text»", K.MILESTONE_CHANGE_REQUEST: "«the user's request»"}
PATHS = {K.CHECKLIST_UNREADABLE: "/proj/.orch/checklist.md", K.REM_CHECKLIST: "/proj/.orch/checklist.md"}


def vals(k):
    import time
    v = values(k)
    if "path" in v:
        v["path"] = PATHS.get(k, "/proj/.orch/golden.md")
    if k == K.HANDOVER_REQUEST_MANAGER:
        v["agent"] = "critic"
    if "since" in v:
        v["since"] = time.time() - 3 * 3600
    if k == K.COMMAND_REFUSED:
        v["why"] = "terminal_open"
    if k == K.REM_WRITING:
        first, *rest = reminders.writing_rules(C.Role.CRITIC)
        v["rules"] = [first, rest[2]]
    return v


def block(text):
    return text.strip()


def team():
    s = snapshot(workers=2, golden=C.GoldenBoard("/proj/.orch/golden.md", "d1", chars=100))
    s.agents["monitor"] = agent("monitor")
    return s


def main():
    s = team()
    covered = {k for _, _, items in GROUPS for k, _ in items} | {k for k, _, _ in REMINDERS}
    missing = [k.value for k in K if k not in covered]
    assert not missing, missing
    out = ["# Orch prompts for review", "",
           "Everything the new runtime writes to an agent or the user, filled in with sample values (team: the manager, "
           "the critic, worker-1, worker-2, the monitor; golden board `/proj/.orch/golden.md`). Generated from the code "
           "on the `redesign` branch; the source of each part is named. Running the tool again overwrites this file.", "",
           "## How an agent's input is made", "",
           "- **System prompt**, once per session: `prompts/<role>.md` + `prompts/protocol/<role>.md` + \"How to "
           "write\" (the `## All` and `## <role>` rules of `prompts/style.md`). The monitor gets no writing rules. "
           "Placeholders: `{critic}` (the critic's name), `{golden_board}`, `{job_line}` (per program, §1.5). Put together "
           "in `orch/runtime/texts.py` `system_prompt()`. Folder: `orch/runtime/prompts/`.",
           "- **Each message**: Orch's text for the situation (§2; it wraps the sender's words where there are some), "
           "then one `<system-reminder>` block with the reminder lines due (§3). Words in `orch/runtime/texts.py` "
           "`TEXTS`; when each reminder is due in `orch/runtime/reminders.py`.",
           "- A message from one agent to another with no Orch text is passed as written, plus the reminder block.",
           "- **User-facing texts** (gates, command answers, notices, §4) are never sent to an agent.", ""]

    out += ["## 1. System prompts", ""]
    for n, (aid, role) in enumerate([("manager", "manager"), ("critic", "critic"), ("worker-1", "worker"),
                                     ("monitor", "monitor")], 1):
        srcs = f"`prompts/{role}.md`, `prompts/protocol/{role}.md`" + (", `prompts/style.md`" if role != "monitor" else "")
        out += [f"### 1.{n} {role.capitalize()}", "", f"Sent once at the start of each session. Source: {srcs}.", "",
                block(texts.system_prompt(s, aid)), ""]
    aud = (P / "auditor.md").read_text().replace("{points}", "5")
    out += ["### 1.5 Job line (`{job_line}` above, by program)", "",
            "Source: `JOB_LINES` in `texts.py`. Others get: \"Use your program's own background option to start one.\"", ""]
    out += [f"- **{k}**: {v}" for k, v in texts.JOB_LINES.items()] + [""]
    out += ["### 1.6 Auditor", "", "Sent to a one-off auditor process at each audit (every `audit_every` rounds and "
            "once when the run finishes), followed by the digest of the stretch (§5). Source: `prompts/auditor.md`; "
            "`{points}` from settings (5 here).", "", block(aud), ""]

    out += ["## 2. Message texts to agents", "",
            "«…» marks where the sender's own words go. Sample values are made up.", ""]
    sec = 2
    num = 0
    for title, reader, items in GROUPS:
        if reader is None:
            continue
        num += 1
        out += [f"### {sec}.{num} {title}", ""]
        for k, when in items:
            t = texts._Text(s, C.TextRef(k, vals(k)), reader, BODY.get(k, ""))
            out += [f"**`{k.value}`** — {when}", "", block(texts._compose(t)), ""]

    out += ["## 3. Reminders", "",
            "Each message's due lines go into one block at its end, never a line twice. Chances are drawn per message, "
            "seeded by its id (a resend gets the same lines). Handover-writing messages get none. Monitor and auditor "
            "get none.", "", "| Line | To | When |", "|---|---|---|"]
    for k, who, when in REMINDERS:
        out.append(f"| `{k.value}` | {who} | {when} |")
    out += ["", "Each line, filled in:", ""]
    for k, who, _ in REMINDERS:
        reader = "manager" if who.startswith("manager") else who.split(",")[0].split(" ")[0]
        v = vals(k)
        if k == K.REM_WORKER_LINE:
            v = reminders.worker_line(s, s.agents["worker-1"])
        t = texts._Text(s, C.TextRef(k, v), reader)
        out += [f"**`{k.value}`**", "", block(texts._compose(t)), ""]
    msg = C.Message(id="x42", sender="worker-1", recipient="critic", body="Done: labels checked, 3 fixed.", created=s.now)
    s2 = replace(s, messages={"x42": msg})
    out += ["Example: a worker's reply as the critic receives it (no random lines drawn):", "",
            block(texts.render(s2, replace(msg, text=C.TextRef(K.WORKER_FINAL, {"worker": "worker-1"}))).text), ""]

    out += ["## 4. Texts shown to the user", "", "Not sent to agents. Shown in the dashboard.", ""]
    num = 0
    for title, reader, items in GROUPS:
        if reader is not None:
            continue
        num += 1
        out += [f"### 4.{num} {title}", ""]
        for k, when in items:
            t = texts._Text(s, C.TextRef(k, vals(k)), "user")
            out += [f"**`{k.value}`** — {when}", "", block(texts._compose(t)), ""]

    out += ["## 5. Templates", "",
            "- **Audit digest** (`orch/runtime/audit.py` `build()`), sent after the auditor prompt: headings `# Audit of "
            "run …`, `## Agents`, `## Facts Orch checked (exact)`, `## Messages`, `## Turns`, `## Your findings file`, "
            "plus the user's audit notes. Facts and quotes come from the run.",
            "- **`orch init --project`** writes `.orch/settings.yaml`, `agents/<role>/agent.yaml` and "
            "`agents/manager/goal.md` (\"Describe what you want done, and any decisions already made.\"). These are "
            "settings, not prompts.", ""]
    (ROOT / "docs/prompts-review.md").write_text("\n".join(out))
    print(len(out), "lines")


main()
