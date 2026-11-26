from rt_e2e_helpers import C, CRITIC, MANAGER, QUIET, W1, Reply, RuntimeCase, milestone, tell
from test_rt_e2e_messages import QUESTION


class TestControls(RuntimeCase):
    def test_S04_SIL_1_SIL_2_three_checks_pause_and_reset(self):
        self.settings.silence_after = 5
        self.start()
        self.chat(MANAGER, "Begin.")
        self.advance(2)
        for _ in range(3):
            self.advance(6)
            self.advance(2)
        self.advance(6)
        checks = self.incoming(MANAGER, key=C.TextKey.SILENCE_CHECK)
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(m.status == C.MessageStatus.ANSWERED for m in checks))
        self.assertTrue(self.state.paused)
        gates = self.state.open_gates()
        self.assertEqual([g.kind for g in gates], [C.GateKind.PAUSE])
        self.command(C.GateAnswer, gate_id=gates[0].id)
        self.cli(W1).script(Reply("Still working.", after=10))
        self.chat(W1, "Resume work.")
        self.advance(2)
        self.assert_received_once(W1, body="Resume work.")
        self.assertEqual(self.state.silence.tries, 0)
        self.advance(10)
        self.advance(6)
        self.advance(2)
        self.assertEqual(len(self.incoming(MANAGER, key=C.TextKey.SILENCE_CHECK)), 4)
        self.assertEqual(self.state.silence.tries, 1)
        self.assertFalse(self.state.paused)

    def test_S13_DISC_1_DISC_2_DISC_3_DISC_4_DISC_5_apply_and_discard(self):
        self.cli(CRITIC).script(
            tell(W1, "Produce the sample.") + QUIET,
            Reply("Private progress." + tell(W1, "Draft instruction."), after=6),
            tell(W1, "Consolidated instruction.") + QUIET,
            "Do not forward." + tell(W1, "Discard instruction."),
        )
        self.cli(W1).script(Reply("Sample ready.", after=4), "Consolidated work done.")
        self.start()
        self.assign()
        self.chat(CRITIC, "Write the progress note.")
        self.drain()
        self.assertEqual(self.state.agents[CRITIC].status, C.AgentStatus.WRITING)
        self.command(C.DiscussionStart, CRITIC)
        self.advance(4)
        self.assert_received_once(CRITIC, body="Sample ready.", key=C.TextKey.WORKER_FINAL, answered=False)
        self.command(C.DiscussionApply, CRITIC, text="Apply the discussion.")
        self.assertFalse(self.incoming(CRITIC, key=C.TextKey.DISCUSSION_APPLY))
        self.advance(4)
        self.advance(2)
        self.advance(2)
        self.assert_received_once(CRITIC, key=C.TextKey.DISCUSSION_APPLY)
        self.assert_received_once(W1, body="Consolidated instruction.")
        self.assertFalse(self.incoming(W1, body="Draft instruction."))
        self.assertFalse(self.incoming(MANAGER, body="Private progress."))
        self.command(C.DiscussionStart, CRITIC)
        self.chat(CRITIC, "Try a draft and discard it.")
        self.advance(2)
        self.command(C.DiscussionEnd, CRITIC)
        self.advance(2)
        self.assertNotIn(CRITIC, self.state.discussions)
        self.assertFalse(self.incoming(W1, body="Discard instruction."))
        self.assertFalse(self.incoming(MANAGER, body="Do not forward."))

    def test_S14_TERM_4_TERM_6_terminal_holds_input_until_take_back(self):
        self.start()
        self.chat(W1, "Establish session.")
        self.advance(2)
        session = self.state.agents[W1].session.session_id
        self.command(C.TerminalOpen, W1)
        self.assertIn(W1, self.state.terminals)
        self.cli(CRITIC).script(tell(W1, "First held instruction.") + QUIET,
                                tell(W1, "Second held instruction.") + QUIET)
        self.assign("First assignment.")
        self.assign("Second assignment.")
        self.assertFalse(self.incoming(W1, body="held instruction."))
        self.assertEqual(len(self.state.waiting_for(W1)), 2)
        self.assertIn(C.HoldReason.TERMINAL, self.state.agents[W1].holds)
        self.command(C.TerminalTakeBack, W1)
        self.advance(2)
        self.advance(2)
        self.assert_received_once(W1, body="First held instruction.")
        self.assert_received_once(W1, body="Second held instruction.")
        self.assertNotIn(W1, self.state.terminals)
        self.assertNotIn(C.HoldReason.TERMINAL, self.state.agents[W1].holds)
        self.assertEqual(self.state.agents[W1].session.session_id, session)

    def test_S15_MS_10_MS_11_MS_12_MS_13_change_during_review(self):
        self.cli(MANAGER).script(
            "<score>5</score><decision>ACCEPT</decision><summary>Verified.</summary>",
            "1. [m1] Revised output\n<rubric>Updated output works</rubric>",
        )
        self.start()
        self.write_checklist(True)
        self.wait_for(lambda: bool(self.state.open_gates()), "milestone approval")
        review = next(g for g in self.state.open_gates() if g.kind == C.GateKind.MILESTONE)
        initial_review = self.assert_received_once(MANAGER, key=C.TextKey.CHECKLIST_COMPLETE)
        old = self.state.milestone
        self.command(C.ChangeMilestones, text="Revise the output requirement.")
        self.wait_for(lambda: any(g.kind == C.GateKind.MILESTONE_CHANGE for g in self.state.open_gates()),
                      "milestone change approval")
        change = next(g for g in self.state.open_gates() if g.kind == C.GateKind.MILESTONE_CHANGE)
        self.assertEqual(self.state.milestone.title, old.title)
        self.command(C.GateAnswer, gate_id=change.id, approve=True)
        self.advance(2)
        self.assertEqual(self.cli(MANAGER).received.count(initial_review.id), 1)
        self.assert_received_once(MANAGER, key=C.TextKey.MILESTONE_CHANGE_REQUEST)
        self.assertTrue(self.state.gates[review.id].withdrawn)
        self.assertEqual(self.state.milestone.id, old.id)
        self.assertEqual(self.state.milestone.title, "Revised output")
        self.assertEqual(self.state.milestone.checklist_path, old.checklist_path)

    def test_S15_MS_14_finished_run_accepts_an_added_milestone(self):
        self.cli(MANAGER).script("1. [m1] Finished output\n2. Publish output\n   Publish the sample.")
        self.start(milestone(status="done", title="Finished output", ended=1000), finished=True)
        self.command(C.ChangeMilestones, text="Add publication.")
        self.wait_for(lambda: bool(self.state.open_gates()), "approval for added milestone")
        gate, = self.state.open_gates()
        self.assertEqual(gate.kind, C.GateKind.MILESTONE_CHANGE)
        self.command(C.GateAnswer, gate_id=gate.id, approve=True)
        self.advance(2)
        self.assert_received_once(MANAGER, key=C.TextKey.MILESTONE_CHANGE_REQUEST)
        self.assertFalse(self.state.finished)
        self.assertEqual(self.state.milestones[0].status, C.MilestoneStatus.DONE)
        self.assertEqual(self.state.milestone.id, "m2")

    def test_S15_MS_14_UI_6_reopen_saved_completed_run(self):
        self.start(milestone(status="done", ended=1000, checklist_path=str(self.checklist)), finished=True)
        self.chat(MANAGER, "Leave the session open.")
        self.advance(2)
        session = self.state.manager.session.session_id
        self.restart()
        self.command(C.ReopenMilestone, milestone_id="m1")
        self.advance(2)
        self.assert_received_once(MANAGER, body="Leave the session open.")
        self.assertEqual(self.state.manager.session.session_id, session)
        self.assertFalse(self.state.finished)
        self.assertEqual(self.state.milestone.id, "m1")

    def test_S18_GATE_1_GATE_2_ASSUMPTION_2_night_records_choice_without_holding(self):
        self.cli(MANAGER).script(QUESTION + "\nI recommend B) Blue.\n" + tell(CRITIC, "Use the blue sample."))
        self.start()
        self.command(C.SetNight, on=True)
        self.chat(MANAGER, "Choose a sample.")
        self.advance(2)
        self.advance(2)
        self.assert_received_once(CRITIC, body="Use the blue sample.")
        self.assertTrue(self.state.night)
        self.assertFalse(self.state.open_gates())
        self.assertFalse(self.state.paused)
        register = self.path / "decisions.md"
        self.assertTrue(register.is_file(), "night decision was not saved")
        self.assertIn("Blue", register.read_text())
