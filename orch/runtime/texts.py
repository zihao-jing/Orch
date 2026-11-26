"""Every text Orch writes: messages to agents with their reminder, words for the user, system prompts. Pure, apart from
reading the prompt files that come with Orch.

A text is written for its reader. To an agent "I" is its own user, whoever that is: no text names an agent's upper
levels, and nothing names one worker to another. Labels are explained in prompts/protocol/ only.
"""
from __future__ import annotations

import hashlib
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Union

from . import reminders
from .contracts import (USER, AgentStatus, ErrorClass, FailCause, Health, Message, MilestoneStatus, Problem,
                        Refusal, Rendered, Role, Snapshot, SwitchReason, TextKey, TextRef)
from .reminders import PROMPTS, given_to, writing_rules

K = TextKey

# how a worker starts a job its CLI keeps track of (JOB-1; docs/probes)
JOB_LINES: Dict[str, str] = {
    "claude": "To start one, use the Bash tool with `run_in_background: true`.",
    "qoder": "To start one, use the Bash tool with `run_in_background: true` and always set `timeout`, up to 7200000 "
             "(2 hours); without it the job is stopped after 10 minutes. Split longer work into jobs that fit.",
    "qwen": "To start one, use `run_shell_command` with `is_background: true`.",
    "agy": "To start one, use `run_command` with `WaitMsBeforeAsync: 0`.",
    "codex": "To start one, run the command with a short `yield_time_ms` (500), so that it goes on after the call returns "
             "and your turn ends.",
    "opencode": "To start one, use the shell tool with `background: true`.",
}

STATUS = {
    AgentStatus.IDLE: "idle", AgentStatus.WRITING: "writing", AgentStatus.WAITING_JOB: "waiting for a job",
    AgentStatus.FAILED: "failed", AgentStatus.CLOSED: "closed", "stopped": "stopped for now",
    "switching": "changing its model", "retrying": "trying again after an error",
}
WHY = {
    FailCause.NO_ANSWER: "it did not answer", FailCause.FROZEN: "it stopped responding",
    ErrorClass.TRANSIENT: "its provider kept failing", ErrorClass.BILLING: "a billing problem",
    ErrorClass.MISSING_CLI: "its program is missing", ErrorClass.SESSION_LOST: "its conversation could not be continued",
    ErrorClass.OTHER: "an unknown error", "unreadable": "its answer could not be read", SwitchReason.USER: "chosen by hand", SwitchReason.BACKUP: "its model failed",
    SwitchReason.RETURN: "its usual model works again",
}
HEALTH = {Health.HEALTHY: "healthy", Health.QUIET: "quiet", Health.OFF_TRACK: "off track", Health.STOPPED: "stopped"}
PROBLEM = {
    Problem.UNCLOSED: "its start or end is missing", Problem.EMPTY: "it is empty",
    Problem.NO_RECIPIENT: "it names no one you can write to", Problem.REMOVED: "there is no such label",
    Problem.BAD_VALUE: "its value is not one of those allowed",
}
REFUSED = {
    Refusal.UNKNOWN: "There is no such item.", Refusal.EMPTY: "Nothing was entered.",
    Refusal.CLOSED: "It is closed.", Refusal.DELETED: "It was deleted.",
    Refusal.WRONG_ROLE: "This is not possible with this agent.", Refusal.ALREADY: "This is already the case.",
    Refusal.NOT_OPEN: "There is nothing to end.", Refusal.TERMINAL_OPEN: "A terminal is open on this agent.",
    Refusal.GATE_OPEN: "A question is still open.", Refusal.SWITCHING: "The model switch is too far along to change.",
    Refusal.FILE_CHANGED: "The file has changed since you opened it.",
    Refusal.NOT_POSSIBLE: "This agent's program cannot do it.",
}
PHRASES = {"status": STATUS, "why": WHY, "health": HEALTH, "problem": PROBLEM}
AGENT_VALUES = ("worker", "agent", "by", "monitor", "sender")


