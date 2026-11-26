"""Scenario 2 over each adapter's existing local fake transport."""

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rt_e2e_helpers import C, CLAUDE, CRITIC, MANAGER, QUIET, W1, W2, RuntimeCase, tell
import test_rt_x1 as claude
import test_rt_x2 as codex
import test_rt_x3 as opencode


class TestAdapters(RuntimeCase):
    def start_job(self, instruction):
        self.cli(CRITIC).script(tell(W1, instruction) + QUIET, tell(W1, "Keep the job log.") + QUIET)
        self.start()
        self.assign()
        self.wait_for(lambda: any(j.state == C.JobState.RUNNING for j in self.state.jobs.values())
                      and self.state.turns.get(W1) and self.state.turns[W1].ending == C.Ending.INTERMEDIATE,
                      "the adapter's intermediate reply", limit=200, real_io=True, interval=0.1)
        self.advance(2)
        self.wait_for(lambda: bool(self.messages(W1, body="Keep the job log.")),
                      "critic guidance", limit=200, real_io=True, interval=0.1)

    def assert_result(self, text):
        self.wait_for(lambda: bool(self.messages(CRITIC, body=text, key=C.TextKey.WORKER_FINAL)),
                      "the job's final reply", limit=150, real_io=True, interval=0.1)
        self.advance(2)
        self.assert_received_once(CRITIC, body=text, key=C.TextKey.WORKER_FINAL)
        job, = self.state.jobs.values()
        self.assertTrue(job.woke)
        self.assertEqual(job.state, C.JobState.ENDED)

    def test_S02_JOB_2_JOB_3_JOB_4_claude_fake_stream(self):
        fake = claude.TestProcess()
        fake.setUp()
        for key in ("FAKE_SCENARIO", "FAKE_LOG"):
            if key in os.environ:
                self.addCleanup(os.environ.__setitem__, key, os.environ[key])
        self.addCleanup(fake.doCleanups)
        queued = claude.queued(claude.A1).replace(claude.X.to_uuid(claude.A1), "{uuid}")
        echo = claude.echo(claude.A1).replace(claude.X.to_uuid(claude.A1), "{uuid}")
        start = [queued, claude.init(), echo, claude.tool("tool1", "sleep 9", True),
                 claude.task_started("j1", "tool1"), claude.tool_done("tool1"),
                 claude.say("Started report."), claude.result("Started report.", 2)]
        guide_and_wake = [queued, claude.init(), echo, claude.say("Log kept."), claude.result("Log kept."),
                          claude.task_end("j1"), claude.note_echo("j1"), claude.init(),
                          claude.say("Claude job complete."), claude.result("Claude job complete.")]
        fake.scenario(on_message=[[s.replace(claude.SID, "{sid}") for s in block]
                                  for block in (start, guide_and_wake)])
        def make():
            adapter = claude.X.ClaudeAdapter(self.clock)
            fake.started.append(adapter)
            return adapter

        choice = fake.spec().choice
        self.fake_path("claude", choice.options["executable"])
        self.install_real_worker(choice, make)
        self.start_job("Start Claude job.")
        self.assert_result("Claude job complete.")
        inputs = [json.loads(line[4:])["message"]["content"] for line in fake.lines("GOT ")]
        self.assertEqual(len(inputs), 2)
        self.assertIn("Start Claude job.", str(inputs[0]))
        self.assertIn("Keep the job log.", str(inputs[1]))

    def fake_path(self, name, executable):
        # Engine resolves persisted choices again, so options in the snapshot cannot select the executable.
        # The test PATH exposes only the named fake and system utilities.
        bin_dir = self.path / "bin"
        bin_dir.mkdir()
        (bin_dir / name).symlink_to(Path(executable))
        env = patch.dict(os.environ, PATH=f"{bin_dir}:/usr/bin:/bin")
        env.start()
        self.addCleanup(env.stop)

    def install_real_worker(self, choice, make):
        self.agents[W1].choice = choice
        self.agents[W1].choices = [choice]
        if choice.backend == "claude":
            mock_choice = replace(CLAUDE, backend="qoder", model="qwen4-max", model_id="qwen4-max")
            for name in (MANAGER, CRITIC, W2):
                self.add_agent(name, choice=mock_choice)
                self.banks["claude"][name] = self.banks["qoder"][name]
        self.factories[choice.backend] = make

    def test_S02_JOB_2_JOB_3_JOB_4_codex_fake_fallback(self):
        fake = codex.Rig()
        fake.setUp()
        self.addCleanup(fake.doCleanups)
        fake.clock = self.clock
        self.fake_path("codex", fake.exe)
        self.install_real_worker(fake.spec().choice, fake.adapter)
        self.start_job("JOB start Codex work.")
        proc = self.state.agents[W1].proc
        session = self.state.agents[W1].session.session_id

        def history():
            reply = fake.ctl(proc, "thread/turns/list", threadId=session)
            return reply["result"]["data"]

        def inputs():
            return [item for turn in history() for item in turn["items"] if item["type"] == "userMessage"]

        guidance, = self.messages(W1, body="Keep the job log.")
        self.wait_for(lambda: self.state.messages[guidance.id].answered_at, "Codex takes its guidance",
                      limit=150, real_io=True, interval=0.1)
        self.wait_for(lambda: self.state.agents[W1].status == C.AgentStatus.WAITING_JOB,
                      "Codex finishes guidance turn", limit=150, real_io=True, interval=0.1)
        reply = fake.ctl(proc, "x/endJob", code=0)
        self.assertNotIn("error", reply)
        self.advance(1)
        self.wait_for(lambda: bool(self.messages(W1, key=C.TextKey.JOB_WAKEUP)), "Codex wake-up input",
                      limit=150, real_io=True, interval=0.1)
        self.assert_result("ok:")
        wake, = self.messages(W1, key=C.TextKey.JOB_WAKEUP)
        self.assertEqual(self.state.messages[wake.id].status, C.MessageStatus.ANSWERED)
        assignment, = self.messages(W1, body="JOB start Codex work.")
        self.assertEqual([item.get("clientId") for item in inputs()], [assignment.id, guidance.id, wake.id])

    def test_S02_JOB_2_JOB_3_JOB_4_opencode_fake_self_wake(self):
        fake = opencode.Fake()
        choice = C.Choice(backend="opencode", provider="p", model="m1")

        def make():
            adapter = opencode.FakeAdapter(self.clock, fake)
            self.addCleanup(adapter.close)
            return adapter

        self.install_real_worker(choice, make)
        self.cli(CRITIC).script(tell(W1, "Start OpenCode job.") + QUIET, tell(W1, "Keep the job log.") + QUIET)
        self.start()
        self.assign()
        sid = "ses_1"
        inbox = fake.sessions[sid]["inbox"]

        def take(text):
            matches = [m for m in inbox if text in m["text"]]
            self.assertEqual(len(matches), 1)
            fake.rec(sid, "user", **matches[0])

        def reply(text, content=None):
            fake.rec(sid, "assistant", model={"id": "m1", "providerID": "p"},
                     content=content or [{"type": "text", "text": text}])
            fake.rec(sid, "idle", outcome="succeeded")

        take("Start OpenCode job.")
        fake.shells["sh_a"] = opencode.shell("sh_a")
        fake.jobs = [{"id": "sh_a", "shellID": "sh_a", "kind": "shell", "sessionID": sid,
                      "status": "running", "command": "sleep 9", "time": {"started": 5000}}]
        reply("", [{"type": "tool", "id": "t1", "name": "shell", "state": {
            "status": "running", "input": {"command": "sleep 9", "background": True},
            "metadata": {"shellID": "sh_a"}}}, {"type": "text", "text": "OpenCode job started."}])
        self.advance()
        self.assertEqual(self.state.turns[W1].ending, C.Ending.INTERMEDIATE)
        self.advance(2)
        take("Keep the job log.")
        reply("Log kept.")
        self.advance()
        fake.jobs[0]["status"] = "completed"
        fake.shells["sh_a"] = opencode.shell("sh_a", status="completed", exit=0)
        fake.rec(sid, "synthetic", text="<shell/>",
                 metadata={"source": "shell", "shellID": "sh_a", "state": "completed", "exit": 0})
        reply("OpenCode job complete.")
        self.advance()
        self.assert_result("OpenCode job complete.")
        self.assertEqual(len(inbox), 2)
