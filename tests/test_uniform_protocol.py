"""Routing/dispatch regressions for the uniform protocol; raw replies, no legacy fixture adapter."""
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orch import audit, protocol, routing
from orch.backends import MockBackend, StopRequested, TurnResult, _opencode_tool_event
from orch.config import load_config
from orch.managed import ManagedRunner
from orch.timing import TurnTiming, describe
from test_orch import make_cfg, managed, run, turns
from test_b11_parallel import Base as ParallelBase

PASS = '<checklist>- [x] change checked</checklist><verdict>PASS</verdict>'


class TestUniformDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_untagged_done_fail_is_upward_and_never_dispatches_acknowledgment(self):
        cfg = make_cfg(self.root, [managed(mgr={'milestones': 1}, critic={'replies': {
            'verify': ['Your part is finished; nothing more for you. <verdict>FAIL</verdict>']}})])
        with patch.object(ManagedRunner, 'wait_gate', side_effect=StopRequested):
            status, rec = run(cfg)
        self.assertEqual(status, 'stopped')
        ts = turns(rec)
        self.assertEqual(len([t for t in ts if t['purpose'] == 'work']), 1)
        pause = next(t for t in ts if t['purpose'] == 'pause')
        self.assertIn('Your part is finished', pause['input'])
        note = next(m for m in rec.state['messages'] if m['body'].startswith('Your part'))
        self.assertEqual(note['to'], 'boss')
        self.assertEqual(note['status'], 'delivered')
        self.assertIn(note['created_at'], pause['input'])
        self.assertIn('Progress records from the critic', pause['input'])
        self.assertTrue(all(t['communication_protocol'] == protocol.VERSION for t in ts))

    def test_pass_with_explicit_message_starts_one_turn_and_keeps_session(self):
        cfg = make_cfg(self.root, [managed(mgr={'milestones': 1}, critic={'replies': {
            'verify': ['Manager-only observation. <message to="Sam">Check the caption once.</message>' + PASS,
                       'All done. ' + PASS]}})])
        status, rec = run(cfg)
        self.assertEqual(status, 'completed')
        work = [t for t in turns(rec) if t['purpose'] == 'work']
        self.assertEqual(len(work), 2)
        self.assertEqual(work[0]['session_id'], work[1]['session_id'])
        self.assertEqual(work[1]['input'].count('Check the caption once.'), 1)
        self.assertNotIn('Manager-only observation', work[1]['input'])
        message = next(m for m in rec.state['messages'] if m['body'] == 'Check the caption once.')
        self.assertEqual(message['status'], 'delivered')

    def test_final_review_is_the_report_without_a_duplicate_progress_record(self):
        cfg = make_cfg(self.root, [managed(mgr={'milestones': 1}, critic={'replies': {
            'verify': ['All labels hold; nothing is open. ' + PASS]}})])
        status, rec = run(cfg)
        self.assertEqual(status, 'completed')
        self.assertFalse([t for t in turns(rec) if t['purpose'] == 'report'])  # no second turn for the same news
        review = next(t for t in turns(rec) if t['purpose'] == 'review')
        self.assertEqual(review['input'].count('All labels hold; nothing is open.'), 1)
        self.assertNotIn('Progress records from the critic', review['input'])

    def test_external_role_views_have_the_same_upper_default(self):
        agents = [SimpleNamespace(name=n, role=r, called=n) for n, r in
                  [('boss', 'manager'), ('critic', 'verifier'), ('writer', 'worker')]]
        sent, bad = routing.split_reply('Status. <message to="you">A detail.</message>'
                                       '<message to="writer">Do the edit.</message>', agents[1], agents,
                                       include_upward=True)
        self.assertFalse(bad)
        self.assertEqual([(m['target'].name, m['body']) for m in sent],
                         [('boss', 'Status.'), ('boss', 'A detail.'), ('writer', 'Do the edit.')])
        sent, _ = routing.split_reply('Worker report.', agents[2], agents, include_upward=True)
        self.assertEqual(sent[0]['target'].name, 'critic')


class TestUniformParallel(ParallelBase):
    def test_finished_worker_waits_then_explicit_message_reuses_session(self):
        original = MockBackend._run
        counts = {}
        def scripted(backend, req, stop, sid):
            if req.context.get('purpose') == 'par_review':
                who = req.context['par_worker']
                counts[who] = counts.get(who, 0) + 1
                if who == 'writer' and counts[who] == 1:
                    out = 'This assignment is done; the shared work remains open. <verdict>FAIL</verdict>'
                elif who == 'writer-2':
                    out = '<message to="writer">Please check the new cross-reference.</message><verdict>PASS</verdict>'
                else:
                    out = 'Done. <verdict>PASS</verdict>'
                self.ck.write_text(self.ck.read_text().replace('- [ ]', '- [x]'))
                return TurnResult('ok', out, sid, 0)
            return original(backend, req, stop, sid)
        cfg = self.cfg(worker={'delay_for': {'writer': {'work': .01}, 'writer-2': {'work': .3},
                                               'writer-3': {'work': .5}}})
        with patch.object(MockBackend, '_run', scripted):
            status, rec = run(cfg)
        self.assertEqual(status, 'completed')
        work = [t for t in turns(rec) if t['purpose'] == 'work' and t['agent'] == 'writer']
        self.assertEqual(len(work), 2)
        self.assertEqual(work[0]['session_id'], work[1]['session_id'])
        self.assertEqual(work[1]['input'].count('Please check the new cross-reference.'), 1)
        self.assertNotIn('This assignment is done', work[1]['input'])

    def test_all_idle_unfinished_workers_preserve_parallel_state_at_manager_wait(self):
        cfg = self.cfg(critic={'replies': {
            'brief': ['<workers n="3" steps="1,2,3">Independent.</workers>'],
            'par_assign': ['<to worker="writer" step="1">Work A.</to>'
                           '<to worker="writer-2" step="2">Work B.</to>'
                           '<to worker="writer-3" step="3">Work C.</to>'],
            'par_review': ['No additional assignment. <verdict>FAIL</verdict>']}})
        with patch.object(ManagedRunner, 'wait_gate', side_effect=StopRequested):
            status, rec = run(cfg)
        self.assertEqual(status, 'stopped')
        par = rec.state['managed']['par']
        self.assertTrue(par['active'])
        self.assertTrue(all(slot['status'] == 'blocked' for slot in par['slots'].values()))
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'work']), 3)
        self.assertEqual(len([t for t in turns(rec) if t['purpose'] == 'pause']), 1)


