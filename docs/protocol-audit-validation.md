# Protocol and audit implementation validation

Date: 2026-11-21 UTC. Branch: `fix/uniform-protocol-audit`; base: `ff284da`.
Source: `docs/protocol-audit-design.md` (copy of the maintained project decision file).

## Final verification

After the user authorized continuing verification, the corrected source passed the full repository suite:

```text
/root/miniconda3/bin/python tests/run.py -j 8 --slow 5
Ran 416 tests in 33.8s on 8 workers (245.3s of test time):
416 ok, 0 skipped, 0 failed, 0 errors
```

This includes all 11 new protocol/timing/audit regression tests and all 28 tests that failed in the initial run.
The final suite output is retained locally at `/tmp/orch-final-suite.log`; `tests/.last_failed` is empty.

## Verification history

The initial suite used `/root/miniconda3/bin/python /tmp/orch_final_validation.py`, a wrapper around the
repository's test discovery and `run_one`, with eight processes and a 90-second per-test deadline.
It recorded **388 passing and 28 failing/error tests out of 416**, including all 11 new regressions passing.
After corrections, a targeted rerun of the 28 failures passed 25; the remaining three were stale prompt-text
and upward-chat-routing assertions. Those assertions were corrected before the final successful full suite.

Initial results: `/tmp/orch-final-validation-results.json` and `/tmp/orch-final-validation.log`.
Targeted rerun: `/tmp/orch-recheck-results.json` and `/tmp/orch-recheck.log`.
The wrappers and logs are local validation artifacts, not production code changes.

## Corrections verified by the final suite

- Migrate old implicit-instruction fixtures and assertions to explicit worker messages, timestamped upward
  records and the new protocol reminder. PASS with unfinished work needs an explicit follow-up assignment.
- Ask the critic for a single-worker instruction after a parallel proposal is declined, rather than relying
  on untagged proposal prose to dispatch work. The request is bounded to one attempt.
- Pass the mock backend's turn environment to spawned background jobs so Linux job detection can see `ORCH_JOB`.
  All four previously failing job-related tests pass after this correction.
- The stress-test ledger showed a worker side message was superseded by a newer explicit review instruction.
  Preserve replacement semantics and mark the fixture's follow-up instructions as additive.
- Initialize the six updater-test repositories without `git init -b`, unsupported by this environment's Git 2.25.1,
  and explicitly set the bare repository HEAD to `refs/heads/main`.
- Static review found two additional multi-worker risks: queued messages must not wake unrelated workers,
  and amending one worker's assignment must preserve other workers' pending assignments. Correct both paths.
- Preserve the original progress timestamp when extracting an escalated question from a queued note.
  Ignore non-object OpenCode timestamp payloads instead of raising in the stream callback.

Final syntax compilation (`python -m compileall -q orch tests`) and `git diff --check` also pass.
Changes remain in the isolated worktree; the installed checkout and running Orch process were not changed.
Live-provider validation and model-based replay of historical auditor inputs have not been performed;
the tests verify behavior, digest construction and rule/version selection, not auditor model quality.

## Initially failing test IDs (all pass in the final suite)

- `test_b10_audit.TestAuditorPrompt.test_the_prompt_states_the_design` — fail
- `test_b11_parallel.TestParallelGateAndStress.test_stress_reviews_fail_then_pass_messages_by_id_and_safe_state` — fail
- `test_e2e_fixes2.TestNotesUp.test_a_plain_note_up_waits_for_the_managers_next_turn` — fail
- `test_e2e_fixes5.TestNoWorkerIdsAcrossLanes.test_the_protocol_keeps_each_section_to_its_person` — fail
- `test_b11_parallel.TestParallelGateAndStress.test_declined_at_the_gate_one_worker_goes_on` — fail
- `test_e2e_fixes4.TestParallelReview.test_a_message_turn_during_a_job_does_not_end_the_wait` — fail
- `test_orch.TestManaged.test_checkins_and_notes_up` — fail
- `test_orch.TestManaged.test_manager_side_question_in_a_review_has_explicit_reply_routing` — fail
- `test_orch.TestManaged.test_orch_finds_a_job_the_worker_left_running` — fail
- `test_orch.TestManaged.test_the_digest_traces_where_each_part_of_a_message_came_from` — fail
- `test_orch.TestManaged.test_review_now_skips_the_job_wait` — error
- `test_orch.TestProjectFolder.test_init_layout_settings_split_and_default_prompts` — fail
- `test_progress.TestProgressRun.test_checks_watch_a_job_without_polling_rounds` — fail
- `test_role_views.TestRoleViews.test_board_chat_reaches_the_critic_and_worker_as_from_their_usual_user` — fail
- `test_role_views.TestRoleViews.test_default_prompts_keep_each_role_view` — fail
- `test_routing.TestRouting.test_worker_chat_tags_go_nowhere` — fail
- `test_update.TestApply.test_a_dependency_change_is_left_to_me` — error
- `test_update.TestApply.test_a_newer_main_is_taken_in` — error
- `test_update.TestApply.test_new_code_that_fails_validate_is_rolled_back` — error
- `test_update.TestApply.test_not_on_main_dirty_or_ahead_is_left_alone` — error
- `test_update.TestApply.test_restart_keeps_the_arguments_and_resumes` — error
- `test_update.TestApply.test_up_to_date_does_nothing` — error
- `test_e2e_fixes5.TestChecksDuringAReview.test_another_workers_monitor_checks_while_the_critic_reviews` — error
- `test_e2e_fixes5.TestDroppedMessages.test_explicit_final_review_message_is_delivered_even_after_pass` — error
- `test_e2e_fixes5.TestLaneLabels.test_a_copy_is_called_by_its_id` — error
- `test_e2e_fixes5.TestNoWorkerIdsAcrossLanes.test_a_step_naming_another_worker_reaches_its_worker_without_the_id` — error
- `test_orch.TestManaged.test_untagged_checklist_is_gated_hidden_from_the_worker_and_shown_to_the_manager` — error
- `test_sessions.TestSessions.test_worker_keeps_its_session_across_steps_and_not_across_milestones` — error
