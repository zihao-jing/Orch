"""Slow history storage must not block current status or create one scan per viewer."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orch.board import Board
from orch.recorder import Recorder


class BackgroundBoardTest(unittest.TestCase):
    def test_running_assignments_do_not_borrow_another_workers_step(self):
        board = Board('m', lambda: Path('/unused'))
        model = {'run_id': 'r', 'managed': {'par': {'active': True, 'slots': {
            'worker': {'step': None, 'out': 'cases/'},
            'worker-2': {'step': 38, 'out': 'pilots/architecture/'}}}},
            'status_lines': {'worker': {'agent': 'worker', 'step': 'step 38'},
                             'worker-2': {'agent': 'worker-2', 'kind': 'job'}},
            'turns': [{'turn_id': 't1', 'agent': 'worker', 'status': 'running',
                       'par_worker': 'worker', 'par_step': None},
                      {'turn_id': 't2', 'agent': 'worker-2', 'status': 'running',
                       'par_worker': 'worker-2', 'par_step': 38}]}
        items = [{'id': 't:t1', 'tid': 't1', 'ms': 0, 'step': 38},
                 {'id': 't:t2', 'tid': 't2', 'ms': 0, 'step': 38}]
        with mock.patch.object(board, '_timeline', return_value=(model, items, [], '')):
            state = board.state()
        a, b = state['running']
        self.assertEqual(a['step_label'], 'Step 38')
        self.assertEqual(a['location'], 'cases/')
        self.assertEqual(b['step_label'], 'Step 38')
        self.assertEqual(b['location'], 'pilots/architecture/')
        self.assertEqual(state['status_lines']['worker']['step_label'], a['step_label'])
        self.assertEqual(state['status_lines']['worker-2']['location'], b['location'])
        self.assertNotIn('step_label', model['status_lines']['worker'])

    def test_status_responds_while_one_shared_history_build_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            board = Board('m', lambda: rd)
            board.background_refresh = True
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            current = {'run_id': 'r1', 'monitor': 'm', 'status': 'running', 'turns': []}
            builds = []

            def build():
                builds.append(1)
                started.set()
                release.wait(3)
                board._tl = (time.time(), rd, dict(current), [], [], '')
                finished.set()

            with mock.patch.object(board, '_model', side_effect=lambda **kw: dict(current)), \
                 mock.patch.object(board, '_build_background_timeline', side_effect=build):
                try:
                    self.assertTrue(board.state()['history_loading'])
                    self.assertTrue(started.wait(1))
                    current['status'] = 'awaiting_approval'
                    for _ in range(3):
                        state = board.state()
                        self.assertEqual(state['status'], 'awaiting_approval')
                        self.assertTrue(state['history_loading'])
                    self.assertEqual(len(builds), 1)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(1))
                self.assertFalse(board.state()['history_loading'])

    def test_saved_history_is_available_during_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            board = Board('m', lambda: rd)
            board.background_refresh = True
            snapshot = ({'run_id': 'r1'}, [{'id': 'old-message'}], [], '')
            board._tl = (0, rd, *snapshot)
            with board._refresh_lock:
                self.assertEqual(board._timeline(), snapshot)

    def test_previous_round_finishes_without_leaving_a_stale_running_badge(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / 'state.json').write_text(json.dumps({'run_id': 'r', 'round': 2}))
            turn_path = rd / 'rounds' / 'r001' / 't01-worker.json'
            turn_path.parent.mkdir(parents=True)
            turn = {'turn_id': 'r001-t01-worker', 'round': 1, 'agent': 'worker', 'status': 'running'}
            turn_path.write_text(json.dumps(turn))
            board = Board('m', lambda: rd)
            entry = board._turn_entry(turn_path)
            board._tl = (0, rd, {'turns': [entry[0]]}, [], [], '')
            turn['status'] = 'ok'
            turn_path.write_text(json.dumps(turn))
            for _ in range(2):
                current = board._model(full=False, history=False)
                self.assertEqual(current['turns'][0]['status'], 'ok')

    def test_disabled_html_reports_keep_recording_without_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder.create(tmp, 'm', {'controller': {'mode': 'managed'}, 'goal': 'test',
                                             'workspace': tmp, 'working_dir': tmp, 'max_rounds': 1})
            (rec.run_dir / 'DISABLE_HTML_REPORTS').touch()
            with mock.patch('orch.report.write_run_report') as render, \
                 mock.patch('orch.report.write_index') as index:
                rec.refresh_reports()
            render.assert_not_called()
            index.assert_not_called()
            self.assertTrue((rec.run_dir / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
