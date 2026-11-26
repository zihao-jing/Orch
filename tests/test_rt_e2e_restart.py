import time
from unittest.mock import patch

from rt_e2e_helpers import BACKUP, C, CLAUDE, CRITIC, QUIET, W1, Reply, RuntimeCase, tell


class PowerLoss(BaseException):
    pass


class TestRestart(RuntimeCase):
    def run_send_crash(self, already_received):
        worker = self.cli(W1).script("Finished the write.")
        self.start()
        # Open the process first so this failure injection targets only the test message.
        self.chat(W1, "Open session.")
        self.advance(2)
        adapter = worker.owner
        original_send = adapter.send

        def crash(message_id, text):
            if already_received:
                original_send(message_id, text)
                worker.flush()
            raise PowerLoss

        with patch.object(adapter, "send", side_effect=crash):
            with self.assertRaises(PowerLoss):
                self.chat(W1, "Persist this write.")
        pending, = self.messages(W1, body="Persist this write.")
        self.assertEqual(worker.received.count(pending.id), int(already_received))
        self.restart()
        self.advance(2)
        self.advance(2)
        recovered = self.assert_received_once(W1, body="Persist this write.")
        self.assertEqual(recovered.id, pending.id)

    def test_S08_MSG_6_REC_8_crash_before_cli_input(self):
        self.run_send_crash(False)

    def test_S08_MSG_6_REC_8_crash_after_cli_input(self):
        self.run_send_crash(True)

    def test_S09_REC_7_REC_9_reconnect_during_work(self):
        self.cli(CRITIC).script(tell(W1, "Prepare results.") + QUIET)
        worker = self.cli(W1).script(Reply("First pass running.", jobs=["pass"]), "Follow-up.")
        self.start()
        self.assign()
        self.advance(2)
        session = self.state.agents[W1].session.session_id
        self.engine.close()
        worker.job_end("pass", wake="First pass finished.")
        self.clock.advance(3)
        self.start(resume=True)
        self.advance(2)
        self.assert_received_once(W1, body="Prepare results.")
        self.assert_received_once(CRITIC, body="First pass finished.", key=C.TextKey.WORKER_FINAL)
        self.assertEqual(self.state.agents[W1].session.session_id, session)
        self.assertEqual(len(worker.starts), 1)
        self.assertTrue(self.state.jobs[f"{W1}/pass"].woke)

    def test_S09_REC_7_REC_8_reconnect_cross_backend_backup(self):
        self.add_agent(W1, choice=BACKUP, backups=(CLAUDE,))
        self.agents[W1].choices = [CLAUDE, BACKUP]
        self.cli(CRITIC).script(tell(W1, "Run on backup.") + QUIET)
        worker = self.cli(W1, "codex").script(Reply("Backup result.", after=8))
        self.start()
        self.assign()
        self.advance()
        session = self.state.agents[W1].session.session_id
        self.assertEqual(self.state.agents[W1].status, C.AgentStatus.WRITING)
        self.engine.close()
        self.clock.advance(10)
        self.start(resume=True)
        self.advance(2)
        self.assert_received_once(W1, body="Run on backup.", backend="codex")
        self.assert_received_once(CRITIC, body="Backup result.", key=C.TextKey.WORKER_FINAL)
        self.assertEqual(self.state.agents[W1].session.session_id, session)
        self.assertEqual(self.state.agents[W1].choice.backend, "codex")
        self.assertEqual(len(worker.starts), 1)
        self.assertFalse(self.cli(W1).starts)

    def run_switch_crash(self, step):
        self.add_agent(W1, backups=(BACKUP,))
        self.cli(W1).script(Reply("Job running.", jobs=["switch-job"]), Reply("", ends=False),
                            "Resume the job from checkpoints.")
        self.cli(W1, "codex").script(Reply("Restarted on backup.", after=4))
        self.cli(CRITIC).script(tell(W1, "Compute the matrix.") + QUIET, QUIET,
                                tell(W1, "Resume from the persisted checkpoint.") + QUIET)
        self.start()
        self.assign()
        self.advance()
        self.assert_received_once(W1, body="Compute the matrix.")
        self.chat(W1, "User request: preserve covariance.")
        self.advance(2)
        self.assert_received_once(W1, body="User request: preserve covariance.")
        old_session = self.state.agents[W1].session.session_id
        self.command(C.ChooseModel, W1, backup=BACKUP.name)
        self.assertEqual(self.state.agents[W1].switch.step, C.SwitchStep.PENDING)
        self.assertEqual(self.state.jobs[f"{W1}/switch-job"].state, C.JobState.RUNNING)
        if step == C.SwitchStep.PENDING:
            self.restart()
            self.command(C.ForceSwitch, W1, confirmed=True)
        else:
            original = self.engine._perform

            def cut_power(*args, **kwargs):
                switch = self.state.agents[W1].switch
                if (switch and switch.step == step) or (step == C.SwitchStep.DONE and switch is None
                                                       and self.state.agents[W1].choice == BACKUP):
                    raise PowerLoss
                return original(*args, **kwargs)

            with patch.object(self.engine, "_perform", side_effect=cut_power):
                with self.assertRaises(PowerLoss, msg=f"switch never reached {step.value}"):
                    self.command(C.ForceSwitch, W1, confirmed=True)
                    for _ in range(100):
                        time.sleep(0.025)
                        self.advance()
            self.restart()
        self.wait_for(lambda: self.state.agents[W1].switch is None
                      and self.state.agents[W1].choice == BACKUP, "successful restored forced switch", limit=130, real_io=True)
        self.advance(2)
        lookup = self.cli(W1, "codex")
        self.assertEqual(len(lookup.starts), 1)
        self.assertEqual(len(lookup.sent), len({mid for mid, _ in lookup.sent}))
        self.wait_for(lambda: any("preserve covariance" in text for _, text in lookup.sent),
                      "unfinished user request on the replacement CLI")
        self.assertEqual(set(lookup.received), {mid for mid, _ in lookup.sent})
        self.assertEqual(self.state.agents[W1].sessions[CLAUDE.session_key].session_id, old_session)
        self.assertNotEqual(self.state.jobs[f"{W1}/switch-job"].state, C.JobState.RUNNING)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_pending(self):
        self.run_switch_crash(C.SwitchStep.PENDING)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_testing(self):
        self.run_switch_crash(C.SwitchStep.TESTING)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_handover(self):
        self.run_switch_crash(C.SwitchStep.HANDOVER)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_opening(self):
        self.run_switch_crash(C.SwitchStep.OPENING)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_confirming(self):
        self.run_switch_crash(C.SwitchStep.CONFIRMING)

    def test_S12_MOD_6_MOD_7_REC_7_restart_at_done(self):
        self.run_switch_crash(C.SwitchStep.DONE)

    def test_S12_MOD_6_MOD_7_REC_7_restart_after_failed_smoke(self):
        self.add_agent(W1, backups=(BACKUP,))
        self.smoke_replies["codex"].append(Reply(error=C.ErrorClass.BILLING, after=0))
        self.cli(W1).script(Reply("Job still running.", jobs=["train"]))
        self.cli(CRITIC).script(tell(W1, "Keep the job running.") + QUIET)
        self.start()
        self.assign()
        self.advance(2)
        worker = self.cli(W1)
        session = self.state.agents[W1].session.session_id
        original = self.engine._perform

        def cut_power(*args, **kwargs):
            agent = self.state.agents[W1]
            if self.smoke and agent.switch is None:
                raise PowerLoss
            return original(*args, **kwargs)

        with patch.object(self.engine, "_perform", side_effect=cut_power):
            with self.assertRaises(PowerLoss, msg="the smoke failure did not end the pending switch"):
                self.command(C.ChooseModel, W1, backup=BACKUP.name, force=True, confirmed=True)
                for _ in range(100):
                    time.sleep(0.025)
                    self.advance()
        self.restart()
        self.advance(2)
        self.assert_received_once(W1, body="Keep the job running.")
        self.assertFalse(self.cli(W1, "codex").received)
        self.assertEqual(len(worker.starts), 1)
        self.assertEqual(self.state.agents[W1].session.session_id, session)
        self.assertEqual(self.state.jobs[f"{W1}/train"].state, C.JobState.RUNNING)
