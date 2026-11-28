"""Package M: config, commands, migration (MOD-3, MOD-4, PRM-2, MS-14, REC-9, TERM-1, section 6 items 5 and 8)."""
from __future__ import annotations

from rt_helpers import *  # noqa: F401,F403

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from orch import cli, config
from orch.recorder import Recorder, read_json, write_json
from orch.runtime import milestones, models, migrate
from orch.runtime.contracts import (
    ChangeMilestones,
    CriticTell,
    Choice,
    GateAnswer,
    Message,
    MessageStatus,
    MilestoneStatus,
    Role,
    Send,
    SessionRef,
    Snapshot,
    StopOrch,
    TerminalClose,
    TerminalOpen,
    TextKey,
    UserCommand,
    from_dict,
)
from orch.runtime.store import Store


class FakeEngine:
    """Fake Engine matching the Task L Engine interface."""

    instances: list = []

    def __init__(self, run_dir, clock, adapters, settings, modules=None):
        self.run_dir = Path(run_dir)
        self.clock = clock
        self.adapters = adapters
        self.settings = settings
        self.modules = modules
        self.started_with = None
        self.started = False
        self.ran = False
        self.closed = False
        self.raise_ctrl_c = False
        FakeEngine.instances.append(self)

    def start(self, snap=None):
        self.started = True
        self.started_with = snap
        if snap is not None and not (self.run_dir / "state.json").is_file():
            st = Store.create(self.run_dir, self.clock, snap)
            st.close()

    def run(self, stop_event):
        self.ran = True
        if self.raise_ctrl_c:
            raise KeyboardInterrupt

    def close(self):
        self.closed = True


