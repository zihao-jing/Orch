"""A board restart must not leave chats running, or interrupt another board's chat."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from orch.board import _append, _chat_rows, _start_chat


class ChatInterruptTest(unittest.TestCase):
    def test_process_death_is_recorded_once_with_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            script = '''
import sys, time
from pathlib import Path
from orch.board import _start_chat
from orch.recorder import live_event
rd = Path(sys.argv[1])
live_event(rd, 'chat-test', {'k': 'text', 's': 'Partial reply'})
_start_chat(rd, {'id': 'chat-test', 'role': 'manager', 'status': 'running', 'ts': 'original'}, lambda: time.sleep(60))
print('ready', flush=True)
time.sleep(60)
'''
            child = subprocess.Popen([sys.executable, '-c', script, tmp], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                self.assertEqual(_chat_rows(rd)[0]['status'], 'running')
                child.kill()
                child.wait(timeout=5)
                row = _chat_rows(rd)[0]
                self.assertEqual(row['status'], 'interrupt')
                self.assertEqual(row['text'], 'Partial reply')
                self.assertEqual(row['ts'], 'original')
                self.assertIn('interrupted_detected_at', row)
                self.assertIn('last_activity_ts', row)
                before = (rd / 'chat.jsonl').read_text()
                _chat_rows(rd)
                self.assertEqual((rd / 'chat.jsonl').read_text(), before)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()

    def test_queued_chat_and_completed_chat_are_not_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            release, done = threading.Event(), threading.Event()
            def work():
                release.wait(5)
                _append(rd / 'chat.jsonl', {'id': 'chat-test', 'status': 'ok', 'text': 'Reply'})
                done.set()
            _start_chat(rd, {'id': 'chat-test', 'status': 'running'}, work)
            try:
                self.assertEqual(_chat_rows(rd)[0]['status'], 'running')
            finally:
                release.set()
            self.assertTrue(done.wait(5))
            self.assertEqual(_chat_rows(rd)[0]['status'], 'ok')

    def test_unfinished_task_is_interrupted_but_legacy_owner_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            _append(rd / 'chat.jsonl', {'id': 'legacy', 'status': 'running'})
            _start_chat(rd, {'id': 'chat-test', 'status': 'running'}, lambda: None)
            until = time.monotonic() + 5
            while time.monotonic() < until:
                rows = _chat_rows(rd)
                if rows[-1]['status'] == 'interrupt':
                    break
                time.sleep(.01)
            self.assertEqual(rows[0]['status'], 'running')
            self.assertEqual(rows[-1]['status'], 'interrupt')


if __name__ == '__main__':
    unittest.main()