class TestAuditProtocolEvidence(unittest.TestCase):
    def test_relay_calculations_use_historical_or_uniform_rules(self):
        source = {'role': 'verifier', 'output': 'Upper status. <message to="writer">Worker task.</message>'}
        worker = {'agent': 'writer', 'role': 'worker'}
        manager = {'agent': 'boss', 'role': 'manager'}
        self.assertIn('Upper status', audit.passable(source, worker)[0])
        source['communication_protocol'] = protocol.VERSION
        self.assertEqual(audit.passable(source, worker)[0], 'Worker task.')
        self.assertEqual(audit.passable(source, manager)[0], 'Upper status.')
        source['output'] = '<message to="you">Manager answer.</message>'
        self.assertEqual(audit.passable(source, manager)[0], 'Manager answer.')

    def test_digest_includes_protocol_boundaries_and_preserves_reviewed_decisions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rd = root / 'run'
            folder = rd / 'rounds' / 'r001'
            folder.mkdir(parents=True)
            for i, version in enumerate([None, protocol.VERSION], 1):
                t = {'turn_id': f'r001-t0{i}-critic', 'round': 1, 'agent': 'critic', 'role': 'verifier',
                     'purpose': 'verify', 'status': 'ok', 'input': 'Review.', 'output': 'Checked.',
                     'started_at': f'2026-01-01T00:00:0{i}+00:00', 'ended_at': f'2026-01-01T00:00:0{i+1}+00:00'}
                if version: t['communication_protocol'] = version
                (folder / f't0{i}-critic.json').write_text(json.dumps(t))
            decision = root / 'audit-decisions.md'
            decision.write_text('- [won\'t fix] Repeated verification reminders.\n')
            agent = SimpleNamespace(state_file=str(root / 'state.md'), name='auditor', working_dir=td,
                                    timeout=10, backend_options={}, env={},
                                    current_system_prompt=lambda: 'Audit {{points}} points.')
            mon = SimpleNamespace(controller=SimpleNamespace(audit_points=5, templates={}), working_dir=td, name='m')
            def respond(_agent, req, stop):
                self.assertIn(protocol.LEGACY, req.prompt)
                self.assertIn(protocol.VERSION, req.prompt)
                self.assertIn('Human-reviewed audit decisions', req.prompt)
                self.assertIn("[won't fix] Repeated verification reminders", req.prompt)
                self.assertIn('No timing breakdown recorded', req.prompt)
                return TurnResult('ok', '<points>Nothing new.</points><findings></findings>', 'audit')
            with patch('orch.backends.run_variants', respond):
                result = audit.run_audit(mon, agent, rd, 1, 1)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(decision.read_text(), "- [won't fix] Repeated verification reminders.\n")


class TestObservedTiming(unittest.TestCase):
    def test_overlapping_tools_are_not_double_counted(self):
        clock = [0]
        t = TurnTiming(lambda: clock[0])
        clock[0] = 5; t.event({'k': 'text', 's': 'Working.'})
        clock[0] = 6; t.event({'k': 'tool_start', 'id': 'a'})
        clock[0] = 7; t.event({'k': 'tool_start', 'id': 'b'})
        clock[0] = 9; t.event({'k': 'tool_end', 'id': 'a'})
        clock[0] = 10; t.event({'k': 'tool_end', 'id': 'b'})
        clock[0] = 12; t.event({'k': 'text', 's': 'Done.'})
        clock[0] = 13
        result = t.finish()
        self.assertEqual(result['first_activity_s'], 5)
        self.assertEqual(result['observed_tool_s'], 4)
        self.assertEqual(result['unattributed_s'], 9)
        self.assertEqual(result['text_window_s'], 7)
        self.assertTrue(result['tool_timing_complete'])

    def test_missing_tool_boundaries_are_unknown_not_zero(self):
        clock = [0]
        t = TurnTiming(lambda: clock[0])
        clock[0] = 600; t.event({'k': 'tool', 's': 'shell'})
        clock[0] = 720
        result = t.finish()
        self.assertIsNone(result['observed_tool_s'])
        self.assertFalse(result['tool_timing_complete'])
        self.assertIn('unknown', describe(result))
        self.assertEqual(result['first_activity_s'], 600)

    def test_opencode_uses_recorded_tool_timestamps(self):
        events = []
        _opencode_tool_event({'id': 't', 'tool': 'shell', 'state': {
            'time': {'start': 1000, 'end': 121000}, 'input': {'command': 'read'}}}, events.append)
        self.assertTrue(events[0]['timed'])
        self.assertEqual(events[1]['duration_s'], 120)
