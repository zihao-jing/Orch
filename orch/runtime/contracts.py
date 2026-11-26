"""Contracts of the new runtime (docs/redesign.md 3.4): data, events, commands, actions, protocols. No logic.

How one event is handled (3.5)
  1. `AgentEvent`s come from adapters, `UserCommand`s from the dashboard or a command, `EngineEvent`s from the engine.
     The store drops an event whose `key` it has already handled, saves it as pending, and applies its FACTS (the
     "Store:" line of each event), so modules always see a snapshot that already contains the event.
  2. The engine asks the modules in MODULE_ORDER: `decide(snap, event, prior)`. `prior` holds the actions of the
     earlier modules for this event. The snapshot does not change in between.
  3. In ONE save the store applies the "Store:" line of every action in list order, stores one `Intent` per action
     with an "Engine:" line, and marks the event handled. A crash before this save: the event is decided again.
  4. The engine performs the "Engine:" lines, first all others, then the transmissions, and marks each intent done.
     On restart, unfinished intents are performed again, a transmission only after `Adapter.arrived` (MSG-6).
     After a start the engine first drains the missed events of every agent; it transmits nothing before the actions
     of OrchStarted are done.

Ids: a module makes the ids of what it creates with `new_id(module, event, n)`, n counting its own ones for that event.
They depend only on the event, so deciding an event twice gives the same ids; the store ignores the creation of an id it
knows (and raises if the content differs). The form is fixed ("x" and 12 lower-case hex digits), for every message,
so an adapter may put it into a UUID and read it back. That is how "nothing is sent twice" works, so never use random
ids or a counter of your own. Ticks have no replay: state decides.

Holds
  - `Hold(message_id, reason)` keeps ONE message: also one that an earlier module's Send in `prior` creates. Holds and
    drops are applied before anything is transmitted, so a hold always wins over a Send of the same event. The message
    stays `waiting` until its last hold is released.
  - `HoldAgent(agent, reason)` keeps EVERY message to one agent, whichever module sends it, also in later events, until
    `ReleaseAgent`. Only a Send naming the reason in `bypass` passes (the handover during a switch).
  - Only the owner in HOLD_OWNERS may set or lift a reason. Nothing may release what the same event held.
  - `Drop` closes a message for good and wins over holds.
  A Send to a `waiting` message whose id exists does not create it again; it only adds its `bypass`.
  Messages to one agent are transmitted in `seq` order; a held message does not block later ones, a held agent does.

Sending (MSG-6)
  - A message may go when it is `sendable` (waiting, no hold, sent_at 0) and its agent is not held. The store saves
    `rendered`, `sent_at` and attempts BEFORE Adapter.send(); SendFailed sets sent_at back to 0. Wherever sent_at
    becomes 0, so does checked_at, except the MSG-5 resend after arrived() said False: it keeps checked_at, so the next
    expiry fails the agent instead of sending again.
  - delivered = the CLI has it (Received). taken = its text entered the conversation: Received(taken=True) into the
    running turn, or a TurnStarted. answered = the turn that took it went on (next output) or started.
  - In doubt: when the CLI exited, or its session or process was replaced, every message to it that is waiting with
    sent_at set, or delivered but neither taken nor answered (the CLI's queue dies with it; the store makes it waiting
    again, sent_at kept; notices too). The engine asks arrived() on the new process: True: it reports
    Received(taken=True); False: sent_at 0, so it goes again; None: a notice counts as delivered, any other stays and
    MSG-5 decides. After an Orch restart with the same process: only those waiting with sent_at set, True reports
    Received(taken=False); delivered ones stay.
  - The engine's own Received carries a uid with `attempts`, so each transmission has its own.

Timers: never from before this Orch began. Frozen (SIL-4) counts from max(last_output, orch_started). Unanswered (MSG-5)
counts from max(sent_at, checked_at, recipient.idle_since, orch_started) and stands still while the recipient writes, is
held or retrying. The job fallback (JOB-5, also JOB_LOST) counts from max(job.ended, idle_since), never while writing.
With caps.wakes_itself False there is no minute: WakeWorker goes out as soon as the job ended (or is LOST) and its agent
has no open turn, also after an Orch restart. Never while Agent.retry or Agent.switch is set, failed_why is set, or the
agent is held.

Failures: exactly one report each. Open turn: TurnEnded(error), then ProcessExited(reported=True) if the CLI is gone.
No open turn: SessionChanged(LOST or DIFFERENT). Otherwise ProcessExited. asked=True, or a turn ended by our own
stop(), is STOPPED and no failure.

Routing handles every ended turn except those of the monitor and auditor and those answering HANDOVER_WRITE (in
Turn.keys); MONITOR_REPORT is the supervisor's. A worker's reply goes to the user only if
set(turn.triggers) == {USER_CHAT} (LAB-6), else to the critic.

Who writes what: only the store changes records, and only through facts and actions. The mark after a field names
whose events or actions change it: [fact] = adapter or engine events, [engine] = the engine itself, else the module.
"[create]" = given by whoever creates the record, then fixed. No mark on a record = the record's docstring says it.

Times are epoch seconds (float) from the injected Clock; 0.0 or None = not yet. Snapshots are never changed in place.
Turn files keep today's fields and role names (`verifier`); only here the role is `critic`.
"""
from __future__ import annotations

import hashlib
import re
import typing
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Protocol, Sequence, Tuple, Type, TypeVar, Union

CONTRACT_VERSION = 1

USER = "user"  # a message's sender or recipient that is no agent
ORCH = "orch"

# the names of the modules, in the order they decide about one event (3.5)
MODULE_ORDER = ("recovery", "models", "workers", "routing", "gates", "terminal", "discussion", "milestones",
                "golden", "supervisor")

# what the engine imports from each package; signatures are in "Entry points" below
ENTRY_POINTS = {
    "recovery": "module() -> Module", "models": "module(...) -> Module; resolve; catalog; smoke_test; save_choice",
    "workers": "module() -> Module", "routing": "module() -> Module", "gates": "module() -> Module",
    "discussion": "module() -> Module", "terminal": "module() -> Module",
    "milestones": "module() -> Module; read_checklist", "golden": "module() -> Module; read_board",
    "supervisor": "module() -> Module", "labels": "parse", "texts": "render; words; system_prompt",
    "store": "Store", "engine": "Engine", "board": "serve(runtime: Runtime, ...)",
}


class _Str(str, Enum):
    """An enum stored and compared as its plain string."""

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


# --------------------------------------------------------------------------------------------------------- enums


class Role(_Str):
    MANAGER = "manager"
    CRITIC = "critic"  # `verifier` in config and turn files; Role("verifier") is CRITIC
    WORKER = "worker"
    MONITOR = "monitor"
    AUDITOR = "auditor"

    @classmethod
    def _missing_(cls, value: object) -> Optional["Role"]:
        return cls.CRITIC if value in ("verifier", "reviewer") else None


class MessageStatus(_Str):
    """MSG-3. A dropped message is `failed` with `drop` set; show and count it by its `drop`."""
    WAITING = "waiting"  # not yet with the recipient: held, or its transmission has not succeeded yet
    DELIVERED = "delivered"
    ANSWERED = "answered"  # the recipient started answering
    FAILED = "failed"


class AgentStatus(_Str):
    """UI-1. Retrying, switching, stopped are separate fields of Agent."""
    IDLE = "idle"
    WRITING = "writing"
    WAITING_JOB = "waiting_job"  # idle with a job running
    FAILED = "failed"
    CLOSED = "closed"


class Lifecycle(_Str):
    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"  # its record is kept: ids are never reused


class Trigger(_Str):
    """Where a message comes from, and so what started a turn (LAB-6)."""
    CRITIC = "critic"
    MANAGER = "manager"
    WORKER = "worker"
    MONITOR = "monitor"
    USER_CHAT = "user_chat"
    JOB = "job"  # a job's end (JOB-4, 5)
    ORCH = "orch"
    GATE_ANSWER = "gate_answer"


class ErrorClass(_Str):
    TRANSIENT = "transient"  # API error, overload, rate or usage limit, network, crash: retried (REC-1)
    BILLING = "billing"
    MISSING_CLI = "missing_cli"
    SESSION_LOST = "session_lost"  # a turn failed because the CLI refuses the session or its history (SES-2), also a
    #                                prompt too long that compact() could not help
    OTHER = "other"  # unknown: treated as transient


class Ending(_Str):
    INTERMEDIATE = "intermediate"  # the agent has an open job (Job.open): running, or ended and not woken (JOB-2)
    FINAL = "final"


class TurnStatus(_Str):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    STOPPED = "stopped"  # ended by Orch or the user; not an error


class JobState(_Str):
    RUNNING = "running"
    ENDED = "ended"
    LOST = "lost"


class HoldReason(_Str):
    """Why a message waits (MSG-2). Message holds and agent holds; owners in HOLD_OWNERS."""
    QUESTION = "question"  # message: its reply holds a question (GATE-2); never set when snap.night
    GATES = "gates"  # message: belongs to a held ACCEPT/REVISE, answer gates are open (MS-9)
    DRAFT = "draft"  # message: written in a discussion (DISC-1)
    TERMINAL = "terminal"  # agent: a terminal is open on it (TERM-4)
    SWITCH = "switch"  # agent: it is being switched (MOD-6)
    TURN = "turn"  # agent: its program takes no input while running, or is compacting
    SEND_FAILED = "send_failed"  # agent: a send failed, or it has failed with no choice left
    STOPPED = "stopped"  # agent: the user stopped it


# reason -> (who sets it, who lifts it). QUESTION and GATES messages are dropped, not released, when the answers go out.
HOLD_OWNERS: Dict[HoldReason, Tuple[str, str]] = {
    HoldReason.QUESTION: ("routing", "gates"),
    HoldReason.GATES: ("gates", "gates"),
    HoldReason.DRAFT: ("discussion", "discussion"),
    HoldReason.TERMINAL: ("terminal", "terminal"),
    HoldReason.SWITCH: ("recovery", "recovery"),
    HoldReason.TURN: ("engine", "engine"),
    HoldReason.SEND_FAILED: ("recovery", "recovery"),
    HoldReason.STOPPED: ("engine", "engine"),
}


class DropReason(_Str):
    """Why a message is recorded but never (or no longer) sent."""
    ANSWERED_GATES = "answered_gates"  # QUESTION/GATES hold: the manager writes anew after the answers (GATE-3)
    DRAFT_ENDED = "draft_ended"  # discussion ended, or applied: the consolidated message replaces it
    SILENT = "silent"  # untagged text of a silent reply: shown on the dashboard only (LAB-4)
    REJECTED = "rejected"  # unknown or closed worker (MSG-8)
    WORKER_CLOSED = "worker_closed"  # late result or message of a closed worker (WRK-4, 5)
    REPLACED = "replaced"  # the handover instruction replaces it (SES-6)
    MERGED = "merged"  # an outdated intermediate reply after a restart (REC-7)
    NOT_NEEDED = "not_needed"  # engine: a held WakeWorker whose jobs all got woke meanwhile


class GateKind(_Str):
    QUESTION = "question"
    PAUSE = "pause"
    PLAN = "plan"
    MILESTONE = "milestone"
    MILESTONE_CHANGE = "milestone_change"
    OPERATIONAL = "operational"


ANSWER_GATES = (GateKind.QUESTION, GateKind.PAUSE, GateKind.PLAN, GateKind.MILESTONE, GateKind.MILESTONE_CHANGE)


class GateCause(_Str):
    """Why Orch opened a gate. One open gate per (cause, agent)."""
    SILENCE = "silence"  # GateKind.PAUSE (SIL-2), the rest OPERATIONAL
    FROZEN = "frozen"
    ROUND_LIMIT = "round_limit"
    BILLING = "billing"
    MISSING_CLI = "missing_cli"
    ALL_FAILED = "all_failed"
    AGENT_FAILED = "agent_failed"
    HANDOVER = "handover"


class Decision(_Str):
    """The manager's <decision> (LAB-3)."""
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    PAUSE = "PAUSE"


class MilestoneStatus(_Str):
    PENDING = "pending"
    CURRENT = "current"
    DONE = "done"
    DROPPED = "dropped"


