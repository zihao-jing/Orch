"""Exercise the Codex streaming transport with a local JSONL protocol peer."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from orch.backends import CodexBackend, TurnRequest


PEER = r'''
import json, os, sys, time, subprocess
mode = os.environ.get('PEER_MODE', 'stream')
tool = None
steers = []
def send(m):
    print(json.dumps(m), flush=True)
def event(method, **params):
    send({'method': method, 'params': {'threadId': 'session-test', 'turnId': 'turn-test', **params}})
for line in sys.stdin:
    m = json.loads(line)
    with open(os.environ['PEER_LOG'], 'a') as f:
        f.write(json.dumps(m) + '\n')
    method = m.get('method')
    if method == 'initialize':
        send({'id': m['id'], 'result': {}})
    elif method in ('thread/start', 'thread/resume'):
        send({'id': m['id'], 'result': {'thread': {'id': 'session-test'}}})
    elif method == 'turn/start':
        if mode.startswith('steer') and os.path.exists(os.environ['PEER_LOG'] + '.tool'):
            mode = 'stream'  # a later Orch turn after an explicit steer rejection
        if mode == 'rpc-error':
            send({'id': m['id'], 'error': {'code': -1, 'message': 'model unavailable'}})
            continue
        send({'id': m['id'], 'result': {'turn': {'id': 'turn-test'}}})
        event('turn/started', turn={'id': 'turn-test'})
        if mode.startswith('steer'):
            # A real child stays alive until the protocol peer lets its current tool finish.
            tool = subprocess.Popen([sys.executable, '-c',
                'import sys; print("ready", flush=True); sys.stdin.readline(); print("tool finished", flush=True)'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert tool.stdout.readline().strip() == 'ready'
            event('item/started', item={'id': 'tool-1', 'type': 'commandExecution', 'command': 'running tool'})
            continue
        if mode == 'unsupported':
            send({'id': 99, 'method': 'item/tool/requestUserInput', 'params': {}})
            continue
        if mode != 'completion-only':
            event('item/agentMessage/delta', itemId='reply', delta='hello')
        if mode == 'hang':
            continue
        time.sleep(0.15)
        if mode != 'completion-only':
            event('item/agentMessage/delta', itemId='reply', delta=' world')
        event('item/completed', item={'id': 'reply', 'type': 'agentMessage', 'text': 'hello world'})
        event('turn/completed', turn={'id': 'turn-test', 'status': 'failed' if mode == 'failed' else 'completed',
              'error': {'message': 'provider failed'} if mode == 'failed' else None})
    elif method == 'turn/steer':
        assert m['params']['expectedTurnId'] == 'turn-test'
        assert m['params']['threadId'] == 'session-test'
        assert tool.poll() is None, 'The running tool was interrupted'
        steers.append(m['params']['input'][0]['text'])
        if mode == 'steer-rejected':
            send({'id': m['id'], 'error': {'code': -32601, 'message': 'steer unsupported'}})
        elif mode not in ('steer-race', 'steer-unconfirmed'):
            send({'id': m['id'], 'result': {'turnId': 'turn-test'}})
        if mode == 'steer-multiple' and len(steers) < 2:
            continue
        tool.stdin.write('finish\n'); tool.stdin.flush(); tool.stdin.close()
        assert tool.stdout.read().strip() == 'tool finished'
        assert tool.wait(timeout=2) == 0
        tool.stdout.close()
        with open(os.environ['PEER_LOG'] + '.tool', 'w') as f: f.write('finished naturally')
        event('item/completed', item={'id': 'tool-1', 'type': 'commandExecution', 'exitCode': 0})
        event('item/completed', item={'id': 'reply', 'type': 'agentMessage', 'text': 'work finished'})
        event('turn/completed', turn={'id': 'turn-test', 'status': 'completed'})
        if mode == 'steer-race':
            send({'id': m['id'], 'error': {'code': -32600, 'message': 'no active turn'}})
'''


class CodexStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.peer = self.root / 'codex-peer'
        self.peer.write_text('#!' + sys.executable + '\n' + PEER)
        self.peer.chmod(0o700)
        self.log = self.root / 'requests.jsonl'
        self.events = []

    def request(self, mode='stream', sid=None, timeout=5):
        return TurnRequest(agent='worker', role='worker', prompt='the prompt', system_prompt='the system',
                           session_id=sid, cwd=str(self.root), timeout=timeout,
                           options={'transport': 'app-server', 'executable': str(self.peer), 'effort': 'low'},
                           env={'PEER_MODE': mode, 'PEER_LOG': str(self.log)},
                           on_event=lambda e: self.events.append((time.monotonic(), e)))

    def messages(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_output_arrives_incrementally_and_prompt_is_sent_once(self):
        result = CodexBackend().run(self.request(), lambda: False)
        self.assertEqual((result.status, result.output, result.session_id), ('ok', 'hello world', 'session-test'))
        chunks = [(t, e['s']) for t, e in self.events if e['k'] == 'text']
        self.assertEqual(''.join(s for _, s in chunks), 'hello world\n\n')
        self.assertGreater(chunks[1][0] - chunks[0][0], 0.1)
        starts = [m for m in self.messages() if m.get('method') == 'turn/start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['params']['effort'], 'low')
        text = starts[0]['params']['input'][0]['text']
        self.assertIn('the system', text)
        self.assertTrue(text.endswith('the prompt'))

    def test_resume_preserves_session_without_repeating_system_prompt(self):
        result = CodexBackend().run(self.request(sid='session-test'), lambda: False)
        self.assertEqual(result.status, 'ok')
        messages = self.messages()
        self.assertTrue(any(m.get('method') == 'thread/resume' for m in messages))
        turn = next(m for m in messages if m.get('method') == 'turn/start')
        self.assertEqual(turn['params']['input'][0]['text'], 'the prompt')

    def test_completion_only_fallback(self):
        result = CodexBackend().run(self.request('completion-only'), lambda: False)
        self.assertEqual(result.output, 'hello world')
        self.assertEqual(''.join(e['s'] for _, e in self.events if e['k'] == 'text'), 'hello world\n\n')

    def test_cancel_retains_partial_output_and_session(self):
        result = CodexBackend().run(self.request('hang'),
                                   lambda: any(e['k'] == 'text' for _, e in self.events))
        self.assertEqual((result.status, result.output, result.session_id), ('stopped', 'hello', 'session-test'))

    def test_timeout_retains_partial_output(self):
        result = CodexBackend().run(self.request('hang', timeout=0.5), lambda: False)
        self.assertEqual((result.status, result.output), ('timeout', 'hello'))

    def test_protocol_and_provider_errors_are_reported(self):
        for mode, error in [('rpc-error', 'model unavailable'), ('unsupported', 'unsupported interaction'),
                            ('failed', 'provider failed')]:
            with self.subTest(mode=mode):
                result = CodexBackend().run(self.request(mode), lambda: False)
                self.assertEqual(result.status, 'error')
                self.assertIn(error, result.stderr)

    def test_live_input_preserves_the_tool_and_turn(self):
        for mode, expected in [('steer-multiple', 'accepted'), ('steer-rejected', 'deferred'),
                               ('steer-race', 'deferred'), ('steer-unconfirmed', 'unknown')]:
            with self.subTest(mode=mode):
                self.events.clear()
                self.log.unlink(missing_ok=True)
                Path(str(self.log) + '.tool').unlink(missing_ok=True)
                count = 2 if mode == 'steer-multiple' else 1
                notes = [{'id': str(i), 'message': 'updated requirement ' + str(i), 'status': 'queued'}
                         for i in range(count)]
                transitions = []
                def pending():
                    if not any(e.get('s') == 'run: running tool' for _, e in self.events):
                        return []
                    return [dict(n) for n in notes if n['status'] == 'queued']
                def delivered(ident, status, details):
                    notes[int(ident)]['status'] = status
                    transitions.append((ident, status))
                req = self.request(mode)
                req.poll_messages, req.on_message = pending, delivered
                result = CodexBackend().run(req, lambda: False)
                self.assertEqual((result.status, result.output, result.session_id),
                                 ('ok', 'work finished', 'session-test'), result.stderr)
                messages = self.messages()
                self.assertEqual(sum(m.get('method') == 'turn/start' for m in messages), 1)
                self.assertFalse(any(m.get('method') == 'turn/interrupt' for m in messages))
                updates = [m['params']['input'][0]['text'] for m in messages if m.get('method') == 'turn/steer']
                self.assertEqual(updates, [n['message'] for n in notes])
                self.assertEqual([n['status'] for n in notes], [expected] * count)
                self.assertEqual(len(transitions), count * 2)
                self.assertEqual(Path(str(self.log) + '.tool').read_text(), 'finished naturally')


if __name__ == '__main__':
    unittest.main()
