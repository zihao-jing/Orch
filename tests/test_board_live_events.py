"""SSE clients share disk reads and abandoned streams have a bounded lifetime."""
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from orch.board import Board, LiveEvents, serve


class TestLiveEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rd = Path(self.tmp.name)
        (self.rd / 'live').mkdir()
        (self.rd / 'state.json').write_text('{}')
        self.file = self.rd / 'live' / 'turn.jsonl'
        self.file.write_text(json.dumps({'k': 'text', 's': 'old text'}) + '\n')
        self.board = Board('m', lambda: self.rd)

    def append(self, text):
        with self.file.open('a') as f:
            f.write(json.dumps({'k': 'text', 's': text}) + '\n')

    def test_many_clients_scan_once_and_receive_the_same_new_events(self):
        hub = self.board.live_events
        with patch('orch.board.time.monotonic', return_value=0) as clock, patch.object(hub, 'scan', wraps=hub.scan) as scan:
            cursors = [hub.poll(None)[0] for _ in range(50)]
            self.assertEqual(scan.call_count, 1)
            self.append('new text')
            clock.return_value = 1
            replies = [hub.poll(c) for c in cursors]
            self.assertEqual(scan.call_count, 2)
            self.assertTrue(all(x == replies[0] for x in replies))
            self.assertIn(b'new text', replies[0][1])
            self.assertNotIn(b'old text', replies[0][1])
            self.assertEqual(hub.poll(replies[0][0])[1], b'')
            self.assertNotIn(b'new text', hub.poll(None)[1])

    def test_partial_lines_and_new_files_are_delivered_once_complete(self):
        hub = self.board.live_events
        with patch.object(LiveEvents, 'INTERVAL', 0):
            cursor, _ = hub.poll(None)
            with self.file.open('a') as f:
                f.write('{"k":"text","s":"partial"}')
            cursor, data = hub.poll(cursor)
            self.assertNotIn(b'partial', data)
            with self.file.open('a') as f:
                f.write('\n')
            cursor, data = hub.poll(cursor)
            self.assertIn(b'partial', data)
            (self.rd / 'live' / 'new-turn.jsonl').write_text('{"k":"text","s":"fresh"}\n')
            _, data = hub.poll(cursor)
            self.assertIn(b'fresh', data)
            self.assertIn(b'new-turn', data)

    def test_slow_client_resynchronizes_after_history_is_evicted(self):
        hub = self.board.live_events
        with patch.object(LiveEvents, 'INTERVAL', 0):
            old, _ = hub.poll(None)
            for n in range(hub.KEEP + 2):
                self.append(str(n))
                hub.poll(None)
            _, data = hub.poll(old)
            self.assertEqual(data, b'event: state\ndata: {}\n\n')
            self.assertEqual(len(hub.batches), hub.KEEP)

    def test_http_stream_closes_and_a_reconnection_receives_state(self):
        with patch('orch.board.SSE_MAX_AGE', .05), patch.object(LiveEvents, 'INTERVAL', .01):
            srv, url = serve({'m': self.board}, port=0)
            try:
                for _ in range(2):
                    with urllib.request.urlopen(url + 'api/events', timeout=2) as response:
                        self.assertEqual(response.headers['Connection'], 'close')
                        self.assertIn(b'event: state', response.read())
            finally:
                srv.shutdown()
                srv.server_close()
