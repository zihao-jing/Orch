"""Package J: the dashboard server. UI-1 to UI-6 (data and API), UI-4."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rt_helpers import *  # noqa: F401,F403
from orch.runtime import board as B


class FakeRuntime:
    def __init__(self, snap):
        self.snap, self.got = snap, []

    def state(self):
        return public_state(self.snap)

    def submit(self, command):
        self.got.append(command)
        return reply_json(CommandReply(command_id=command.id, text=TextRef(next(iter(TextKey)), {})))


def make(tmp, **kw):
    snap = Snapshot(agents={"critic": agent("critic"), "worker-1": agent("worker-1")},
                    messages={"m1": message("m1", "critic", "worker-1", "Hello there", seq=1, text=TextRef(next(iter(TextKey)), {})),
                              "m2": message("m2", "worker-1", "critic", "Done", seq=2)})
    rt = FakeRuntime(snap)
    board = B.Board(rt, Path(tmp), words=lambda ref: "WORDS", **kw)
    srv, url = B.serve(board, "127.0.0.1", 0)
    return rt, board, srv, srv.server_address[1]


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=body if isinstance(body, (bytes, type(None))) else json.dumps(body), headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt, self.board, self.srv, self.port = make(self.tmp.name)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_UI_1_state_has_agents_rev_shown_and_no_rendered(self):
        s, st = call(self.port, "GET", "/api/state")
        self.assertEqual(s, 200)
        self.assertIn("worker-1", st["agents"])
        self.assertEqual(st["version"], CONTRACT_VERSION)
        self.assertNotIn("rendered", st["messages"]["m1"])
        self.assertEqual(st["messages"]["m1"]["text"]["shown"], "WORDS")
        self.assertEqual(call(self.port, "GET", "/api/state")[1]["rev"], st["rev"])

    def test_UI_1_rev_changes_with_the_state(self):
        r1 = call(self.port, "GET", "/api/state")[1]["rev"]
        self.rt.snap.paused = True
        time.sleep(B.STATE_TTL + 0.05)
        self.assertGreater(call(self.port, "GET", "/api/state")[1]["rev"], r1)

    def test_UI_2_messages_in_order_with_paging_and_search(self):
        s, r = call(self.port, "GET", "/api/messages")
        self.assertEqual([m["id"] for m in r["messages"]], ["m1", "m2"])
        s, r = call(self.port, "GET", "/api/messages?limit=1")
        self.assertEqual([m["id"] for m in r["messages"]], ["m2"])
        self.assertTrue(r["more"])
        s, r = call(self.port, "GET", "/api/messages?before=2")
        self.assertEqual([m["id"] for m in r["messages"]], ["m1"])
        self.assertEqual([m["id"] for m in call(self.port, "GET", "/api/messages?q=done")[1]["messages"]], ["m2"])
        self.assertEqual(len(call(self.port, "GET", "/api/messages?agent=nobody")[1]["messages"]), 0)

    def test_UI_2_ledger_adds_older_messages_and_one_message_with_shown(self):
        old = to_dict(message("m0", "critic", "worker-1", "Old", seq=0))
        (Path(self.tmp.name) / "ledger.jsonl").write_text(json.dumps(old) + "\n")
        ids = [m["id"] for m in call(self.port, "GET", "/api/messages")[1]["messages"]]
        self.assertEqual(ids, ["m0", "m1", "m2"])
        s, r = call(self.port, "GET", "/api/message?id=m1")
        self.assertEqual(r["message"]["text"]["shown"], "WORDS")
        self.assertEqual(call(self.port, "GET", "/api/message?id=zz")[0], 404)

    def test_UI_3_command_goes_to_the_engine_and_bad_ones_are_400(self):
        s, r = call(self.port, "POST", "/api/command", {"type": "WorkerClose", "id": "c1", "agent": "worker-1"})
        self.assertEqual((s, r["ok"], r["shown"]), (200, True, "WORDS"))
        self.assertIsInstance(self.rt.got[0], WorkerClose)
        self.assertEqual(call(self.port, "POST", "/api/command", {"type": "Nope", "id": "c"})[0], 400)
        self.assertEqual(call(self.port, "POST", "/api/command", {"type": "Chat", "id": "c", "text": 5})[0], 400)
        self.assertEqual(call(self.port, "POST", "/api/command", {"type": "Chat", "text": "x"})[0], 400)
        self.assertEqual(call(self.port, "POST", "/api/command", b"{bad")[0], 400)

    def test_UI_3_request_limits_and_cross_site(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/command", headers={"Content-Type": "application/json",
                                                   "Content-Length": str(B.MAX_BODY + 1)})
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        self.assertEqual(call(self.port, "POST", "/api/command", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(call(self.port, "POST", "/api/command", {}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(call(self.port, "GET", "/api/state", None, {"Host": "evil.example"})[0], 403)
        self.assertEqual(call(self.port, "GET", "/api/command")[0], 404)

    def test_UI_1_live_lines_and_sse(self):
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        (live / "worker-1.jsonl").write_text('{"at":1,"k":"text","s":"a"}\n{"at":2,"k":"tool","s":"b"}\n{"at":3')
        r = call(self.port, "GET", "/api/live?agent=worker-1&from=1")[1]
        self.assertEqual(r["items"], [{"at": 2, "k": "tool", "s": "b"}])
        self.assertEqual(r["next"], 2)
        self.assertEqual(call(self.port, "GET", "/api/live?agent=../state")[1]["items"], [])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events")
        resp = c.getresponse()
        first = resp.fp.readline()
        self.assertEqual(first, b"event: state\n")
        resp.fp.readline(); resp.fp.readline()
        with open(live / "worker-1.jsonl", "a") as f:
            f.write(',"k":"text","s":"c"}\n')
        got = b""
        while b"event: live" not in got:
            got += resp.fp.readline()
        self.assertIn(b'"agent": "worker-1"', resp.fp.readline())
        c.close()

    def test_UI_4_golden_and_audit_notes_texts(self):
        g = Path(self.tmp.name) / "g.md"
        g.write_text("rules")
        self.rt.snap.golden.path = str(g)
        time.sleep(B.STATE_TTL + 0.05)
        r = call(self.port, "GET", "/api/golden")[1]
        self.assertEqual((r["text"], r["path"]), ("rules", str(g)))
        self.assertEqual(len(r["digest"]), 16)
        (Path(self.tmp.name) / "audit-decisions.md").write_text("my notes")
        self.assertEqual(call(self.port, "GET", "/api/audit_notes")[1]["text"], "my notes")
        r = call(self.port, "POST", "/api/command", {"type": "AuditNotes", "id": "n", "text": "x", "base": r["digest"]})
        self.assertIsInstance(self.rt.got[-1], AuditNotes)

    def test_UI_4_audit_report(self):
        (Path(self.tmp.name) / "audits").mkdir()
        (Path(self.tmp.name) / "audits" / "a1.md").write_text("report")
        self.assertEqual(call(self.port, "GET", "/api/audit")[1], {"report": "report", "running": False})

    def test_UI_5_no_review_api(self):
        for p in ("/api/review", "/api/review_comments"):
            self.assertEqual(call(self.port, "GET", p)[0], 404)

    def test_safety_page_files_only_and_no_traversal(self):
        for p in ("/../board.py", "/%2e%2e/board.py", "/page/../board.py", "/.gitkeep", "/..%2fboard.py"):
            self.assertEqual(call(self.port, "GET", p)[0], 404, p)
        self.assertEqual(call(self.port, "GET", "/api/turn?id=../../x")[0], 404)
        self.assertEqual(call(self.port, "GET", "/api/run?id=../..")[0], 404)

    def test_safety_error_has_no_detail(self):
        self.rt.submit = lambda c: 1 / 0
        s, r = call(self.port, "POST", "/api/command", {"type": "Chat", "id": "c", "text": "x"})
        self.assertEqual((s, r), (500, {"ok": False, "error": "the command failed"}))

    def test_safety_turn_stderr_redacted(self):
        d = Path(self.tmp.name) / "rounds" / "r001"
        d.mkdir(parents=True)
        (d / "t01-w.json").write_text(json.dumps({"turn_id": "x1", "agent": "w", "stderr": "api_key=abcdefgh12345"}))
        r = call(self.port, "GET", "/api/turn?id=x1")[1]
        self.assertNotIn("abcdefgh12345", r["stderr"])

    def test_safety_slow_state_does_not_block_other_clients(self):
        """A stuck SSE reader holds only its own thread."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events")
        c.getresponse()
        t = time.time()
        self.assertEqual(call(self.port, "GET", "/api/state")[0], 200)
        self.assertLess(time.time() - t, 2)
        c.close()