class SwitchReason(_Str):
    USER = "user"
    BACKUP = "backup"
    RETURN = "return"  # back to the main choice (REC-5)
    SESSION_LOST = "session_lost"  # same choice, new session


class SwitchStep(_Str):
    """Steps of a switch; each is saved before its effect, so a restart repeats the last one (REC-7). Recovery writes
    every step with AdvanceSwitch; the effect is the engine's."""
    PENDING = "pending"  # waits until Snapshot.settled (asked again before OPENING, unless forced); no effect
    TESTING = "testing"  # engine: smoke-test `target`, then report SmokeResult
    HANDOVER = "handover"  # no effect; recovery has asked `handover_by` (agent id, or USER for a gate) with a Send
    OPENING = "opening"  # engine: stop the old CLI, start `target` on `new` (None: new session); SessionChanged or
    #                      ProcessExited / AgentFailed follows. From here on the switch cannot be replaced.
    #                      TESTING and OPENING always call models.resolve: a stored choice has no keys
    CONFIRMING = "confirming"  # no effect; the first message went out, its answer decides
    DONE = "done"  # store: target becomes `choice`, its session the current one, `switch` becomes None
    FAILED = "failed"  # engine: if the old CLI was stopped and the agent is open, start it again on `old`; store:
    #                    `switch` becomes None. A worker closed during a switch ends it so, and is not reopened


class SessionChange(_Str):
    NEW = "new"  # a session was started as asked; session_id may be "" and is then reported again when known
    RESUMED = "resumed"  # opened as asked (proves nothing yet: a rejection follows as LOST)
    DIFFERENT = "different"  # the CLI returned another session than asked (SES-2); before that turn's TurnStarted
    LOST = "lost"  # the CLI rejected the session or its history (SES-2)
    COMPACTED = "compacted"  # same id, summarised: what was given before is gone (PRM-5, 9)


class Health(_Str):
    """A monitor's check (REC-10). All but HEALTHY are unhealthy."""
    HEALTHY = "healthy"
    QUIET = "quiet"
    OFF_TRACK = "off_track"
    STOPPED = "stopped"


class DiscussionPhase(_Str):
    OPEN = "open"
    APPLYING = "applying"  # the apply message went out; the reply to it follows the normal rules


class JobsChoice(_Str):
    """The user's answer to "this ends a running job" (WRK-5, TERM-5)."""
    FINISH = "finish"  # let it finish
    STOP = "stop"


class IntentState(_Str):
    PLANNED = "planned"
    DONE = "done"
    FAILED = "failed"


# ----------------------------------------------------------------------------------------------------- text keys


class TextKey(_Str):
    """Every text Orch itself writes; rendered by texts.py (H). The values each needs are in TEXT_VALUES.
    For a message with a `body`, the text wraps it: the template may place {body}; with no text the body goes alone."""
    # worker -> critic
    WORKER_FINAL = "worker_final"  # final reply, ready for review (WRK-8)
    WORKER_INTERMEDIATE = "worker_intermediate"  # job still running, not ready for review (JOB-3)
    WORKER_TO_CRITIC = "worker_to_critic"  # a user chat's reply the user sent on (LAB-6)
    WORKER_FAILED = "worker_failed"  # not as work to review (REC-3)
    # between the others
    CRITIC_REPORT = "critic_report"  # critic's untagged text to the manager
    DIRECT_MESSAGE_NOTICE = "direct_message_notice"  # shown to the critic as its own user's instruction (MSG-9)
    # labels
    LABEL_RETURNED = "label_returned"  # LAB-5, first time
    LABEL_BROKEN_PASSED = "label_broken_passed"  # LAB-5, second time: passed on, marked as broken
    UNKNOWN_WORKER = "unknown_worker"  # MSG-8; `available` may be empty
    NO_WORKERS = "no_workers"  # MSG-8, to the manager: ask the user
    # jobs and recovery, to the agent itself
    JOB_WAKEUP = "job_wakeup"  # fallback (JOB-5)
    JOB_LOST = "job_lost"  # JOB-7; says that the job may still be running
    RETRY_CONTINUE = "retry_continue"  # after a failed try in the same session
    RESTART_CONTINUE = "restart_continue"  # after RestartSession (SIL-4)
    MESSAGE_AGAIN = "message_again"  # the unfinished message, again to the new model (MOD-6)
    SESSION_RETURN = "session_return"  # an earlier session reopened: what happened since (SES-4)
    SESSION_FRESH = "session_fresh"  # a new session without a switch (assumption 3); the engine sends it
    # handover (SES-5)
    HANDOVER_WRITE = "handover_write"  # to the old model
    HANDOVER_REQUEST_CRITIC = "handover_request_critic"  # the critic writes a worker's instruction
    HANDOVER_REQUEST_MANAGER = "handover_request_manager"  # the manager writes the critic's instruction
    HANDOVER_INSTRUCTION = "handover_instruction"  # first message of the new session
    MANAGER_TAKEOVER = "manager_takeover"  # a new manager: pointer to the saved turns
    # to the manager
    PLAN_REQUEST = "plan_request"
    PLAN_LIST_MISSING = "plan_list_missing"
    MILESTONE_START = "milestone_start"
    SILENCE_CHECK = "silence_check"  # SIL-2, 3
    CHECKLIST_COMPLETE = "checklist_complete"  # MS-4: review, or first answer the critic; full or short (PRM-9): H, by
    #                                            SessionRef.given
    REVIEW_RUBRIC_MISSING = "review_rubric_missing"
    REVIEW_SCORE_LOW = "review_score_low"  # MS-6: an ACCEPT below Settings.pass_score is not accepted; the manager is asked
    #                                        what the critic should do next
    GATE_ANSWERS = "gate_answers"  # all answers in one message (GATE-4); `unsent`: what was held and not sent;
    #                                `decide_again`: MS-8, 9
    MILESTONE_CHANGE_REQUEST = "milestone_change_request"  # MS-10
    MILESTONE_CHANGE_UNREADABLE = "milestone_change_unreadable"
    # to the critic
    CHECKLIST_UNREADABLE = "checklist_unreadable"  # MS-2
    NOBODY_WATCHES = "nobody_watches"  # REC-10, after 2 failures
    JOB_NO_PROGRESS = "job_no_progress"  # JOB-6
    JOBS_STOPPED = "jobs_stopped"  # the user closed a worker and its job, or its job was stopped for a terminal
    # monitor
    MONITOR_CHECK = "monitor_check"
    MONITOR_REPORT = "monitor_report"  # supervisor to the critic
    # discussion and terminal
    DISCUSSION_APPLY = "discussion_apply"
    # reminder lines (one short line each, PRM-3)
    REM_WORKER_LINE = "rem_worker_line"  # one per worker: status, assignment, unanswered report, idle since, failed
    REM_WORKERS = "rem_workers"  # how many are available; split only independent parts (WRK-9)
    REM_LABELS = "rem_labels"  # examples with the ids and <silent/>
    REM_UNWATCHED = "rem_unwatched"
    REM_GOLDEN_USE = "rem_golden_use"
    REM_GOLDEN_PATH = "rem_golden_path"  # new or compacted session, or the board changed (PRM-5)
    REM_GOLDEN_KEEP = "rem_golden_keep"
    REM_GOLDEN_SIZE = "rem_golden_size"
    GOLDEN_LEAK = "golden_leak"  # to the manager
    REM_MESSAGE_CRITIC = "rem_message_critic"
    REM_OPEN_GATES = "rem_open_gates"
    REM_DISCUSSION = "rem_discussion"
    REM_NIGHT = "rem_night"
    REM_ASK_AND_END = "rem_ask_and_end"
    REM_WRITING = "rem_writing"
    REM_STATE_EDITED = "rem_state_edited"  # Agent.state_edited is set and differs from given["state"]
    REM_CHECKLIST = "rem_checklist"  # to the critic: where its checklist file is (new session or new milestone)
    REM_DISCUSSION_OVER = "rem_discussion_over"  # the discussion ended without being applied
    REM_NIGHT_OVER = "rem_night_over"
    # gates Orch opens, shown to the user
    GATE_PLAN = "gate_plan"
    GATE_MILESTONE = "gate_milestone"
    GATE_MILESTONE_CHANGE = "gate_milestone_change"
    GATE_SILENCE = "gate_silence"
    GATE_FROZEN = "gate_frozen"
    GATE_ROUND_LIMIT = "gate_round_limit"
    GATE_BILLING = "gate_billing"
    GATE_MISSING_CLI = "gate_missing_cli"
    GATE_ALL_FAILED = "gate_all_failed"
    GATE_AGENT_FAILED = "gate_agent_failed"
    GATE_HANDOVER = "gate_handover"
    # answers to a command, shown to the user
    ASK_JOBS_CLOSE = "ask_jobs_close"  # WRK-5
    ASK_JOBS_DELETE = "ask_jobs_delete"
    ASK_JOB_TERMINAL = "ask_job_terminal"  # TERM-5
    ASK_FORCE_SWITCH = "ask_force_switch"  # MOD-6; `new_session`: a new conversation with a handover starts
    SMOKE_TEST = "smoke_test"  # MOD-7: the short real turn of a smoke test
    SMOKE_FAILED = "smoke_failed"  # MOD-7
    SWITCH_PENDING = "switch_pending"
    COMMAND_REFUSED = "command_refused"  # `why`: a Refusal
    # notices to the user in the chat
    NOTE_AGENT_FAILED = "note_agent_failed"
    NOTE_WAKEUP_SENT = "note_wakeup_sent"  # not where wakes_itself is False: there it is the normal way
    NOTE_MONITOR_FAILED = "note_monitor_failed"
    NOTE_SWITCHED = "note_switched"
    NOTE_REVIEW_WITHDRAWN = "note_review_withdrawn"  # MS-13
    NOTE_FINISHED = "note_finished"  # MS-14
    NOTE_MIGRATED = "note_migrated"


