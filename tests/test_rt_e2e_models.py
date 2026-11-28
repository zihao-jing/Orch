from rt_e2e_helpers import BACKUP, C, CLAUDE, CRITIC, QUIET, W1, W2, Reply, RuntimeCase, tell


class TestModels(RuntimeCase):
    def test_switch_back_to_configured_opencode_model_after_session_drift(self):
        qwen = C.Choice(backend="opencode", provider="dashscope", model="qwen4-max")
        self.add_agent(W1, choice=qwen)
        cli = self.cli(W1, "opencode")
        cli.script(Reply(model="provider/doubao"))
        self.start()
        self.chat(W1, "Establish the session.")
        self.advance(2)
        self.assertEqual(self.state.agents[W1].actual_model, "provider/doubao")
        sid, starts = cli.session_id, len(cli.starts)
        self.assertTrue(self.command(C.ChooseModel, W1)["ok"])
        self.wait_for(lambda: len(cli.starts) > starts and self.state.agents[W1].switch is None,
                      "configured model reapplied", real_io=True)
        self.assertEqual((cli.starts[-1][0].choice, cli.session_id), (qwen, sid))
        self.assertNotEqual(self.state.agents[W1].actual_model, "provider/doubao")
        cli.script(Reply(model="dashscope/qwen4-max"))
        self.chat(W1, "Continue.")
        self.advance(2)
        self.assertEqual(self.state.agents[W1].actual_model, "dashscope/qwen4-max")
        self.assertFalse(self.command(C.ChooseModel, W1)["ok"])

    def test_opencode_switch_uses_requested_route_on_existing_conversation(self):
        old = C.Choice(backend="opencode", provider="p", model="old")
        target = C.Choice(backend="opencode", provider="q", model="new", name="second")
        self.add_agent(W1, choice=old, backups=(target,))
        self.start()
        self.chat(W1, "Establish session.")
        self.advance(2)
        sid = self.state.agents[W1].session.session_id
        self.command(C.ChooseModel, W1, backup=target.name)
        self.wait_for(lambda: self.state.agents[W1].choice == target, "switch", real_io=True)
        spec, resumed = self.cli(W1, "opencode").starts[-1]
        self.assertEqual(spec.choice, target)
        self.assertEqual(resumed.session_id, sid)
        self.assertEqual(self.state.agents[W1].session.session_id, sid)

    def test_S10_REC_1_REC_5_SES_4_SES_5_retry_backup_handover_and_return(self):
        self.add_agent(W1, backups=(BACKUP,))
        self.settings.retry_waits = [2]
        self.cli(W1).script("Main ready.", *[Reply(error=C.ErrorClass.TRANSIENT) for _ in range(self.settings.retries)], "Main resumed.")
        self.cli(W1, "codex").script("Backup ready.")
        self.cli(CRITIC).script(tell(W1, "Run the experiment.") + QUIET,
                                tell(W1, "Recover with the clean dataset.") + QUIET)  # a chat with a worker is not shown to the critic
        self.start()
        self.chat(W1, "Establish main session.")
        self.advance(2)
        old = self.state.agents[W1].session.session_id
        self.assign()
        self.wait_for(lambda: self.state.agents[W1].choice == BACKUP, "backup choice", limit=100, real_io=True)
        self.assert_received_once(CRITIC, key=C.TextKey.HANDOVER_REQUEST_CRITIC)
        self.assert_received_once(W1, body="Recover with the clean dataset.", backend="codex")
        self.assertNotEqual(self.state.agents[W1].session.session_id, old)
        errors = [ev for ev in self.cli(W1).log if isinstance(ev, C.TurnEnded)
                  and ev.error == C.ErrorClass.TRANSIENT]
        self.assertEqual(len(errors), self.settings.retries)
        self.command(C.ChooseModel, W1)
        self.wait_for(lambda: self.state.agents[W1].choice == CLAUDE, "return to saved main session",
                      limit=40, real_io=True)
        self.assert_received_once(W1, key=C.TextKey.SESSION_RETURN)
        self.assertEqual(self.state.agents[W1].session.session_id, old)

    def test_S11_REC_2_REC_6_billing_isolated_then_smoke_closes_gate(self):
        self.settings.smoke_every = 1800
        self.cli(W1).script(Reply(error=C.ErrorClass.BILLING), "Billing restored.")
        self.cli(W2).script("Independent result.")
        self.cli(CRITIC).script(tell(W1, "Use primary quota.") + tell(W2, "Produce an independent result.") + QUIET)
        self.start()
        self.assign()
        self.advance(2)
        self.advance(2)
        self.assert_received_once(W2, body="Produce an independent result.")
        self.assert_received_once(CRITIC, body="Independent result.", key=C.TextKey.WORKER_FINAL)
        gates = [g for g in self.state.open_gates() if g.agent == W1 and g.kind == C.GateKind.OPERATIONAL]
        self.assertEqual(len(gates), 1)
        self.assertEqual(self.state.agents[W1].status, C.AgentStatus.FAILED)
        self.assertFalse(self.state.agents[W2].failed_why)
        self.assertFalse(self.incoming(W1, key=C.TextKey.RETRY_CONTINUE))
        self.advance(1801)
        self.wait_for(lambda: not self.state.gates[gates[0].id].is_open, "billing gate to close after recovery", real_io=True)
        self.chat(W1, "Verify the recovered choice.")
        self.advance(2)
        self.assert_received_once(W1, body="Verify the recovered choice.")
        self.assertEqual(self.state.agents[W1].status, C.AgentStatus.IDLE)

    def test_S11_REC_6_periodic_probes_continue_until_one_choice_recovers(self):
        self.add_agent(W1, backups=(BACKUP,))
        self.settings.smoke_every = 1800
        unavailable = Reply(error=C.ErrorClass.BILLING, after=0)
        self.smoke_replies["claude"].extend((unavailable, Reply("OK", after=0)))
        # The backup stays unavailable through the initial probe and both periodic probes.
        self.smoke_replies["codex"].extend((unavailable, unavailable, unavailable))
        self.cli(W1).script(Reply(error=C.ErrorClass.BILLING), "Primary quota restored.")
        self.cli(CRITIC).script(tell(W1, "Start billing test.") + QUIET)
        self.start()
        self.assign()
        self.wait_for(lambda: bool(self.state.open_gates()), "gate after both choices fail", real_io=True)
        gate, = [g for g in self.state.open_gates() if g.agent == W1]
        self.assertEqual(self.state.agents[W1].status, C.AgentStatus.FAILED)
        self.assertEqual(gate.kind, C.GateKind.OPERATIONAL)
        self.advance(1801)

        def probes():
            return [cli.starts[-1][0].choice.backend for cli in self.smoke if cli.received]

        self.wait_for(lambda: len(probes()) >= 3, "both choices probed on the periodic check", real_io=True)
        self.assertCountEqual(probes(), ["claude", "codex", "codex"])
        self.assertTrue(self.state.gates[gate.id].is_open)
        self.advance(1801)
        self.wait_for(lambda: not self.state.gates[gate.id].is_open, "one working choice closes the gate",
                      real_io=True)
        self.chat(W1, "Check primary after recovery.")
        self.advance(2)
        self.assert_received_once(W1, body="Check primary after recovery.")
        self.assertEqual(self.state.agents[W1].choice, CLAUDE)
