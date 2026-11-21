"""Model selection is visible while an attempt runs, including reused sessions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orch.backends import MockBackend
from orch.board import Board
from orch.config import load_config
from test_orch import make_cfg, pipeline, run


class TestLiveModelRecord(unittest.TestCase):
    def test_worker_model_is_saved_before_backend_runs(self):
        self.check_live_model(0, "w1", "worker")

    def test_critic_model_is_saved_before_backend_runs(self):
        self.check_live_model(1, "v1", "verifier")

    def check_live_model(self, index, name, role):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            m = pipeline()
            m['agents'][index].update(backend_options={'model': 'primary', 'fail_turns': [2], 'pass_after': 2},
                                  retry_waits=[], retries=0,
                                  fallback=[{'backend_options': {'model': 'backup', 'fail_turns': []}}])
            cfg = make_cfg(root, [m])
            mon = load_config(cfg).monitor('m')
            original = MockBackend._run
            observed = []

            def spy(backend, req, stop, sid):
                if req.role == role:
                    rd = next((root / 'out' / 'm').iterdir())
                    records = [json.loads(p.read_text()) for p in (rd / 'rounds').glob(f'r*/t*-{name}.json')]
                    active = next(t for t in records if t['status'] == 'running')
                    expected = 'mock:' + req.options['model']
                    self.assertEqual(active.get('model'), expected)
                    live = Board('m', lambda: rd, mon).state()['running']
                    self.assertEqual(next(t for t in live if t['agent'] == name)['model'], expected)
                    observed.append((expected, req.session_id))
                return original(backend, req, stop, sid)

            with patch.object(MockBackend, '_run', spy):
                status, _rec = run(cfg)
            self.assertEqual(status, 'completed')
            self.assertEqual([x[0] for x in observed], ['mock:primary', 'mock:primary', 'mock:backup'])
            self.assertIsNone(observed[0][1])
            self.assertTrue(observed[1][1])
            self.assertEqual(observed[1][1], observed[2][1])
