"""End-to-end tests using the mock and command backends. Run: python -m unittest discover tests"""

from protocol_fixtures import worker_instruction

import dataclasses
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.config import ConfigError, load_config  # noqa: E402
from orch.backends import TurnResult  # noqa: E402
from orch.recorder import Recorder, read_json  # noqa: E402
from orch import cli  # noqa: E402


def make_cfg(tmp: Path, monitors: list, defaults=None) -> str:
    (tmp / "ws").mkdir(exist_ok=True)
    cfg = {"output_dir": str(tmp / "out"), "defaults": defaults or {"backend": "mock"}, "monitors": monitors}
    p = tmp / "cfg.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def pipeline(name="m", **kw):
    m = {"name": name, "workspace": "ws", "goal": "g", "max_rounds": 4,
         "controller": {"mode": "pipeline"},
         "agents": [{"name": "w1", "role": "worker", "task": "do it"},
                    {"name": "v1", "role": "verifier", "backend_options": {"pass_after": 2}}]}
    m.update(kw)
    return m




def turns(rec):
    out = []
    for p in sorted((rec.run_dir / "rounds").glob("r*/t*.json")):
        out.append(read_json(p))
    return out






class TestCommandBackendAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())



    def test_validation_errors(self):
        bad = pipeline()
        bad["agents"].append({"name": "v2", "role": "verifier", "reviews": ["nobody"]})
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [bad]))
        bad = pipeline()
        bad["agents"][0]["colour"] = "red"
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [bad]))
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [pipeline(), pipeline()]))

    def test_text_files_and_defaults_merge(self):
        (self.tmp / "goal.md").write_text("from a file")
        m = pipeline()
        del m["goal"]
        m["goal_file"] = "goal.md"
        m["defaults"] = {"backend_options": {"b": 2}}
        cfg = load_config(make_cfg(self.tmp, [m], defaults={"backend": "mock", "backend_options": {"a": 1}}))
        mon = cfg.monitor("m")
        self.assertEqual(mon.goal, "from a file")
        self.assertEqual(mon.agents[0].backend_options, {"a": 1, "b": 2})



def managed(name="m", controller=None, mgr=None, critic=None, worker=None):
    c = {"mode": "managed", "checklist": True, "approve_plan": False, "approve_milestones": False, "tag_reminder": 0}
    c.update(controller or {})
    return {"name": name, "workspace": "ws", "goal": "Revise the plan.", "max_rounds": 12, "controller": c,
            "agents": [{"name": "boss", "role": "manager", "display_name": "Alex", "backend_options": mgr or {}},
                       {"name": "critic", "role": "verifier", "display_name": "Jordan", "backend_options": critic or {}},
                       {"name": "writer", "role": "worker", "task": "Edit the plan.", "display_name": "Sam",
                        "backend_options": worker or {}}]}


