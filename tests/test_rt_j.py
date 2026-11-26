"""Package J: the dashboard server. UI-1 to UI-6 (data and API), UI-4, and the fixes of the job (items J1 to J9)."""
import http.client
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def messages(self, before=0, limit=50, q="", agent=""):
        """As the engine: messages older than seq `before` (0: newest), in seq order, and whether more exist."""
        rows = sorted(self.snap.messages.values(), key=lambda m: m.seq)
        rows = [m for m in rows if (not before or m.seq < before) and (not agent or agent in (m.sender, m.recipient))
                and q.lower() in m.body.lower()]
        return (rows[-limit:] if limit else rows), bool(limit) and len(rows) > limit

    def message(self, id):
        return self.snap.messages.get(id)


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
        self.rt.snap.run.goal = "a new goal"
        time.sleep(B.STATE_TTL + 0.05)
        self.assertGreater(call(self.port, "GET", "/api/state")[1]["rev"], r1)

    def test_J4_rev_ignores_the_now_field(self):
        """J4: rev hashes the state without `now`, so a new clock alone does not change it."""
        r1 = call(self.port, "GET", "/api/state")[1]["rev"]
        self.rt.snap.now = 12345.0
        time.sleep(B.STATE_TTL + 0.05)
        self.assertEqual(call(self.port, "GET", "/api/state")[1]["rev"], r1)

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

    def test_J3_messages_and_message_come_from_the_runtime_only(self):
        """J3: the ledger file of the run folder is not read; messages are the runtime's."""
        old = to_dict(message("m0", "critic", "worker-1", "Old", seq=0))
        (Path(self.tmp.name) / "ledger.jsonl").write_text(json.dumps(old) + "\n")
        ids = [m["id"] for m in call(self.port, "GET", "/api/messages")[1]["messages"]]
        self.assertEqual(ids, ["m1", "m2"])
        s, r = call(self.port, "GET", "/api/message?id=m1")
        self.assertEqual(r["message"]["text"]["shown"], "WORDS")
        self.assertEqual(call(self.port, "GET", "/api/message?id=zz")[0], 404)
        self.assertEqual(call(self.port, "GET", "/api/message?id=m0")[0], 404)

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

    def test_J2_host_check_on_every_bind(self):
        """J2: a Host without port is accepted only as an IP literal, a loopback name, this computer's name or
        that name + '.local'. The Origin check stays."""
        me = socket.gethostname()
        for host in ("localhost:%d", "127.0.0.1:%d", "[::1]:%d", "[::1]", "10.0.0.5:%d", "192.168.1.9",
                     me + ":%d", me + ".local:%d"):
            h = host % self.port if "%d" in host else host
            self.assertEqual(call(self.port, "GET", "/api/state", None, {"Host": h})[0], 200, h)
        for host in ("evil.example:%d", "evil.local:%d", "localhost.evil.example:%d", "evil.example", ""):
            h = host % self.port if "%d" in host else host
            self.assertEqual(call(self.port, "GET", "/api/state", None, {"Host": h})[0], 403, h)
        self.assertEqual(call(self.port, "GET", "/api/state", None,
                              {"Origin": "http://evil.example"})[0], 403)

    def test_configured_vscode_preview_can_open_and_control_the_local_board(self):
        proxy = "https://preview.example"
        with patch.dict("os.environ", {"VSCODE_PROXY_URI": proxy + "/proxy/{{port}}/"}):
            rt, _, server, port = make(self.tmp.name)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        headers = {"Host": "preview.example"}
        self.assertEqual(call(port, "GET", "/", headers=headers)[0], 200)
        command = {"type": "WorkerClose", "id": "preview", "agent": "worker-1"}
        self.assertEqual(call(port, "POST", "/api/command", command,
                              {**headers, "Origin": proxy})[0], 200)
        self.assertEqual(call(port, "POST", "/api/command", command,
                              {**headers, "Origin": "https://other.example"})[0], 403)
        self.assertEqual(call(port, "GET", "/", headers={"Host": "other.example"})[0], 403)
        self.assertEqual(len(rt.got), 1)

    def test_J6_live_lines_have_index_and_next(self):
        """J6: every live item carries its line index `n`; `next` is the index after the last line returned."""
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        (live / "worker-1.jsonl").write_text('{"at":1,"k":"text","s":"a"}\n{"at":2,"k":"tool","s":"b"}\n')
        r = call(self.port, "GET", "/api/live?agent=worker-1&from=1")[1]
        self.assertEqual(r, {"items": [{"n": 1, "at": 2, "k": "tool", "s": "b"}], "next": 2})
        self.assertEqual(call(self.port, "GET", "/api/live?agent=../state")[1]["items"], [])

    def test_J6_live_incomplete_last_line_waits_for_its_end(self):
        """J6: a line without its newline is not returned; it comes once it is complete."""
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        (live / "worker-1.jsonl").write_text('{"at":1,"k":"text","s":"a"}\n{"at":3')
        self.assertEqual(call(self.port, "GET", "/api/live?agent=worker-1")[1],
                         {"items": [{"n": 0, "at": 1, "k": "text", "s": "a"}], "next": 1})
        with open(live / "worker-1.jsonl", "a") as f:
            f.write(',"k":"text","s":"c"}\n')
        self.assertEqual(call(self.port, "GET", "/api/live?agent=worker-1&from=1")[1],
                         {"items": [{"n": 1, "at": 3, "k": "text", "s": "c"}], "next": 2})

    def test_J6_live_file_shorter_than_from_is_read_from_zero(self):
        """J6: a file with fewer lines than `from` was replaced or cut: read from 0, with reset true."""
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        (live / "worker-1.jsonl").write_text('{"at":1,"k":"text","s":"a"}\n')
        r = call(self.port, "GET", "/api/live?agent=worker-1&from=5")[1]
        self.assertEqual(r, {"items": [{"n": 0, "at": 1, "k": "text", "s": "a"}], "next": 1, "reset": True})

    def test_J6_live_reads_past_four_megabytes(self):
        """J6: no 4 MB cap on the live file; the page size limits the answer."""
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        line = '{"at":1,"k":"text","s":"' + "x" * 1000 + '"}\n'
        (live / "worker-1.jsonl").write_text(line * 5000)  # about 5 MB
        r = call(self.port, "GET", "/api/live?agent=worker-1&from=4900")[1]
        self.assertEqual((len(r["items"]), r["items"][0]["n"], r["next"]), (100, 4900, 5000))

    def test_J6_sse_first_look_only_notes_size_and_sends_index(self):
        """J6: SSE sends lines written after the stream began, each with its index n; earlier lines are not sent."""
        live = Path(self.tmp.name) / "live"
        live.mkdir()
        (live / "worker-1.jsonl").write_text('{"at":1,"k":"text","s":"a"}\n{"at":2,"k":"tool","s":"b"}\n')
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events")
        resp = c.getresponse()
        first = resp.fp.readline()
        self.assertEqual(first, b"event: state\n")
        resp.fp.readline(); resp.fp.readline()
        with open(live / "worker-1.jsonl", "a") as f:
            f.write('{"at":4,"k":"text","s":"c"}\n')
        got = b""
        while b"event: live" not in got:
            got += resp.fp.readline()
        data = resp.fp.readline()
        self.assertIn(b'"agent": "worker-1"', data)
        self.assertIn(b'"n": 2', data)
        c.close()

    def test_J7_audit_notes_from_the_run_path_or_nothing(self):
        """J7: audit notes are read from run.audit_notes_path; an empty path gives the empty answer."""
        self.assertEqual(call(self.port, "GET", "/api/audit_notes")[1], {"text": "", "digest": "", "path": ""})
        p = Path(self.tmp.name) / "notes" / "audit-decisions.md"
        p.parent.mkdir()
        p.write_text("my notes")
        self.rt.snap.run.audit_notes_path = str(p)
        time.sleep(B.STATE_TTL + 0.05)
        self.assertEqual(call(self.port, "GET", "/api/audit_notes")[1]["text"], "my notes")

    def test_UI_4_golden_and_audit_notes_texts(self):
        g = Path(self.tmp.name) / "g.md"
        g.write_text("rules")
        self.rt.snap.golden.path = str(g)
        time.sleep(B.STATE_TTL + 0.05)
        r = call(self.port, "GET", "/api/golden")[1]
        self.assertEqual((r["text"], r["path"]), ("rules", str(g)))
        self.assertEqual(len(r["digest"]), 16)
        r = call(self.port, "POST", "/api/command", {"type": "AuditNotes", "id": "n", "text": "x", "base": r["digest"]})
        self.assertIsInstance(self.rt.got[-1], AuditNotes)

    def test_J9_golden_relative_path_is_joined_with_the_workspace(self):
        """J9: a relative golden path is read from run.workspace, as the store does."""
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        (ws / "board.md").write_text("from the workspace")
        self.rt.snap.run.workspace = str(ws)
        self.rt.snap.golden.path = "board.md"
        time.sleep(B.STATE_TTL + 0.05)
        self.assertEqual(call(self.port, "GET", "/api/golden")[1]["text"], "from the workspace")

    def test_UI_4_audit_report(self):
        (Path(self.tmp.name) / "audits").mkdir()
        (Path(self.tmp.name) / "audits" / "a1.md").write_text("# Audit\n\n## Reply\n\nreport\n\n## stderr\nboom")
        self.assertEqual(call(self.port, "GET", "/api/audit")[1], {"report": "report", "running": False})

    def test_J1_audit_is_the_reply_only_redacted_and_running_from_the_run(self):
        """J1: only the ## Reply part of the newest audit, without the stderr part, redacted; running is
        run.audit_running."""
        (Path(self.tmp.name) / "audits").mkdir()
        (Path(self.tmp.name) / "audits" / "a1.md").write_text("## Reply\n\nold")
        (Path(self.tmp.name) / "audits" / "a2.md").write_text(
            "# Audit\n\n## Reply\n\nkey sk-abcdefghijklmnop here\n## stderr\nsecret stuff")
        self.rt.snap.run.audit_running = True
        time.sleep(B.STATE_TTL + 0.05)
        r = call(self.port, "GET", "/api/audit")[1]
        self.assertEqual(r, {"report": "key *** here", "running": True})

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

    def test_a_stream_that_reaches_its_age_ends_with_bye(self):
        """The page reconnects at once on `bye`, without the lost banner."""
        old = B.SSE_MAX_AGE
        B.SSE_MAX_AGE = 0.5
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/api/events")
            body = c.getresponse().read()
            c.close()
        finally:
            B.SSE_MAX_AGE = old
        self.assertTrue(body.startswith(b"event: state\n"))
        self.assertTrue(body.endswith(b"event: bye\ndata: {}\n\n"), body[-60:])

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

    def test_J5_new_format_run_read_from_its_runtime_snapshot(self):
        """J5: a run whose state.json has a `runtime` key is read from runtime.snap; the list uses the top-level
        status and started_at."""
        with tempfile.TemporaryDirectory() as root:
            rd = Path(root) / "proj" / "r1"
            rd.mkdir(parents=True)
            snap = Snapshot(run=RunInfo(id="r1", project="proj", started=1767225600.0),
                            agents={"worker-1": agent("worker-1")})
            (rd / "state.json").write_text(json.dumps({"status": "running", "started_at": "2026-01-01T00:00:00",
                                                       "runtime": {"snap": to_dict(snap)}}))
            cur = Path(root) / "cur"
            cur.mkdir()
            rt, board, srv, port = make(str(cur), runs_root=Path(root))
            try:
                runs = call(port, "GET", "/api/runs")[1]["runs"]
                self.assertEqual([(r["id"], r["old"], r["status"]) for r in runs], [("proj/r1", False, "running")])
                s, r = call(port, "GET", "/api/run?id=proj/r1")
                self.assertEqual((s, r["old"]), (200, False))
                self.assertIn("worker-1", r["agents"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_J8_old_run_unknown_role_is_worker_and_chat_is_merged(self):
        """J8: an unknown role is a worker (no 400); turns are numbered by started_at; chat.jsonl rows are merged
        by id into the messages."""
        with tempfile.TemporaryDirectory() as root:
            rd = Path(root) / "proj" / "20260101-000000"
            (rd / "rounds" / "r001").mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({"status": "done", "monitor": "proj", "run_id": rd.name}))
            # the file names sort the other way round from their start times
            (rd / "rounds" / "r001" / "t01-z.json").write_text(json.dumps(
                {"turn_id": "late", "agent": "z", "role": "boss", "purpose": "work", "output": "second",
                 "started_at": "2026-01-01T00:05:00"}))
            (rd / "rounds" / "r001" / "t02-a.json").write_text(json.dumps(
                {"turn_id": "early", "agent": "a", "role": "worker", "purpose": "work", "output": "first",
                 "started_at": "2026-01-01T00:01:00"}))
            (rd / "chat.jsonl").write_text(
                json.dumps({"id": "c1", "role": "human", "with": "a", "text": "hi", "ts": "2026-01-01T00:02:00"})
                + "\n" + json.dumps({"id": "c1", "status": "done"}) + "\n")
            cur = Path(root) / "cur"
            cur.mkdir()
            rt, board, srv, port = make(str(cur), runs_root=Path(root))
            try:
                s, r = call(port, "GET", "/api/run?id=proj/20260101-000000")
                self.assertEqual(s, 200)
                self.assertEqual(r["agents"]["z"]["role"], "worker")
                msgs = sorted(r["messages"].values(), key=lambda m: m["seq"])
                self.assertEqual([m["id"] for m in msgs], ["early", "c1", "late"])
                self.assertEqual((msgs[1]["sender"], msgs[1]["recipient"], msgs[1]["body"]), ("user", "a", "hi"))
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
