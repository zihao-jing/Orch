"""Engine integration tests using scripted CLIs, a fake clock and real store files."""
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rt_helpers import C, CLAUDE, T0, agent, snapshot
from orch.runtime.adapters.mock import ALL_CAPS, FakeClock, MockAdapter, MockCLI, Reply
from orch.runtime.engine import Engine


class Module:
    def __init__(self, name, decide):
        self.name = name
        self.decide = decide


class Crash(BaseException):
    pass


class EngineTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.clock = FakeClock(T0)
        self.clis = {}
        self.factories = {}

    def cli(self, name="worker-1", *, caps=ALL_CAPS, backend="claude", adapter=MockAdapter):
        cli = MockCLI(self.clock, name, caps)
        self.clis[name] = cli
        self.factories[backend] = lambda: adapter(self.clock, cli)
        return cli, agent(name, proc=None, choice=replace(CLAUDE, backend=backend))

    def engine(self, agents=None, modules=None, **settings):
        engine = Engine(self.path, self.clock, self.factories, settings, modules)
        snap = None if agents is None else snapshot(workers=0, agents={a.id: a for a in agents})
        if snap is not None:
            snap.agents = {a.id: a for a in agents}
        engine.start(snap)
        self.addCleanup(engine.close)
        return engine

    def chat(self, engine, id, agent="worker-1"):
        return engine.submit(C.Chat(agent, self.clock.now(), id, text=id))

    def test_MOD_1_a_started_orch_lists_the_models_of_its_settings(self):
        engine = self.engine([agent("worker-1")], list_models=False,
                             models={"gpt": {"backend": "codex", "backend_options": {"model": "gpt-5.5", "effort": "high"}}})
        self.assertEqual([(m.id, m.backend, m.model) for m in engine.store.snapshot().catalog], [("gpt", "codex", "gpt-5.5")])

    def test_MSG_1_writing_capabilities(self):
        for while_writing in (True, False):
            with self.subTest(while_writing=while_writing), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory)
                cli, worker = self.cli(caps=replace(ALL_CAPS, sends_while_writing=while_writing))
                cli.script(Reply("Working", after=3))
                engine = self.engine([worker])
                self.assertTrue(self.chat(engine, "first")["ok"])
                engine.step()
                self.assertEqual(engine.store.snapshot().agents[worker.id].status, C.AgentStatus.WRITING)
                self.assertTrue(self.chat(engine, "second")["ok"])
                self.assertEqual(len(cli.sent), 2 if while_writing else 1)
                self.clock.advance(3)
                self.assertTrue(engine.step())
                self.assertEqual(len(cli.sent), 2)
                engine.close()

    def test_MSG_2_hold_wins_over_send_then_later_release_sends_once(self):
        cli, worker = self.cli()
        send = C.Send(C.Message("m", recipient=worker.id, body="held"))

        def decide(snap, ev, prior):
            if isinstance(ev, C.Tick):
                if "m" not in snap.messages:
                    return [send, C.Hold("m", C.HoldReason.DRAFT)]
                return [C.Release("m", C.HoldReason.DRAFT)]
            return []

        engine = self.engine([worker], [Module("discussion", decide)], tick_s=1)
        self.clock.advance(1)
        engine.step()
        self.assertEqual(cli.sent, [])
        self.assertEqual(engine.message("m").holds, [C.HoldReason.DRAFT])
        self.clock.advance(1)
        engine.step()
        self.assertEqual([m for m, _ in cli.sent], ["m"])
        self.clock.advance(1)
        engine.step()
        self.assertEqual(len(cli.sent), 1)

    def test_MSG_6_REC_8_in_doubt_true_false_and_unknown(self):
        for answer in (True, False, None):
            with self.subTest(arrived=answer), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory)
                probes = []

                class Adapter(MockAdapter):
                    def arrived(self, message_id):
                        probes.append(message_id)
                        return answer

                cli, worker = self.cli(adapter=Adapter)
                cli.script(Reply(receipt=False, starts=False), Reply(starts=False))
                engine = self.engine([worker])
                id = C.new_id("routing", C.Chat(worker.id, self.clock.now(), "crash", text="crash"))
                with patch.object(engine.store, "intent_done", side_effect=Crash):
                    # Crash on the transmission completion, after begin_send left its durable marker.
                    real_apply = engine.store.apply

                    def no_command_reply(ev, actions):
                        return real_apply(ev, [a for a in actions if not isinstance(a, C.CommandReply)])

                    with patch.object(engine.store, "apply", side_effect=no_command_reply):
                        with self.assertRaises(Crash):
                            self.chat(engine, "crash")
                self.assertEqual(engine.message(id).attempts, 1)
                if answer is True:
                    cli.received.append(id)
                engine.close()
                recovered = self.engine()
                self.assertEqual(probes, [id])
                self.assertEqual(recovered.message(id).attempts, 2 if answer is False else 1)
                self.assertEqual([m for m, _ in cli.sent], [id] * (2 if answer is False else 1))
                recovered.step()
                self.assertLessEqual(cli.received.count(id), 1)
                recovered.close()

    def test_crash_before_or_after_apply_replays_identical_ids(self):
        for before in (True, False):
            with self.subTest(before_apply=before), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory)
                cli, worker = self.cli()
                ids = []

                def decide(snap, ev, prior):
                    if not isinstance(ev, C.Chat):
                        return []
                    id = C.new_id("routing", ev)
                    ids.append(id)
                    return [C.Send(C.Message(id, recipient=worker.id, body=ev.text)), C.CommandReply(ev.id)]

                modules = [Module("routing", decide)]
                engine = self.engine([worker], modules)
                with patch.object(engine.store if before else engine, "apply" if before else "_perform",
                                  side_effect=Crash):
                    with self.assertRaises(Crash):
                        self.chat(engine, "save")
                self.assertEqual(cli.sent, [])
                engine.close()
                recovered = self.engine(modules=modules)
                self.assertEqual(ids, [ids[0]] * (2 if before else 1))
                self.assertEqual([id for id, _ in cli.sent], [ids[0]])
                self.assertTrue(self.chat(recovered, "save")["ok"])
                self.assertEqual(len(cli.sent), 1)
                recovered.close()

    def test_REC_9_close_detaches_and_reconnects_without_resending(self):
        cli, worker = self.cli()
        seen = []

        def decide(snap, ev, prior):
            seen.append(ev)
            if isinstance(ev, C.Chat):
                msg = C.Message(C.new_id("routing", ev), recipient=worker.id, body=ev.text)
                return [C.Send(msg), C.CommandReply(ev.id)]
            return []

        modules = [Module("routing", decide)]
        engine = self.engine([worker], modules)
        self.chat(engine, "work")
        engine.step()
        engine.close()
        self.assertTrue(cli.alive)
        self.assertIsNone(cli.owner)
        self.clock.advance(2)
        reopened = self.engine(modules=modules)
        self.assertTrue(cli.alive)
        self.assertEqual(len(cli.starts), 1)
        self.assertEqual(len(cli.sent), 1)
        self.assertEqual(sum(isinstance(ev, C.Reconnected) for ev in seen), 1)
        self.assertIsInstance(seen[-1], C.OrchStarted)
        reopened.stop_agents()
        self.assertFalse(cli.alive)

    def test_startup_actions_can_hold_an_unfinished_transmission(self):
        cli, worker = self.cli()
        msg = C.Message("startup", recipient=worker.id, body="Wait")

        def decide(snap, ev, prior):
            if isinstance(ev, C.Chat):
                return [C.Send(msg)]
            if isinstance(ev, C.OrchStarted) and msg.id in snap.messages:
                return [C.HoldAgent(worker.id, C.HoldReason.STOPPED)]
            return []

        modules = [Module("recovery", decide)]
        engine = self.engine([worker], modules)
        with patch.object(engine, "_perform", side_effect=Crash):
            with self.assertRaises(Crash):
                self.chat(engine, "startup")
        engine.close()
        reopened = self.engine(modules=modules)
        self.assertEqual(cli.sent, [])
        self.assertEqual(reopened.store.snapshot().agents[worker.id].holds, [C.HoldReason.STOPPED])

    def test_MSG_6_no_receipts_are_confirmed_by_the_engine(self):
        cli, worker = self.cli(caps=replace(ALL_CAPS, receipts=False, sends_while_writing=False))
        engine = self.engine([worker])
        cli.script(Reply(starts=False))
        self.chat(engine, "no-receipt")
        msg = engine.message(cli.sent[0][0])
        self.assertEqual(msg.status, C.MessageStatus.DELIVERED)
        self.assertEqual(msg.attempts, 1)

    def test_MSG_5_resend_uses_saved_text_and_keeps_the_check_time(self):
        cli, worker = self.cli()
        engine = self.engine([worker])
        cli.script(Reply(starts=False, receipt=False), Reply(starts=False, receipt=False))
        self.chat(engine, "retry")
        self.clock.advance(301)
        engine.step()
        self.assertEqual(len(cli.sent), 2)
        self.assertEqual(cli.sent[0], cli.sent[1])
        msg = engine.message(cli.sent[0][0])
        self.assertEqual(msg.attempts, 2)
        self.assertGreater(msg.checked_at, 0)

    def test_JOB_4_self_waking_and_codex_qwen_fallback(self):
        for backend, wakes, steer in (("claude", True, True), ("codex", False, True), ("qwen", False, False)):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory)
                worker, entry = self.cli(caps=replace(ALL_CAPS, wakes_itself=wakes, sends_while_writing=steer),
                                         backend=backend)
                critic, other = self.cli("critic", backend="mock")
                worker.script(Reply("Training in the background.", jobs=["train"]), "Training finished.")
                engine = self.engine([entry, other], tick_s=1)
                self.chat(engine, "Train.")
                engine.step()
                self.clock.advance(2)
                engine.step()
                worker.job_end("train", wake="Training finished." if wakes else None)
                self.clock.advance(1)
                engine.step()
                self.assertEqual(len(worker.sent), 1 if wakes else 2)
                if not wakes:
                    self.assertEqual(engine.message(worker.sent[-1][0]).key, C.TextKey.JOB_WAKEUP)
                engine.step()
                self.clock.advance(2)
                engine.step()
                self.assertTrue(engine.store.snapshot().jobs["worker-1/train"].woke)
                self.assertEqual(engine.store.snapshot().turns[entry.id].text, "Training finished.")
                self.assertTrue(critic.sent)
                engine.close()

    def test_SendFailed_is_saved_and_recovery_retries(self):
        cli, worker = self.cli()
        engine = self.engine([worker])
        # Open a CLI before refusing its next input.
        self.chat(engine, "hello")
        engine.step()
        self.clock.advance(2)
        engine.step()
        cli.refuse_sends()
        self.chat(engine, "refused")
        id = C.new_id("routing", C.Chat(worker.id, self.clock.now(), "refused"))
        msg = engine.message(id)
        self.assertEqual((msg.sent_at, msg.error), (0, "refused"))
        self.assertIsNotNone(engine.store.snapshot().agents[worker.id].retry)
        self.clock.advance(61)
        engine.step()
        self.assertEqual(engine.message(id).attempts, 2)
        self.assertIn(id, [mid for mid, _ in cli.sent])

    def test_commands_files_oldest_first_and_submit_returns_reply_once(self):
        cli, worker = self.cli()
        commands = self.path / "commands"
        commands.mkdir()
        # named <time ms>-<id>.json as orch.cli.write_command does; the name decides, not the file time
        newer = commands / "0000000020000-a.json"
        older = commands / "0000000010000-z.json"
        for path, id, stamp in ((newer, "newer", 10), (older, "older", 20)):
            path.write_text(json.dumps(C.to_dict(C.Chat(worker.id, self.clock.now(), id, text=id))))
            os.utime(path, (stamp, stamp))
        engine = self.engine([worker])
        self.assertTrue(engine.step())
        self.assertFalse(newer.exists())
        self.assertFalse(older.exists())
        self.assertEqual([engine.message(id).body for id, _ in cli.sent], ["older", "newer"])
        replied = self.chat(engine, "newer")
        self.assertTrue(replied["ok"])
        self.assertEqual(self.chat(engine, "newer"), replied)
        self.assertEqual(len(cli.sent), 2)

    def test_order_and_prior_use_one_snapshot(self):
        cli, worker = self.cli()
        seen = []
        modules = []
        for name in reversed(C.MODULE_ORDER):
            def decide(snap, ev, prior, name=name):
                if isinstance(ev, C.Chat):
                    seen.append((name, snap, prior))
                    return [C.ToUser(C.Message(C.new_id(name, ev), recipient=C.USER, needs_answer=False))]
                return []

            modules.append(Module(name, decide))
        engine = self.engine([worker], modules)
        self.chat(engine, "order")
        self.assertEqual(tuple(name for name, _, _ in seen), C.MODULE_ORDER)
        for i, (_, snap, prior) in enumerate(seen):
            self.assertIs(snap, seen[0][1])
            self.assertEqual(snap.messages, {})
            self.assertIsInstance(prior, tuple)
            self.assertEqual(len(prior), i)
            self.assertEqual(prior[:max(0, i - 1)], seen[max(0, i - 1)][2])

    def test_smoke_runs_in_background_and_result_is_decided_once(self):
        cli, worker = self.cli()
        entered, finished = threading.Event(), threading.Event()
        seen = []

        def smoke(adapter, spec, clock, wait):
            entered.set()
            finished.wait(2)
            return C.SmokeResult(spec.agent, clock.now(), choice=spec.choice, ok=True)

        def decide(snap, ev, prior):
            if isinstance(ev, C.OrchStarted):
                return [C.SmokeTest(worker.id, worker.choice)]
            if isinstance(ev, C.SmokeResult):
                seen.append(ev)
            return []

        with patch("orch.runtime.engine.models.smoke_test", smoke):
            engine = self.engine([worker], [Module("recovery", decide)])
            self.assertTrue(entered.wait(1))
            self.assertFalse(seen)
            self.assertFalse(engine.step())
            finished.set()
            thread = next(t for t in threading.enumerate() if t.name == "orch-smoke-worker-1")
            thread.join(2)
            engine.step()
            engine.step()
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0].ok)


if __name__ == "__main__":
    unittest.main()