class TestManaged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

















    def test_claude_can_skip_my_hooks(self):
        from orch.backends import ClaudeBackend, TurnRequest
        req = lambda o: TurnRequest("a", "worker", "hi", "", None, ".", 10, o)
        cmd, _ = ClaudeBackend().build_command(req({"disable_hooks": True}))
        self.assertIn('{"disableAllHooks": true}', cmd)
        cmd, _ = ClaudeBackend().build_command(req({}))
        self.assertNotIn("--settings", cmd)

    def _urgent_run(self, note: dict, worker_opts: dict, critic_opts=None):
        """Run a one-milestone project; drop `note` (urgent) into the inbox while the writer's first turn runs."""
        from unittest import mock
        from orch.backends import MockBackend
        sent, orig = [], MockBackend.run

        def spy(self_, req, stop):
            sent.append((req.agent, req.session_id, req.prompt))
            return orig(self_, req, stop)

        def drop():
            for _ in range(400):
                for t in (self.tmp / "out").glob("**/rounds/r*/t*-writer.json"):
                    if (read_json(t) or {}).get("status") == "running":
                        time.sleep(0.3)
                        inbox = t.parents[2] / "inbox"
                        (inbox / "20990101-000000-urgent.json").write_text(json.dumps({**note, "urgent": True}))
                        return
                time.sleep(0.05)
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        with mock.patch.object(MockBackend, "run", spy):
            status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1}, worker=worker_opts,
                                                        critic=critic_opts)]))
        th.join(timeout=5)
        return status, rec, sent

    def test_a_system_process_whose_directory_cant_be_read_is_not_a_job(self):
        import os
        from unittest import mock
        from orch import jobs
        ws, here = os.path.realpath(self.tmp), os.getcwd()
        before = {1: (0, "launchd")}
        after = {1: (0, "launchd"), 900: (1, "/System/Library/nbstated"), 901: (1, "python run.py")}
        os.chdir(ws)  # orch runs from the project folder; 900's directory can't be read (a root daemon)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {901: ws}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual([j["pid"] for j in found], [901])

    def test_a_terminal_shell_started_in_the_workspace_is_not_a_job(self):
        # the round-80 case: an interactive zsh in the project folder, reparented under an editor's terminal host
        import os
        from unittest import mock
        from orch import jobs
        ws, here = os.path.realpath(self.tmp), os.getcwd()
        before = {1: (0, "launchd"), 50: (1, "node ptyHost")}
        after = {**before, 60: (50, "/bin/zsh -il"), 61: (60, "-zsh"), 62: (50, "bash"),
                 63: (1, "bash train.sh"), 64: (1, "python run.py")}
        os.chdir(ws)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {p: ws for p in pids}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual(sorted(j["pid"] for j in found), [63, 64])
        self.assertTrue(jobs.is_shell("/bin/zsh -il"))
        self.assertFalse(jobs.is_shell("bash train.sh"))

    def test_a_waiter_loop_under_a_turn_is_found(self):
        from orch import jobs
        procs = {100: (1, "claude -p --resume x"),            # the worker's turn
                 101: (100, "/bin/zsh -c eval 'cd ws; while kill -0 72926 2>/dev/null; do sleep 10; done'"),
                 102: (101, "sleep 10"),
                 103: (100, "python -m scripts.run_stage --match"),   # real work under the turn: not a waiter
                 104: (1, "bash scripts/job.sh")}                  # not under the turn at all
        self.assertEqual(jobs.waiters(100, procs), [101])
        self.assertEqual(jobs.waiters(104, procs), [])

    def test_an_editor_helper_orphaned_into_the_workspace_is_not_a_job(self):
        from orch import jobs
        from unittest import mock
        import os
        here = os.getcwd()
        ws = str(Path(tempfile.mkdtemp()).resolve())
        vscode = ("/Users/x/.vscode-server/cli/servers/Stable-abc/server/node "
                  "/Users/x/.vscode-server/extensions/markdown-language-features/dist/serverWorkerMain --node-ipc")
        before = {10: (1, "launchd")}
        after = {**before, 70: (1, vscode), 71: (1, "python build_report.py")}
        os.chdir(ws)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {p: ws for p in pids}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual([j["pid"] for j in found], [71])
        self.assertTrue(jobs.is_tool(vscode))
        self.assertFalse(jobs.is_tool("python build_report.py"))

    def test_only_processes_the_worker_started_are_its_jobs(self):
        # A browser or server someone else opened in the project folder looks like a detached job (new, in the
        # workspace, parent is init) but lacks the ORCH_JOB tag every process of the worker's turn inherits.
        from orch import jobs
        from unittest import mock
        import os
        ws = str(Path(tempfile.mkdtemp()).resolve())
        tag = jobs.tag_for("run1", "writer")
        py = [sys.executable, "-c", "import time; time.sleep(30)"]
        mine = subprocess.Popen(py, cwd=ws, env={**os.environ, jobs.TAG_VAR: tag})
        other = subprocess.Popen(py, cwd=ws, env={k: v for k, v in os.environ.items() if k != jobs.TAG_VAR})
        bare = subprocess.Popen(["/bin/sleep", "30"], cwd=ws, env={**os.environ, jobs.TAG_VAR: tag})
        try:
            time.sleep(0.5)
            before = {10: (1, "launchd")}
            after = {**before, mine.pid: (1, "python mine"), other.pid: (1, "python other"),
                     bare.pid: (1, "/bin/sleep 30")}
            with mock.patch.object(jobs, "cwds", lambda pids: {p: ws for p in pids}):
                found = {j["pid"] for j in jobs.detect(before, [ws], after=after, tags=[tag])}
                untagged = {j["pid"] for j in jobs.detect(before, [ws], after=after)}
            self.assertIn(mine.pid, found)
            self.assertNotIn(other.pid, found)
            if bare.pid not in jobs.tag_check([bare.pid], [tag]):  # environment hidden (macOS system program)
                self.assertIn(bare.pid, found)                     # → judged the old way
            self.assertEqual(untagged, {mine.pid, other.pid, bare.pid})  # without tags: the old rule
        finally:
            for p in (mine, other, bare):
                p.kill()
                p.wait()

    AUDIT_REPLY = ("<points>\n1. The critic resends its whole checklist. Evidence: r001. Fix: send the change only.\n"
                   "2. Second point.\n3. Third point.\n</points>\n<findings>\n"
                   "- [repetition] the checklist is resent every round · seen 1× since 2026-11-09 · open\n</findings>")

    def _audited(self, auditor_opts, controller=None):
        m = managed(controller={"audit_every": 1, "audit_points": 2, **(controller or {})}, mgr={"milestones": 1})
        m["agents"].append({"name": "auditor", "role": "auditor", "backend_options": auditor_opts})
        return run(make_cfg(self.tmp, [m]))





    def test_the_digest_flags_a_resent_message_and_a_note_that_waited(self):
        from orch import audit
        from datetime import datetime
        # Inbox filenames use local time; event timestamps must describe that same timezone.
        def local_time(value):
            return datetime.fromisoformat(value).astimezone().isoformat()
        rd = self.tmp / "run"
        (rd / "rounds" / "r001").mkdir(parents=True)
        (rd / "inbox" / "consumed").mkdir(parents=True)
        big = "\n\n".join(f"Paragraph {i}: " + "the same long instruction, sent again " * 3 for i in range(60))
        for seq, (agent, role, inp, out, t0) in enumerate([
                ("w", "worker", big, "Done the first part, details follow." * 20, local_time("2026-11-09T10:00:00")),
                ("w", "worker", big, "ok", local_time("2026-11-09T10:20:00"))], 1):
            (rd / "rounds" / "r001" / f"t{seq:02d}-{agent}.json").write_text(json.dumps({
                "turn_id": f"r001-t{seq:02d}-{agent}", "round": 1, "agent": agent, "role": role, "purpose": "work",
                "status": "ok", "started_at": t0, "ended_at": t0.replace(":00:00", ":05:00").replace(":20:00", ":25:00"),
                "input": inp, "output": out, "system_prompt": "You are the worker."}))
        (rd / "inbox" / "consumed" / "20261109-090000-1-board-note.json").write_text(
            json.dumps({"to": ["w"], "message": "Please stop the sweep."}))
        (rd / "events.jsonl").write_text(json.dumps({"ts": local_time("2026-11-09T10:20:00"), "event": "human_messages",
                                                     "files": ["20261109-090000-1-board-note.json"]}) + "\n")
        md, st = audit.build(rd, 1, 1)
        kinds = {f["kind"] for f in st["flags"]}
        self.assertTrue({"repeated content", "short or failed reply", "note waited"} <= kinds, kinds)
        self.assertIn("waited 80 min", md)





    def _critic_codex_update(self, mode="steer"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # found whether run from the repo root or tests/
        from test_codex_stream import PEER
        peer = self.tmp / "peer"
        peer.write_text("#!" + sys.executable + "\n" + PEER)
        peer.chmod(0o700)
        log = self.tmp / "peer.jsonl"
        instruction = "Keep the current experiment running; also evaluate the missing independent cases."
        private = "You can track the results in the report."
        m = managed(mgr={"milestones": 1}, critic={"pass_after": 1, "replies": {"checkin": [
            f'<message to="you">{private}</message>\n<message to="writer">{instruction}</message>']}})
        m["agents"][2].update(backend="codex", timeout=8, retries=0,
            backend_options={"transport": "app-server", "executable": str(peer)},
            env={"PEER_MODE": mode, "PEER_LOG": str(log)})
        stop_drop = threading.Event()
        def drop():
            while not stop_drop.wait(0.02):
                if log.exists() and '"turn/start"' in log.read_text():
                    for p in (self.tmp / "out").glob("**/inbox"):
                        (p / "urgent.json").write_text(json.dumps({"to": ["critic"], "from": "boss",
                            "urgent": True, "message": "I want independent cases run concurrently."}))
                        return
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        try:
            status, rec = run(make_cfg(self.tmp, [m]))
        finally:
            stop_drop.set()
            th.join(2)
        self.assertEqual(status, "completed")
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(Path(str(log) + '.tool').read_text(), 'finished naturally')
        return rec, calls, instruction, private




    def test_worker_message_queue_survives_restart_without_replaying_uncertain_sends(self):
        from orch.recorder import Recorder
        mon = load_config(make_cfg(self.tmp, [managed()])).monitor("m")
        rec = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        a = rec.queue_worker_message("writer", "Keep working.", "r001-t02-critic", "critic")
        rec.queue_worker_message("writer", "Keep working.", "r001-t02-critic", "critic")
        self.assertEqual(len(rec.pending_worker_messages("writer")), 1)
        rec.worker_message_status(a["id"], "sending", delivery_turn="r001-t01-writer")
        b = rec.queue_worker_message("writer", "Add a case.", "r001-t03-critic", "critic")
        rec.worker_message_status(b["id"], "deferred", delivery_turn="r001-t01-writer")
        self.assertEqual(rec.pending_worker_messages("writer", "r001-t01-writer"), [])
        restarted = Recorder(rec.run_dir, rec.output_root)
        restarted.recover_worker_messages()
        self.assertEqual(restarted.state["worker_messages"][0]["status"], "unknown")
        self.assertEqual([m["id"] for m in restarted.pending_worker_messages("writer")], [b["id"]])
        restarted.worker_message_status(b["id"], "accepted")
        self.assertEqual(restarted.pending_worker_messages("writer"), [])


    def test_the_manager_can_send_an_urgent_message_from_the_chat(self):
        from orch import tags as T
        a, body = T.blocks('<message to="Jordan" urgent="yes">Stop now.</message>', "message")[0]
        self.assertTrue(T.urgent(a))
        self.assertEqual(body, "Stop now.")
        self.assertFalse(T.urgent(T.blocks('<message to="Jordan">x</message>', "message")[0][0]))





























    def test_managed_validation(self):
        m = managed()
        m["agents"] = m["agents"][1:]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))


class TestProjectFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_init_layout_settings_split_and_default_prompts(self):
        self.assertEqual(cli.main(["init", str(self.tmp), "--project"]), 0)
        d = self.tmp / ".orch"
        for f in (".gitignore", "agents/manager/agent.yaml", "agents/manager/goal.md", "agents/worker/agent.yaml"):
            self.assertTrue((d / f).is_file(), f)
        self.assertFalse(list(d.rglob("preference.md")) + list(d.rglob("system_prompt.md")))  # DEL: no copies
        self.assertEqual([p.name for p in d.glob("*.yaml")], ["settings.yaml"])  # MOD-4: the model list
        self.assertFalse(list(d.rglob("task.md")))
        cfg = load_config(str(self.tmp))  # the project root finds .orch/agents
        mon = cfg.monitors[0]
        self.assertEqual(mon.name, self.tmp.name.strip("-.") or mon.name)
        self.assertEqual((mon.workspace, cfg.output_dir), (str(self.tmp.resolve()), str((d / "runs").resolve())))
        c = mon.controller
        self.assertEqual((c.mode, c.approve_plan, c.checklist, c.milestone_rounds, mon.max_rounds), ("managed", True, True, 50, 400))
        self.assertTrue(mon.goal.startswith("Describe what you want done"))
        self.assertEqual([(a.name, a.role) for a in mon.agents], [("manager", "manager"), ("critic", "verifier"), ("worker", "worker"),
                                                                ("auditor", "auditor"), ("monitor", "monitor")])
        mo = mon.agent("monitor")  # B5: a small read-only watcher; reports only to the critic
        self.assertEqual((mo.backend, mo.backend_options["model"], mo.check_session), ("opencode", "alibaba-token-plan-cn/qwen3.8-flash", "own"))
        self.assertEqual([f["backend_options"]["model"] for f in mo.fallback], ["zenmux/deepseek/deepseek-v4.1-flash"])
        self.assertIn("Edit", mo.backend_options["disallowed_tools"])
        self.assertIn("Quick check", mo.current_system_prompt())
        self.assertNotRegex(mo.current_system_prompt().lower().replace("`.orch/`", ""), r"manager|orch\b")
        self.assertEqual((mon.agent("worker").turn_limit(True), mon.agent("worker").check_every), (0.0, 20.0))
        aud = mon.agent("auditor")
        self.assertFalse((d / "agents/auditor/system_prompt.md").exists())  # orch's own prompt: it audits orch
        self.assertIn("You are the auditor", aud.current_system_prompt())
        self.assertEqual((mon.controller.audit_every, mon.controller.audit_points), (10, 5))
        self.assertIn("Bash", aud.backend_options["disallowed_tools"])
        critic = mon.agent("critic")  # PRM-2: Orch's own prompt for the critic, never a project copy
        self.assertEqual(Path(critic.system_prompt_path), ROOT / "orch/runtime/prompts/critic.md")

    def test_conflicting_workflow_setting_and_run(self):
        cli.main(["init", str(self.tmp), "--project"])
        d = self.tmp / ".orch"
        with open(d / "agents/worker/agent.yaml", "a") as f:
            f.write("max_rounds: 3\n")
        with self.assertRaises(ConfigError):
            load_config(str(self.tmp))
        for name, extra in (("manager", "approve_plan: false\napprove_milestones: false\nmax_rounds: 20\n"),
                            ("critic", ""), ("worker", ""), ("auditor", ""), ("monitor", "")):
            y = d / "agents" / name / "agent.yaml"
            text = y.read_text().replace("approve_plan: true\n", "") \
                .replace("approve_milestones: true\n", "").replace("max_rounds: 20 ", "# ").replace("max_rounds: 3\n", "")
            text = re.sub(r"(?m)^backend: \w+", "backend: mock", text)
            y.write_text(text + extra)
        import yaml
        st = yaml.safe_load((d / "settings.yaml").read_text())  # MOD-4: agents name models of settings.yaml
        st["models"] = {k: {"backend": "mock", "backend_options": {}} for k in st["models"]}
        (d / "settings.yaml").write_text(yaml.safe_dump(st))
        self.assertTrue(all(a.backend == "mock" for a in load_config(str(self.tmp)).monitors[0].agents))




class TestWaiterStop(unittest.TestCase):
    """A silent agent whose turn sits in a waiter loop has the loop stopped, and the turn ends."""

    def test_a_silent_waiter_loop_is_stopped_and_the_turn_ends(self):
        import os
        from unittest import mock
        from orch import backends
        script = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done & wait"
        with mock.patch.object(backends, "WAITER_GRACE", 0.5), mock.patch.object(backends, "WAITER_CHECK", 0.2):
            start = time.monotonic()
            rc, out, err, status = backends.run_process(["sh", "-c", script], None, ".", 0, {}, lambda: False,
                                                        on_line=lambda line: None)
        self.assertLess(time.monotonic() - start, 30)
        self.assertIn("stopped a waiter loop", err)