# The value names each key needs. `body` may always be used. Agents are given by id; texts.py turns them into names.
# `why`, `reason`, `problem`, `health`, `status` are enum values, or a provider's own words (why of a failure).
# Lists (jobs, available, gates, answers, items, milestones, ...) are lists of strings unless noted.
TEXT_VALUES: Dict[TextKey, Tuple[str, ...]] = {
    TextKey.WORKER_FINAL: ("worker",),
    TextKey.WORKER_INTERMEDIATE: ("worker", "jobs"),
    TextKey.WORKER_TO_CRITIC: ("worker", "question"),  # question: the user's chat message it answers
    TextKey.WORKER_FAILED: ("worker", "why", "assignment"),
    TextKey.CRITIC_REPORT: (),
    TextKey.DIRECT_MESSAGE_NOTICE: ("worker",),
    TextKey.LABEL_RETURNED: ("label", "problem"),  # problem: a Problem value
    TextKey.LABEL_BROKEN_PASSED: ("sender", "label", "problem"),
    TextKey.UNKNOWN_WORKER: ("to", "available"),
    TextKey.NO_WORKERS: (),
    TextKey.JOB_WAKEUP: ("jobs",),  # [{job, exit_code}]
    TextKey.JOB_LOST: ("jobs",),
    TextKey.RETRY_CONTINUE: (),
    TextKey.RESTART_CONTINUE: (),
    TextKey.MESSAGE_AGAIN: (),
    TextKey.SESSION_RETURN: ("turns_dir", "since"),  # since: time; H says where the turns from then on are
    TextKey.SESSION_FRESH: ("state_file",),
    TextKey.HANDOVER_WRITE: ("agent",),
    TextKey.HANDOVER_REQUEST_CRITIC: ("agent", "why", "assignment"),
    TextKey.HANDOVER_REQUEST_MANAGER: ("agent", "why"),
    TextKey.HANDOVER_INSTRUCTION: (),
    TextKey.MANAGER_TAKEOVER: ("turns_dir", "latest"),
    TextKey.PLAN_REQUEST: ("goal",),
    TextKey.PLAN_LIST_MISSING: (),
    TextKey.MILESTONE_START: ("milestone", "title", "text", "checklist_path", "last_title", "approved_plan"),
    TextKey.SILENCE_CHECK: ("attempt", "attempts"),
    TextKey.CHECKLIST_COMPLETE: ("milestone", "title", "text", "rubric", "checklist", "jobs"),
    TextKey.REVIEW_RUBRIC_MISSING: ("milestone", "title"),
    TextKey.REVIEW_SCORE_LOW: ("milestone", "title", "score", "pass_score"),
    TextKey.GATE_ANSWERS: ("answers", "unsent", "decide_again"),  # answers: [{kind, question, answer, approve}]
    #   (approve: True/False for plan, milestone and change gates, else None); unsent:
    #                                      [{to, body}]; decide_again: milestone title or ""
    TextKey.MILESTONE_CHANGE_REQUEST: ("milestones",),  # [{id, title, text, status}]
    TextKey.MILESTONE_CHANGE_UNREADABLE: ("problem",),
    TextKey.CHECKLIST_UNREADABLE: ("path", "problem"),
    TextKey.NOBODY_WATCHES: ("worker",),
    TextKey.JOB_NO_PROGRESS: ("worker", "job", "minutes"),
    TextKey.JOBS_STOPPED: ("worker", "jobs"),
    TextKey.MONITOR_CHECK: ("worker", "status", "minutes", "assignment", "live_file", "items"),
    TextKey.MONITOR_REPORT: ("worker", "health", "minutes"),
    TextKey.DISCUSSION_APPLY: ("request", "milestone_change"),  # request: the user's own words with Apply, or "";
    #   milestone_change: True when the discussion wanted one, so a reply after a later question is still read as a change
    TextKey.REM_WORKER_LINE: ("worker", "status", "assignment", "open_report", "idle_since", "jobs"),
    TextKey.REM_WORKERS: ("workers",),
    TextKey.REM_LABELS: ("workers",),
    TextKey.REM_UNWATCHED: ("workers",),
    TextKey.REM_GOLDEN_USE: (),
    TextKey.REM_GOLDEN_PATH: ("path",),
    TextKey.REM_GOLDEN_KEEP: ("path",),
    TextKey.REM_GOLDEN_SIZE: ("path", "chars", "limit"),
    TextKey.GOLDEN_LEAK: ("path", "words"),
    TextKey.REM_MESSAGE_CRITIC: (),
    TextKey.REM_OPEN_GATES: ("gates",),
    TextKey.REM_DISCUSSION: (),
    TextKey.REM_NIGHT: (),
    TextKey.REM_ASK_AND_END: (),
    TextKey.REM_WRITING: ("rules",),
    TextKey.REM_STATE_EDITED: ("path",),
    TextKey.REM_CHECKLIST: ("path",),
    TextKey.REM_DISCUSSION_OVER: (),
    TextKey.REM_NIGHT_OVER: (),
    TextKey.GATE_PLAN: ("milestones",),
    TextKey.GATE_MILESTONE: ("milestone", "title", "score"),
    TextKey.GATE_MILESTONE_CHANGE: (),
    TextKey.GATE_SILENCE: ("attempts",),
    TextKey.GATE_FROZEN: ("agent", "minutes"),
    TextKey.GATE_ROUND_LIMIT: ("rounds",),
    TextKey.GATE_BILLING: ("agent", "why"),
    TextKey.GATE_MISSING_CLI: ("agent", "why"),
    TextKey.GATE_ALL_FAILED: ("agent", "why", "choices"),
    TextKey.GATE_AGENT_FAILED: ("agent", "why"),
    TextKey.GATE_HANDOVER: ("agent", "by"),
    TextKey.ASK_JOBS_CLOSE: ("worker", "jobs"),
    TextKey.ASK_JOBS_DELETE: ("worker", "jobs"),
    TextKey.ASK_JOB_TERMINAL: ("agent", "jobs"),  # jobs empty: it is writing
    TextKey.ASK_FORCE_SWITCH: ("agent", "jobs", "new_session"),
    TextKey.SMOKE_TEST: (),
    TextKey.SMOKE_FAILED: ("agent", "choice", "why"),
    TextKey.SWITCH_PENDING: ("agent", "choice"),
    TextKey.COMMAND_REFUSED: ("why",),
    TextKey.NOTE_AGENT_FAILED: ("agent", "why"),
    TextKey.NOTE_WAKEUP_SENT: ("worker", "jobs"),
    TextKey.NOTE_MONITOR_FAILED: ("monitor", "worker", "why"),
    TextKey.NOTE_SWITCHED: ("agent", "choice", "new_session"),
    TextKey.NOTE_REVIEW_WITHDRAWN: ("milestone", "title"),
    TextKey.NOTE_FINISHED: (),
    TextKey.NOTE_MIGRATED: ("files",),
}


@dataclass
class TextRef:
    """A text to render: a key and its values (plain JSON values, names as in TEXT_VALUES)."""
    key: TextKey
    values: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------------- records


@dataclass
class Choice:
    """One way to run an agent (backend + provider + model + effort), main or backup. Made by models.resolve."""
    backend: str = ""
    provider: str = ""  # OpenCode; not part of the session key
    model: str = ""
    effort: str = ""
    model_id: str = ""  # its id in .orch/settings.yaml, if from there
    options: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)  # may hold keys: see `_stored`
    name: str = ""  # a backup's name (REC, MOD-3); "" for the main choice

    @property
    def session_key(self) -> str:
        """Choices with the same key are expected to continue one session (as today's Variant.session_key, but without
        the user, password and query of an address)."""
        return "|".join([self.backend] + sorted(f"{k}={v}" for k, v in self._urls().items()))

    def _urls(self) -> Dict[str, str]:
        return {k: _bare_url(v) for k, v in self.env.items() if k.endswith("_BASE_URL") and v}

    def same(self, other: "Choice") -> bool:
        """The same choice, whether or not its env and options are filled in."""
        key = ("backend", "provider", "model", "effort", "name", "session_key")
        return all(getattr(self, k) == getattr(other, k) for k in key)

    @property
    def label(self) -> str:
        return self.name or self.model_id or ":".join(x for x in (self.backend, self.model) if x)

    def _stored(self) -> Dict[str, Any]:
        """As saved: no options, and of env only the addresses the session key needs, so never a key (MOD-1). Before
        starting a choice the engine calls models.resolve, which fills both in again."""
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["options"], d["env"] = {}, self._urls()
        return d

    def public(self) -> Dict[str, Any]:
        """For the dashboard: no env, options or addresses."""
        return {"backend": self.backend, "provider": self.provider, "model": self.model, "effort": self.effort,
                "model_id": self.model_id, "name": self.name, "label": self.label}


@dataclass
class ModelInfo:
    """One selectable model (settings file or CLI catalog), without secrets. Written by the engine from F1."""
    id: str  # its id in .orch/settings.yaml
    backend: str = ""
    provider: str = ""
    model: str = ""
    label: str = ""
    efforts: List[str] = field(default_factory=list)  # empty: hide the choice (MOD-2)
    default_effort: str = ""


@dataclass
class SessionRef:
    """One conversation kept by a CLI. Agent.sessions holds one per session key (SES-4). It is always reopened in its own
    `cwd` (some CLIs find it only there)."""
    session_id: str
    session_key: str = ""
    choice: Choice = field(default_factory=Choice)  # the choice it last ran with (REC-8); [fact]
    cwd: str = ""
    started: float = 0.0
    last_used: float = 0.0  # [fact]
    confirmed: bool = False  # [fact] a turn in it ended ok, with this session id
    # [engine] what this session was shown -> digest, e.g. {"golden": "ab12", "review": "1"}; where only changed items
    # are to be shown, one entry per item ({"item:<id>": digest}). From Message.given when taken; emptied on COMPACTED
    given: Dict[str, str] = field(default_factory=dict)


@dataclass
class ProcRef:
    """The open CLI process of an agent (REC-7). [engine], read_pos [fact]"""
    pid: int = 0
    started: float = 0.0  # the process start time: pid + started identify it after a restart
    endpoint: str = ""  # pipe, socket or URL
    output_file: str = ""
    stream: str = ""  # names the output `read_pos` counts in (AgentEvent.stream)
    read_pos: str = ""  # `pos` of the last saved event that had one; events() resumes after it


@dataclass
class Caps:
    """What an adapter can do; the engine copies it to Agent.caps."""
    sends_while_writing: bool = False  # False: messages wait for the turn to end (HoldReason.TURN). True needs receipts,
    #                                    and a CLI that takes a message into the running turn or tells when it starts:
    #                                    Claude, Codex, OpenCode True; Qwen, Qoder, Antigravity (own later turn) False
    reconnects: bool = False  # Orch may close and reconnect to the same process
    job_record: bool = False  # jobs() and job events are reliable
    wakes_itself: bool = False  # a job's end starts a turn (JOB-4); False for Codex and Qwen
    receipts: bool = False  # reports Received; otherwise the engine reports it when send() returns
    shares_terminal: bool = False  # a terminal can join the open session (TERM-4)
    compacts: bool = False
    stops_jobs: bool = False  # stop_job() ends one job without ending the CLI


@dataclass
class Job:
    """A background job. [fact] unless marked."""
    id: str  # "<agent>/<the CLI's id>" (base.job_id)
    agent: str = ""
    label: str = ""  # the command, one line
    started: float = 0.0
    state: JobState = JobState.RUNNING  # LOST: [supervisor]
    exit_code: Optional[int] = None
    ended: float = 0.0
    woke: bool = False  # its end reached its agent: a turn took it in, JobNoticed; set at once for a job that has
    #                     ended, or ends, while its agent is not open (its result is only recorded)
    fallback_sent: bool = False  # [supervisor] WakeWorker was sent (JOB-5, JOB-7), once
    stopped_by_user: bool = False  # [workers], [terminal], [engine] (stop agents): JOB-7 does not wake
    last_progress: float = 0.0  # last sign of progress, where the CLI shows one
    reported: bool = False  # [supervisor] no progress was reported (JOB-6)

    @property
    def open(self) -> bool:
        """Running, or its end has not reached its agent yet (and the user did not stop it)."""
        return self.state == JobState.RUNNING or not (self.woke or self.stopped_by_user)


@dataclass
class Message:
    """One message (MSG-3). Fixed by whoever creates it: id, sender, recipient, body, text, needs_answer, trigger,
    turn, gates, continues, when_idle. The rest is written as marked."""
    id: str
    sender: str = ORCH  # agent id, USER or ORCH
    recipient: str = ""  # agent id or USER
    body: str = ""  # what an agent or the user wrote, cleaned (MSG-10); never Orch's own words
    text: Optional[TextRef] = None  # Orch's words (alone, or around the body); None: the body alone
    needs_answer: bool = True  # False: ends at delivered (MSG-4); always False to USER
    trigger: Trigger = Trigger.ORCH
    turn: str = ""  # the turn (Turn.id) whose reply holds it
    gates: List[str] = field(default_factory=list)  # gates whose answers it carries: they take effect with it
    continues: str = ""  # RETRY_CONTINUE, RESTART_CONTINUE, MESSAGE_AGAIN: the unfinished turn (Turn.id) it lets go on.
    #                      Whichever turn takes it in also gets that turn's started_by, joined, jobs, triggers, keys
    #                      and draft (not a turn that took in the apply message), and not this message's own trigger
    #                      and key. A turn that starts while Agent.retry.turn is set continues that one, as if a message
    #                      did. "": an ordinary message, with its own trigger and key. Store: when it is taken, the
    #                      messages the continued turn took in and left unanswered count as answered (MSG-5 ignores them)
    when_idle: bool = False  # goes out only while the recipient has no open turn and no message sent to it unanswered:
    #                          the discussion apply message, so it never joins a draft turn. Never in `holds`; the
    #                          dashboard shows that it waits
    jobs: List[str] = field(default_factory=list)  # [engine] WakeWorker.job_ids: the turn that takes it in gets them
    created: float = 0.0  # [engine]
    seq: int = 0  # [engine] order of creation in the run
    status: MessageStatus = MessageStatus.WAITING  # [fact], [engine]
    holds: List[HoldReason] = field(default_factory=list)  # message holds only; agent holds are on the agent
    bypass: List[HoldReason] = field(default_factory=list)  # from Send.bypass
    drop: Optional[DropReason] = None  # set: closed for good, status failed
    attempts: int = 0  # [engine] transmissions
    sent_at: float = 0.0  # [engine] the last one, saved before send(); 0: never, or it failed
    delivered_at: float = 0.0  # [fact]
    taken_at: float = 0.0  # [fact] when its text entered the conversation
    answered_at: float = 0.0  # [fact]
    reply_turn: str = ""  # [fact] the turn that took it in
    session_id: str = ""  # [fact] the session it reached (SES-6)
    checked_at: float = 0.0  # [engine] when Resend last asked whether it arrived (MSG-5)
    error: str = ""  # [engine] why the last transmission failed
    rendered: str = ""  # [engine] the exact text sent, reminder included; a resend uses it again
    given: Dict[str, str] = field(default_factory=dict)  # [engine] Rendered.given; into SessionRef.given when taken

    @property
    def key(self) -> Optional[TextKey]:
        return self.text.key if self.text else None

    @property
    def sendable(self) -> bool:
        """It may go, unless its agent is held (or it is when_idle)."""
        return self.status == MessageStatus.WAITING and not self.holds and self.drop is None and not self.sent_at

    @property
    def open(self) -> bool:
        """It needs an answer and has none yet, so it counts as active (SIL-1)."""
        return (self.needs_answer and self.drop is None
                and self.status in (MessageStatus.WAITING, MessageStatus.DELIVERED))