class _Text:
    """One text being written: its values, and who reads it (role None: the user)."""

    def __init__(self, snap: Snapshot, ref: TextRef, reader: str = USER, body: str = "",
                 seen: Optional[Dict[str, str]] = None):
        a = snap.agents.get(reader)
        self.snap, self.key, self.reader, self.role = snap, ref.key, reader, a.role if a else None
        self.d = dict(ref.values)
        self.body = body.strip() or str(self.d.get("body") or "").strip()
        self.seen = seen or {}
        self.given: Dict[str, str] = {}
        self.body_used = False

    def get(self, name: str) -> Any:
        return self.d.get(name)

    def name(self, agent_id: str) -> str:
        """An agent as the reader knows it: the critic knows workers by their ids, the others by their display names."""
        a = self.snap.agents.get(agent_id)
        if agent_id == USER:
            return "you" if self.role is None else "me"
        if a is None:
            return agent_id
        if self.role == Role.CRITIC:
            return agent_id
        if a.display and a.display != agent_id:
            return a.display
        return agent_id if a.role == Role.WORKER else f"the {a.role.value}"

    @property
    def critic(self) -> str:
        return self.name(self.snap.critic.id) if self.snap.critic else "the critic"

    def value(self, name: str) -> str:
        if name == "body":
            self.body_used = True
            return self.body
        if name == "critic":
            return self.critic
        v = self.get(name)
        if name in AGENT_VALUES:
            return self.name(str(v or ""))
        if name in PHRASES and not isinstance(v, (list, dict)):
            return _phrase(name, v)
        if name == "assignment":
            return str(v or "").rstrip(" .")
        if name == "choice" and isinstance(v, dict):
            v = v.get("label")
        if isinstance(v, (list, tuple)):
            return ", ".join(f"`{x}`" if name == "jobs" else str(x) for x in v)
        return "" if v is None else str(v)

    def fill(self, template: str) -> str:
        def sub(m: "re.Match[str]") -> str:
            v, before = self.value(m.group(1)), m.string[:m.start()]
            if v.startswith("the ") and m.group(1) in AGENT_VALUES + ("critic",) \
                    and re.search(r"(^|[.!?]\s|\n|\n- )$", before):
                v = "T" + v[1:]
            return v
        return re.sub(r"\{([a-z_]+)\}", sub, template)

    def jobs(self, before: str, after: str = "") -> str:
        """`before` + the jobs + `after`, or nothing without jobs."""
        return before + self.value("jobs") + after if self.get("jobs") else ""


def _phrase(name: str, v: Any) -> str:
    table = PHRASES[name]
    s = str(v.value if hasattr(v, "value") else v if v is not None else "")
    for key, words in table.items():
        if str(key) == s:
            return words
    return s


