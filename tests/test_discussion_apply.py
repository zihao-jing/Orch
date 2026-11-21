"""Discussion application reuses one manager reply across questions and gate handling."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_b6_b9 import unit


class TestApplyDiscussion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = unit(Path(self.tmp.name))
        self.r.mg.update(phase='wait', gate={'kind': 'wait', 'key': 'wait-0-0', 'text': 'Review queries'},
                         wait={'text': 'Review queries'})
        self.r.st['questions'] = [dict(id=f'Q{i}', text=f'Choice {i}?', status='open', to='human',
                                        parent=[], **{'from': 'boss'}) for i in (1, 2)]

    def reply(self, answers=None, gate='revise', suffix=''):
        data = {'answers': {'Q1': 'Use A', 'Q2': 'Use B'} if answers is None else answers, 'gate': gate}
        return dict(status='ok', turn_id='r001-t01-boss', purpose='discussion_apply',
                    output='<discussion-resolution>' + json.dumps(data) + '</discussion-resolution>\n'
                    '<message to="Jordan">Use A and B; rebuild the queries.</message>\n'
                    '<decision>CONTINUE</decision>' + suffix)

    def apply(self, reply):
        self.r.discussion_action({'action': 'apply', 'request': 'request-1'})
        with mock.patch.object(self.r, 'say', return_value=reply) as say:
            self.r.apply_discussion()
        return say

    def test_end_only_keeps_questions_gate_and_directions_held(self):
        self.r.set_discussion(True)
        held = self.r.deliver(self.r.manager, self.r.critic, 'OLD INSTRUCTION')
        self.r.discussion_action({'action': 'end'})
        with mock.patch.object(self.r, 'say') as say:
            self.r.discussion_batch()
        say.assert_not_called()
        self.assertFalse(self.r.st['discussion'])
        self.assertTrue(self.r.discussion_on())  # routing remains held after ending
        self.assertEqual(held['status'], 'held')
        self.assertEqual(self.r.mg['gate']['key'], 'wait-0-0')
        self.assertTrue(all(q['status'] == 'open' for q in self.r.st['questions']))

    def test_one_call_settles_all_answers_and_queues_gate(self):
        self.r.set_discussion(True)
        say = self.apply(self.reply())
        say.assert_called_once()
        self.assertIn('Q1', say.call_args.args[1])
        self.assertIn('Q2', say.call_args.args[1])
        self.assertIn('wait-0-0', say.call_args.args[1])
        self.assertEqual([q['answer'] for q in self.r.st['questions']], ['Use A', 'Use B'])
        self.assertFalse(self.r.discussion_on())
        decision = self.r.st['gate_queue'][0]
        self.assertEqual(decision['decision'], 'revise')
        self.assertEqual(decision['discussion_result']['turn_id'], 'r001-t01-boss')
        # Reusing that result in the wait phase must call only the critic, never the manager again.
        with mock.patch.object(self.r, 'wait_gate', return_value=decision), \
             mock.patch.object(self.r, 'say', return_value={'output': '', 'status': 'ok'}) as say, \
             mock.patch.object(self.r, 'worker_feedback_for'), mock.patch.object(self.r, 'critic_asks'):
            self.r.ph_wait(self.r.mg)
        say.assert_called_once()
        self.assertEqual(say.call_args.args[0].name, self.r.critic.name)

    def test_duplicate_request_does_not_call_manager_twice(self):
        self.apply(self.reply())
        self.r.discussion_action({'action': 'apply', 'request': 'request-1'})
        with mock.patch.object(self.r, 'say') as say:
            self.r.apply_discussion()
        say.assert_not_called()
        self.assertEqual(len(self.r.st['gate_queue']), 1)

    def test_validated_discussion_starts_the_requested_parallel_work(self):
        reply = self.reply(suffix='<workers n="2">Independent writing and test runs.</workers>')
        self.apply(reply)
        self.assertEqual(self.r.mg['par_req']['status'], 'approved')
        self.assertEqual(self.r.mg['par_req']['n'], 2)
        with mock.patch.object(self.r, 'par_start') as start:
            self.r.ph_work(self.r.mg)
        start.assert_called_once()
        self.assertEqual(self.r.mg['phase'], 'parallel')

    def test_unresolved_discussion_does_not_release_worker_request(self):
        self.apply(self.reply(answers={'Q1': 'Use A'},
                              suffix='<workers n="2">Independent work.</workers>'))
        self.assertNotIn('par_req', self.r.mg)

    def test_worker_request_without_a_gate_and_budget_checks(self):
        self.r.mg['gate'] = None
        self.apply(self.reply(suffix='<workers n="2">Independent work.</workers>'))
        self.assertEqual(self.r.mg['par_req']['status'], 'approved')

    def test_worker_request_above_budget_still_needs_approval(self):
        self.apply(self.reply(suffix='<workers n="4">Independent work.</workers>'))
        self.assertEqual(self.r.mg['par_req']['status'], 'gate')

    def test_partial_answers_leave_gate_open_and_release_nothing(self):
        with mock.patch.object(self.r, 'route_reply') as route:
            self.apply(self.reply(answers={'Q1': 'Use A'}))
        route.assert_not_called()
        self.assertEqual(self.r.st['questions'][0]['status'], 'answered')
        self.assertEqual(self.r.st['questions'][1]['status'], 'open')
        self.assertFalse(self.r.st.get('gate_queue'))
        self.assertTrue(self.r.mg['discussion_saved'])

    def test_invalid_reply_does_not_settle_anything(self):
        self.apply(self.reply(answers={'Q404': 'Invented answer'}))
        self.assertTrue(all(q['status'] == 'open' for q in self.r.st['questions']))
        self.assertFalse(self.r.st.get('gate_queue'))
        self.assertIn('invalid', self.r.mg['discussion_error'])

    def test_busy_manager_waits_without_losing_request(self):
        self.r.discussion_action({'action': 'apply', 'request': 'request-1'})
        with mock.patch.object(self.r.rec, 'agent_busy', return_value=True), \
             mock.patch.object(self.r, 'say') as say:
            self.r.apply_discussion()
        say.assert_not_called()
        self.assertEqual(self.r.mg['discussion_apply']['status'], 'queued')

    def test_completed_result_is_reused_after_restart(self):
        self.r.discussion_action({'action': 'apply', 'request': 'request-1'})
        self.r.mg['discussion_apply']['result'] = self.reply()
        with mock.patch.object(self.r, 'say') as say:
            self.r.apply_discussion()
        say.assert_not_called()
        self.assertEqual(len(self.r.st['gate_queue']), 1)

    def test_changed_gate_rejects_stale_apply(self):
        self.r.discussion_action({'action': 'apply', 'request': 'request-1'})
        self.r.mg['gate']['key'] = 'wait-0-1'
        with mock.patch.object(self.r, 'say') as say:
            self.r.apply_discussion()
        say.assert_not_called()
        self.assertIn('gate changed', self.r.mg['discussion_error'])

    def test_api_end_only_and_apply_controls_and_processing_state(self):
        from orch.board import Board, serve
        from urllib.request import Request, urlopen
        self.r.st['discussion_controls'] = 2
        self.r.rec.save()
        board = Board('m', lambda: self.r.rec.run_dir, self.r.mon)
        server, url = serve({'m': board}, host='127.0.0.1', port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for body in ({'on': False}, {'action': 'apply', 'request': 'api-1'}):
            req = Request(url + 'api/discussion?m=m', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=5) as response:
                self.assertTrue(json.load(response)['ok'])
        controls = [json.loads(p.read_text()) for p in sorted((self.r.rec.run_dir / 'inbox').glob('*.json'))]
        self.assertEqual([d['action'] for d in controls], ['end', 'apply'])
        self.assertEqual(controls[-1]['request'], 'api-1')
        self.assertTrue(board.state()['discussion_applying'])

    def test_milestone_revision_reuses_manager_reply(self):
        self.r.mg.update(gate={'kind': 'milestone', 'key': 'milestone-0-0'}, ms=0,
                         milestones=[{'title': 'Queries', 'accepted': True}])
        self.apply(self.reply())
        with mock.patch.object(self.r, 'wait_gate', return_value=self.r.st['gate_queue'][0]), \
             mock.patch.object(self.r, 'say') as say:
            self.r.ph_gate(self.r.mg)
        say.assert_not_called()
        self.assertEqual(self.r.mg['phase'], 'guidance')
        self.assertIn('rebuild the queries', self.r.mg['guidance'])

    def test_plan_revision_reuses_manager_reply(self):
        self.r.mg.update(gate={'kind': 'plan', 'key': 'plan-0-0'})
        self.apply(self.reply(suffix='\n1. Queries — rebuild the queries\n'))
        with mock.patch.object(self.r, 'wait_gate', return_value=self.r.st['gate_queue'][0]), \
             mock.patch.object(self.r, 'say') as say:
            self.r.ph_plan_gate(self.r.mg)
            self.r.ph_plan(self.r.mg)
        say.assert_not_called()
        self.assertEqual(self.r.mg['milestones'][0]['title'], 'Queries')

    def test_click_for_a_previous_gate_does_not_start_application(self):
        self.r.discussion_action({'action': 'apply', 'request': 'old-click', 'gate': 'old-gate'})
        self.assertNotIn('discussion_apply', self.r.mg)
        self.assertIn('gate changed', self.r.mg['discussion_error'])

    def test_worker_count_gate_passes_consolidated_instruction(self):
        self.r.mg.update(gate={'kind': 'workers', 'key': 'workers-0-0'}, par_req={'n': 2})
        self.apply(self.reply(gate='approve'))
        with mock.patch.object(self.r, 'wait_gate', return_value=self.r.st['gate_queue'][0]), \
             mock.patch.object(self.r, 'say') as say:
            self.r.ph_workers_gate(self.r.mg)
        say.assert_not_called()
        self.assertEqual(self.r.mg['par_req']['status'], 'approved')
        self.assertIn('rebuild the queries', ' '.join(self.r.st['fwd'][self.r.critic.name]))

    def test_new_question_keeps_gate_open(self):
        self.apply(self.reply(suffix='<question>Which dataset?</question>'))
        self.assertFalse(self.r.st.get('gate_queue'))
        self.assertTrue(any(q['text'] == 'Which dataset?' and q['status'] == 'open'
                            for q in self.r.st['questions']))


class TestApplyDiscussionLive(unittest.TestCase):
    def test_wait_gate_applies_in_one_manager_turn(self):
        from test_b6_b9 import Live
        from test_orch import make_cfg, managed, turns
        from protocol_fixtures import worker_instruction
        with tempfile.TemporaryDirectory() as td:
            resolution = '<discussion-resolution>{"answers":{"Q1":"Use the pilot","Q2":"Keep tables short"},"gate":"revise"}</discussion-resolution>'
            reply = (resolution + '<message to="Jordan">BATCH: Use pilot data and short tables.</message>'
                     '<decision>CONTINUE</decision>')
            cfg = make_cfg(Path(td), [managed(mgr={'milestones': 1, 'replies': {
                'pause': ['<question>Which data?</question><question>Which format?</question><decision>PAUSE</decision>'],
                'discussion_apply': [reply]}}, critic={'replies': {'verify': [
                    worker_instruction('<pause>Need decisions.</pause><checklist>- [ ] done</checklist><verdict>FAIL</verdict>'),
                    '<checklist>- [x] done</checklist><verdict>PASS</verdict>']}})])
            live = Live(self, cfg)
            live.gate('wait')
            live.control({'kind': 'discussion', 'on': True}, 'start')
            live.until(lambda st: st.get('discussion'), 'discussion started')
            live.control({'kind': 'discussion', 'action': 'end', 'on': False}, 'end')
            live.until(lambda st: st['managed'].get('discussion_saved'), 'ended without applying')
            live.control({'kind': 'discussion', 'action': 'apply', 'on': False, 'request': 'live-1'}, 'apply')
            status, rec = live.done()
            self.assertEqual(status, 'completed')
            ts = turns(rec)
            self.assertEqual(len([t for t in ts if t['purpose'] == 'discussion_apply']), 1)
            self.assertEqual(len([t for t in ts if t['agent'] == 'boss' and t['purpose'] == 'pause']), 1)
            self.assertFalse([t for t in ts if t['purpose'] == 'discussion'])
            self.assertTrue(all(q['status'] == 'answered' for q in rec.state['questions']))
            guidance = [t for t in ts if t['agent'] == 'critic' and t['purpose'] == 'guidance']
            self.assertEqual(len(guidance), 1)
            self.assertIn('BATCH: Use pilot data and short tables.', guidance[0]['input'])
            self.assertNotIn('discussion-resolution', guidance[0]['input'])


if __name__ == '__main__':
    unittest.main()
