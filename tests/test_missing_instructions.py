"""A failed review with no worker instruction must not spend empty worker rounds."""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_orch import managed, make_cfg, run, turns
from orch.backends import MockBackend, StopRequested
from orch.managed import ManagedRunner
from test_b11_parallel import Base as ParallelBase, ASSIGN, PROPOSE


NO_STEP = ('<message to="you">The run is closed. No further work is authorized.</message>\n'
           '<plan>- [ ] Unfinished backlog, inactive.</plan>\n'
           '<checklist>- [ ] Work remains incomplete.</checklist>\n<verdict>FAIL</verdict>')
PAUSE = '<pause>Choose whether to reopen the work.</pause>\n<verdict>FAIL</verdict>'


class TestMissingInstructions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_until_wait(self, config, resume=False):
        # End a deterministic test at the real human gate, before it would wait indefinitely for input.
        with patch.object(ManagedRunner, 'wait_gate', side_effect=StopRequested) as gate:
            status, rec = run(config, resume=resume)
        self.assertEqual(status, 'stopped')
        self.assertEqual(rec.state['managed']['phase'], 'wait')
        self.assertEqual(gate.call_args.args[0]['kind'], 'wait')
        return rec

    def test_failed_review_without_instructions_asks_manager_before_another_worker_turn(self):
        cfg = make_cfg(self.root, [managed(critic={'replies': {'verify': [NO_STEP]}})])
        rec = self.run_until_wait(cfg)
        ts = turns(rec)
        self.assertEqual(len([t for t in ts if t['purpose'] == 'work']), 1)
        self.assertEqual(len([t for t in ts if t['purpose'] == 'pause']), 1)
        self.assertEqual(rec.state['round'], 1)

    def test_empty_brief_never_starts_a_worker(self):
        cfg = make_cfg(self.root, [managed(critic={'replies': {'brief': [NO_STEP]}})])
        rec = self.run_until_wait(cfg)
        self.assertFalse(any(t['purpose'] == 'work' for t in turns(rec)))
        self.assertEqual(rec.state['round'], 0)

    def test_continue_without_a_next_step_is_bounded_and_waits_even_in_night_mode(self):
        cfg = make_cfg(self.root, [managed(
            mgr={'replies': {'pause': ['<message to="Jordan">Resume if there is work.</message>'
                                     '<decision>CONTINUE</decision>']}},
            critic={'replies': {'brief': [NO_STEP], 'guidance': [NO_STEP]}})])
        rec = self.run_until_wait(cfg)
        ts = turns(rec)
        self.assertEqual(len([t for t in ts if t['purpose'] == 'pause']), 1)
        self.assertFalse(any(t['purpose'] == 'work' for t in ts))
        self.assertIn('another empty instruction', rec.state['managed']['wait']['text'])
        # A persisted wait must not turn back into work on resume, including with night mode enabled.
        rec.state['night'] = True
        rec.save()
        again = self.run_until_wait(cfg, resume=True)
        self.assertEqual(len(turns(again)), len(ts))

    def test_manager_can_recover_with_real_worker_instructions(self):
        cfg = make_cfg(self.root, [managed(
            mgr={'milestones': 1, 'replies': {'pause': [
                '<message to="Jordan">Finish the remaining edit.</message><decision>CONTINUE</decision>']}},
            critic={'replies': {'brief': [NO_STEP], 'guidance': ['Make the remaining edit and test it.'],
                                'verify': ['The edit is correct.\n<plan>- [x] Finish the edit.</plan>\n'
                                           '<checklist>- [x] The edit is tested.</checklist><verdict>PASS</verdict>']}})])
        status, rec = run(cfg)
        self.assertEqual(status, 'completed')
        work = [t for t in turns(rec) if t['purpose'] == 'work']
        self.assertIn('Make the remaining edit and test it.', work[0]['input'])
        self.assertNotIn('instruction_retry', rec.state['managed'])

    def test_missing_manager_pause_decision_does_not_implicitly_continue(self):
        cfg = make_cfg(self.root, [managed(
            mgr={'replies': {'pause': ['The work is closed. Preserve the files.']}},
            critic={'replies': {'verify': [PAUSE]}})])
        rec = self.run_until_wait(cfg)
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'work']), 1)
        self.assertEqual(rec.state['managed']['wait']['who'], 'manager decision unclear')

    def test_approval_without_message_cannot_restart_with_an_empty_critic_instruction(self):
        cfg = make_cfg(self.root, [managed(critic={'replies': {'verify': [PAUSE], 'guidance': [NO_STEP]}})])
        gates = []

        def answer_once(_runner, gate):
            gates.append(gate)
            if len(gates) == 1:
                return {'decision': 'approve', 'message': ''}
            raise StopRequested()

        with patch.object(ManagedRunner, 'wait_gate', answer_once):
            status, rec = run(cfg)
        self.assertEqual(status, 'stopped')
        self.assertEqual(len(gates), 2)
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'work']), 1)