@dataclass
class Assignment:
    """The latest message the critic sent a worker (WRK-1)."""
    message_id: str
    line: str = ""  # its first line, cut to 160 characters
    at: float = 0.0


@dataclass
class RetryState:
    """An agent waiting to try again, or trying choices. [recovery]"""
    attempt: int = 0  # tries made with the current choice since it last worked
    until: float = 0.0  # when to try next
    error: ErrorClass = ErrorClass.TRANSIENT
    why: str = ""  # the provider's message, redacted
    turn: str = ""  # the unfinished turn, for Message.continues
    tried: List[str] = field(default_factory=list)  # Choice.label of the choices that failed


@dataclass
class Switch:
    """A switch under way (MOD-6, SES). Created by models (user) or recovery, then written by recovery only."""
    agent: str
    target: Choice = field(default_factory=Choice)
    reason: SwitchReason = SwitchReason.USER
    forced: bool = False
    step: SwitchStep = SwitchStep.PENDING
    step_at: float = 0.0
    created: float = 0.0
    command_id: str = ""  # the command that asked for it
    old: Optional[SessionRef] = None  # kept until the new one has answered (REC-8)
    new: Optional[SessionRef] = None  # the target key's saved session, if there is one to reopen
    new_session: bool = False  # a new session is needed, so a handover
    tried_new: bool = False  # a new session was tried once (SES-3)
    handover_by: str = ""  # who writes it: the agent itself, its upper level, or USER (gate open)
    handover_request: str = ""  # message id of the request
    handover: str = ""  # the text, or
    handover_message: str = ""  # the id of the upper level's own message used as the first message
    first_message: str = ""  # message id whose answer confirms the switch
    unfinished: List[str] = field(default_factory=list)  # message ids to send again after it
    turn: str = ""  # the unfinished turn, for Message.continues
    error: str = ""


@dataclass
class Agent:
    """One registry entry (WRK-1). Deleted workers stay in the registry."""
    id: str  # stable, the config name, e.g. "worker-2"
    role: Role = Role.WORKER
    display: str = ""  # how others name it
    cwd: str = ""
    state_file: str = ""  # its state.md (SaveNotes)
    state_edited: str = ""  # [engine] digest of state.md while it differs from what SaveNotes wrote last; else ""
    fresh_session: str = ""  # config: "compact", "new", "never" at a milestone start (assumption 3)
    lifecycle: Lifecycle = Lifecycle.OPEN  # [workers]
    status: AgentStatus = AgentStatus.IDLE  # the store derives it: Snapshot.status_of
    choice: Choice = field(default_factory=Choice)  # the current one; [recovery] (switch done)
    choice_since: float = 0.0  # [store] when `choice` last changed (REC-5 counts return_after from it)
    choices: List[Choice] = field(default_factory=list)  # main first, then backups in order; [engine] from config
    actual_model: str = ""  # [fact] MOD-5
    caps: Caps = field(default_factory=Caps)  # [engine]
    sessions: Dict[str, SessionRef] = field(default_factory=dict)  # by session key; [fact]
    session_key: str = ""  # key of the current session; [recovery]
    proc: Optional[ProcRef] = None  # [engine]; None: no CLI open
    holds: List[HoldReason] = field(default_factory=list)  # agent holds (HoldAgent)
    assignment: Optional[Assignment] = None  # [engine] when a message critic -> worker is created and not dropped
    open_report: str = ""  # [engine] id of its latest final reply to the critic; "" once the critic writes to it
    open_report_at: float = 0.0  # [engine]
    writing_since: float = 0.0  # [fact]; 0 when not writing
    idle_since: float = 0.0  # [fact]; 0 when writing
    last_output: float = 0.0  # [fact]
    tools: List[str] = field(default_factory=list)  # [fact] ids of the tool calls running now
    failed_why: str = ""  # [recovery] MarkFailed, never ""; "" again when a turn ended ok
    retry: Optional[RetryState] = None  # [recovery]
    switch: Optional[Switch] = None  # [recovery]
    frozen_restart: float = 0.0  # [supervisor] SIL-4: restarted once in this turn; [fact] 0 after a turn ended ok
    last_smoke: float = 0.0  # [engine] last smoke test (REC-6)
    stopped_by_user: bool = False  # [engine] SIL-1, REC-9

    @property
    def session(self) -> Optional[SessionRef]:
        return self.sessions.get(self.session_key)

    @property
    def is_open(self) -> bool:
        return self.lifecycle == Lifecycle.OPEN

    @property
    def on_backup(self) -> bool:
        return bool(self.choice.name)


class FailCause(_Str):
    """FailAgent.why."""
    NO_ANSWER = "no_answer"  # MSG-5
    FROZEN = "frozen"  # SIL-4


class Refusal(_Str):
    """The `why` of COMMAND_REFUSED."""
    UNKNOWN = "unknown"  # no such agent, gate, message, milestone, model, job
    EMPTY = "empty"
    CLOSED = "closed"  # the agent or gate is closed
    DELETED = "deleted"
    WRONG_ROLE = "wrong_role"  # e.g. a discussion with a worker
    ALREADY = "already"  # already open, closed, chosen, running
    NOT_OPEN = "not_open"  # no discussion, terminal or switch to end
    TERMINAL_OPEN = "terminal_open"
    GATE_OPEN = "gate_open"  # e.g. night mode while a question is open
    SWITCHING = "switching"  # the switch under way is past the point where it can be replaced
    FILE_CHANGED = "file_changed"
    NOT_POSSIBLE = "not_possible"  # the CLI cannot do it


class Problem(_Str):
    """What is wrong with a label (LAB-5)."""
    UNCLOSED = "unclosed"
    EMPTY = "empty"
    NO_RECIPIENT = "no_recipient"  # missing or not an agent this sender may address; unknown workers are MSG-8
    REMOVED = "removed"  # a label this role no longer has
    BAD_VALUE = "bad_value"  # e.g. an unknown <decision>


@dataclass
class OutMessage:
    """One <message to="..."> of a reply. `to` as written; routing finds the agent."""
    to: str
    body: str


@dataclass
class Question:
    text: str
    options: List[str] = field(default_factory=list)


@dataclass
class BrokenLabel:
    label: str  # the tag as written, cut to one line
    problem: Problem
    body: str = ""


@dataclass
class Reply:
    """A reply parsed by labels.parse (C), for one role. Every module reads Turn.reply, none parses."""
    text: str = ""  # the whole reply, cleaned (MSG-10)
    untagged: str = ""  # outside labels
    messages: List[OutMessage] = field(default_factory=list)
    silent: bool = False
    questions: List[Question] = field(default_factory=list)
    pauses: List[str] = field(default_factory=list)
    decision: Optional[Decision] = None
    rubric: List[str] = field(default_factory=list)
    score: Optional[float] = None
    summary: str = ""
    state: str = ""  # <state>
    broken: List[BrokenLabel] = field(default_factory=list)
    health: Optional[Health] = None  # monitor only; None: unreadable


@dataclass
class Turn:
    """An agent's current or last turn. [fact]"""
    id: str  # given by the store, as in the turn files (rNNN-tNN-agent)
    agent: str = ""
    started: float = 0.0
    ended: float = 0.0
    status: TurnStatus = TurnStatus.RUNNING
    started_by: List[str] = field(default_factory=list)  # ids of the messages it began with
    joined: List[str] = field(default_factory=list)  # ids of the messages taken in while it ran
    jobs: List[str] = field(default_factory=list)  # ids of the ended jobs it learned of, at its start or while it ran
    # of all of started_by and joined, plus JOB if `jobs` is not empty; an id the store does not know is the user's.
    # With no message and no job: USER_CHAT if a terminal is open on the agent, else JOB (LAB-6)
    triggers: List[Trigger] = field(default_factory=list)
    keys: List[TextKey] = field(default_factory=list)  # text keys of started_by and joined (what it answers)
    draft: bool = False  # it ran while a discussion was open (its apply turn excepted); stays so after the discussion
    text: str = ""
    reply: Optional[Reply] = None  # set when it ended ok
    ending: Optional[Ending] = None  # set when it ended (JOB-2)
    error: Optional[ErrorClass] = None
    detail: str = ""  # the error's text


@dataclass
class Answer:
    """The user's answer to a gate."""
    text: str = ""
    option: Optional[int] = None  # index into Gate.options
    approve: Optional[bool] = None  # plan, milestone, milestone_change: approve or ask for changes
    at: float = 0.0


@dataclass
class Gate:
    """Something on the dashboard that waits for the user. Opened by: question, pause, operational as in OpenGate;
    plan, milestone, milestone_change by milestones. Closed by gates (answers) or its opener (withdrawn, solved).
    Night (Snapshot.night): no question, plan or milestone gate opens; the manager's own recommendation, or the
    approval, acts at once and is recorded (RecordDecision)."""
    id: str  # new_id; for a `cause`: "op-<cause>-<agent>", so it opens once
    kind: GateKind = GateKind.QUESTION
    text: str = ""  # an agent's words
    words: Optional[TextRef] = None  # Orch's words
    options: List[str] = field(default_factory=list)
    opened: float = 0.0
    closed: float = 0.0  # 0: open
    agent: str = ""  # whose question, or which agent the failure concerns
    cause: Optional[GateCause] = None
    source_turn: str = ""
    milestone: str = ""
    summary: str = ""  # <summary> of the reply
    old: List[Milestone] = field(default_factory=list)  # milestone_change: the lists side by side
    new: List[Milestone] = field(default_factory=list)  # also plan: the proposed list
    answer: Optional[Answer] = None
    withdrawn: bool = False  # closed by Orch without an answer
    sent_in: str = ""  # [engine] the message that carried its answer (Message.gates)

    @property
    def is_open(self) -> bool:
        return not self.closed

    @property
    def is_answer_gate(self) -> bool:
        return self.kind in ANSWER_GATES


@dataclass
class ChecklistStep:
    title: str
    done: bool = False
    items: List[Tuple[bool, str]] = field(default_factory=list)


@dataclass
class Checklist:
    """The current milestone's checklist file, read by milestones.read_checklist (G) for each snapshot."""
    path: str = ""
    digest: str = ""  # changes with the content
    steps: List[ChecklistStep] = field(default_factory=list)
    complete: bool = False
    error: str = ""  # missing or unreadable: not complete (MS-2)


@dataclass
class Milestone:
    """One milestone. The list order is the order of work. [milestones] unless marked."""
    id: str  # "m1", "m2", ...; fixed, never reused (MS-12)
    title: str = ""
    text: str = ""
    status: MilestoneStatus = MilestoneStatus.PENDING
    rubric: List[str] = field(default_factory=list)
    score: Optional[float] = None
    checklist_path: str = ""
    accepted: bool = False  # the manager accepted it; the approval gate may still be open
    summary: str = ""  # the manager's <summary> at its review
    notified: str = ""  # checklist digest the manager was last told about (MS-4)
    asked_to_fix: str = ""  # checklist digest (or error) the critic was asked to fix (MS-2)
    held_decision: Optional[Decision] = None  # [gates] MS-9: waits for the answers, then the manager decides again
    refreshed: List[str] = field(default_factory=list)  # agents whose session start was handled for this milestone
    started: float = 0.0
    ended: float = 0.0