class TestMessageRoutes(unittest.TestCase):
    """tags.routes: where each <message to="…"> goes, and why a block can't go anywhere."""

    def setUp(self):
        from types import SimpleNamespace as NS
        self.agents = [NS(name="critic", called="The critic", role="verifier"),
                       NS(name="worker", called="The worker", role="worker"),
                       NS(name="manager", called="The manager", role="manager"),
                       NS(name="auditor", called="The auditor", role="auditor")]

    def test_a_block_to_another_agent_is_routed_with_its_urgent_flag(self):
        from orch import tags as T
        text = '<message to="The worker" urgent="yes">Stop the job.</message> and <message to="manager">Ack.</message>'
        sent, problems = T.routes(text, "critic", self.agents)
        self.assertEqual([(t.name, b, u) for t, b, u in sent],
                         [("worker", "Stop the job.", True), ("manager", "Ack.", False)])
        self.assertEqual(problems, [])

    def test_the_role_words_reach_the_only_agent_of_that_role(self):
        from orch import tags as T
        sent, problems = T.routes('<message to="worker">Do X.</message>', "critic", self.agents)
        self.assertEqual([t.name for t, _b, _u in sent], ["worker"])
        self.assertEqual(problems, [])

    def test_a_block_addressed_to_the_sender_is_reported_not_sent(self):
        # the round-79 case: the critic addressed its own instructions to "The critic"
        from orch import tags as T
        sent, problems = T.routes('<message to="The critic">Diagnosis accepted.</message>', "critic", self.agents)
        self.assertEqual(sent, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("addressed to the sender itself", problems[0])

    def test_unknown_empty_and_auditor_blocks_are_reported(self):
        from orch import tags as T
        text = ('<message to="the team">x</message><message to="worker">  </message>'
                '<message to="auditor">y</message>')
        sent, problems = T.routes(text, "critic", self.agents)
        self.assertEqual(sent, [])
        self.assertEqual(len(problems), 3)
        self.assertIn("names no agent", problems[0])
        self.assertIn("is empty", problems[1])
        self.assertIn("auditor", problems[2])

    def test_a_block_to_the_human_is_skipped_silently(self):
        from orch import tags as T
        sent, problems = T.routes('<message to="you">Answer.</message>', "critic", self.agents)
        self.assertEqual((sent, problems), ([], []))




class TestAuditFixes(unittest.TestCase):
    """Cost basis, a gate-held manager message, and the audit digest's view of relays, sessions and notes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_cost_is_an_estimate_behind_another_providers_endpoint_or_for_an_unknown_model(self):
        from orch.report import cost_basis, endpoint_host, turn_cost_basis, turn_model
        self.assertEqual(endpoint_host({"ANTHROPIC_BASE_URL": "https://zenmux.ai/api/anthropic", "X_TOKEN": "s"}), "zenmux.ai")
        self.assertEqual(endpoint_host({}), "")
        self.assertEqual(cost_basis("claude", ["opus"]), "reported")
        self.assertEqual(cost_basis("claude", ["claude-sonnet-5-5"], "api.anthropic.com"), "reported")
        self.assertEqual(cost_basis("claude", ["opus"], "zenmux.ai"), "estimate")  # "opus" mapped to another model
        self.assertEqual(cost_basis("claude", ["deepseek/deepseek-v4.1-flash"]), "estimate")
        self.assertEqual(cost_basis("codex", ["gpt-6-astra"]), "reported")
        old = {"backend": "claude", "stderr": '[claude-code:unrecognized_model] {"model":"deepseek/deepseek-v4.1-flash"}'}
        self.assertEqual((turn_cost_basis(old), turn_model(old)), ("estimate", "deepseek/deepseek-v4.1-flash"))
        self.assertEqual(turn_model({"model": "opus", "meta": {"models": ["claude-opus-5-5"]}}), "claude-opus-5-5")

    def test_the_claude_backend_keeps_token_counts_and_the_models_that_answered(self):
        from orch.backends import ClaudeBackend, TurnRequest
        fake = self.tmp / "claude"
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho '" + json.dumps(
            {"type": "result", "result": "hi", "session_id": "s1", "total_cost_usd": 1.5,
             "usage": {"input_tokens": 10, "output_tokens": 20, "service_tier": "standard"},
             "modelUsage": {"deepseek/deepseek-v4.1-flash": {"inputTokens": 10}}}) + "'\n")
        fake.chmod(0o755)
        res = ClaudeBackend().run(TurnRequest("w", "worker", "go", "", None, str(self.tmp), 30,
                                              {"executable": str(fake), "output_format": "json"}), lambda: False)
        self.assertEqual(res.meta["usage"], {"input_tokens": 10, "output_tokens": 20})
        self.assertEqual(res.meta["models"], ["deepseek/deepseek-v4.1-flash"])

    def test_the_run_keeps_the_estimated_part_of_its_cost_apart(self):
        from orch.recorder import Recorder
        mon = load_config(make_cfg(self.tmp, [managed()])).monitor("m")
        rec = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        for i, (basis, cost) in enumerate([("estimate", 5.0), ("reported", 0.5)], 1):
            rec.write_turn({"turn_id": f"r001-t0{i}-x", "round": 1, "seq": i, "agent": f"a{i}", "status": "ok",
                            "session_id": f"s{i}", "cost_basis": basis, "meta": {"total_cost_usd": cost}})
        self.assertEqual((rec.state["cost_usd"], rec.state["cost_estimate_usd"]), (5.5, 5.0))


    def test_the_digest_measures_relays_against_what_orch_passes_on(self):
        from orch.audit import passable
        critic = {"agent": "critic", "role": "verifier",
                  "output": "Fix the table.\n\n<checklist>\n- [ ] a\n- [ ] b\n</checklist>\n<verdict>FAIL</verdict>"
                            '\n<message to="you">Long answer for the human.</message>'}
        text, withheld = passable(critic, {"agent": "worker", "role": "worker"})
        self.assertEqual(text, "Fix the table.")
        self.assertEqual(set(withheld), {"checklist", "verdict", "message to you"})
        mgr = {"agent": "boss", "role": "manager", "output": 'Report for you.\n<message to="critic">Do X.</message>'}
        text, withheld = passable(mgr, {"agent": "critic", "role": "verifier"})
        self.assertEqual((text, withheld), ("Do X.", {"reply to the human": len("Report for you.")}))

    def test_the_digest_names_a_notes_sender_and_shows_the_session(self):
        from orch.audit import build
        rd = self.tmp / "run"
        (rd / "rounds" / "r001").mkdir(parents=True)
        (rd / "inbox" / "consumed").mkdir(parents=True)
        note = "Approved: run the revised labeller prompt as a re-pilot on twenty disjoint traces, and freeze it first."
        (rd / "inbox" / "consumed" / "20260101-095900-board-note.json").write_text(
            json.dumps({"to": ["writer"], "message": note, "from": "critic"}))
        (rd / "rounds" / "r001" / "t01-writer.json").write_text(json.dumps(
            {"turn_id": "r001-t01-writer", "round": 1, "agent": "writer", "role": "worker", "purpose": "work",
             "status": "ok", "started_at": "2026-01-01T10:00:00", "ended_at": "2026-01-01T10:01:00",
             "resumed_session": True, "session_turn": 7, "input": note, "output": "Done." * 100}))
        (rd / "rounds" / "r001" / "t02-critic.json").write_text(json.dumps(
            {"turn_id": "r001-t02-critic", "round": 1, "agent": "critic", "role": "verifier", "purpose": "verify",
             "status": "ok", "started_at": "2026-01-01T10:02:00", "ended_at": "2026-01-01T10:03:00",
             "input": "x", "output": "ok"}))
        md, stats = build(rd, 1, 1)
        self.assertIn("| same session, turn 7 |", md)
        self.assertIn("note from critic", md)
        self.assertFalse([f for f in stats["flags"] if f["kind"] == "unexpected origin"])


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _w1(self, rec):
        return [t for t in turns(rec) if t["agent"] == "w1"]

    def test_config_merges_backups_over_the_primary(self):
        m = pipeline()
        m["agents"][0].update(backend="claude", backend_options={"model": "opus", "effort": "high"},
                              env={"ANTHROPIC_BASE_URL": "https://a"}, retries=2,
                              fallback=["sonnet", {"backend_options": {"effort": None}, "env": {}},
                                        {"backend": "agy", "backend_options": {"model": "gemini-x"}, "retries": 0}])
        a = load_config(make_cfg(self.tmp, [m])).monitor("m").agent("w1")
        v = a.variants()
        self.assertEqual([x.label for x in v], ["claude:opus", "claude:sonnet", "claude:opus", "agy:gemini-x"])
        self.assertEqual(v[1].options, {"model": "sonnet", "effort": "high"})
        self.assertEqual(v[1].env, {"ANTHROPIC_BASE_URL": "https://a"})
        self.assertEqual(v[1].session_key, v[0].session_key)
        self.assertEqual(v[2].options, {"model": "opus"})
        self.assertNotEqual(v[2].session_key, v[0].session_key)  # another endpoint: a new session
        self.assertEqual((v[3].options, v[3].env, v[3].retries, v[2].retries), ({"model": "gemini-x"}, {}, 0, 2))
        self.assertEqual(a.public()["fallback"][0]["env"], ["ANTHROPIC_BASE_URL"])  # no env values in logs

    def test_bad_backup_is_a_config_error(self):
        m = pipeline()
        m["agents"][0]["fallback"] = [{"backend": "nope"}]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))
        m["agents"][0]["fallback"] = [{"modle": "x"}]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))




    def test_audit_helper_tries_backups(self):
        from orch.backends import TurnRequest, run_variants
        m = pipeline()
        m["agents"][0].update(backend_options={"fail_turns": [1]}, fallback=[{"backend_options": {"fail_turns": []}}])
        a = load_config(make_cfg(self.tmp, [m])).monitor("m").agent("w1")
        selected = []
        res = run_variants(a, TurnRequest("w1", "worker", "hi", "", None, str(self.tmp), 10, {}), lambda: False,
                           on_model=selected.append)
        self.assertEqual(res.status, "ok")
        self.assertEqual(len(res.meta["models_tried"]), 2)
        self.assertEqual(selected, res.meta["models_tried"])


class TestAgy(unittest.TestCase):
    def test_command(self):
        from orch.backends import AgyBackend, TurnRequest
        req = TurnRequest("a", "worker", "hi", "sys", "c-1", ".", 10,
                          {"model": "gemini-3.6-pro", "effort": "high", "mode": "auto", "add_dirs": ["/x"],
                           "dangerously_skip_permissions": True})
        cmd = AgyBackend().build_command(req)
        self.assertEqual(cmd[:5], ["agy", "--output-format", "json", "--conversation", "c-1"])
        for part in (["--model", "gemini-3.6-pro"], ["--effort", "high"], ["--mode", "auto"], ["--add-dir", "/x"]):
            self.assertIn(part[1], cmd[cmd.index(part[0]):])
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_run_with_a_fake_agy(self):
        from orch.backends import AgyBackend, TurnRequest
        d = Path(tempfile.mkdtemp())
        exe = d / "agy"
        exe.write_text("#!/bin/sh\necho '{\"type\":\"init\",\"conversation_id\":\"conv-9\"}'\n"
                       "echo '{\"type\":\"result\",\"result\":\"done: '\"$#\"' args\"}'\n")
        exe.chmod(0o755)
        res = AgyBackend().run(TurnRequest("a", "worker", "hello", "be brief", None, str(d), 10,
                                           {"executable": str(exe)}), lambda: False)
        self.assertEqual((res.status, res.session_id), ("ok", "conv-9"))
        self.assertTrue(res.output.startswith("done"))
        self.assertIn("<prompt>", res.command)  # the prompt (with the system prompt in front) is not logged

    def test_unparsable_output_is_an_error(self):
        from orch.backends import AgyBackend, TurnRequest
        res = AgyBackend().run(TurnRequest("a", "worker", "hi", "", None, ".", 10,
                                           {"executable": "echo"}), lambda: False)
        self.assertEqual(res.status, "error")


FAKE_QWEN = r'''#!/usr/bin/env python3
"""Replays Qwen Code 0.28's headless output: stream-json lines, or one json array."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_QWEN_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "stdin": sys.stdin.read()}) + "\n")
sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
def ev(**k):
    return {"session_id": sid, **k}
def msg(content, usage):
    return ev(type="assistant", parent_tool_use_id=None, message={"role": "assistant", "model": "qwen4-max",
                                                                 "content": content, "usage": usage})
events = [
    ev(type="system", subtype="init", tools=["read_file"], model="qwen4-max"),
    ev(type="stream_event", parent_tool_use_id=None, event={"type": "goal_state"}),
    ev(type="stream_event", parent_tool_use_id=None, event={"type": "message_start"}),
    ev(type="stream_event", parent_tool_use_id=None,
       event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
    msg([{"type": "tool_use", "name": "read_file", "input": {"file_path": "a.py"}}],
        {"input_tokens": 9000, "output_tokens": 20}),
    ev(type="stream_event", parent_tool_use_id=None,
       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}}),
    msg([{"type": "text", "text": "done"}], {"input_tokens": 9500, "output_tokens": 10}),
    # the result's usage is the session's running total once resumed
    ev(type="result", subtype="success", is_error=False, num_turns=2, result="done",
       usage={"input_tokens": 50000, "output_tokens": 300}),  # stream-json's result has no `stats`
]
if "stream-json" in args:
    for e in events:
        print(json.dumps(e))