class TestOldRuns(unittest.TestCase):
    def test_UI_6_old_run_listed_and_read(self):
        with tempfile.TemporaryDirectory() as root:
            rd = Path(root) / "proj" / "20260101-000000"
            (rd / "rounds" / "r001").mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({"status": "done", "started_at": "2026-01-01T00:00:00",
                                                       "monitor": "proj", "run_id": rd.name}))
            (rd / "rounds" / "r001" / "t01-w.json").write_text(json.dumps(
                {"turn_id": "r001-t01-w", "agent": "w", "role": "worker", "purpose": "work", "output": "Did it",
                 "started_at": "2026-01-01T00:01:00"}))
            cur = Path(root) / "cur"
            cur.mkdir()
            rt, board, srv, port = make(str(cur), runs_root=Path(root))
            try:
                runs = call(port, "GET", "/api/runs")[1]["runs"]
                self.assertEqual([(r["id"], r["old"], r["status"]) for r in runs], [("proj/20260101-000000", True, "done")])
                s, r = call(port, "GET", "/api/run?id=proj/20260101-000000")
                self.assertEqual(s, 200)
                self.assertEqual(r["agents"]["w"]["status"], "closed")
                m = next(iter(r["messages"].values()))
                self.assertEqual((m["sender"], m["recipient"], m["body"]), ("w", "critic", "Did it"))
                self.assertEqual(call(port, "GET", "/api/turn?id=r001-t01-w&run=proj/20260101-000000")[1]["output"], "Did it")
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