@dataclass
class GoldenBoard:
    """The golden board file, read by golden.read_board (G) for each snapshot. digest, chars, words: from the file."""
    path: str = ""
    digest: str = ""
    chars: int = 0
    words: List[str] = field(default_factory=list)  # manager-only words found (leak check)
    leak_told: str = ""  # [golden] digest the manager was told about


@dataclass
class Discussion:
    """An open discussion with the manager or critic. [discussion]"""
    agent: str
    started: float = 0.0
    phase: DiscussionPhase = DiscussionPhase.OPEN
    by_terminal: bool = False
    apply_message: str = ""  # id of the DISCUSSION_APPLY message
    milestone_change: bool = False  # the user asked for a milestone change in it (MS-11)


@dataclass
class Terminal:
    """An open terminal on an agent's session. [terminal]"""
    agent: str
    opened: float = 0.0
    shared: bool = False  # it joined the open session; Orch's copy stays open


@dataclass
class Watch:
    """What the monitor last said about a worker (REC-10). [supervisor]"""
    worker: str
    checked_at: float = 0.0
    asked: str = ""  # message id of the check under way; at most one for all workers
    health: Optional[Health] = None
    reported: Optional[Health] = None  # the status last told to the critic
    reported_at: float = 0.0
    failures: int = 0  # in a row; 2: the critic is told
    told_unwatched: bool = False


@dataclass
class Silence:
    """SIL-2. [supervisor]"""
    tries: int = 0
    asked_at: float = 0.0


@dataclass
class Settings:
    """Numbers and switches the modules use. [engine] from config."""
    unanswered_after: float = 300.0  # MSG-5
    wakeup_after: float = 60.0  # JOB-5
    silence_after: float = 120.0  # all silent for this long before the manager is asked (each try)
    silence_tries: int = 3
    frozen_after: float = 2400.0  # SIL-4
    round_limit: int = 5000
    retries: int = 5  # REC-1
    retry_waits: List[float] = field(default_factory=lambda: [60.0, 120.0, 240.0])
    smoke_every: float = 1800.0  # REC-6
    handover_wait: float = 900.0  # SES-5: then the gate
    check_every: float = 1200.0  # monitor
    report_every: float = 3600.0  # REC-10: the same unhealthy status again
    job_quiet_after: float = 3600.0  # JOB-6
    return_after: float = 18000.0  # REC-5: try the main choice again
    golden_limit: int = 8000
    compact_wait: float = 600.0  # how long compact() may hold an agent
    pass_score: Optional[float] = None
    approve_plan: bool = True
    approve_milestones: bool = True


@dataclass
class RunInfo:
    """[engine]"""
    id: str = ""
    project: str = ""
    goal: str = ""
    workspace: str = ""
    run_dir: str = ""
    decisions_path: str = ""  # the decisions register
    audit_notes_path: str = ""  # audit-decisions.md beside audit.findings_path(...), the file the auditor reads
    audit_running: bool = False  # an audit runs now
    started: float = 0.0
    version: str = ""  # Orch's version


@dataclass
class Snapshot:
    """The state of a run as the store saves it and the modules see it. Never changed in place."""
    now: float = 0.0
    run: RunInfo = field(default_factory=RunInfo)
    settings: Settings = field(default_factory=Settings)
    agents: Dict[str, Agent] = field(default_factory=dict)  # all, deleted ones too
    messages: Dict[str, Message] = field(default_factory=dict)  # in seq order: every open or held one, those of the
    #                                              current turns and switches, the newest 300 others; the rest: ledger
    turns: Dict[str, Turn] = field(default_factory=dict)  # by agent: its current or last turn. Inside the store only,
    #                                         those named by an open message's `continues`, Agent.retry.turn, switch.turn
    gates: Dict[str, Gate] = field(default_factory=dict)  # open, closed but answer not sent, newest 50 others
    jobs: Dict[str, Job] = field(default_factory=dict)  # every open one, newest 50 others
    milestones: List[Milestone] = field(default_factory=list)  # only store of milestones (MS-15)
    checklist: Checklist = field(default_factory=Checklist)  # of the current milestone; not saved, read anew
    golden: GoldenBoard = field(default_factory=GoldenBoard)
    discussions: Dict[str, Discussion] = field(default_factory=dict)  # by agent
    terminals: Dict[str, Terminal] = field(default_factory=dict)  # by agent
    watch: Dict[str, Watch] = field(default_factory=dict)  # by worker
    catalog: List[ModelInfo] = field(default_factory=list)  # [engine]
    paused: bool = False  # by OpenGate and CloseGate only
    finished: bool = False  # [milestones] MS-14
    night: bool = False  # [engine] (SetNight); see Gate
    rounds: int = 0  # [supervisor] SIL-5: worker replies delivered to the critic
    silence: Silence = field(default_factory=Silence)
    orch_started: float = 0.0  # [engine] when this Orch process began

    def by_role(self, role: Role) -> List[Agent]:
        return [a for a in self.agents.values() if a.role == role]

    @property
    def manager(self) -> Optional[Agent]:
        return next(iter(self.by_role(Role.MANAGER)), None)

    @property
    def critic(self) -> Optional[Agent]:
        return next(iter(self.by_role(Role.CRITIC)), None)

    @property
    def workers(self) -> List[Agent]:
        """The open ones: those the critic may address."""
        return [a for a in self.by_role(Role.WORKER) if a.is_open]

    @property
    def milestone(self) -> Optional[Milestone]:
        return next((m for m in self.milestones if m.status == MilestoneStatus.CURRENT), None)

    def running_jobs(self, agent: str) -> List[Job]:
        return [j for j in self.jobs.values() if j.agent == agent and j.state == JobState.RUNNING]

    def open_jobs(self, agent: str) -> List[Job]:
        return [j for j in self.jobs.values() if j.agent == agent and j.open]

    def status_of(self, agent: str) -> AgentStatus:
        """What the store writes into Agent.status after every event."""
        a, t = self.agents[agent], self.turns.get(agent)
        if not a.is_open:
            return AgentStatus.CLOSED
        if a.failed_why:
            return AgentStatus.FAILED
        if t is not None and t.status == TurnStatus.RUNNING:
            return AgentStatus.WRITING
        return AgentStatus.WAITING_JOB if self.running_jobs(agent) else AgentStatus.IDLE

    def settled(self, agent: str) -> bool:
        """Nothing is under way with it: no open turn, no open job, no message sent and unanswered. For a pending
        switch, RefreshSession, compact() and silence."""
        t = self.turns.get(agent)
        writing = self.agents[agent].status == AgentStatus.WRITING or (t and t.status == TurnStatus.RUNNING)
        return not (writing or self.open_jobs(agent)
                    or any(m.recipient == agent and m.open and m.sent_at for m in self.messages.values()))

    def open_gates(self, answer_only: bool = False) -> List[Gate]:
        return [g for g in self.gates.values() if g.is_open and (g.is_answer_gate or not answer_only)]

    def waiting_for(self, agent: str) -> List[Message]:
        return [m for m in self.messages.values()
                if m.recipient == agent and m.status == MessageStatus.WAITING and m.drop is None]

    def drafting(self, agent: str) -> bool:
        """Its current or last turn is part of a discussion, also of one that ended while it wrote: what it writes to
        others is a draft, and its question, pause and decision have no effect (DISC-1, assumption 4). False for the
        reply to the apply message."""
        d, t = self.discussions.get(agent), self.turns.get(agent)
        if d and t and d.phase == DiscussionPhase.APPLYING and d.apply_message in t.started_by + t.joined:
            return False
        return d is not None or (t is not None and t.draft)