else:
    print(json.dumps(events))
'''


class TestQwen(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.exe = self.d / "qwen"
        self.exe.write_text(FAKE_QWEN.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.exe.chmod(0o755)
        self.log = self.d / "calls.jsonl"

    def req(self, sid=None, opts=None, on_event=None, prompt="hello"):
        from orch.backends import TurnRequest
        return TurnRequest("q", "worker", prompt, "be brief", sid, str(self.d), 20,
                           {"executable": str(self.exe), **(opts or {})}, env={"FAKE_QWEN_LOG": str(self.log)},
                           on_event=on_event)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_command_drops_claude_only_flags_and_maps_the_rest(self):
        from orch.backends import QwenBackend
        cmd, sid = QwenBackend().build_command(self.req(opts={
            "model": "qwen4-max", "permission_mode": "acceptEdits", "effort": "low", "max_turns": 3,
            "disable_hooks": True, "allowed_tools": ["read_file", "glob"], "disallowed_tools": "run_shell_command",
            "add_dirs": ["/x"]}, on_event=lambda e: None))
        self.assertEqual(cmd[1:5], ["-p", "--output-format", "stream-json", "--include-partial-messages"])
        for flag in ("--verbose", "--effort", "--permission-mode", "--max-turns", "--settings", "--allowedTools"):
            self.assertNotIn(flag, cmd)
        self.assertIn(["--session-id", sid], [cmd[i:i + 2] for i in range(len(cmd))])
        joined = " ".join(cmd)
        for part in ("--append-system-prompt be brief", "--model qwen4-max", "--approval-mode auto-edit",
                     "--allowed-tools read_file --allowed-tools glob", "--exclude-tools run_shell_command",
                     "--include-directories /x"):
            self.assertIn(part, joined)
        cmd, _ = QwenBackend().build_command(self.req(sid="s-1", opts={"approval_mode": "yolo", "stream": False},
                                                      on_event=lambda e: None))
        self.assertIn("json", cmd[cmd.index("--output-format") + 1])
        self.assertNotIn("stream-json", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-1")
        self.assertEqual(cmd[cmd.index("--approval-mode") + 1], "yolo")

    def test_streamed_turn(self):
        from orch.backends import QwenBackend
        events = []
        res = QwenBackend().run(self.req(on_event=events.append), lambda: False)
        self.assertEqual((res.status, res.output), ("ok", "done"))
        self.assertEqual(res.session_id, self.calls()[0]["args"][self.calls()[0]["args"].index("--session-id") + 1])
        self.assertEqual(self.calls()[0]["stdin"], "hello")
        kinds = [(e["k"], e["s"]) for e in events]
        self.assertEqual(kinds[0], ("session", res.session_id))  # known before the reply, for interrupted turns
        self.assertIn(("tool", "read_file: a.py"), kinds)
        self.assertIn(("text", "done"), kinds)
        self.assertNotIn("hmm", [s for _, s in kinds])  # thinking is not the reply
        self.assertEqual(res.meta["usage"], {"input_tokens": 18500, "output_tokens": 30})  # this turn's own
        self.assertEqual(res.meta["session_usage"]["input_tokens"], 50000)
        self.assertEqual(res.meta["models"], ["qwen4-max"])
        self.assertNotIn("total_cost_usd", res.meta)

    def test_json_turn_resumes_and_compacts_with_compress(self):
        from orch.backends import QwenBackend
        res = QwenBackend().run(self.req(sid="s-9"), lambda: False)
        self.assertEqual((res.status, res.output, res.session_id), ("ok", "done", "s-9"))
        self.assertEqual(res.meta["usage"]["input_tokens"], 18500)
        QwenBackend().compact(self.req(sid="s-9", opts={"compact_instructions": "keep the plan"}), lambda: False)
        self.assertEqual(self.calls()[-1]["stdin"], "/compress keep the plan")

    def test_config_accepts_qwen(self):
        from orch.backends import BACKENDS, QwenBackend
        self.assertIsInstance(BACKENDS["qwen"], QwenBackend)
        self.assertEqual(BACKENDS["qwen"].name, "qwen")


FAKE_OPENCODE = r'''#!/usr/bin/env python3
"""Replays OpenCode 2.3.2: `run --format json` events (with --standalone, or against a server); `serve`, a server
with the live event stream, steering (POST .../prompt, delivery steer), wait and the message list; and the `api`
calls used to compact a session. A run against a server reports its live events to the server (/_emit), which
broadcasts them, and takes steered messages from it (/_inbox) between its two steps, as OpenCode does."""
import base64, json, os, sys, threading, time, urllib.request
args = sys.argv[1:]
with open(os.environ["FAKE_OC_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "stdin": "" if args[0] in ("api", "serve") else sys.stdin.read(),
                        "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
                        "password": os.environ.get("OPENCODE_SERVER_PASSWORD")}) + "\n")
