from rt_e2e_helpers import C, CRITIC, MANAGER, QUIET, W1, W2, Reply, RuntimeCase, tell


QUESTION = "<question>Which sample?\nA) Red\nB) Blue</question>"
SECOND = "<question>Which format?\nA) CSV\nB) JSON</question>"


class TestMessages(RuntimeCase):
    def test_S01_MSG_1_MSG_7_WRK_8_simultaneous_reports(self):
        self.cli(CRITIC).script(
            tell(W1, "Run alpha.") + tell(W2, "Run beta.") + QUIET,
            Reply(tell(W1, "Archive alpha.") + tell(W2, "Archive beta.") + QUIET, after=8),
        )
        self.cli(W1).script(Reply("Alpha result.", after=4), "Alpha archived.")
        self.cli(W2).script(Reply("Beta result.", after=4), "Beta archived.")
        self.start()
        self.assign()
        self.chat(CRITIC, "Keep writing while results return.")
        self.drain()
        self.advance(5)
        alpha = self.assert_received_once(CRITIC, key=C.TextKey.WORKER_FINAL, body="Alpha result.", answered=False)
        beta = self.assert_received_once(CRITIC, key=C.TextKey.WORKER_FINAL, body="Beta result.", answered=False)
        self.assertEqual(alpha.created, beta.created)
        self.assertEqual(self.state.agents[CRITIC].status, C.AgentStatus.WRITING)
        self.advance(10)
        self.assertEqual(self.state.messages[alpha.id].status, C.MessageStatus.ANSWERED)
        self.assertEqual(self.state.messages[beta.id].status, C.MessageStatus.ANSWERED)
        for worker, label in [(W1, "alpha"), (W2, "beta")]:
            self.assert_received_once(worker, body=f"Archive {label}.")
        self.advance(2)
        self.advance(2)
        for label in ("Alpha", "Beta"):
            self.assert_received_once(CRITIC, body=f"{label} archived.", key=C.TextKey.WORKER_FINAL)
        self.assertEqual(self.state.rounds, 4)

    def test_S05_LAB_4_MS_4_silent_reply_routes_work_and_offers_review(self):
        self.cli(CRITIC).script(
            "Private explanation." + tell(W1, "Verify output.") + QUIET,
            "Verification complete.\n<done/>" + QUIET,
        )
        self.cli(W1).script("Output verified.")
        self.start()
        self.assign()
        self.advance(1)
        self.write_checklist(True)
        self.advance(2)
        self.advance(2)
        self.assert_received_once(W1, body="Verify output.")
        self.assert_received_once(CRITIC, body="Output verified.", key=C.TextKey.WORKER_FINAL)
        self.assert_received_once(MANAGER, key=C.TextKey.CHECKLIST_COMPLETE)
        self.assertEqual(self.incoming(MANAGER, key=C.TextKey.CRITIC_REPORT), [])
        self.assertTrue(self.state.milestone.done_turn)  # the critic's <done/> started the review
        self.assertEqual(self.state.milestone.notified, self.state.milestone.done_turn)

    def test_MS_12_the_rubric_is_kept_in_a_file_the_critic_reads(self):
        self.cli(CRITIC).script("Ready.\n<done/>" + QUIET)
        self.start()
        self.write_checklist(True)
        self.assign()
        rubric = self.path / "run" / "rubric-1.md"
        self.assertEqual(rubric.read_text(encoding="utf-8"), "# Rubric: Milestone m1\n\n- Output works\n")

    def test_S06_GATE_1_GATE_2_GATE_4_GATE_7_answers_batched(self):
        self.cli(MANAGER).script(
            QUESTION + tell(CRITIC, "Wait for sample approval.") + "<pause>Confirm access.</pause>",
            SECOND,
            tell(CRITIC, "Use blue JSON."),
        )
        self.start()
        self.chat(MANAGER, "Choose sample.")
        self.advance(2)
        self.chat(MANAGER, "Choose format.")
        self.advance(2)
        gates = self.state.open_gates()
        self.assertCountEqual([g.kind for g in gates], [C.GateKind.QUESTION] * 2 + [C.GateKind.PAUSE])
        self.assertTrue(self.state.paused)
        self.assertEqual(self.incoming(CRITIC, body="Wait for sample approval."), [])
        for gate in gates:
            if gate.kind == C.GateKind.QUESTION:
                self.command(C.GateAnswer, gate_id=gate.id, option=1)
                self.advance(2)
                self.assertEqual(self.incoming(MANAGER, key=C.TextKey.GATE_ANSWERS), [])
        pause = next(g for g in gates if g.kind == C.GateKind.PAUSE)
        self.command(C.GateAnswer, gate_id=pause.id, text="Access confirmed.")
        self.advance(2)
        self.advance(2)
        combined = self.assert_received_once(MANAGER, key=C.TextKey.GATE_ANSWERS)
        self.assertCountEqual(combined.gates, [g.id for g in gates])
        self.assert_received_once(CRITIC, body="Use blue JSON.")
        self.assertFalse(self.state.paused)
        self.assertFalse(self.state.open_gates())

    def test_S07_LAB_1_LAB_5_removed_and_malformed_labels(self):
        broken = '<message to="worker-1">Incomplete instruction'
        self.cli(CRITIC).script("<verdict>PASS</verdict>", QUIET, broken, broken)
        self.start()
        self.assign("Try an old label.")
        self.advance(2)
        self.assign("Try the new label.")
        self.advance(2)
        self.advance(2)
        returned = self.incoming(CRITIC, key=C.TextKey.LABEL_RETURNED)
        self.assertEqual(len(returned), 2, returned)
        self.assertEqual(len({m.id for m in returned}), 2)
        self.assertTrue(all(m.status == C.MessageStatus.ANSWERED for m in returned))
        self.assert_received_once(MANAGER, key=C.TextKey.LABEL_BROKEN_PASSED)

    def test_S07_MSG_8_closed_worker_address_is_rejected(self):
        self.cli(CRITIC).script(tell(W2, "Closed instruction.") + QUIET)
        self.start()
        self.command(C.WorkerClose, W2)
        self.assign("Ask the closed worker.")
        self.advance(2)
        self.assert_received_once(CRITIC, key=C.TextKey.UNKNOWN_WORKER)
        self.assertEqual(self.cli(W2).received, [])
        self.assertEqual(self.state.agents[W2].status, C.AgentStatus.CLOSED)