# ------------------------------------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Event:
    """Base of everything the modules decide about. `key` identifies it for good."""
    agent: str = ""
    at: float = 0.0

    @property
    def key(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class AgentEvent(Event):
    """Reported by an adapter (the engine too reports Received, with no pos). `stream` names the output (session id or
    file) `pos` belongs to, and never names two different contents. `pos` is opaque: where reading resumes after this
    event (a byte offset, "<turn id>/<item id>", ...); "" leaves the read position as it is. `uid` is the same on
    replay, and needed when one record yields several events (all but the last carry the record's START as pos) or
    the source has no order (e.g. "job:7:ended"). Reporting an old event again with its key does nothing."""
    stream: str = ""
    pos: str = ""
    uid: str = ""

    @property
    def key(self) -> str:
        return f"{self.agent}/{self.stream}/{self.uid or f'{self.pos}.{type(self).__name__}'}"


@dataclass(frozen=True)
class Received(AgentEvent):
    """taken False: the CLI has the message (Claude `queued`, OpenCode inbox.enqueued). Store: delivered, session_id.
    taken True: its text entered the conversation (Claude's echo with our id, Codex's userMessage item, OpenCode
    inbox.delivered): taken_at too, and in an open turn Turn.joined, triggers, keys and reply_turn, and its Message.jobs
    into Turn.jobs (they get woke); `answered` with the next output. For the message that starts a turn, TurnStarted
    says that, not this."""
    message_id: str = ""
    taken: bool = False


@dataclass(frozen=True)
class Discarded(AgentEvent):
    """The CLI gave up a message it had received (Claude `cancelled`). Store: in doubt, unless taken or answered."""
    message_id: str = ""


@dataclass(frozen=True)
class TurnStarted(AgentEvent):
    """Exactly one per TurnEnded. A wake turn starts with its first own output, not with Claude's `init`. Empty results,
    /compact, and foreground tasks yield no turn or job event.
    Store: while a turn is open: only its message_ids join it, with their Message.jobs (they get woke). Else a new Turn:
    started_by = `message_ids`, or else the messages received and not taken; and those taken while no turn was open. They become delivered, taken, answered.
    Turn.jobs = Message.jobs of these, and, if caps.wakes_itself, every ended job of the agent not yet woke; they get
    woke. retry keeps."""
    message_ids: Tuple[str, ...] = ()  # where the CLI tells


@dataclass(frozen=True)
class Text(AgentEvent):
    """Output, as a delta, for the live view; "" is bare progress (e.g. the CLI's own API retry). Progress: Text,
    ToolStart, ToolEnd may repeat and are not saved one by one. Store: last_output."""
    text: str = ""


@dataclass(frozen=True)
class ToolStart(AgentEvent):
    """Store: last_output, Agent.tools."""
    tool_id: str = ""
    label: str = ""


@dataclass(frozen=True)
class ToolEnd(AgentEvent):
    """Store: last_output, Agent.tools."""
    tool_id: str = ""


@dataclass(frozen=True)
class JobStarted(AgentEvent):
    """Store: adds the job. Antigravity, whose turn waits for its jobs, reports JobNoticed before its TurnEnded. Codex: a
    command is a job only if it still runs when its turn ends: JobStarted just before TurnEnded, jobs() leaves out the
    open turn's commands; when catching up, compare the item's and the turn's end times."""
    job: Optional[Job] = None


@dataclass(frozen=True)
class JobEnded(AgentEvent):
    """Store: state ended (lost: it ended without a result), exit_code, ended. A job its CLI stopped at a time limit
    has ended, not lost."""
    job_id: str = ""
    exit_code: Optional[int] = None
    lost: bool = False


@dataclass(frozen=True)
class JobNoticed(AgentEvent):
    """The end of the job was told to the turn that is running: no new turn will start for it. Store: woke, and the job
    into the open Turn's jobs; no turn open: nothing."""
    job_id: str = ""


@dataclass(frozen=True)
class TurnEnded(AgentEvent):
    """Store: closes the Turn (opens one first if none is), `ending` by Snapshot.open_jobs (the engine stores JobsChecked
    just before, except while catching up), tools cleared, idle_since; on ok: Turn.reply (labels.parse), the session
    confirmed (if it is the one asked for), retry None, failed_why cleared."""
    text: str = ""
    status: TurnStatus = TurnStatus.OK
    error: Optional[ErrorClass] = None
    detail: str = ""  # the CLI's error text, redacted as it is stored
    retry_after: float = 0.0  # seconds, when the provider says
    session_id: str = ""  # as reported; a different one than asked is also reported as SessionChanged
    meta: Dict[str, Any] = field(default_factory=dict)  # costs and tokens, as in today's turn files


@dataclass(frozen=True)
class SessionChanged(AgentEvent):
    """The adapter reports NEW or RESUMED itself once the process is up, with uid "start:<pid>" (then "session:<id>" when
    a NEW one's id comes later). Store: NEW/RESUMED: Agent.sessions and session_key of the running choice (a switch:
    Switch.new until done), never `confirmed`. COMPACTED: `given` emptied. DIFFERENT, LOST: nothing; recovery decides."""
    session_id: str = ""
    change: SessionChange = SessionChange.NEW
    detail: str = ""


@dataclass(frozen=True)
class ModelReported(AgentEvent):
    """What the CLI says it runs, also when it silently fell back to another. Store: actual_model."""
    model: str = ""


@dataclass(frozen=True)
class ProcessExited(AgentEvent):
    """The CLI is gone (events() checks pid and start time). Reported once, also when it is found dead at reconnect; an
    open turn was ended by a TurnEnded just before (STOPPED if asked, else error).
    Store: proc None if `stream` is still its stream; messages in doubt (see Sending). Engine: if a job was running,
    JobsChecked(()) follows: the supervisor marks it lost and wakes the agent, recovery does nothing."""
    exit_code: Optional[int] = None
    detail: str = ""
    asked: bool = False  # Orch or the user stopped it: not a failure
    reported: bool = False  # the event before it already told the failure: recovery does nothing


@dataclass(frozen=True)
class EngineEvent(Event):
    """Reported by the engine itself."""
    uid: str = ""  # when one agent has two events of a type at one time, e.g. the message id

    @property
    def key(self) -> str:
        return f"{type(self).__name__}/{self.agent}/{self.at}/{self.uid}"


@dataclass(frozen=True)
class Tick(EngineEvent):
    """Every few seconds; `at` is the time. Never replayed."""


@dataclass(frozen=True)
class OrchStarted(EngineEvent):
    """Orch began (or reopened) this run: after the reconnects and their missed events."""


@dataclass(frozen=True)
class Reconnected(EngineEvent):
    """Orch is connected to the agent's CLI again after a restart (dead instead: ProcessExited)."""


@dataclass(frozen=True)
class JobsChecked(EngineEvent):
    """Adapter.jobs(), taken before each TurnEnded, every minute while a job runs, and after a reconnect once, when all
    missed events were read (until then JobStarted and JobEnded alone count). Always taken after all available events
    were read. Store: adds missing jobs, takes over ended states. A job recorded as running and not in `jobs` is left
    to the supervisor (MarkLost)."""
    jobs: Tuple[Job, ...] = ()


@dataclass(frozen=True)
class SendFailed(EngineEvent):
    """A transmission raised an error. Store: message.error, sent_at and checked_at 0; it stays waiting."""
    message_id: str = ""
    error: ErrorClass = ErrorClass.TRANSIENT
    detail: str = ""


@dataclass(frozen=True)
class AgentFailed(EngineEvent):
    """Follows a FailAgent action, or a CLI that could not be started: recovery applies the retry rule (REC-1)."""
    error: ErrorClass = ErrorClass.TRANSIENT
    why: str = ""


@dataclass(frozen=True)
class SmokeResult(EngineEvent):
    """MOD-7, REC-6."""
    choice: Choice = field(default_factory=Choice)
    ok: bool = False
    why: str = ""
    error: Optional[ErrorClass] = None


@dataclass(frozen=True)
class Said:
    """One message of a session's transcript."""
    by: str  # USER or the agent id
    text: str
    at: float = 0.0


@dataclass(frozen=True)
class TerminalRead(EngineEvent):
    """What was said in a terminal, read after it closed (TERM-2). Reported after the CLI is open again. A shared
    terminal's turns were ordinary turns already (routing sends their replies to the user), so there `said` has only
    the user's lines. The terminal module records each reply in `said` with a ToUser, and tells the critic what the
    user said to a worker (DIRECT_MESSAGE_NOTICE)."""
    said: Tuple[Said, ...] = ()


# ----------------------------------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class UserCommand(Event):
    """One dashboard or CLI action. `agent` is the agent it is about. `id` is made by the sender (any unique string), so
    a repeated POST does nothing. `handler` names who answers it: that module emits exactly one CommandReply (other
    modules may act too)."""
    id: str = ""
    handler: ClassVar[str] = "engine"

    @property
    def key(self) -> str:
        return f"user/{self.id}"


@dataclass(frozen=True)
class Chat(UserCommand):
    """The user writes to `agent`."""
    handler: ClassVar[str] = "routing"
    text: str = ""


@dataclass(frozen=True)
class GateAnswer(UserCommand):
    """Answers one gate, of any kind. Nothing given: closes a pause or operational gate."""
    handler: ClassVar[str] = "gates"
    gate_id: str = ""
    text: str = ""
    option: Optional[int] = None
    approve: Optional[bool] = None


@dataclass(frozen=True)
class WorkerAdd(UserCommand):
    """A new worker, set up like worker `like` ("": the first one). agent empty: the next free id."""
    handler: ClassVar[str] = "workers"
    like: str = ""


@dataclass(frozen=True)
class WorkerClose(UserCommand):
    """jobs None with a job running: CommandReply asks (choices finish, stop)."""
    handler: ClassVar[str] = "workers"
    jobs: Optional[JobsChoice] = None


@dataclass(frozen=True)
class WorkerReopen(UserCommand):
    handler: ClassVar[str] = "workers"


@dataclass(frozen=True)
class WorkerDelete(UserCommand):
    handler: ClassVar[str] = "workers"
    jobs: Optional[JobsChoice] = None


@dataclass(frozen=True)
class ChooseModel(UserCommand):
    """A model of the catalog (model_id + effort), or one of the agent's own choices by name (backup = its name, ""
    with no model_id = the main one). force without confirmed: CommandReply asks first (MOD-6). Choosing the current one
    again ends the pending switch. Refused (SWITCHING) from SwitchStep.OPENING on."""
    handler: ClassVar[str] = "models"
    model_id: str = ""
    effort: str = ""
    backup: str = ""
    force: bool = False
    confirmed: bool = False


@dataclass(frozen=True)
class ForceSwitch(UserCommand):
    """Do the pending switch now."""
    handler: ClassVar[str] = "recovery"
    confirmed: bool = False


@dataclass(frozen=True)
class DiscussionStart(UserCommand):
    handler: ClassVar[str] = "discussion"


@dataclass(frozen=True)
class DiscussionApply(UserCommand):
    """text: the user's own words with it, or ""."""
    handler: ClassVar[str] = "discussion"
    text: str = ""


@dataclass(frozen=True)
class DiscussionEnd(UserCommand):
    """Without applying."""
    handler: ClassVar[str] = "discussion"


@dataclass(frozen=True)
class TerminalOpen(UserCommand):
    """From `orch terminal`. The reply's data has {"command": [...], "cwd": "..."}; `orch terminal` fills in the
    environment itself (models.resolve), so no key passes the dashboard. On a writing agent or one with a job it asks
    first; stop_job: the user agreed that the turn or job ends (TERM-5)."""
    handler: ClassVar[str] = "terminal"
    stop_job: bool = False


@dataclass(frozen=True)
class TerminalClose(UserCommand):
    """`orch terminal` ended."""
    handler: ClassVar[str] = "terminal"


@dataclass(frozen=True)
class TerminalTakeBack(UserCommand):
    """The dashboard's button (TERM-6)."""
    handler: ClassVar[str] = "terminal"


@dataclass(frozen=True)
class ChangeMilestones(UserCommand):
    """MS-10. text: what the user wants."""
    handler: ClassVar[str] = "milestones"
    text: str = ""


@dataclass(frozen=True)
class ReopenMilestone(UserCommand):
    handler: ClassVar[str] = "milestones"
    milestone_id: str = ""


@dataclass(frozen=True)
class SendToCritic(UserCommand):
    """LAB-6: pass a worker's reply to the user on."""
    handler: ClassVar[str] = "routing"
    message_id: str = ""


# handled by the engine itself, no module decides

@dataclass(frozen=True)
class StopAgents(UserCommand):
    """REC-9. agents empty: all. Store: stopped_by_user, their running jobs stopped_by_user. Engine: HoldAgent STOPPED,
    stop the CLIs."""
    agents: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StartAgents(UserCommand):
    """Undo StopAgents: reopen the saved sessions, release STOPPED."""
    agents: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StopOrch(UserCommand):
    """Close Orch; agents=True also stops the agents (assumption 5)."""
    agents: bool = False


@dataclass(frozen=True)
class UpdateOrch(UserCommand):
    pass


@dataclass(frozen=True)
class SetNight(UserCommand):
    on: bool = False


@dataclass(frozen=True)
class AuditRun(UserCommand):
    pass


@dataclass(frozen=True)
class AuditNotes(UserCommand):
    """UI-4: the whole file. base: digest of the text it was edited from; refused if the file changed since."""
    text: str = ""
    base: str = ""


@dataclass(frozen=True)
class EditGolden(UserCommand):
    """As AuditNotes, for the golden board."""
    text: str = ""
    base: str = ""


@dataclass(frozen=True)
class ConfirmMigration(UserCommand):
    """MOD-4: the old files may go."""


# ------------------------------------------------------------------------------------------------------ actions


@dataclass(frozen=True)
class Action:
    """What a module asks for. `emitters`: the only ones that may (see `may_emit`)."""
    emitters: ClassVar[Tuple[str, ...]] = ()


@dataclass(frozen=True)
class Send(Action):
    """Store: create the message (a known id: only add `bypass`). Engine: transmit it once nothing holds it ("Sending" at
    the top). A message to USER is delivered by being stored. Its subclasses are Sends too: holds and drops work on
    them alike."""
    emitters: ClassVar[Tuple[str, ...]] = MODULE_ORDER + ("engine",)
    message: Message = field(default_factory=lambda: Message(""))
    bypass: Tuple[HoldReason, ...] = ()  # agent holds it passes


@dataclass(frozen=True)
class Tell(Send):
    """A notice to an agent: needs_answer is False. Sent at once like any other (MSG-9)."""


@dataclass(frozen=True)
class ToUser(Send):
    """recipient USER, needs_answer False."""


@dataclass(frozen=True)
class ReturnToSender(Send):
    """LAB-5: `message` (LABEL_RETURNED) goes back to the sender; the broken parts of `turn` go nowhere. The second
    time (the turn's keys hold LABEL_RETURNED) routing passes them on instead."""
    emitters: ClassVar[Tuple[str, ...]] = ("routing",)
    turn: str = ""


@dataclass(frozen=True)
class Reject(Send):
    """MSG-8: `message` tells the sender; `rejected` is recorded as dropped REJECTED."""
    emitters: ClassVar[Tuple[str, ...]] = ("routing",)
    rejected: Message = field(default_factory=lambda: Message(""))


@dataclass(frozen=True)
class WakeWorker(Send):
    """JOB-5 (JOB_WAKEUP) and JOB-7 (JOB_LOST), one for all its jobs that wait; both only while the jobs are not woke.
    When: "Timers" at the top.
    Store: Job.fallback_sent, Message.jobs = job_ids. Engine: as Send; reopens the session first if the process is
    gone."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    job_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AskManagerToCheck(Send):
    """SIL-2. Store: silence.tries + 1, asked_at."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)


@dataclass(frozen=True)
class NotifyReviewReady(Send):
    """MS-4. Store: the milestone's `notified` = digest."""
    emitters: ClassVar[Tuple[str, ...]] = ("milestones",)
    milestone_id: str = ""
    digest: str = ""


@dataclass(frozen=True)
class Hold(Action):
    """Store: adds a message hold."""
    emitters: ClassVar[Tuple[str, ...]] = ("routing", "gates", "discussion")
    message_id: str = ""
    reason: HoldReason = HoldReason.QUESTION


@dataclass(frozen=True)
class Release(Action):
    """Store: lifts a message hold. Engine: transmit if it was the last."""
    emitters: ClassVar[Tuple[str, ...]] = ("gates", "discussion")
    message_id: str = ""
    reason: HoldReason = HoldReason.QUESTION


@dataclass(frozen=True)
class Drop(Action):
    """Store: status failed, `drop`. With a Send of the same event: recorded, never sent."""
    emitters: ClassVar[Tuple[str, ...]] = ("routing", "gates", "discussion", "recovery", "workers", "milestones",
                                           "engine")
    message_id: str = ""
    reason: DropReason = DropReason.REJECTED


@dataclass(frozen=True)
class Resend(Action):
    """Store: checked_at. Engine: ask arrived(): True: report Received, send nothing; False: transmit `rendered` again;
    None: transmit only if if_unknown: the supervisor's (MSG-5) says True; in doubt after a restart the engine acts as
    with False and leaves the rest to MSG-5."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor", "recovery")
    message_id: str = ""
    if_unknown: bool = False


@dataclass(frozen=True)
class HoldAgent(Action):
    """Store: adds an agent hold."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery", "terminal", "engine")
    agent: str = ""
    reason: HoldReason = HoldReason.SWITCH