class TestRuntimeChoiceAndEngine(unittest.TestCase):
    """Item 1: Choosing the runtime (runtime: new vs old modes, REC-9 close on Ctrl-C)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        FakeEngine.instances.clear()

    def _project(self, settings_yaml: str = "") -> Path:
        d = self.tmp / ".orch"
        for name, role, extra in (
            ("manager", "manager", "goal: test goal\n"),
            ("critic", "critic", ""),
            ("worker", "worker", ""),
        ):
            ad = d / "agents" / name
            ad.mkdir(parents=True, exist_ok=True)
            (ad / "agent.yaml").write_text(f"role: {role}\nbackend: mock\n{extra}", encoding="utf-8")
        if settings_yaml:
            (d / "settings.yaml").write_text(settings_yaml, encoding="utf-8")
        return self.tmp

    def test_item1_REC_9_runtime_new_uses_engine_and_closes_on_ctrl_c(self):
        proj = self._project(
            "runtime: new\nmodels:\n  m1: {backend: mock, backend_options: {model: mock}}\n"
        )
        for name in ("manager", "critic", "worker"):
            p = proj / ".orch" / "agents" / name / "agent.yaml"
            p.write_text(p.read_text().replace("backend: mock", "model: m1"))

        class CtrlCEngine(FakeEngine):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.raise_ctrl_c = True

        with patch.object(cli, "Engine", CtrlCEngine):
            rc = cli.main(["run", str(proj)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(FakeEngine.instances), 1)
        eng = FakeEngine.instances[0]
        self.assertTrue(eng.started)
        self.assertTrue(eng.ran)
        self.assertTrue(eng.closed)
        for b in ("claude", "qoder", "qwen", "codex", "opencode", "agy", "command"):
            self.assertIn(b, eng.adapters)
            self.assertTrue(callable(eng.adapters[b]))
        # Check adapter factories resolve to the right classes for available adapters
        from orch.runtime.adapters.claude import ClaudeAdapter, QoderAdapter, QwenAdapter
        from orch.runtime.adapters.codex import CodexAdapter
        from orch.runtime.adapters.agy import AgyAdapter
        from orch.runtime.adapters.oneshot import CommandAdapter

        self.assertIsInstance(eng.adapters["claude"](), ClaudeAdapter)
        self.assertIsInstance(eng.adapters["qoder"](), QoderAdapter)
        self.assertIsInstance(eng.adapters["qwen"](), QwenAdapter)
        self.assertIsInstance(eng.adapters["codex"](), CodexAdapter)
        self.assertIsInstance(eng.adapters["agy"](), AgyAdapter)
        self.assertIsInstance(eng.adapters["command"](), CommandAdapter)

    def test_item1_N_a_removed_mode_is_refused_before_anything_runs(self):
        cfg_file = self.tmp / "old.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "output_dir": str(self.tmp / "runs"),
                    "monitors": [
                        {
                            "name": "m",
                            "workspace": str(self.tmp),
                            "goal": "g",
                            "max_rounds": 1,
                            "controller": {"mode": "pipeline"},
                            "agents": [
                                {"name": "w1", "role": "worker", "backend": "mock", "task": "do it"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch.object(cli, "Engine", FakeEngine):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                cli.main(["run", str(cfg_file)])
        self.assertIn("pipeline mode was removed", str(e.exception))
        self.assertEqual(FakeEngine.instances, [])


class TestAddWorkerAndCommands(unittest.TestCase):
    """Items 2, 3, 4: config.add_worker, CLI commands for new runtime, and orch terminal (TERM-1)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_item2_WRK_2_add_worker_copies_agent_yaml_changes_name_and_refuses_existing(self):
        orch_dir = self.tmp / ".orch"
        w1 = orch_dir / "agents" / "worker-1"
        w1.mkdir(parents=True)
        src_text = "# comment kept\nname: worker-1\nrole: worker\nmodel: haiku\nfresh_session: compact\n"
        (w1 / "agent.yaml").write_text(src_text, encoding="utf-8")

        config.add_worker(self.tmp, "worker-1", "worker-2")
        w2_file = orch_dir / "agents" / "worker-2" / "agent.yaml"
        self.assertTrue(w2_file.is_file())
        self.assertEqual(
            w2_file.read_text(encoding="utf-8"),
            "# comment kept\nname: worker-2\nrole: worker\nmodel: haiku\nfresh_session: compact\n",
        )
        with self.assertRaises(config.ConfigError):
            config.add_worker(self.tmp, "worker-1", "worker-2")

    def _new_run_dir(self) -> Path:
        run_dir = self.tmp / "runs" / "proj" / "20261126-100000"
        clock = FakeClock()
        s = snapshot(
            gate("g-plan", "plan", text="1. First"),
            gate("q1", "question", text="Which?"),
        )
        s.run.workspace = str(self.tmp)
        s.run.run_dir = str(run_dir)
        st = Store.create(run_dir, clock, s)
        st.close()
        return run_dir

    def _read_commands(self, run_dir: Path):
        files = sorted((run_dir / "commands").glob("*.json"))
        return [from_dict(UserCommand, read_json(f)) for f in files]

    def test_item3_REC_9_commands_for_new_runtime_run(self):
        run_dir = self._new_run_dir()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["stop", str(run_dir)]), 0)
            self.assertEqual(cli.main(["stop", "--agents", str(run_dir)]), 0)
            self.assertEqual(cli.main(["tell", str(run_dir), "critic", "Focus on tests."]), 1)
            self.assertEqual(cli.main(["tell", str(run_dir), "worker-1", "Focus on tests."]), 0)
            self.assertEqual(cli.main(["approve", str(run_dir), "--note", "Looks good"]), 0)
            self.assertEqual(cli.main(["approve", str(run_dir), "--revise", "Add edge cases"]), 0)
            self.assertEqual(cli.main(["answer", str(run_dir), "q1", "Option B"]), 0)

        cmds = self._read_commands(run_dir)
        self.assertEqual(len(cmds), 6)
        self.assertIsInstance(cmds[0], StopOrch)
        self.assertFalse(cmds[0].agents)
        self.assertIsInstance(cmds[1], StopOrch)
        self.assertTrue(cmds[1].agents)
        self.assertIsInstance(cmds[2], CriticTell)
        self.assertEqual((cmds[2].agent, cmds[2].text), ("worker-1", "Focus on tests."))
        self.assertIsInstance(cmds[3], GateAnswer)
        self.assertEqual((cmds[3].gate_id, cmds[3].approve, cmds[3].text), ("g-plan", True, "Looks good"))
        self.assertIsInstance(cmds[4], GateAnswer)
        self.assertEqual((cmds[4].gate_id, cmds[4].approve, cmds[4].text), ("g-plan", False, "Add edge cases"))
        self.assertIsInstance(cmds[5], GateAnswer)
        self.assertEqual((cmds[5].gate_id, cmds[5].text), ("q1", "Option B"))

    def test_item3_N_UI_6_an_old_run_is_read_only(self):
        old_run = self.tmp / "old_run"
        old_run.mkdir(parents=True)
        write_json(old_run / "state.json", {"monitor": "m", "run_id": "1", "status": "running", "sessions": {}})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["stop", str(old_run)]), 1)
            self.assertEqual(cli.main(["tell", str(old_run), "critic", "hello"]), 1)
            self.assertEqual(cli.main(["approve", str(old_run)]), 1)
            self.assertEqual(cli.main(["answer", str(old_run), "q1", "yes"]), 1)
        self.assertEqual(out.getvalue().count("an old run, which can only be read"), 4)
        self.assertEqual(sorted(p.name for p in old_run.iterdir()), ["state.json"])  # nothing written

    def test_item4_TERM_1_terminal_for_new_run_sends_open_execs_and_closes(self):
        run_dir = self._new_run_dir()
        events_seen = []

        def fake_run(cmd, cwd=None, env=None):
            # When the command runs, TerminalOpen must already be written and TerminalClose not yet
            cmds_now = self._read_commands(run_dir)
            events_seen.append(([type(c).__name__ for c in cmds_now], list(cmd), cwd))
            return 0

        out = io.StringIO()
        with patch("orch.cli.subprocess.run", side_effect=fake_run), redirect_stdout(out):
            rc = cli.main(["terminal", str(run_dir), "critic"])
        self.assertEqual(rc, 0)
        self.assertIn("s-critic", out.getvalue())
        self.assertEqual(len(events_seen), 1)
        self.assertEqual(events_seen[0][0], ["TerminalOpen"])
        self.assertIn("s-critic", events_seen[0][1])

        cmds_after = self._read_commands(run_dir)
        self.assertEqual([type(c) for c in cmds_after], [TerminalOpen, TerminalClose])
        self.assertEqual((cmds_after[0].agent, cmds_after[1].agent), ("critic", "critic"))


