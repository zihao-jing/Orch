"""A missing executable is a setup problem, not a provider error: no retries on the same model; a backup is used only
if it starts another executable; otherwise the run stops (as for billing problems) and resume re-sends the turn."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.backends import TurnResult, backend_executable, missing_executable, run_process  # noqa: E402
from test_models import FakeClock, events, with_models  # noqa: E402
from test_orch import make_cfg, pipeline, run, turns  # noqa: E402

GONE = "/nonexistent/orch-test-opencode"
MODELS = {
    "oc": {"backend": "opencode", "backend_options": {"executable": GONE, "model": "a/b"}},
    "oc2": {"backend": "opencode", "backend_options": {"executable": GONE, "model": "c/d"}},
    "cmd": {"backend": "command", "backend_options": {"command": [GONE, "x"]}},
    "cx": {"backend": "codex", "backend_options": {"executable": GONE, "transport": "app-server"}},
    "spare": {"backend": "mock", "backend_options": {}},
}


class TestClassify(unittest.TestCase):
    def test_run_process_names_the_missing_executable(self):
        rc, out, err, status = run_process([GONE, "run"], None, "/", 5, {}, lambda: False)
        self.assertEqual((rc, status), (None, "error"))
        self.assertEqual(missing_executable(TurnResult(status, out, None, rc, err)), GONE)

    def test_a_missing_working_directory_is_not_a_missing_executable(self):
        rc, out, err, status = run_process(["true"], None, "/nonexistent/orch-test-dir", 5, {}, lambda: False)
        self.assertEqual(status, "error")
        self.assertEqual(missing_executable(TurnResult(status, out, None, rc, err)), "")

    def test_only_failed_turns_and_only_the_start_error(self):
        self.assertEqual(missing_executable(TurnResult("ok", "", None, 0, "executable not found: x")), "")
        self.assertEqual(missing_executable(TurnResult("error", "executable not found: x", None, 1, "boom")), "")

    def test_codex_app_server_names_the_missing_executable(self):
        from orch.backends import TurnRequest, get_backend
        req = TurnRequest("w1", "worker", "hi", "", None, "/", 30,
                          {"transport": "app-server", "executable": GONE})
        res = get_backend("codex").run(req, lambda: False)
        self.assertEqual(missing_executable(res), GONE)
        req.cwd = "/nonexistent/orch-test-dir"
        req.options = {"transport": "app-server", "executable": "true"}
        self.assertEqual(missing_executable(get_backend("codex").run(req, lambda: False)), "")

    def test_backend_executable(self):
        self.assertEqual(backend_executable("opencode", {}), "opencode")
        self.assertEqual(backend_executable("claude", {"executable": "/opt/claude"}), "/opt/claude")
        self.assertEqual(backend_executable("command", {"command": "my-agent --x {prompt}"}), "my-agent")
        self.assertEqual(backend_executable("mock", {}), "")

    def test_run_variants_skips_backups_with_the_same_missing_executable(self):
        from orch.backends import TurnRequest, run_variants
        from orch.config import load_config
        m = pipeline()
        m["agents"][0].update(model="oc", backup="oc2", backup2="spare")
        tmp = Path(tempfile.mkdtemp()).resolve()
        a = load_config(with_models(make_cfg(tmp, [m]), MODELS)).monitor("m").agent("w1")
        req = TurnRequest("w1", "worker", "hi", "", None, str(tmp), 30, {})
        res = run_variants(a, req, lambda: False)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.meta["models_tried"], ["oc", "spare"])


class TestSetupError(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def cfg(self, **models):
        m = pipeline()
        m["agents"][0].update(retry_waits=[60, 120, 240], **models)
        return with_models(make_cfg(self.tmp, [m]), MODELS)

    def test_no_backup_with_another_executable_stops_the_run(self):
        cfg = self.cfg(model="oc", backup="oc2")
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual(clock.waits, [])
        self.assertEqual(status, "stopped")
        t = turns(rec)[0]
        self.assertEqual(t["agent"], "w1")
        self.assertEqual([a["model"] for a in t["attempts"]], ["oc"])  # no retry, and oc2 needs the same executable
        self.assertEqual(t["setup_error"], f"executable not found: {GONE}")
        self.assertFalse(t.get("fallbacks"))
        self.assertIn("setup problem", rec.state["stop_reason"])
        self.assertIn(GONE, rec.state["stop_reason"])
        ev = events(rec, "setup_error")
        self.assertEqual((len(ev), ev[0]["executable"]), (1, GONE))
        self.assertEqual(events(rec, "retry"), [])
        progress = (rec.run_dir / "progress.md").read_text()
        self.assertIn("⛔", progress)
        self.assertIn("then resume", progress)
        from orch import timeline as TL
        from orch.board import Board
        msgs = [c for c in Board("m", lambda: rec.run_dir).data().get("messages", [])
                if c.get("kind") == "warning" and GONE in (c.get("text") or "")]
        self.assertEqual(len(msgs), 1)
        rows = TL.read_events(rec.run_dir / "events.jsonl", {})
        b = TL.Builder(rec.run_dir, {"agents": [{"name": "w1", "role": "worker", "called": "w1"}]}, [],
                       [r for r in rows if r[1].get("event") == "setup_error"])
        b.system()
        self.assertEqual([(i["kind"], GONE in i["line"]) for i in b.items], [("warning", True)])

    def test_skips_backups_with_the_same_executable(self):
        cfg = self.cfg(model="oc", backup="oc2", backup2="spare")
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual(clock.waits, [])
        self.assertEqual(status, "completed")
        t = turns(rec)[0]
        self.assertEqual([a["model"] for a in t["attempts"]], ["oc", "spare"])
        self.assertEqual([(f["from"], f["to"]) for f in t["fallbacks"]], [("oc", "spare")])
        self.assertEqual([e["backup"] for e in events(rec, "fallback")], [2])
        self.assertNotIn("setup_error", t)

    def test_codex_app_server_stops_the_run(self):
        cfg = self.cfg(model="cx", backup="oc")  # the backup needs the same (missing) executable
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual((clock.waits, status), ([], "stopped"))
        t = turns(rec)[0]
        self.assertEqual([a["model"] for a in t["attempts"]], ["cx"])
        self.assertEqual(t["setup_error"], f"executable not found: {GONE}")

    def test_command_backend_counts_too(self):
        cfg = self.cfg(model="cmd", backup="spare")
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual((clock.waits, status), ([], "completed"))
        self.assertEqual([a["model"] for a in turns(rec)[0]["attempts"]], ["cmd", "spare"])

    def test_resume_sends_the_turn_again(self):
        cfg = self.cfg(model="oc")
        with FakeClock():
            status, rec = run(cfg)
        self.assertEqual(status, "stopped")
        raw = json.loads(Path(cfg).read_text())
        raw["models"]["oc"] = {"backend": "mock", "backend_options": {}}  # the environment is fixed
        Path(cfg).write_text(json.dumps(raw))
        with FakeClock():
            status, rec2 = run(cfg, resume=True)
        self.assertEqual(status, "completed")
        self.assertEqual(rec2.run_dir, rec.run_dir)
        w = [t for t in turns(rec2) if t["agent"] == "w1"]
        self.assertEqual(w[0]["status"], "error")
        self.assertEqual(w[1]["status"], "ok")
        self.assertEqual(w[1]["input"], w[0]["input"])


if __name__ == "__main__":
    unittest.main()