def duration(seconds: float) -> str:
    m = int(max(0.0, seconds) // 60)
    if m < 1:
        return "under a minute"
    h, m = divmod(m, 60)
    return " ".join(x for x in (f"{h} h" if h else "", f"{m} min" if m else "") if x)


def _bullets(items: Any) -> str:
    return "\n".join(f"- {x}" for x in items or [])


def _when(t: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "then"


# ------------------------------------------------------------------------------------- texts that need more than values


def _worker_final(t: _Text) -> str:
    full = t.seen.get("review") != "1"
    if full:
        t.given["review"] = "1"
    return t.fill("From {worker}, finished and ready for review:\n\n{body}") + (t.fill(
        "\n\nReview it, bring your checklist up to date, and either write to {worker} what comes next or leave it idle.")
        if full else "")


def _worker_failed(t: _Text) -> str:
    last = t.fill(" Its last assignment: {assignment}.") if t.get("assignment") else ""
    return t.fill("{worker} has failed and does not work any more ({why}). This is not work to review.") + last + t.fill(
        " You may give the work to another worker, after checking what {worker} left behind.")


def _broken_passed(t: _Text) -> str:
    who = t.fill("From {sender}, with") if t.role != Role.CRITIC or _is_worker(t, "sender") else "With"
    return t.fill(who + " a label that could not be read ({label}: {problem}), so it is passed on as it is:\n\n{body}")


def _is_worker(t: _Text, name: str) -> bool:
    a = t.snap.agents.get(str(t.get(name)))
    return a is not None and a.role == Role.WORKER


def _unknown_worker(t: _Text) -> str:
    to = t.fill("Your message to {to} was not sent: there is no such open worker.")
    if t.get("available"):
        return to + t.fill(" Available: {available}.")
    return to + " No worker is available at the moment; tell me what you need one for."


def _job_wakeup(t: _Text) -> str:
    done = [f"`{j.get('job')}`" + ("" if j.get("exit_code") is None else f" (exit code {j['exit_code']})")
            for j in t.get("jobs") or []]
    one = len(done) < 2
    return (f"Your background {'job has' if one else 'jobs have'} ended: {', '.join(done)}. Check the result and "
            "continue with what depended on it.")


def _many(t: _Text) -> bool:
    return len(t.get("jobs") or []) > 1


def _job_lost(t: _Text) -> str:
    jobs = [j.get("job") if isinstance(j, dict) else j for j in t.get("jobs") or []]
    if len(jobs) > 1:
        return (f"Background jobs of yours have no known result: {', '.join(f'`{j}`' for j in jobs)}. They were probably "
                "cut off, but they may still be running. Check their processes and output before you go on; start one "
                "again only if it is gone.")
    return (f"A background job of yours has no known result: {', '.join(f'`{j}`' for j in jobs)}. It was probably cut "
            "off, but it may still be running. Check its process and output before you go on; start it again only if it "
            "is gone.")


def _session_return(t: _Text) -> str:
    start = f"You are back in an earlier conversation. The work went on without it since {_when(t.get('since'))}."
    if t.role == Role.WORKER:
        return start + " Check the project's files, its git log and any running job of yours before you go on."
    return start + (f" Your own turns since then are in `{_own_turns(t)}`; read those, the newest first, before you go "
                    "on.")


def _own_turns(t: _Text) -> str:
    return f"{str(t.get('turns_dir') or '').rstrip('/')}/**/t*-{t.reader}.*.md"


def _session_fresh(t: _Text) -> str:
    out = ("This is a new conversation. Everything done so far is in the project folder; read what you need there before "
           "you change anything.")
    if t.role == Role.MANAGER:
        out += t.fill(" Your notes are in `{state_file}`.") if t.get("state_file") else ""
        if t.snap.run.goal:
            out += f"\n\nMy goal:\n\n{t.snap.run.goal.strip()}"
    elif t.role == Role.CRITIC:
        out += " Where the work stands is in your checklist, if it has one. Then wait for my instruction."
    else:
        out += " Also check the jobs you started."
    return out


def _handover_request(t: _Text, read: str, extra: str = "") -> str:
    lost = str(t.get("why")) == ErrorClass.SESSION_LOST.value
    return t.fill(("{agent} goes on in a new conversation ({why}) that knows nothing so far. " if lost else
                   "{agent} changes to another model ({why}) and starts a new conversation that knows nothing so far. ")
                  + extra + f"Read the current progress first ({read}), then write {{agent}} a short instruction to go on "
                  "with: where the work stands, what comes next, what holds. It replaces your last message to it if that "
                  "is still unanswered, so carry over what matters.")


def _handover_critic(t: _Text) -> str:
    return _handover_request(t, "your checklist, the files, its latest report",
                             "Its assignment: {assignment}. " if t.get("assignment") else "")


def _manager_takeover(t: _Text) -> str:
    notes = t.snap.manager.state_file if t.snap.manager else ""
    return ("You take over this work in a new conversation. To learn the goal, how we work and where things stand, read "
            f"your predecessor's turns in `{_own_turns(t)}`, the newest first ({t.get('latest')}), as far back as "
            "you need; the first ones hold my goal and the plan."
            + (f" Its notes are in `{notes}`." if notes else "") + " Then go on from where it stopped.")


def _milestone_start(t: _Text) -> str:
    last = t.fill("“{last_title}” is done. ") if t.get("last_title") else ""
    return last + t.fill(
        "The milestone “{title}” starts now.\n\n{text}\n\n"
        "Write {critic} its instruction, and set the rubric. {critic} keeps its checklist for it in `{checklist_path}`.")


def _checklist_complete(t: _Text) -> str:
    full = t.seen.get("review") != "1"
    if full:
        t.given["review"] = "1"
    parts = [t.fill("Every item of {critic}'s checklist is ticked, for the milestone “{title}” ({milestone}).")
             + t.jobs(" Still running: ", ".")]
    if t.get("text"):
        parts.append(t.fill("Done when: {text}"))
    if t.get("rubric"):
        parts.append("Your rubric:\n\n" + _bullets(t.get("rubric")))
    if t.get("checklist"):
        c = t.get("checklist")
        parts.append("The checklist:\n\n" + (c if isinstance(c, str) else "\n".join(str(x) for x in c)))
    if full:
        parts.append(t.fill(
            "Review the milestone now: look at the work, score each criterion, then give the score and your decision. If "
            "{critic} still waits for an answer that matters for this, or the work is not ready, write to {critic} "
            "instead and review later. You are told again only after the checklist has changed and is complete again."))
    else:
        parts.append(t.fill("Review the milestone, or first answer {critic}, as before."))
    return "\n\n".join(parts)


def _gate_answers(t: _Text) -> str:
    kinds = {"question": "Your question", "pause": "The pause", "plan": "The plan", "milestone": "The milestone",
             "milestone_change": "The change of milestones"}
    rows = []
    for a in t.get("answers") or []:
        what = kinds.get(str(a.get("kind")), "Your question")
        words = " ".join(str(a.get("answer") or "").split())
        if a.get("approve") is True:
            answer = "approved" + (f": {words}" if words else "")
        elif a.get("approve") is False:
            answer = "not approved" + (f": {words}" if words else "")
        else:
            answer = words or ("done" if a.get("kind") == "pause" else "no answer")
        q = " ".join(str(a.get("question") or "").split()) if a.get("kind") in ("question", "pause") else ""
        rows.append(f"- {what}{f' (“{q}”)' if q else ''}: {answer}")
    parts = ["My answers:\n\n" + "\n".join(rows)] if rows else ["I have closed what was open."]
    unsent = t.get("unsent") or []
    if unsent:
        held = [f"To {t.name(str(u.get('to')))}:\n" + "\n".join(f"> {x}" for x in str(u.get("body")).splitlines())
                for u in unsent]
        parts.append("These messages of yours were not sent. Write again what still holds:\n\n" + "\n\n".join(held))
    if t.get("decide_again"):
        parts.append(t.fill("Your review of “{decide_again}” still needs a decision: decide again now."))
    return "\n\n".join(parts)


def _milestone_rows(t: _Text) -> str:
    rows = []
    kept = [m for m in t.get("milestones") or [] if m.get("status") != MilestoneStatus.DROPPED]
    for i, m in enumerate(kept, 1):
        state = "" if m.get("status") == MilestoneStatus.PENDING else f" ({m.get('status')})"
        text = str(m.get("text") or "").strip()
        rows.append(f"{i}. [{m.get('id')}] {m.get('title')}{f' — {text}' if text else ''}{state}")
    return "\n".join(rows)


def _milestone_change(t: _Text) -> str:
    parts = [t.body] if t.body else ["I want to change the milestones."]
    t.body_used = True
    parts.append("The milestones now:\n\n" + _milestone_rows(t))
    parts.append("Please propose the new list: the whole list in this numbered form, one milestone per line, without the "
                 "states in brackets. Finished milestones stay as they are. Keep the id of a milestone that you keep or "
                 "rewrite, leave out a later one that should go, and give a new one no id. Dropped milestones stay out "
                 "unless you list one again. If the current milestone changes, add its new <rubric>. Nothing changes "
                 "before I approve it. Add a <summary> of what changes and why.")
    return "\n\n".join(parts)


def _monitor_check(t: _Text) -> str:
    out = [t.fill("How is {worker}'s run going? It is ") + _phrase("status", t.get("status"))
           + f", for {duration(60 * float(t.get('minutes') or 0))}."]
    if t.get("assignment"):
        out.append(t.fill("Its task: {assignment}"))
    if t.get("live_file"):
        out.append(t.fill("What it writes now: `{live_file}`"))
    items: List[str] = []
    for item in t.get("items") or []:
        m = re.match(r"^\s*(?:- )?\[([ xX])\]\s*", str(item))
        plain, mark = str(item)[m.end():] if m else str(item), (m.group(1).lower() if m else "")
        # as "item:<id>": digest, so that a session is shown an item again only when its mark changed (REC-10)
        key = "item:" + hashlib.sha1(plain.encode("utf-8")).hexdigest()[:10]
        if t.seen.get(key) != f"[{mark}]":
            items.append(str(item).strip())
            t.given[key] = f"[{mark}]"
    if items:
        first = not any(k.startswith("item:") for k in t.seen)
        out.append(("The checklist:\n" if first else "Changed in the checklist:\n")
                   + "\n".join(x if x.startswith("- ") else f"- {x}" for x in items))
    return "\n\n".join(out)


def _monitor_report(t: _Text) -> str:
    first, _, rest = t.body.partition("\n")
    if first.strip(" .:").lower().replace("-", " ") in HEALTH.values():  # its first line is the status itself
        t.body = rest.strip()
    return t.fill("Progress check on {worker}, {minutes} minutes into its run: {health}.\n\n{body}")


def _detail(t: _Text) -> str:
    """The provider's words, if there are any."""
    return "" if str(t.get("why")) in ("", "billing", "missing_cli") else t.fill(" ({why})")


def _discussion_apply(t: _Text) -> str:
    out = ("I am applying our discussion now. Write what we agreed on, one complete message for each recipient; it "
           "replaces everything you drafted in the discussion. If nothing changes for someone, write them nothing.")
    d = t.snap.discussions.get(t.reader)
    if t.get("milestone_change") or (d is not None and d.milestone_change):
        out += " Include the whole new list of milestones in the numbered form."
    if t.role == Role.CRITIC:
        out += " Also sum up in plain text what changes, without <silent/>."
    return out + (t.fill("\n\n{request}") if t.get("request") else "")


def _worker_line(t: _Text) -> str:
    status = _phrase("status", t.get("status"))
    if status == STATUS[AgentStatus.WAITING_JOB]:
        status += t.jobs(" (", ")")
    elif status == STATUS[AgentStatus.IDLE] and t.get("idle_since"):
        status += f" for {duration(t.snap.now - float(t.get('idle_since')))}"
    work = f"assigned: {t.value('assignment')}" if t.get("assignment") else "nothing assigned yet"
    report = "; you have not written to it since its last report" if t.get("open_report") else ""
    return f"{t.get('worker')}: {status}; {work}{report}"


def _labels(t: _Text) -> str:
    ids = list(t.get("workers") or [])
    to = " or ".join(f'<message to="{i}">…</message>' for i in ids)
    return (f"To a worker: {to}. " if ids else "") + ("Plain text goes to me; add <silent/> when nothing in the reply "
                                                      "needs me.")


def _workers(t: _Text) -> str:
    n = len(t.get("workers") or [])
    if n == 0:
        return "No worker is available at the moment."
    if n == 1:
        return f"1 worker is available: {t.value('workers')}."
    return f"{n} workers are available. Split work only into parts that don't depend on each other; using one is fine."


GATE_KINDS = {"question": "a question", "pause": "a pause", "plan": "the plan", "milestone": "a milestone",
              "milestone_change": "a change of milestones", "operational": "a problem"}


def _gate(t: _Text, g: Any) -> str:
    """One open gate in one line: its words, Orch's words rendered for this reader, or its kind."""
    if isinstance(g, dict) and g.get("key"):
        sub = _Text(t.snap, TextRef(TextKey(g["key"]), dict(g.get("values") or {})), t.reader)
        return reminders.short(next((x for x in _compose(sub).splitlines() if x.strip()), ""), 80)
    if isinstance(g, dict):
        return GATE_KINDS.get(str(g.get("kind")), "a question")
    return str(g)


def _golden_use(t: _Text) -> str:
    return ("Write the golden board's rules that relate to the task into your instruction"
            + (", and check finished work against them." if t.role == Role.CRITIC else "."))


def _force(t: _Text) -> str:
    stop = t.jobs("its running jobs (", ")")
    return t.fill("Switching {agent} now stops its current work") + (f" and {stop}" if stop else "") + "." + (
        " Its conversation cannot continue on the new model: a new one starts with a handover." if t.get("new_session")
        else "") + " Switch anyway?"


def _ask_terminal(t: _Text) -> str:
    what = t.jobs("ends its running jobs: ") or "stops what it is writing"
    return t.fill("Opening a terminal on {agent} ") + what + ". Open it anyway?"


# ----------------------------------------------------------------------------------------------------- all the texts

TEXTS: Dict[str, Union[str, Callable[[_Text], str]]] = {
    # to the critic
    K.WORKER_FINAL: _worker_final,
    K.WORKER_INTERMEDIATE: lambda t: t.fill("From {worker}, " + ("jobs" if _many(t) else "job")
                                            + " still running ({jobs}), not ready for review:\n\n{body}"),
    K.WORKER_TO_CRITIC: lambda t: t.fill("I asked {worker}: “{question}” It answered:\n\n{body}" if str(
        t.get("question") or "").strip() else "From {worker}, to me:\n\n{body}"),
    K.WORKER_FAILED: _worker_failed,
    K.DIRECT_MESSAGE_NOTICE: "I wrote to {worker} directly:\n\n{body}\n\nNo answer needed; go by it.",
    K.CHECKLIST_UNREADABLE: "I cannot read your checklist `{path}` ({problem}). Please put it in order.",
    K.NOBODY_WATCHES: "The progress checks on {worker} keep failing, so nobody watches its runs. Look yourself when one "
                      "takes long.",
    K.JOB_NO_PROGRESS: "{worker}'s job `{job}` has shown no progress for {minutes} minutes. Check whether it still makes "
                       "sense.",
    K.JOBS_STOPPED: "I stopped {worker}'s background work: {jobs}. No result will come from it.",
    K.MONITOR_REPORT: _monitor_report,
    # labels and workers, to whoever wrote them
    K.LABEL_RETURNED: "Part of your reply went nowhere, because a label could not be read ({label}: {problem}). Write just "
                      "that part again; the rest was passed on.",
    K.LABEL_BROKEN_PASSED: _broken_passed,
    K.UNKNOWN_WORKER: _unknown_worker,
    K.NO_WORKERS: "{critic} has no worker to give work to, and only I can add one. Ask me.",
    # to the agent itself
    K.JOB_WAKEUP: _job_wakeup,
    K.JOB_LOST: _job_lost,
    K.RETRY_CONTINUE: "Your last turn was cut off by an error. Go on from where you stopped.",
    K.RESTART_CONTINUE: "Your program was restarted in the middle of your turn. Check where things stand and go on from "
                        "where you stopped; don't restart a job that still runs.",
    K.MESSAGE_AGAIN: lambda t: "Your last turn was stopped before it was done. Check what is already done, then go on"
                               + t.fill(" with this:\n\n{body}" if t.body else "."),
    K.SESSION_RETURN: _session_return,
    K.SESSION_FRESH: _session_fresh,
    K.HANDOVER_WRITE: "Your work continues in a new conversation that knows nothing of this one. Write the handover it "
                      "starts with: what was asked and decided, where things stand (files, running jobs), what comes next "
                      "and what to watch out for. Reply with that text only, and start nothing new.",
    K.HANDOVER_REQUEST_CRITIC: _handover_critic,
    K.HANDOVER_REQUEST_MANAGER: lambda t: _handover_request(t, "the critic's checklist, the files, its latest report"),
    K.HANDOVER_INSTRUCTION: "You take over this work in a new conversation. Check the current files and running jobs "
                            "before you change anything.\n\n{body}",
    K.MANAGER_TAKEOVER: _manager_takeover,
    # to the manager
    K.CRITIC_REPORT: "From {critic}:\n\n{body}",
    K.PLAN_REQUEST: "My goal:\n\n{goal}\n\nPlease split it into milestones, as a numbered list with one milestone per "
                    "line: “1. Short title — what has to be true when it is done”. Say which overall score out of 5 a "
                    "milestone needs, and add a <summary> of what the plan covers. If I ask for changes, send the whole "
                    "list again. Nothing starts before I approve it.",
    K.PLAN_LIST_MISSING: "I found no list of milestones in your reply. Please send it as a numbered list, one milestone "
                         "per line: “1. Short title — what has to be true when it is done”.",
    K.MILESTONE_START: _milestone_start,
    K.SILENCE_CHECK: "Nothing is going on: nobody works, no job runs and no message waits (check {attempt} of {attempts}; "
                     "after the last one the work pauses for me). Find out why, from the critic's checklist, its last report "
                     "and the files, and write {critic} what to do so that the work goes on. Ask me or pause only if it "
                     "really needs me.",
    K.CHECKLIST_COMPLETE: _checklist_complete,
    K.REVIEW_RUBRIC_MISSING: "This milestone ({milestone}) has no rubric. Set one first, then score against it and decide.",
    K.REVIEW_SCORE_LOW: "Your score of {score} out of 5 is below the {pass_score} we agreed on, so the milestone is not "
                        "accepted. Write {critic} what to fix and to update its checklist.",
    K.GATE_ANSWERS: _gate_answers,
    K.MILESTONE_CHANGE_REQUEST: _milestone_change,
    K.MILESTONE_CHANGE_UNREADABLE: "I could not read the new list of milestones ({problem}). Please send the whole list "
                                   "again in the numbered form.",
    K.GOLDEN_LEAK: "The golden board (`{path}`) contains words that {critic} must not see: {words}. Rewrite those entries "
                   "as rules about the work alone; until then {critic} is not pointed to the board.",
    # monitor, discussion
    K.MONITOR_CHECK: _monitor_check,
    K.DISCUSSION_APPLY: _discussion_apply,
    # reminder lines
    K.REM_WORKER_LINE: _worker_line,
    K.REM_WORKERS: _workers,
    K.REM_LABELS: _labels,
    K.REM_UNWATCHED: "Nobody watches the runs of {workers}; look yourself when one takes long.",
    K.REM_GOLDEN_USE: _golden_use,
    K.REM_GOLDEN_PATH: "The golden board is `{path}`; it is new to you or has changed, so read it.",
    K.REM_GOLDEN_KEEP: "Keep the golden board (`{path}`) current: add my new lasting preferences and decisions, merge what "
                       "overlaps, remove what no longer holds.",
    K.REM_GOLDEN_SIZE: "The golden board (`{path}`) has {chars} characters; bring it below {limit} by merging and removing.",
    K.REM_MESSAGE_CRITIC: 'Only <message to="critic">…</message> reaches {critic}; write it as its user, about the work '
                          "alone.",
    K.REM_OPEN_GATES: lambda t: "Still waiting for me: " + "; ".join(f"“{_gate(t, g)}”" for g in t.get("gates") or [])
                                + ". Don't ask again.",
    K.REM_DISCUSSION: "We are in a discussion: what you write to others is only a draft until I apply it.",
    K.REM_DISCUSSION_OVER: "Our discussion is over and I did not apply it: what you drafted for others was not sent.",
    K.REM_NIGHT: "It is night mode and I am away: decide everything yourself and keep the work moving. Write each question "
                 "anyway, with your choice and why; I look at it later. A question then holds nothing back: write to "
                 "{critic} in the same reply. Pause only if nothing at all can go on without me.",
    K.REM_NIGHT_OVER: "Night mode is over; questions wait for me again.",
    K.REM_CHECKLIST: lambda t: t.fill("{critic}'s checklist is `{path}`." if t.role == Role.MANAGER
                                      else "Keep your steps and checklist in `{path}`."),
    K.REM_ASK_AND_END: "If you need an answer to go on, ask me in plain text and end your turn.",
    K.REM_WRITING: lambda t: "\n".join(t.fill(str(x)) for x in t.get("rules") or []),
    K.REM_STATE_EDITED: "I edited your notes in `{path}`; read them and go by them.",
    # gates Orch opens, for the user
    K.GATE_PLAN: lambda t: "The plan is ready for your approval.\n\n" + _bullets(t.get("milestones")),
    K.GATE_MILESTONE: lambda t: t.fill("The milestone “{title}” is ready for your approval")
                      + (f" (score {t.get('score')} of 5)." if t.get("score") is not None else "."),
    K.GATE_MILESTONE_CHANGE: "The milestones should change as shown. Nothing changes before you approve it.",
    K.GATE_SILENCE: "Nothing has been going on, and {attempts} checks by the manager did not start the work again. The "
                    "work is paused.",
    K.GATE_FROZEN: "{agent} gave no output for {minutes} minutes, and restarting it did not help.",
    K.GATE_ROUND_LIMIT: "The limit of {rounds} rounds is reached.",
    K.GATE_BILLING: lambda t: t.fill("{agent} stopped because of a billing problem") + _detail(t)
                    + ". The other agents go on.",
    K.GATE_MISSING_CLI: lambda t: t.fill("{agent} cannot start because its program is missing") + _detail(t)
                        + ". The other agents go on.",
    K.GATE_ALL_FAILED: "No model set for {agent} works ({choices}): {why}. They are tested every 30 minutes; this closes by "
                       "itself when one works.",
    K.GATE_AGENT_FAILED: "{agent} has failed: {why}.",
    K.GATE_HANDOVER: "{agent} needs a handover for its new conversation, but {by} does not answer. Choose working models, "
                     "starting with the manager, then the critic, then the workers.",
    # answers to a command, for the user
    K.ASK_JOBS_CLOSE: lambda t: t.fill("{worker} has " + ("running jobs ({jobs}). Let them" if _many(t) else
                                                      "a running job ({jobs}). Let it")
                                     + " finish or stop " + ("them" if _many(t) else "it") + " before closing the worker?"),
    K.ASK_JOBS_DELETE: lambda t: t.fill("{worker} has " + ("running jobs ({jobs}). Let them" if _many(t) else
                                                      "a running job ({jobs}). Let it")
                                     + " finish or stop " + ("them" if _many(t) else "it") + " before deleting the worker for good?"),
    K.ASK_JOB_TERMINAL: _ask_terminal,
    K.ASK_FORCE_SWITCH: _force,
    K.SMOKE_TEST: "This is a short test of your program. Reply with the single word OK and do nothing else.",
    K.SMOKE_FAILED: "The test of {choice} for {agent} failed, so nothing was switched: {why}.",
    K.SWITCH_PENDING: "{agent} switches to {choice} as soon as it is idle and has no job.",
    K.COMMAND_REFUSED: lambda t: next((s for k, s in REFUSED.items() if str(k) == str(t.get("why"))), t.value("why")),
    # notices in the chat, for the user
    K.NOTE_AGENT_FAILED: "{agent} has failed: {why}.",
    K.NOTE_WAKEUP_SENT: "{worker} did not continue by itself after its job ended ({jobs}), so it was told to.",
    K.NOTE_MONITOR_FAILED: "{monitor} could not check {worker}: {why}.",
    K.NOTE_SWITCHED: lambda t: t.fill("{agent} now runs on {choice}") + (", in a new conversation." if t.get("new_session")
                                                                          else ", in the same conversation."),
    K.NOTE_REVIEW_WITHDRAWN: "The milestone “{title}” changed, so its review was withdrawn and its rubric is "
                             "written again.",
    K.NOTE_FINISHED: "All milestones are done. The sessions are kept; you can add milestones at any time.",
    K.NOTE_MIGRATED: lambda t: "This project was moved to the new version. The old files are kept:\n\n"
                               + _bullets(t.get("files")),
}


def _compose(t: _Text) -> str:
    """The text with the body in its place (after it, if the text has none)."""
    text = TEXTS[t.key]
    out = (text(t) if callable(text) else t.fill(text)).strip()
    if t.body and not t.body_used:
        out += "\n\n" + t.body
    return out


# ----------------------------------------------------------------------------------------------------- entry points


def render(snap: Snapshot, message: Message) -> Rendered:
    """The exact text of a message, with its reminder."""
    agent = snap.agents.get(message.recipient)
    given: Dict[str, str] = {}
    text = message.body.strip()
    if message.text is not None:
        t = _Text(snap, message.text, message.recipient, message.body, given_to(agent) if agent else {})
        text = _compose(t)
        given.update(t.given)
    if agent is None:
        return Rendered(text)
    refs, shown = reminders.lines(snap, message, random.Random(message.id))
    given.update(shown)
    block = reminder(snap, agent.id, refs)
    return Rendered(f"{text}\n\n{block}" if block else text, given)


def reminder(snap: Snapshot, agent: str, refs: List[TextRef]) -> str:
    """All lines in one tag, none twice (PRM-3)."""
    out: List[str] = []
    for ref in refs:
        for line in _compose(_Text(snap, ref, agent)).splitlines():
            if line.strip() and line not in out:
                out.append(line)
    return "<system-reminder>\n" + "\n".join(out) + "\n</system-reminder>" if out else ""


def words(ref: TextRef, snap: Snapshot) -> str:
    """A text for the user: gates, answers to a command, notices."""
    return _compose(_Text(snap, ref))


def system_prompt(snap: Snapshot, agent: str) -> str:
    """Role, protocol and writing rules of an agent. Orch's own files, never a project's copy (PRM-2)."""
    a = snap.agents[agent]
    values = {"critic": _Text(snap, TextRef(K.REM_MESSAGE_CRITIC), agent).critic,
              "golden_board": f"The golden board (`{snap.golden.path}`)" if snap.golden.path else "The golden board",
              "job_line": JOB_LINES.get(a.choice.backend, "Use your program's own background option to start one.")}
    parts = [_prompt(f"{a.role.value}.md", values), _prompt(f"protocol/{a.role.value}.md", values)]
    if a.role in (Role.MANAGER, Role.CRITIC, Role.WORKER):
        parts.append("How to write\n\n" + _fill(_bullets(writing_rules(a.role)), values))
    return "\n\n".join(p for p in parts if p) + "\n"


def _prompt(name: str, values: Dict[str, str]) -> str:
    path = PROMPTS / name
    return _fill(path.read_text(encoding="utf-8").strip(), values) if path.exists() else ""


def _fill(text: str, values: Dict[str, str]) -> str:
    def sub(m: "re.Match[str]") -> str:
        v, before = values.get(m.group(1), m.group(0)), m.string[:m.start()]
        return "T" + v[1:] if v.startswith("the ") and re.search(r"(^|[.!?]\s|\n- )$", before) else v
    return re.sub(r"\{([a-z_]+)\}", sub, text)
