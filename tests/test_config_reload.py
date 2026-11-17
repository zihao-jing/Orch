"""Settings changed in the config while a run is going take effect at the agent's next turn. Run: python -m unittest discover tests"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.config import load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402


class FakeRec:
    """The parts of the Recorder that a settings reload writes to."""

    def __init__(self):
        self.progress_lines, self.events, self.state = [], [], {}

    def progress(self, text):
        self.progress_lines.append(text)

    def event(self, kind, **data):
        self.events.append((kind, data))

    def save(self):
        pass


class TestConfigReload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "ws").mkdir()
        self.cfg_path = self.tmp / "cfg.json"
        self.write(worker={"backend": "mock", "backend_options": {"model": "m1"}})

    def write(self, worker):
        agents = [{"name": "w1", "role": "worker", "task": "do it", **worker}]
        cfg = {"output_dir": str(self.tmp / "out"), "monitors": [
            {"name": "m", "workspace": "ws", "goal": "g", "max_rounds": 4,
             "controller": {"mode": "pipeline"}, "agents": agents}]}
        self.cfg_path.write_text(json.dumps(cfg))

    def runner(self):
        mon = load_config(str(self.cfg_path)).monitor("m")
        r = MonitorRunner(mon, threading.Event(), log=lambda s: None)
        r.rec = FakeRec()
        r.rec.state = {"sessions": {}, "fwd": {}}
        return r, mon.agents[0]

    def test_model_change_applies_before_the_next_turn(self):
        r, w = self.runner()
        self.write(worker={"backend": "mock", "backend_options": {"model": "m2"}})
        r.refresh_agent(w)
        self.assertEqual(w.backend_options, {"model": "m2"})
        self.assertEqual(r.rec.events[-1], ("agent_reloaded", {"agent": "w1", "changed": ["backend_options"]}))
        self.assertIn("m1", r.rec.progress_lines[-1])
        self.assertIn("m2", r.rec.progress_lines[-1])

    def test_no_change_does_nothing(self):
        r, w = self.runner()
        r.refresh_agent(w)
        self.assertEqual(r.rec.events, [])
        self.assertEqual(r.rec.progress_lines, [])

    def test_env_values_are_not_written_to_the_log(self):
        r, w = self.runner()
        self.write(worker={"backend": "mock", "backend_options": {"model": "m1"}, "env": {"TOKEN": "secret-value"}})
        r.refresh_agent(w)
        self.assertEqual(w.env, {"TOKEN": "secret-value"})
        self.assertNotIn("secret-value", " ".join(r.rec.progress_lines))
        self.assertIn("TOKEN", r.rec.progress_lines[-1])

    def test_broken_config_keeps_current_settings_and_reports_once(self):
        r, w = self.runner()
        self.cfg_path.write_text("{ not json")
        r.refresh_agent(w)
        r.refresh_agent(w)
        self.assertEqual(w.backend_options, {"model": "m1"})
        self.assertEqual(len(r.rec.progress_lines), 1)
        self.assertIn("keeping the settings", r.rec.progress_lines[0])

    def test_unknown_backend_is_refused(self):
        r, w = self.runner()
        self.write(worker={"backend": "no-such-backend"})
        r.refresh_agent(w)
        self.assertEqual(w.backend, "mock")
        self.assertIn("unknown backend", r.rec.progress_lines[-1])

    def test_agent_removed_from_config_keeps_its_settings(self):
        r, w = self.runner()
        cfg = json.loads(self.cfg_path.read_text())
        cfg["monitors"][0]["agents"] = [{"name": "other", "role": "worker", "task": "x"}]  # a monitor needs an agent
        self.cfg_path.write_text(json.dumps(cfg))
        r.refresh_agent(w)
        self.assertEqual(w.backend_options, {"model": "m1"})
        self.assertIn("not in the config", r.rec.progress_lines[-1])

    def test_prompt_change_is_reported_but_not_applied(self):
        r, w = self.runner()
        self.write(worker={"backend": "mock", "backend_options": {"model": "m1"}, "description": "new words"})
        r.refresh_agent(w)
        self.assertNotEqual(w.description, "new words")
        self.assertIn("applied only at the next start", r.rec.progress_lines[-1])


if __name__ == "__main__":
    unittest.main()