@dataclass(frozen=True)
class ReleaseAgent(Action):
    """Store: lifts it. Engine: transmit what now may go, in order."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery", "terminal", "engine")
    agent: str = ""
    reason: HoldReason = HoldReason.SWITCH


@dataclass(frozen=True)
class OpenGate(Action):
    """Store: adds the gate (a known open id: nothing; a closed `cause` id: opened again). A pause or operational gate
    also sets Snapshot.paused, so every pause has a gate (GATE-9); there is no separate pause action."""
    emitters: ClassVar[Tuple[str, ...]] = ("gates", "milestones", "supervisor", "recovery")
    gate: Gate = field(default_factory=lambda: Gate(""))


@dataclass(frozen=True)
class CloseGate(Action):
    """Store: closed, answer or withdrawn; with no gate left open, paused ends (GATE-6). The answers of answer
    gates take effect only with the Send whose message.gates names them (gates emits it once no answer gate is open and
    an answer is unsent, on Tick too; milestones reads it in `prior`). Except milestone_change: its approval takes
    effect with its CloseGate: milestones reads that in `prior`, withdraws the open plan or milestone gate (MS-13) and
    applies the list."""
    emitters: ClassVar[Tuple[str, ...]] = ("gates", "milestones", "recovery")
    gate_id: str = ""
    answer: Optional[Answer] = None
    withdrawn: bool = False


@dataclass(frozen=True)
class HoldDecision(Action):
    """MS-9. Store: Milestone.held_decision. In `prior` it tells milestones not to act on the decision. None: lifted."""
    emitters: ClassVar[Tuple[str, ...]] = ("gates",)
    milestone_id: str = ""
    decision: Optional[Decision] = None


@dataclass(frozen=True)
class RecordDecision(Action):
    """Engine: one entry in the decisions register (gate answers; night mode's own recommendation, and from milestones
    the plans and milestones approved by night)."""
    emitters: ClassVar[Tuple[str, ...]] = ("gates", "milestones")
    text: str = ""
    by: str = USER
    source: str = ""  # gate id or turn


@dataclass(frozen=True)
class Retry(Action):
    """Store: Agent.retry = state (None: cleared). Recovery acts on it at a later Tick; a transient failure in an open
    CLI is retried there (REC-4)."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery",)
    agent: str = ""
    state: Optional[RetryState] = None


@dataclass(frozen=True)
class StartSwitch(Action):
    """Store: Agent.switch = switch (replaces one not yet OPENING); target.same(Agent.choice) and a USER switch: None
    (the pending one is ended). Engine: the effect of its step. Recovery lifts SWITCH holds an ended switch left."""
    emitters: ClassVar[Tuple[str, ...]] = ("models", "recovery")
    switch: Switch = field(default_factory=lambda: Switch(""))


@dataclass(frozen=True)
class AdvanceSwitch(Action):
    """Store: Agent.switch = switch, with its new step. Engine: the effect of that step (SwitchStep)."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery",)
    switch: Switch = field(default_factory=lambda: Switch(""))


@dataclass(frozen=True)
class RestartSession(Action):
    """Engine: stop the agent's CLI if it runs, drain its events, open its saved session again with its choice (no second
    CLI ever). Recovery lets an unfinished turn go on: RESTART_CONTINUE (continues = that turn) when the CLI is open
    again after this, StartAgents, a restart after StopOrch(agents=True), TerminalClose or a failed switch; not for an
    interrupt the user made inside a terminal.
    Store: frozen_restart = now if `frozen`."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor", "recovery")
    agent: str = ""
    frozen: bool = False


@dataclass(frozen=True)
class RefreshSession(Action):
    """Assumption 3, for a settled agent. Engine: if it still is, compact its session ("compact"; a new one if it
    cannot), or start a "new" one and send SESSION_FRESH. From compact() on it holds the agent (TURN) until
    SessionChanged(COMPACTED), the process's end or Settings.compact_wait."""
    emitters: ClassVar[Tuple[str, ...]] = ("milestones",)
    agent: str = ""
    mode: str = "compact"


@dataclass(frozen=True)
class SmokeTest(Action):
    """Engine: models.smoke_test in a temporary session, then SmokeResult. Store: last_smoke."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery", "models")
    agent: str = ""
    choice: Choice = field(default_factory=Choice)


@dataclass(frozen=True)
class SaveChoice(Action):
    """MOD-3, when a user's switch passed its smoke test. Store: choices[0]. Engine: models.save_choice into
    agent.yaml."""
    emitters: ClassVar[Tuple[str, ...]] = ("models",)
    agent: str = ""
    choice: Choice = field(default_factory=Choice)


@dataclass(frozen=True)
class FailAgent(Action):
    """MSG-5, SIL-4: treat the agent as failed. Engine: an AgentFailed event follows, which recovery decides."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    agent: str = ""
    why: FailCause = FailCause.NO_ANSWER
    error: ErrorClass = ErrorClass.TRANSIENT


@dataclass(frozen=True)
class MarkFailed(Action):
    """Nothing is left to try. Store: failed_why (so status failed). Recovery adds the notice or gate (REC-3)."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery",)
    agent: str = ""
    why: str = ""


@dataclass(frozen=True)
class AddWorker(Action):
    """Store: adds the agent. Engine: config.add_worker (its agent.yaml, the source of its config, copied from `like`);
    its CLI opens with the first message."""
    emitters: ClassVar[Tuple[str, ...]] = ("workers",)
    agent: Agent = field(default_factory=lambda: Agent(""))
    like: str = ""  # the worker it is set up like


@dataclass(frozen=True)
class SetLifecycle(Action):
    """Store: lifecycle. Engine: close or delete: stop the CLI once no job runs (or at once after StopJob);
    open: reopen the saved session with the next message."""
    emitters: ClassVar[Tuple[str, ...]] = ("workers",)
    agent: str = ""
    lifecycle: Lifecycle = Lifecycle.OPEN


@dataclass(frozen=True)
class StopJob(Action):
    """Store: stopped_by_user. Engine: adapter.stop_job."""
    emitters: ClassVar[Tuple[str, ...]] = ("workers", "terminal")
    job_id: str = ""


@dataclass(frozen=True)
class MarkLost(Action):
    """JOB-7. Store: state lost, ended = now. The process itself may still run."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    job_id: str = ""


@dataclass(frozen=True)
class ReportJob(Action):
    """JOB-6. Store: Job.reported (comes with a Tell to the critic)."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    job_id: str = ""


@dataclass(frozen=True)
class CountRound(Action):
    """SIL-5, when a worker's reply is delivered to the critic. Store: rounds + 1 (reset: 0)."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    message_id: str = ""
    reset: bool = False


@dataclass(frozen=True)
class ResetSilence(Action):
    """Work restarted. Store: silence.tries 0."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)


@dataclass(frozen=True)
class SetWatch(Action):
    """Store: watch[worker]."""
    emitters: ClassVar[Tuple[str, ...]] = ("supervisor",)
    watch: Watch = field(default_factory=lambda: Watch(""))


@dataclass(frozen=True)
class SetMilestones(Action):
    """Store: the whole list (also rubric, score, accepted, refreshed, ...), except held_decision, and `notified` of a
    milestone with a NotifyReviewReady of the same event: those stay as stored. Ids never disappear (MS-12)."""
    emitters: ClassVar[Tuple[str, ...]] = ("milestones",)
    milestones: Tuple[Milestone, ...] = ()


@dataclass(frozen=True)
class SetFinished(Action):
    emitters: ClassVar[Tuple[str, ...]] = ("milestones",)
    finished: bool = True


@dataclass(frozen=True)
class SetGolden(Action):
    """Store: leak_told of `golden`."""
    emitters: ClassVar[Tuple[str, ...]] = ("golden",)
    golden: GoldenBoard = field(default_factory=GoldenBoard)


@dataclass(frozen=True)
class SaveNotes(Action):
    """<state>. Engine: the agent's state.md."""
    emitters: ClassVar[Tuple[str, ...]] = ("routing",)
    agent: str = ""
    text: str = ""


@dataclass(frozen=True)
class SetDiscussion(Action):
    """Store: discussions[agent] (None: ended); a running turn keeps Turn.draft."""
    emitters: ClassVar[Tuple[str, ...]] = ("discussion",)
    agent: str = ""
    discussion: Optional[Discussion] = None


@dataclass(frozen=True)
class OpenTerminal(Action):
    """Store: terminals[agent]. Engine: unless shared, close Orch's CLI; put the command into the CommandReply."""
    emitters: ClassVar[Tuple[str, ...]] = ("terminal",)
    terminal: Terminal = field(default_factory=lambda: Terminal(""))
    command_id: str = ""


@dataclass(frozen=True)
class CloseTerminal(Action):
    """Store: removes it. Engine: take the session back (end a terminal still running), reopen the CLI, report
    TerminalRead. The terminal module releases its hold when TerminalRead comes."""
    emitters: ClassVar[Tuple[str, ...]] = ("terminal",)
    agent: str = ""


@dataclass(frozen=True)
class CommandReply(Action):
    """The result of a UserCommand, returned to whoever sent it. `choices`: ok is False and the same command is to be
    sent again (new id) with one of them: {"label", "set": {field: value}}."""
    emitters: ClassVar[Tuple[str, ...]] = ("recovery", "models", "workers", "routing", "gates", "discussion",
                                           "terminal", "milestones", "engine")
    command_id: str = ""
    ok: bool = True
    text: Optional[TextRef] = None
    choices: Tuple[Dict[str, Any], ...] = ()
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Intent:
    """One action being performed (3.5). Store and engine only."""
    id: str  # "<event key>#<n>"
    action: Optional[Action] = None
    state: IntentState = IntentState.PLANNED
    created: float = 0.0
    done: float = 0.0
    error: str = ""


# --------------------------------------------------------------------------------------------------- protocols


class Clock(Protocol):
    def now(self) -> float: ...


class Module(Protocol):
    """Pure: the same arguments give the same actions; no files, time, randomness or state of its own."""
    name: str  # one of MODULE_ORDER

    def decide(self, snap: Snapshot, event: Event, prior: Sequence[Action] = ()) -> List[Action]: ...


@dataclass
class AgentSpec:
    """What an adapter needs to open an agent's CLI."""
    agent: str
    role: Role = Role.WORKER
    choice: Choice = field(default_factory=Choice)  # resolved: env complete
    cwd: str = ""
    system_prompt: str = ""
    run_dir: str = ""  # the adapter keeps its pipe, output file and pid file in run_dir/cli/<agent>/