class TestSettingsAndPromptMigration(unittest.TestCase):
    """Items 5, 6, 7: MOD-4 settings migration & confirm, MOD-3 save_choice, PRM-2 prompts."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_item5_MOD_4_new_project_has_settings_yaml_and_model_refs(self):
        proj = self.tmp / "new_proj"
        proj.mkdir()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["init", str(proj), "--project"]), 0)
        orch_dir = proj / ".orch"
        self.assertTrue((orch_dir / "settings.yaml").is_file())
        settings = yaml.safe_load((orch_dir / "settings.yaml").read_text(encoding="utf-8"))
        self.assertIn("models", settings)
        for name in ("manager", "critic", "worker", "auditor", "monitor"):
            y = yaml.safe_load((orch_dir / "agents" / name / "agent.yaml").read_text(encoding="utf-8"))
            self.assertIn("model", y)
            self.assertIn(y["model"], settings["models"])
            self.assertNotIn("backend", y)
            self.assertNotIn("backend_options", y)
            self.assertNotIn("fallback", y)
        cfg = config.load_config(str(proj))
        self.assertEqual(len(cfg.monitors[0].agents), 5)

    def test_item5_MOD_4_write_settings_note_migrated_and_confirm(self):
        orch_dir = self.tmp / "existing" / ".orch"
        for name, role, model_name in (("manager", "manager", "opus"), ("critic", "critic", "sonnet"), ("worker", "worker", "haiku")):
            d = orch_dir / "agents" / name
            d.mkdir(parents=True)
            (d / "agent.yaml").write_text(
                f"# {name} config\nrole: {role}\nbackend: claude\nbackend_options: {{model: {model_name}, effort: medium}}\n"
                + ("goal: Finish task\n" if name == "manager" else ""),
                encoding="utf-8",
            )
            (d / "preference.md").write_text("old preference\n", encoding="utf-8")

        mig = migrate.write_settings(orch_dir.parent)
        self.assertTrue((orch_dir / "settings.yaml").is_file())
        self.assertTrue((orch_dir / "migration.json").is_file())
        self.assertTrue(mig["files"])
        # Old files are kept on disk
        for name in ("manager", "critic", "worker"):
            self.assertTrue((orch_dir / "agents" / name / "preference.md").is_file())
            self.assertTrue((orch_dir / "agents" / name / "agent.yaml.bak").is_file())
            y = yaml.safe_load((orch_dir / "agents" / name / "agent.yaml").read_text(encoding="utf-8"))
            self.assertIn("model", y)
            self.assertNotIn("backend", y)

        # The run shows NOTE_MIGRATED
        cfg = config.load_config(str(orch_dir.parent))
        snap = migrate.build_snapshot(
            cfg.monitors[0],
            self.tmp / "run1",
            FakeClock(),
            settings=cfg.settings,
            migrated_files=migrate.migration_files(orch_dir.parent),
        )
        notes = [m for m in snap.messages.values() if m.key == TextKey.NOTE_MIGRATED]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].text.values["files"], mig["files"])

        # ConfirmMigration moves old lines/files to .orch/backup/<date>/
        backup_dir = migrate.confirm(orch_dir.parent, date="2026-11-26")
        self.assertEqual(backup_dir, orch_dir / "backup" / "2026-11-26")
        self.assertFalse((orch_dir / "migration.json").exists())
        for name in ("manager", "critic", "worker"):
            self.assertFalse((orch_dir / "agents" / name / "preference.md").exists())
            self.assertFalse((orch_dir / "agents" / name / "agent.yaml.bak").exists())
            self.assertTrue((backup_dir / "agents" / name / "preference.md").is_file())
            self.assertTrue((backup_dir / "agents" / name / "agent.yaml").is_file())
            self.assertIn("backend: claude", (backup_dir / "agents" / name / "agent.yaml").read_text(encoding="utf-8"))

    def test_item6_MOD_3_save_choice_keeps_comments_and_other_keys(self):
        ay = self.tmp / "agent.yaml"
        ay.write_text(
            "# top comment\n"
            "role: worker  # role comment\n"
            "model: opus\n"
            "backup: haiku\n"
            "backend_options:\n"
            "  effort: medium\n"
            "  extra_args: [--verbose]  # keep arg\n"
            "fresh_session: compact\n",
            encoding="utf-8",
        )
        models.save_choice(ay, Choice(backend="codex", model="gpt-5.5", effort="high", model_id="gpt-5.5"))
        updated = ay.read_text(encoding="utf-8")
        self.assertIn("# top comment", updated)
        self.assertIn("# role comment", updated)
        self.assertIn("# keep arg", updated)
        data = yaml.safe_load(updated)
        self.assertEqual(data["model"], "gpt-5.5")
        self.assertEqual(data["backup"], "haiku")
        self.assertEqual(data["fresh_session"], "compact")
        self.assertEqual(data["backend_options"], {"effort": "high", "extra_args": ["--verbose"]})

    def test_item7_PRM_2_custom_prompts_moved_to_bak_once_and_worker_task_kept(self):
        orch_dir = self.tmp / "prm2" / ".orch"
        orch_dir.mkdir(parents=True)
        (orch_dir / "settings.yaml").write_text(
            "runtime: new\nmodels:\n  m1: {backend: mock, backend_options: {model: mock}}\n",
            encoding="utf-8",
        )
        for name, role, extra in (
            ("manager", "manager", "goal: G\n"),
            ("critic", "critic", ""),
            ("monitor", "monitor", ""),
            ("worker", "worker", "task: Keep this worker task.\n"),
        ):
            d = orch_dir / "agents" / name
            d.mkdir(parents=True)
            (d / "agent.yaml").write_text(f"role: {role}\nmodel: m1\n{extra}", encoding="utf-8")

        # Manager has a hand-edited prompt; critic has Orch's default prompt; worker has its own prompt/task
        (orch_dir / "agents" / "manager" / "system_prompt.md").write_text("Custom manager prompt.", encoding="utf-8")
        default_critic = (Path(config.__file__).with_name("runtime") / "prompts" / "critic.md").read_text(encoding="utf-8")
        (orch_dir / "agents" / "critic" / "system_prompt.md").write_text(default_critic, encoding="utf-8")
        (orch_dir / "agents" / "worker" / "system_prompt.md").write_text("Worker custom task file.", encoding="utf-8")

        err = io.StringIO()
        with redirect_stderr(err):
            cfg = config.load_config(str(orch_dir.parent))
        self.assertIn("agents/manager/system_prompt.md.bak", err.getvalue())
        self.assertFalse((orch_dir / "agents" / "manager" / "system_prompt.md").exists())
        self.assertTrue((orch_dir / "agents" / "manager" / "system_prompt.md.bak").is_file())
        self.assertEqual(
            (orch_dir / "agents" / "manager" / "system_prompt.md.bak").read_text(encoding="utf-8"),
            "Custom manager prompt.",
        )
        # Unchanged default prompt is not moved to .bak
        self.assertFalse((orch_dir / "agents" / "critic" / "system_prompt.md.bak").exists())
        # Worker prompt/task stays
        self.assertTrue((orch_dir / "agents" / "worker" / "system_prompt.md").is_file())
        mon = cfg.monitors[0]
        self.assertEqual(mon.agent("worker").task, "Keep this worker task.")
        # Loading a second time does not emit another notice or overwrite .bak
        err2 = io.StringIO()
        with redirect_stderr(err2):
            config.load_config(str(orch_dir.parent))
        self.assertEqual(err2.getvalue(), "")


class TestMigrateOldRun(unittest.TestCase):
    """Item 8: migrate.migrate_run (section 6 item 8, MS-14)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def _make_old_run(self, status: str = "paused") -> Path:
        run_dir = self.tmp / "runs" / "proj" / "20261120-120000"
        run_dir.mkdir(parents=True)
        write_json(
            run_dir / "config.json",
            {
                "name": "proj",
                "workspace": str(self.tmp),
                "working_dir": str(self.tmp),
                "goal": "Ship v2",
                "max_rounds": 200,
                "controller": {"mode": "managed", "approve_plan": True, "approve_milestones": True},
                "agents": [
                    {"name": "manager", "role": "manager", "backend": "claude", "backend_options": {"model": "opus"}},
                    {"name": "critic", "role": "verifier", "backend": "codex", "backend_options": {"model": "gpt"}},
                    {"name": "worker", "role": "worker", "backend": "claude", "backend_options": {"model": "haiku"}},
                ],
            },
        )
        write_json(
            run_dir / "state.json",
            {
                "monitor": "proj",
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "status": status,
                "mode": "managed",
                "goal": "Ship v2",
                "workspace": str(self.tmp),
                "working_dir": str(self.tmp),
                "max_rounds": 200,
                "round": 4,
                "sessions": {
                    "manager": {"session_id": "sess-mgr-1", "role": "manager", "backend": "claude", "model": "opus"},
                    "critic": {"session_id": "sess-crt-2", "role": "verifier", "backend": "codex", "model": "gpt"},
                    "worker": {"session_id": "sess-wrk-3", "role": "worker", "backend": "claude", "model": "haiku"},
                },
                "fwd": {"critic": ["Check step 2."]},
                "worker_messages": [
                    {"id": "wm1", "to": "worker", "from": "critic", "message": "Run unit tests.", "status": "queued"},
                    {"id": "wm2", "to": "worker", "from": "critic", "message": "Old done.", "status": "accepted"},
                ],
                "managed": {
                    "phase": "done" if status == "completed" else "work",
                    "ms": 1,
                    "milestones": [
                        {"title": "Setup", "done_when": "Tests run", "rubric": ["Clean"]},
                        {"title": "Feature", "done_when": "Feature works", "rubric": ["Fast"]},
                    ],
                },
            },
        )
        return run_dir

    def test_item8_MS_14_migrate_run_keeps_sessions_waiting_messages_and_milestone_ids(self):
        run_dir = self._make_old_run(status="paused")
        snap = migrate.migrate_run(run_dir, clock=FakeClock())

        self.assertTrue((run_dir / "state.old.json").is_file())
        self.assertNotIn("runtime", read_json(run_dir / "state.old.json"))
        self.assertIn("runtime", read_json(run_dir / "state.json"))

        self.assertEqual(snap.agents["manager"].session.session_id, "sess-mgr-1")
        self.assertEqual(snap.agents["critic"].session.session_id, "sess-crt-2")
        self.assertEqual(snap.agents["worker"].session.session_id, "sess-wrk-3")

        waiting = [m for m in snap.messages.values() if m.status == MessageStatus.WAITING]
        self.assertEqual(len(waiting), 2)
        self.assertEqual({(m.recipient, m.body) for m in waiting}, {("critic", "Check step 2."), ("worker", "Run unit tests.")})

        self.assertEqual([m.id for m in snap.milestones], ["m1", "m2"])
        self.assertEqual([m.status for m in snap.milestones], [MilestoneStatus.DONE, MilestoneStatus.CURRENT])
        self.assertFalse(snap.finished)

    def test_item8_migrate_run_refuses_while_process_is_alive(self):
        run_dir = self._make_old_run(status="running")
        rec = Recorder(run_dir, run_dir.parent.parent)
        self.assertIsNone(rec.claim())
        try:
            with self.assertRaises(migrate.MigrationError):
                migrate.migrate_run(run_dir, clock=FakeClock())
            self.assertFalse((run_dir / "state.old.json").exists())
        finally:
            rec.release()

    def test_item8_MS_14_finished_run_reopened_starts_finished_and_accepts_change_milestones(self):
        run_dir = self._make_old_run(status="completed")
        snap = migrate.migrate_run(run_dir, clock=FakeClock())
        self.assertTrue(snap.finished)
        self.assertEqual([m.id for m in snap.milestones], ["m1", "m2"])
        self.assertTrue(all(m.status == MilestoneStatus.DONE for m in snap.milestones))

        # Reopened finished run accepts ChangeMilestones
        cmd = ChangeMilestones(agent="manager", at=snap.now, id="c-add-ms", text="Add a third milestone for benchmarks.")
        acts = decide(milestones.module(), snap, cmd)
        send_act = assert_has(self, acts, Send, recipient="manager")
        self.assertEqual(send_act.message.key, TextKey.MILESTONE_CHANGE_REQUEST)


if __name__ == "__main__":
    unittest.main()
