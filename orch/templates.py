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


# templates replaced by the reminder registry (orch/reminders.py): a config that still overrides one loads, unused
RETIRED_TEMPLATES = (  # the old modes' texts (N): a project may still set them; they are accepted, not used
    "tag_reminder_manager", "tag_reminder_critic", "tag_reminder_worker", "manager_direct_voice", "worker_initial",
    "worker_feedback", "verifier_initial", "verifier_followup", "controller_system", "controller_initial",
    "controller_update", "agent_initial", "agent_followup", "manager_plan", "manager_plan_revise", "manager_kickoff",
    "manager_question", "manager_question_before_report", "manager_review", "manager_review_decision",
    "manager_review_resumed", "manager_review_wait_revise", "manager_feedback", "unsent_message", "critic_brief",
    "critic_brief_workers", "critic_verdict_missing", "critic_review", "critic_review_short", "critic_answer",
    "critic_answer_pending", "critic_report", "critic_jobs_running", "worker_job_done", "manager_pause",
    "critic_continue", "critic_resume", "critic_guidance", "critic_checkin", "critic_urgent_checkin",
    "critic_message_turn", "critic_message_turn_next", "critic_message_turn_idle", "manager_message_turn",
    "manager_message_at_gate", "manager_checkin", "worker_start", "worker_next", "worker_timeout", "milestone_fresh",
    "milestone_fresh_critic", "milestone_compact", "milestone_compact_critic", "decisions_intro", "worker_fresh",
    "manager_restart", "critic_restart", "state_edited", "critic_state_instruction", "decisions_since",
    "decisions_pointer", "decisions_intro_worker", "turn_continue", "turn_unusable", "turn_interrupted",
    "backend_switched", "relay_answer_manager", "relay_answer_critic", "relay_settled_critic",
    "manager_question_chat", "manager_question_settle", "manager_question_settle_mine", "manager_resumed",
    "relay_note", "critic_reply_routing", "urgent_resume", "urgent_cut_short", "worker_cut_short", "worker_after_cut",
    "night_manager", "session_reset", "report_checklist", "report_plan", "report_plan_done", "plan_nudge",
    "checklist_file_nudge", "checklist_cap", "golden_prune", "golden_leak", "relay_note_up",
    "address_correction_manager", "address_correction_critic", "address_correction_worker", "manager_no_message",
    "critic_no_message", "chat_reminder_critic", "chat_reminder_worker", "monitor_check", "worker_model_changed",
    "progress_check_report", "critic_quick_check", "critic_check_turn", "worker_job_message",
    "manager_progress_status", "rubric_review", "rubric_unchanged", "rubric_missing", "rubric_note",
    "discussion_manager", "manager_discussion_apply", "manager_discussion_end", "manager_discussion_at_gate",
    "discussion_held_none", "discussion_held", "manager_wait_revise", "review_file_request", "review_comments",
    "workers_proposal", "workers_declined", "critic_par_assign", "critic_par_review", "critic_par_next",
    "critic_par_last", "critic_par_done", "par_checklist_nudge", "checklist_reopened",
)

DEFAULT_TEMPLATES: Dict[str, str] = {
    # the auditor: the digest of the last stretch of rounds; its system prompt says what to do with it
    "audit_request": "Audit of rounds {{lo}}–{{hi}} (run {{run}}). At most {{points}} points.\n\n{{digest}}",
}