mode = os.environ.get("FAKE_OC_MODE", "ok")
if args[0] == "serve":
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    pw = os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    lock = threading.Lock()
    subs, inbox, pending = [], [], []   # event subscribers; steered texts; admitted, not yet delivered

    def broadcast(ev):
        with lock:
            for q in subs:
                q.append(ev)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def reply(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authed(self):
            want = "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()
            if self.headers.get("Authorization") != want:
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if self.path == "/_pending":  # from the fake run
                with lock:
                    return self.reply(200, list(pending))
            if self.path == "/_inbox":
                with lock:
                    out, pending[:] = list(pending), []
                return self.reply(200, out)
            if not self.authed():
                return
            if self.path == "/api/event":
                q = []
                with lock:
                    subs.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"server.connected","data":{}}\n\n')
                self.wfile.flush()
                while True:
                    with lock:
                        out, q[:] = list(q), []
                    for ev in out:
                        self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(0.01)
            elif self.path.endswith("/message"):
                self.reply(200, {"data": [{"id": "msg_f", "type": "assistant", "content": [
                    {"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "Answered late: " + inbox[-1]}]}
                    if inbox else {"id": "m2", "type": "assistant", "content": []}]})
            else:
                self.reply(404, {})

        def do_POST(self):
            if self.path == "/_emit":  # from the fake run: what the real server would broadcast
                broadcast(self.body())
                return self.reply(200, {})
            if not self.authed():
                return
            if self.path.endswith("/prompt"):
                b = self.body()
                with lock:
                    inbox.append(b["text"])
                    iid = f"msg_steer{len(inbox)}"
                    pending.append({"id": iid, "text": b["text"], "delivery": b.get("delivery")})
                self.reply(200, {"data": {"id": iid, "type": "user", "payload": {"text": b["text"]},
                                          "delivery": b.get("delivery")}})
            elif self.path.endswith("/wait"):
                with lock:
                    late, pending[:] = list(pending), []
                sid = self.path.split("/")[4]
                for item in late:  # the server answers what the run left undelivered
                    broadcast({"type": "session.inbox.delivered", "data": {"sessionID": sid, "inboxID": item["id"]}})
                    broadcast({"type": "session.step.ended", "data": {
                        "sessionID": sid, "assistantMessageID": "msg_f",
                        "tokens": {"input": 7, "output": 3, "reasoning": 0, "cache": {"read": 100, "write": 0}}}})
                time.sleep(0.1)
                self.reply(200, {})
            else:
                self.reply(404, {})

    ThreadingHTTPServer(("127.0.0.1", int(args[args.index("--port") + 1])), H).serve_forever()
if args[0] == "api":
    path = next(a for a in args if a.startswith("/"))
    if path.endswith("/compact"):
        print(json.dumps({"data": {"id": "msg_c1", "type": "compaction", "payload": {}, "delivery": "steer"}}))
    elif path.endswith("/wait"):
        print("")
    else:
        status = "failed" if mode == "compact_fails" else "completed"
        print(json.dumps({"data": [{"id": "msg_i", "type": "idle"},
                                   {"id": "msg_c1", "type": "compaction", "status": status, "summary": "## Objective"},
                                   {"id": "msg_u", "type": "user", "text": "hi"}], "cursor": None}))
    sys.exit(0)
server = args[args.index("--server") + 1] if "--server" in args else None


def http(method, path, body=None):
    req = urllib.request.Request(server + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
        return json.loads(r.read() or b"null")


def live(kind, **data):
    if server:
        http("POST", "/_emit", {"type": kind, "data": {"sessionID": sid, **data}})


sid = args[args.index("--session") + 1] if "--session" in args else "ses_new1"


def ev(kind, msg, **part):
    return {"type": kind, "timestamp": 1, "sessionID": sid, "part": {"sessionID": sid, "messageID": msg, **part}}


def out(e):
    print(json.dumps(e), flush=True)


if mode == "error":
    out({"type": "error", "timestamp": 1, "sessionID": sid, "error": {"type": "unknown", "message": 'Agent not found: "x"'}})
    sys.exit(1)


def step(msg, text, tokens, reason, tool=False):
    out(ev("step_start", msg, type="step-start"))
    live("session.step.started", assistantMessageID=msg, model={"id": "m", "providerID": "p", "variant": "high"})
    live("session.text.started", assistantMessageID=msg, ordinal=0)
    words = text.split(" ")
    for i, w in enumerate(words if mode != "lossy" else words[:-1]):  # lossy: a slow stream lost the last delta
        live("session.text.delta", assistantMessageID=msg, ordinal=0, delta=(w if i == 0 else " " + w))
    live("session.text.ended", assistantMessageID=msg, ordinal=0, text=text)
    time.sleep(0.2 if server else 0)  # the server's stream runs ahead of the run's own output, as in OpenCode
    out(ev("text", msg, type="text", text=text))
    if tool:
        out(ev("tool_use", msg, type="tool", tool="shell",
               state={"status": "completed", "input": {"command": "echo probe123"}, "output": "probe123\n"}))
    live("session.step.ended", assistantMessageID=msg, tokens=tokens)
    out(ev("step_finish", msg, type="step-finish", reason=reason, cost=0.001 if tool else 0.002, tokens=tokens))


step("m1", "Let me check.", {"input": 52, "output": 34, "reasoning": 45, "cache": {"read": 8398, "write": 0}},
     "tool-calls", tool=True)
if mode == "cut":
    sys.exit(0)
final = "Output: probe123"
if mode == "steer":  # the tool runs a while; a message steered meanwhile is delivered before the next step
    end = time.time() + 8
    while time.time() < end:
        got = http("GET", "/_inbox")
        if got:
            live("session.inbox.delivered", inboxID=got[0]["id"])
            final = "Steered: " + got[0]["text"]
            break
        time.sleep(0.05)
step("m2", final, {"input": 103, "output": 27, "reasoning": 23, "cache": {"read": 8444, "write": 10}}, "stop")
if mode == "late":  # a message admitted as the run ends: the run returns before it is delivered
    end = time.time() + 8
    while time.time() < end and not http("GET", "/_pending"):
        time.sleep(0.05)
'''


class TestOpenCode(unittest.TestCase):
    USAGE = {"input_tokens": 155, "output_tokens": 61, "reasoning_tokens": 68,
             "cache_read_input_tokens": 16842, "cache_creation_input_tokens": 10}

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.exe = self.d / "opencode"
        self.exe.write_text(FAKE_OPENCODE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.exe.chmod(0o755)
        self.log = self.d / "calls.jsonl"

    def req(self, sid=None, opts=None, on_event=None, mode="ok", prompt="hello", poll=None, on_message=None):
        from orch.backends import TurnRequest
        return TurnRequest("oc", "worker", prompt, "be brief", sid, str(self.d), 30,
                           {"executable": str(self.exe), **(opts or {})},
                           env={"FAKE_OC_LOG": str(self.log), "FAKE_OC_MODE": mode}, on_event=on_event,
                           poll_messages=poll, on_message=on_message)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def runs(self):
        return [c for c in self.calls() if c["args"][0] == "run"]

    @staticmethod
    def pairs(events):
        return [(e["k"], e.get("s")) for e in events]

    def test_unwatched_turn_runs_standalone(self):
        from orch.backends import OpenCodeBackend
        res = OpenCodeBackend().run(self.req(opts={"model": "p/m#high"}), lambda: False)
        self.assertEqual((res.status, res.output, res.session_id), ("ok", "Output: probe123", "ses_new1"))
        self.assertEqual([c["args"][0] for c in self.calls()], ["run"])  # no server for a turn nobody watches
        call = self.calls()[0]
        self.assertEqual(call["args"][:5], ["run", "--standalone", "--format", "json", "--archived"])
        self.assertIn("--model p/m#high", " ".join(call["args"]))
        self.assertNotIn("--agent", call["args"])
        self.assertIn("--auto", call["args"])  # on by default: no one answers a permission that asks
        self.assertIsNone(call["config"])  # nothing to configure
        self.assertTrue(call["stdin"].startswith("<system_instructions>\nbe brief"))
        self.assertTrue(call["stdin"].endswith("hello"))
        self.assertEqual(res.meta["usage"], self.USAGE)
        self.assertAlmostEqual(res.meta["total_cost_usd"], 0.003)
        self.assertEqual((res.meta["models"], res.meta["num_turns"]), (["p/m#high"], 2))
        self.assertNotIn("transport", res.meta)


    def test_stream_gaps_are_filled_from_the_full_text(self):
        from orch.backends import OpenCodeBackend
        events = []
        res = OpenCodeBackend().run(self.req(on_event=events.append, mode="lossy"), lambda: False)
        self.assertEqual(res.output, "Output: probe123")
        self.assertEqual("".join(e["s"] for e in events if e["k"] == "text"), "Let me check.Output: probe123")

    def test_message_steered_into_the_running_turn(self):
        from orch.backends import OpenCodeBackend
        notes = [{"id": "wm1", "message": "Also add the missing case."}]
        statuses = []
        events = []
        res = OpenCodeBackend().run(self.req(
            mode="steer", on_event=events.append,
            poll=lambda: [n for n in notes if n["id"] not in [s[0] for s in statuses]],
            on_message=lambda i, s, d: statuses.append((i, s, d.get("via")))), lambda: False)
        self.assertEqual((res.status, res.output), ("ok", "Steered: Also add the missing case."))
        self.assertEqual(statuses, [("wm1", "sending", "steer"), ("wm1", "accepted", "steer")])
        self.assertEqual(res.meta["steered"], 1)
        self.assertNotIn("steer_followup", res.meta)
        self.assertIn(("tool", "Additional instruction accepted; current tools continue."), self.pairs(events))
        self.assertEqual(len(self.runs()), 1)  # the same run went on: never interrupted or restarted

    def test_message_admitted_as_the_run_ends_is_answered_before_the_turn_returns(self):
        from orch.backends import OpenCodeBackend
        notes = []
        statuses = []

        def on_event(ev):
            if ev["k"] == "text" and "probe123" in ev.get("s", "") and not notes:
                notes.append({"id": "wm2", "message": "Reply again."})
        res = OpenCodeBackend().run(self.req(
            mode="late", on_event=on_event,
            poll=lambda: [n for n in notes if n["id"] not in [s[0] for s in statuses]],
            on_message=lambda i, s, d: statuses.append((i, s))), lambda: False)
        self.assertEqual((res.status, res.output), ("ok", "Answered late: Reply again."))
        self.assertTrue(res.meta["steer_followup"])
        self.assertEqual(statuses, [("wm2", "sending"), ("wm2", "accepted")])
        self.assertEqual(res.meta["usage"]["input_tokens"], 155 + 7)  # the follow-up step counts too

    def test_unwatched_worker_turn_still_takes_messages(self):
        from orch.backends import OpenCodeBackend
        res = OpenCodeBackend().run(self.req(poll=lambda: [], on_message=lambda *a: None), lambda: False)
        self.assertEqual(res.status, "ok")
        self.assertEqual(self.calls()[0]["args"][0], "serve")

    def test_transport_run_and_a_server_that_does_not_start(self):
        from orch.backends import OpenCodeBackend
        events = []
        res = OpenCodeBackend().run(self.req(opts={"transport": "run"}, on_event=events.append), lambda: False)
        self.assertEqual(res.output, "Output: probe123")
        self.assertEqual([c["args"][0] for c in self.calls()], ["run"])
        self.assertIn(("text", "Let me check."), self.pairs(events))  # a part at a time
        self.assertEqual(OpenCodeBackend().run(self.req(opts={"transport": "x"}), lambda: False).status, "error")
        broken = self.d / "broken"
        broken.write_text(f'#!/bin/sh\nif [ "$1" = serve ]; then echo boom >&2; exit 3; fi\nexec "{self.exe}" "$@"\n')
        broken.chmod(0o755)
        res = OpenCodeBackend().run(self.req(opts={"executable": str(broken)}, on_event=events.append), lambda: False)
        self.assertEqual((res.status, res.output), ("ok", "Output: probe123"))  # ran without the live transport
        self.assertIn("boom", res.meta["warning"])

    def test_stop_ends_the_turn_and_the_server(self):
        import socket
        from orch.backends import OpenCodeBackend
        t0 = time.monotonic()
        res = OpenCodeBackend().run(self.req(mode="steer", poll=lambda: [], on_message=lambda *a: None),
                                    lambda: time.monotonic() - t0 > 1.5)
        self.assertEqual(res.status, "stopped")
        self.assertLess(time.monotonic() - t0, 8)
        port = int(self.calls()[0]["args"][self.calls()[0]["args"].index("--port") + 1])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_resume_replace_mode_and_permissions(self):
        from orch.backends import OpenCodeBackend
        res = OpenCodeBackend().run(self.req(sid="ses_old", opts={
            "system_prompt_mode": "replace", "disallowed_tools": "webfetch,websearch", "add_dirs": ["/x"],
            "permission_mode": "bypassPermissions", "effort": "high", "config": {"compaction": {"auto": True}}},
            on_event=lambda e: None), lambda: False)
        self.assertEqual((res.status, res.session_id), ("ok", "ses_old"))
        serve, call = self.calls()[0], self.runs()[0]
        a = call["args"]
        self.assertEqual(a[a.index("--session") + 1], "ses_old")
        self.assertNotIn("--archived", a)
        self.assertEqual(a[a.index("--agent") + 1], "orch-worker")
        self.assertIn("--auto", a)
        self.assertEqual(call["stdin"], "hello")  # the system prompt is the agent's, every turn
        cfg = json.loads(serve["config"])  # the server runs the turn: it must know the agent
        self.assertEqual(cfg["agents"]["orch-worker"], {"mode": "primary", "system": "be brief"})
        self.assertEqual(cfg["permissions"], [
            {"action": "webfetch", "resource": "*", "effect": "deny"},
            {"action": "websearch", "resource": "*", "effect": "deny"},
            {"action": "external_directory", "resource": "/x/*", "effect": "allow"}])
        self.assertEqual(cfg["compaction"], {"auto": True})

    def test_auto_can_be_turned_off(self):
        from orch.backends import OpenCodeBackend
        cmd, _, _ = OpenCodeBackend().build(self.req(opts={"auto": False}))
        self.assertNotIn("--auto", cmd)

    def test_user_config_content_is_kept(self):
        from orch.backends import OpenCodeBackend
        r = self.req(opts={"disallowed_tools": ["shell"]})
        r.env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permissions": [{"action": "read", "resource": "*.env",
                                                                         "effect": "deny"}], "theme": "x"})
        _, env, _ = OpenCodeBackend().build(r)
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(cfg["theme"], "x")
        self.assertEqual([p["action"] for p in cfg["permissions"]], ["read", "shell"])

    def test_error_event_and_cut_off_turn(self):
        from orch.backends import OpenCodeBackend
        for watch in (None, lambda e: None):
            res = OpenCodeBackend().run(self.req(mode="error", on_event=watch), lambda: False)
            self.assertEqual((res.status, res.output), ("error", ""))
            self.assertIn("Agent not found", res.meta["error"])
            self.assertIn("Agent not found", res.stderr)
            self.assertTrue(res.raw)  # the event stream is kept apart, never the reply
            res = OpenCodeBackend().run(self.req(mode="cut", on_event=watch), lambda: False)
            self.assertEqual((res.status, res.output), ("ok", "Let me check."))

    def test_compact_goes_through_the_service_api(self):
        from orch.backends import OpenCodeBackend
        self.assertIsNone(OpenCodeBackend().compact(self.req(), lambda: False))
        res = OpenCodeBackend().compact(self.req(sid="ses_9"), lambda: False)
        self.assertEqual((res.status, res.output, res.session_id), ("ok", "## Objective", "ses_9"))
        self.assertTrue(res.meta["compacted"])
        calls = self.calls()
        self.assertEqual(calls[0]["args"][0], "serve")  # a private server, with the turn's own config
        port = calls[0]["args"][calls[0]["args"].index("--port") + 1]
        self.assertEqual([c["args"][1:3] for c in calls[1:]], [["--server", f"http://127.0.0.1:{port}"]] * 3)
        self.assertEqual([tuple(c["args"][3:5]) for c in calls[1:]],
                         [("POST", "/api/session/ses_9/compact"), ("POST", "/api/experimental/session/ses_9/wait"),
                          ("GET", "/api/session/ses_9/message")])
        OpenCodeBackend().compact(self.req(sid="ses_9", opts={"system_prompt_mode": "replace"}), lambda: False)
        serve = [c for c in self.calls() if c["args"][0] == "serve"][-1]
        self.assertIn("orch-worker", json.loads(serve["config"])["agents"])  # the session's agent is known
        res = OpenCodeBackend().compact(self.req(sid="ses_9", mode="compact_fails"), lambda: False)
        self.assertEqual(res.status, "error")
        self.assertIn("compaction failed", res.stderr)

    def test_registered(self):
        from orch.backends import BACKENDS, OpenCodeBackend
        self.assertIsInstance(BACKENDS["opencode"], OpenCodeBackend)




def _plan(*done):
    steps = ["STEP-ALPHA validate the first label", "STEP-BETA validate the second label", "STEP-GAMMA redo the table"]
    return "<plan>\n" + "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in zip(done, steps)) + "\n</plan>"


def _review(prose, plan, ck_done, verdict):
    if verdict == "FAIL" or "- [ ]" in plan:
        prose = f'<message to="worker">{prose}</message>'
    return (f"{prose}\n\n{plan}\n<checklist>\n- [{'x' if ck_done else ' '}] the step's result is checked\n"
            f"- [x] protected files unchanged\n</checklist>\n<verdict>{verdict}</verdict>")






if __name__ == "__main__":
    unittest.main()