class Adapter(Protocol):
    """One open CLI. No method blocks for long; errors are raised as base.AdapterError."""
    caps: Caps

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        """Open the CLI on the session (None: a new one), in spec.cwd. First ends or joins a CLI an earlier start left
        (Orch died before saving its ProcRef), found by the pid file. SessionChanged follows as an event. May raise."""

    def reconnect(self, proc: ProcRef) -> bool:
        """Join the running process after a restart; False: it is gone, but events() still gives everything after
        proc.read_pos and ends with ProcessExited. The engine drains events() before start()."""

    def send(self, message_id: str, text: str) -> None:
        """Hand the text to the CLI now; returns when the CLI's input has taken it. Raises on failure."""

    def arrived(self, message_id: str) -> Optional[bool]:
        """Does the CLI have it: in its queue or the conversation? On a newly opened process: is it in the session's
        history? None: cannot tell."""

    def events(self) -> Iterator[AgentEvent]:
        """The events available now after the read position, then stops; never blocks. The engine calls it again and
        again. After reconnect() it starts after proc.read_pos; stop() or a new start() drops none. It also checks that
        the process lives (an idle CLI is silent)."""

    def jobs(self) -> List[Job]:
        """Those running, really (not the CLI's ghost entries), and the ended ones of the session it knows."""

    def stop_job(self, job_id: str) -> bool: ...

    def detach(self) -> None:
        """Orch closes; the CLI and its jobs stay."""

    def stop(self) -> None:
        """End the CLI and its jobs; if it must be killed, also the process groups of its jobs. An open turn ends
        STOPPED; ProcessExited(asked=True) follows."""

    def compact(self) -> bool:
        """Start compacting the idle session; SessionChanged(COMPACTED) follows, and no other event for it. False: it
        cannot."""

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        """The command that opens this session for the user; needs no started adapter."""

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        """What was said in the session after `since` (TERM-2)."""

    def catalog(self) -> List[ModelInfo]:
        """Models with their effort levels, where the CLI lists them; needs no started adapter."""


@dataclass
class Rendered:
    """What texts.render returns. given: to be added to SessionRef.given once delivered."""
    text: str
    given: Dict[str, str] = field(default_factory=dict)


# Entry points (functions with these names and arguments in the module named in ENTRY_POINTS)
#   labels.parse(text: str, role: Role) -> Reply
#   milestones.read_checklist(path: str, text: Optional[str]) -> Checklist       text None: missing or unreadable
#   golden.read_board(path: str, text: Optional[str], last: GoldenBoard, names: Sequence[str]) -> GoldenBoard
#                                                                                names: the agents' names
#   texts.render(snap: Snapshot, message: Message) -> Rendered                   with the reminder; its random picks
#                                                                                use message.id as the seed
#   texts.words(ref: TextRef, snap: Snapshot) -> str                             gates, command replies, notices
#   texts.system_prompt(snap: Snapshot, agent: str) -> str
#   models.module(settings: Dict) -> Module
#   models.resolve(choice: Choice, settings: Dict, agent_yaml: Dict) -> Choice   env and options complete
#   models.catalog(settings: Dict, listed: Dict[str, List[ModelInfo]]) -> List[ModelInfo]    listed: by backend
#   models.smoke_test(adapter: Adapter, spec: AgentSpec, clock: Clock, wait) -> SmokeResult     not ok when
#                                                                                ModelReported differs (aliases allowed)
#   models.save_choice(agent_yaml: Path, choice: Choice) -> None
#   orch.config.add_worker(project: Path, like: str, worker: str) -> None        (M) copies like's agent.yaml


class Runtime(Protocol):
    """What the dashboard server (J) and the commands (M) use of a running engine. While Orch is closed a command is
    saved as <run_dir>/commands/<time>-<id>.json (to_dict form); the engine runs these in order when it starts."""

    def state(self) -> Dict[str, Any]:
        """public_state() of the current snapshot."""

    def submit(self, command: UserCommand) -> Dict[str, Any]:
        """Run the command and return its CommandReply as `reply_json`."""

    def message(self, id: str) -> Optional["Message"]:
        """The full message (with `rendered`), from state.json or the ledger; None if unknown."""

    def messages(self, before: int = 0, limit: int = 50, q: str = "", agent: str = "") -> Tuple[List["Message"], bool]:
        """Messages older than seq `before` (0: newest), at most `limit`, in seq order, and whether more exist."""


# ----------------------------------------------------------------------------------------------- stored form

T = TypeVar("T")
_TYPES: Dict[str, type] = {}
_HINTS: Dict[type, Dict[str, Any]] = {}


# keys in a provider's words: "sk-...", "api_key=...", "Authorization: Bearer ...", the user and query of an address
_SECRETS = (
    # (a number or a snake_case word after "token:" or "key:" is none)
    (re.compile(r"(?i)\b((?:api|access|secret|auth)[-_ ]?(?:key|token)|authorization|secret|password|"
                r"(?:token|key)(?!['\"]?\s*[:=]\s*['\"]?(?:\d+|[a-z]+(?:_[a-z]+)+)(?![\w-])))"
                r"(['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+|basic\s+)?[^\s'\",;}&]{6,}"), r"\1\2***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(api[-_ ]?key)\s+(?=[\w-]*\d)[\w-]{8,}"), r"\1 ***"),
    (re.compile(r"\b(?:sk|pk|rk|tp|ghp|xox[a-z])[-_][\w-]{8,}|\bAIza[\w-]{20,}"), "***"),
    (re.compile(r"(\w://)[^\s/@]+@"), r"\1"),
    (re.compile(r"(\w://[^\s?]+)\?[^\s'\")]+"), r"\1"),
)


def redact(text: str) -> str:
    """An error text without keys. The store applies it to every error, detail and why it takes in."""
    for pattern, by in _SECRETS:
        text = pattern.sub(by, text)
    return text


def _bare_url(url: str) -> str:
    """Without user, password, query and fragment."""
    return re.sub(r"(://)[^/@]*@", r"\1", re.sub(r"[?#].*", "", url))


def _register(base: type) -> None:
    for c in base.__subclasses__():
        _TYPES[c.__name__] = c
        _register(c)


for _b in (Event, Action):
    _register(_b)


def new_id(module: str, event: Event, n: int = 0) -> str:
    """The id of the n-th thing (message, gate, ...) `module` makes because of this event; the same whenever the event
    is decided, and different between modules."""
    return "x" + hashlib.sha1(f"{module}#{event.key}#{n}".encode()).hexdigest()[:12]


def may_emit(module: str, action: Action) -> bool:
    return module in type(action).emitters


def to_dict(obj: Any) -> Any:
    """Plain JSON values. Events, commands and actions carry their class name as "type"."""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        stored = getattr(obj, "_stored", None)
        d = stored() if stored else {f.name: getattr(obj, f.name) for f in fields(obj)}
        out = {k: to_dict(v) for k, v in d.items()}
        return {"type": type(obj).__name__, **out} if isinstance(obj, (Event, Action)) else out
    if isinstance(obj, dict):
        return {str(to_dict(k)): to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def from_dict(cls: Type[T], data: Any) -> T:
    """The reverse of to_dict. Unknown keys are ignored, missing ones take their default, an unknown enum value stays
    the plain string. For Event, UserCommand or Action the "type" picks the class; unknown: ValueError. A UserCommand
    comes from outside, so it must also have an id and fields of the right types (else ValueError)."""
    name = data.get("type") if isinstance(data, dict) else None
    if name is not None and issubclass(cls, (Event, Action)):
        sub = _TYPES.get(name)
        if sub is None or not issubclass(sub, cls):
            raise ValueError(f"unknown {cls.__name__}: {name!r}")
        cls = sub
    if issubclass(cls, UserCommand):
        _check_command(cls, data)
    return _load(cls, data)


def _check_command(cls: type, data: Any) -> None:
    if not isinstance(data, dict) or cls is UserCommand:
        raise ValueError("not a command")
    hints = typing.get_type_hints(cls)
    bad = [f.name for f in fields(cls) if f.name in data and not _fits(hints[f.name], data[f.name])]
    if bad or not data.get("id"):
        raise ValueError(f"{cls.__name__}: bad {', '.join(bad) or 'id'}")


def _fits(tp: Any, v: Any) -> bool:
    """For the types commands use."""
    origin, args = getattr(tp, "__origin__", None), getattr(tp, "__args__", ())
    if origin is Union:
        return any(_fits(a, v) for a in args)
    if origin in (tuple, Tuple):
        return isinstance(v, (list, tuple)) and all(_fits(args[0], x) for x in v)
    if issubclass(tp, Enum):
        return v in [e.value for e in tp]
    if tp is float:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, tp) and (tp is bool or not isinstance(v, bool))


def _load(tp: Any, v: Any) -> Any:
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", ())
    if origin is Union:  # Optional[...]
        return None if v is None else _load(next(a for a in args if a is not type(None)), v)
    if v is None or tp is Any:
        return v
    if origin in (list, List):
        return [_load(args[0], x) for x in v]
    if origin in (tuple, Tuple):
        return tuple(_load(args[0] if args[1] is Ellipsis else args[i], x) for i, x in enumerate(v))
    if origin in (dict, Dict):
        return {_load(args[0], k): _load(args[1], x) for k, x in v.items()}
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(v)
        except ValueError:
            return v
    if isinstance(tp, type) and issubclass(tp, (Event, Action)) and "type" in v and v["type"] != tp.__name__:
        return from_dict(tp, v)
    if is_dataclass(tp):
        if tp not in _HINTS:
            _HINTS[tp] = typing.get_type_hints(tp)
        hints = _HINTS[tp]
        kw = {f.name: _load(hints[f.name], v[f.name]) for f in fields(tp) if f.name in v}
        for f in fields(tp):  # a record written by an older version without a required field
            if f.name not in kw and f.default is MISSING and f.default_factory is MISSING:  # type: ignore[misc]
                kw[f.name] = ""
        return tp(**kw)
    return v


# ------------------------------------------------------------------------------------------------ dashboard API

# GET  /api/state                              public_state(): to_dict(Snapshot) with
#        agents.<id>: choice and choices[] = Choice.public(); sessions = number; proc = open or not; plus
#          waiting = number of messages waiting for it; jobs = ids of its running jobs
#        messages.<id>: without `rendered`
#        version = CONTRACT_VERSION, rev (added by J: changes with every change)
# POST /api/command  {"type": "WorkerClose", "id": "...", "agent": "worker-2", ...}   one UserCommand (to_dict form)
#        -> 200 reply_json(); 400 {"ok": false, "error": "..."} for an unknown type or bad fields
# GET  /api/messages?before=<seq>&limit=&q=&agent=   {"messages": [Message without rendered], "more": bool}
# GET  /api/message?id=                        {"message": Message with rendered}
# GET  /api/turn?id=                           today's turn file (old runs too)
# GET  /api/live?agent=&from=<n>               {"items": [{"n", "at", "k": "text"|"tool", "s"}], "next": n}: the lines of
#                                              <run_dir>/live/<agent>.jsonl, which the engine writes
# GET  /api/events                             server-sent: `state` {"rev"}, `live` {"agent", "n", "at", "k", "s"};
#                                              n: the line's index, so after a reconnect the page asks /api/live from n+1
# GET  /api/golden | /api/audit_notes          {"text", "digest", "path"}
# GET  /api/audit                              {"report", "running"}: report = the reply part of the newest audit, redacted
# GET  /api/runs                               {"runs": [{"id", "project", "status", "started", "old": bool}]}
# GET  /api/run?id=                            an old run, as today's board state (UI-6)
# Shown texts (gate.words, message.text, reply.text) carry a TextRef; J adds `shown`, the rendered words, next to it.


def public_state(snap: Snapshot) -> Dict[str, Any]:
    """The snapshot for GET /api/state: no keys, addresses or process details."""
    d = to_dict(snap)
    for aid, a in snap.agents.items():
        p = d["agents"][aid]
        p["choice"], p["choices"] = a.choice.public(), [c.public() for c in a.choices]
        p["sessions"], p["proc"] = len(a.sessions), a.proc is not None
        p.pop("session_key")
        p["waiting"] = len(snap.waiting_for(aid))
        p["jobs"] = [j.id for j in snap.running_jobs(aid)]
        if a.switch:
            p["switch"] = {k: p["switch"][k] for k in ("reason", "forced", "step", "step_at", "created", "new_session",
                                                       "handover_by", "error")}
            p["switch"]["target"] = a.switch.target.public()
    for m in d["messages"].values():
        m.pop("rendered")
    return {"version": CONTRACT_VERSION, **d}


def reply_json(reply: CommandReply, shown: str = "") -> Dict[str, Any]:
    """The body of a POST /api/command answer. shown: reply.text rendered."""
    return {"ok": reply.ok, "shown": shown, "text": to_dict(reply.text), "choices": list(reply.choices),
            "data": reply.data}