class TestParallelMissingInstructions(ParallelBase):
    def run_until_wait(self, cfg):
        with patch.object(ManagedRunner, 'wait_gate', side_effect=StopRequested):
            status, rec = run(cfg)
        self.assertEqual(status, 'stopped')
        self.assertEqual(rec.state['managed']['phase'], 'wait')
        return rec

    def replies(self, review=NO_STEP, assign=ASSIGN, guidance=NO_STEP):
        return {'brief': [PROPOSE], 'par_assign': [assign],
                'par_review': [review, 'Done. <verdict>PASS</verdict>'],
                'guidance': [guidance], 'verify': ['Done. <verdict>PASS</verdict>']}

    def test_empty_parallel_review_blocks_that_worker_and_finishes_other_assigned_work(self):
        rec = self.run_until_wait(self.cfg(critic={'replies': self.replies()}))
        work = [t for t in turns(rec) if t['purpose'] == 'work']
        self.assertEqual(sorted(t['agent'] for t in work), ['writer', 'writer-2', 'writer-3'])
        par = rec.state['managed']['par_last']
        self.assertFalse(par['active'])
        self.assertEqual(sorted(s['status'] for s in par['slots'].values()), ['blocked', 'done', 'done'])
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'pause']), 1)
        self.assertIn('waiting for a next-step instruction', (self.sdir / 'critic' / 'work-m1.md').read_text())

    def test_checklist_only_parallel_assignment_spends_no_turn_on_that_worker(self):
        assign = ASSIGN.replace('Make table A in out/a.', '- [ ] Table A remains unfinished')
        rec = self.run_until_wait(self.cfg(critic={'replies': self.replies(
            review='Done. <verdict>PASS</verdict>', assign=assign)}))
        self.assertEqual(sorted(t['agent'] for t in turns(rec) if t['purpose'] == 'work'), ['writer-2', 'writer-3'])
        self.assertEqual(rec.state['managed']['par_last']['slots']['writer']['turns'], 0)

    def test_parallel_then_empty_continuation_waits_after_one_manager_attempt(self):
        rec = self.run_until_wait(self.cfg(critic={'replies': self.replies()}, mgr={'replies': {
            'incoming': ['<workers n="3">Approved.</workers>', 'Noted.'],
            'pause': ['<message to="Jordan">Resume if there is work.</message><decision>CONTINUE</decision>']}}))
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'work']), 3)
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'pause']), 1)
        self.assertIn('another empty instruction', rec.state['managed']['wait']['text'])

    def test_parallel_can_continue_after_concrete_manager_and_critic_instructions(self):
        cfg = self.cfg(critic={'replies': self.replies(guidance='Finish the remaining caption.')}, mgr={
            'replies': {'incoming': ['<workers n="3">Approved.</workers>', 'Noted.'],
                        'pause': ['<message to="Jordan">Finish the caption.</message><decision>CONTINUE</decision>']}})
        status, rec = run(cfg)
        self.assertEqual(status, 'completed')
        work = [t for t in turns(rec) if t['purpose'] == 'work']
        self.assertEqual(len(work), 4)
        resumed = next(t for t in work if not t.get('par_worker'))
        self.assertIn('Finish the remaining caption.', resumed['input'])
        self.assertNotIn('instruction_retry', rec.state['managed'])

    def test_parallel_review_preserves_the_workers_timeout_status(self):
        original = MockBackend.run

        def timeout_worker(backend, req, stop):
            result = original(backend, req, stop)
            return dataclasses.replace(result, status='timeout') if req.role == 'worker' else result

        with patch.object(MockBackend, 'run', timeout_worker):
            status, rec = run(self.cfg(controller={'stop_on_error': False}))
        self.assertEqual(status, 'completed')
        reviews = [t for t in turns(rec) if t['purpose'] == 'par_review']
        self.assertEqual(len(reviews), 3)
        for review in reviews:
            self.assertIn('Incomplete turn: timeout', review['input'])


if __name__ == '__main__':
    unittest.main()
