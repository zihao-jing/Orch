"""X3: the OpenCode adapter against a fake `opencode serve` (no CLI, no network beyond 127.0.0.1).

Ids covered: MSG-1, MSG-3, MSG-6, JOB-1, JOB-2, JOB-4, JOB-5 (no wake of its own), REC-7, REC-8, SES-2, MOD-2, MOD-7,
TERM-4, and the Adapter rules of contracts.py (receipt vs taken, no replay, session lost, one server per agent).
A live test runs only with ORCH_LIVE=1 (tests/test_rt_x3_live.py).
"""
from rt_helpers import *  # noqa: F403
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from orch.runtime.adapters import make
from orch.runtime.adapters.base import AdapterError
from orch.runtime.adapters.opencode import OpenCodeAdapter, catalog_from


class Fake:
    """The state of a fake server; survives a restart of its HTTP part."""

    def __init__(self):
        self.sessions = {}  # sid -> {"model": {}, "records": [], "inbox": [], "permissions": []}
        self.jobs = []  # entries of the job list (whole database, ghosts included)
        self.shells = set()  # ids GET /api/shell answers 200 for
        self.calls = []
        self.t = 1_000_000
        self.active = set()

    def rec(self, sid, kind, **kw):
        self.t += 10
        r = {"id": kw.pop("id", f"msg_{self.t}"), "type": kind, "time": {"created": self.t}, **kw}
        self.sessions[sid]["records"].append(r)
        return r


def handler_for(fake, pw):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _route(self, method):
            import base64
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode():
                return self._send(401, {"_tag": "UnauthorizedError"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            fake.calls.append((method, u.path, body))
            if u.path == "/api/info":
                return self._send(200, {"version": "2.3.2"})
            if u.path == "/api/model":
                return self._send(200, {"data": [
                    {"modelID": "m1", "providerID": "p", "name": "M1", "enabled": True,
                     "variants": [{"id": "low", "settings": {"apiKey": "SECRET"}}, {"id": "high"}]},
                    {"modelID": "m2", "providerID": "p", "enabled": False, "variants": []}]})
            if u.path == "/api/session" and method == "POST":
                sid = f"ses_{len(fake.sessions) + 1}"
                fake.sessions[sid] = {"model": {**body.get("model", {}), "variant": "default"}, "records": [],
                                      "inbox": [], "permissions": body.get("permissions")}
                return self._send(200, {"data": {"id": sid}})
            if u.path == "/api/session/active":
                return self._send(200, {"data": {s: {"type": "running"} for s in fake.active}})
            if u.path == "/api/experimental/job":
                return self._send(200, {"data": fake.jobs})
            if parts[:2] == ["api", "shell"]:
                hit = parts[2] in fake.shells
                return self._send(200 if hit else 404, {"pid": 1, "status": "running"} if hit else {"_tag": "ShellNotFoundError"})
            if parts[:3] == ["api", "session", parts[2] if len(parts) > 2 else ""] and len(parts) > 2:
                s = fake.sessions.get(parts[2])
                if s is None:
                    return self._send(404, {"_tag": "SessionNotFoundError", "message": "Session not found"})
                rest = parts[3:]
                if not rest:
                    return self._send(200, {"data": {"id": parts[2], "model": s["model"]}})
                if rest == ["model"]:
                    s["model"] = {**body["model"], "variant": body["model"].get("variant", "default")}
                    return self._send(204)
                if rest == ["prompt"]:
                    if any(r["id"] == body["id"] for r in s["records"]) or any(i["id"] == body["id"] for i in s["inbox"]):
                        return self._send(200, {"data": {"id": body["id"]}})
                    s["inbox"].append({"id": body["id"], "text": body["text"]})
                    return self._send(200, {"data": {"id": body["id"]}})
                if rest == ["inbox"]:
                    return self._send(200, {"data": s["inbox"]})
                if rest == ["compact"]:
                    return self._send(200, {"data": {}})
                if rest[0] == "message" and len(rest) == 2:
                    hit = [r for r in s["records"] if r["id"] == rest[1]]
                    return self._send(200 if hit else 404, hit[0] if hit else {"_tag": "MessageNotFoundError"})
                if rest[0] == "message":
                    return self._send(200, {"data": list(reversed(s["records"])), "cursor": {"next": None}})
            self._send(404, {"_tag": "NotFound"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")
    return H


class FakeAdapter(OpenCodeAdapter):
    """The adapter with its server process replaced by `sleep` and an HTTP thread."""
    poll_every = 0

    def __init__(self, clock, fake, **kw):
        super().__init__(clock, **kw)
        self.fake, self.httpd, self.sleeper = fake, None, None

    def _spawn(self, argv, env, cwd, log):
        port = int(argv[argv.index("--port") + 1])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_for(self.fake, env["OPENCODE_SERVER_PASSWORD"]))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        return self.sleeper

    def close(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.sleeper and self.sleeper.poll() is None:
            self.sleeper.kill()
            self.sleeper.wait()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="x3-")
        self.clock, self.fake = FakeClock(), Fake()
        self.adapters = []
        self.spec = AgentSpec("worker-1", Role.WORKER, Choice(backend="opencode", provider="p", model="m1"),
                              cwd=self.tmp, system_prompt="You are a worker.", run_dir=self.tmp)

    def tearDown(self):
        for a in self.adapters:
            a.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def adapter(self):
        a = FakeAdapter(self.clock, self.fake)
        self.adapters.append(a)
        return a

    def started(self, session=None):
        a = self.adapter()
        proc = a.start(self.spec, session)
        return a, proc

    def drain(self, a):
        return list(a.events())

    def of(self, events, cls):
        return [e for e in events if isinstance(e, cls)]

    def user(self, mid, text="hi"):
        return self.fake.rec("ses_1", "user", id="msg_" + mid, text=text)

    def reply(self, text, model=None, content=None):
        return self.fake.rec("ses_1", "assistant", model=model or {"id": "m1", "providerID": "p"},
                             content=content or [{"type": "text", "text": text}])

    def idle(self, **kw):
        return self.fake.rec("ses_1", "idle", outcome=kw.pop("outcome", "succeeded"), **kw)


class TestStart(Base):
    def test_new_session_in_own_folder_with_model_and_deny_rules(self):
        """One server per agent on its own port and password; JOB-1 deny rules; SessionChanged NEW with start uid."""
        a, proc = self.started()
        s = self.fake.sessions["ses_1"]
        self.assertEqual(s["model"]["id"], "m1")
        self.assertEqual(s["model"]["providerID"], "p")
        self.assertIn({"action": "shell", "resource": "*nohup*", "effect": "deny"}, s["permissions"])
        self.assertEqual(proc.stream, "ses_1")
        self.assertTrue((Path(self.tmp) / "cli" / "worker-1" / "server.json").exists())
        mode = (Path(self.tmp) / "cli" / "worker-1" / "password").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        ev = self.of(self.drain(a), SessionChanged)[0]
        self.assertEqual((ev.change, ev.session_id, ev.uid), (SessionChange.NEW, "ses_1", f"start:{proc.pid}"))

    def test_make_finds_the_adapter_and_caps(self):
        a = make("opencode", self.clock)
        c = a.caps
        self.assertTrue(c.sends_while_writing and c.receipts and c.wakes_itself and c.shares_terminal and c.job_record)

    def test_resume_switches_a_different_model_without_validation(self):
        """MOD-7: the switch call accepts anything; only a real turn proves it."""
        a, _ = self.started()
        a.send("x00000000000a", "one")
        self.spec.choice = Choice(backend="opencode", provider="q", model="m9", effort="low")
        b = self.adapter()
        a.stop()
        b.fake = self.fake
        b.start(self.spec, SessionRef("ses_1"))
        self.assertEqual(self.fake.sessions["ses_1"]["model"]["id"], "m9")
        self.assertEqual(self.fake.sessions["ses_1"]["model"]["variant"], "low")
        self.assertEqual([e.change for e in self.of(self.drain(b), SessionChanged)], [SessionChange.RESUMED])

    def test_SES_2_resume_of_a_missing_session_is_lost(self):
        a = self.adapter()
        a.start(self.spec, SessionRef("ses_nope"))
        ev = self.of(self.drain(a), SessionChanged)
        self.assertEqual([e.change for e in ev], [SessionChange.LOST])
        with self.assertRaises(AdapterError):
            a.send("x00000000000a", "hi")


class TestMessages(Base):
    def test_MSG_6_receipt_at_post_and_client_chosen_id_with_system_prompt_once(self):
        a, _ = self.started()
        self.drain(a)
        a.send("x0123456789ab", "Do it.")
        self.assertEqual(self.fake.calls[-1][2]["id"], "msg_x0123456789ab")
        self.assertEqual(self.fake.calls[-1][2]["delivery"], "steer")
        self.assertIn("You are a worker.", self.fake.calls[-1][2]["text"])
        rec = self.of(self.drain(a), Received)
        self.assertEqual([(e.message_id, e.taken) for e in rec], [("x0123456789ab", False)])
        a.send("x0123456789ac", "More.")
        self.assertNotIn("You are a worker.", self.fake.calls[-1][2]["text"])

    def test_MSG_1_taken_only_when_delivered_and_turn_start_names_the_message(self):
        a, _ = self.started()
        self.drain(a)
        a.send("x0123456789ab", "Go")
        self.drain(a)
        self.assertFalse(self.of(self.drain(a), TurnStarted))  # enqueued, not delivered: not in the records
        self.user("x0123456789ab", "Go")
        ev = self.of(self.drain(a), TurnStarted)
        self.assertEqual([e.message_ids for e in ev], [("x0123456789ab",)])

    def test_MSG_1_a_message_sent_while_writing_is_taken_into_the_turn(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        self.reply("working")
        self.drain(a)
        a.send("x0123456789ac", "also")
        self.user("x0123456789ac")
        ev = self.drain(a)
        self.assertEqual([(e.message_id, e.taken) for e in self.of(ev, Received)],
                         [("x0123456789ac", False), ("x0123456789ac", True)])
        self.assertFalse(self.of(ev, TurnStarted))

    def test_MSG_6_arrived_checks_records_then_inbox(self):
        a, _ = self.started()
        a.send("x0123456789ab", "Go")
        self.assertTrue(a.arrived("x0123456789ab"))  # in the inbox
        self.assertFalse(a.arrived("x0123456789ff"))
        self.user("x0123456789ab")
        self.assertTrue(a.arrived("x0123456789ab"))  # in the history

    def test_send_errors_are_classified(self):
        a, _ = self.started()
        del self.fake.sessions["ses_1"]
        with self.assertRaises(AdapterError) as cm:
            a.send("x0123456789ab", "x")
        self.assertEqual(cm.exception.error, ErrorClass.SESSION_LOST)


class TestTurns(Base):
    def test_MSG_3_turn_start_text_end_and_model(self):
        a, _ = self.started()
        self.drain(a)
        self.user("x0123456789ab")
        self.reply("Hello there")
        self.idle()
        ev = self.drain(a)
        kinds = [type(e).__name__ for e in ev]
        self.assertEqual(kinds, ["TurnStarted", "ModelReported", "Text", "TurnEnded"])
        end = ev[-1]
        self.assertEqual((end.status, end.text, end.session_id), (TurnStatus.OK, "Hello there", "ses_1"))
        self.assertEqual(self.of(ev, ModelReported)[0].model, "p/m1")

    def test_tools_and_text_growth_are_progress(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        r = self.reply("a", content=[{"type": "tool", "id": "t1", "name": "shell", "state": {"status": "running"}}])
        ev = self.drain(a)
        self.assertEqual([e.tool_id for e in self.of(ev, ToolStart)], ["t1"])
        r["content"] = [{"type": "tool", "id": "t1", "name": "shell", "state": {"status": "completed"}},
                        {"type": "text", "text": "done"}]
        ev = self.drain(a)
        self.assertEqual([e.tool_id for e in self.of(ev, ToolEnd)], ["t1"])
        self.assertEqual([e.text for e in self.of(ev, Text)], ["done"])

    def test_repeated_message_id_empty_idle_is_no_turn(self):
        a, _ = self.started()
        self.drain(a)
        self.idle()
        self.assertFalse(self.of(self.drain(a), TurnEnded))

    def test_errors_become_classes(self):
        a, _ = self.started()
        self.drain(a)
        cases = [({"type": "provider.auth", "message": "Invalid API-key", "status": 401}, ErrorClass.OTHER),
                 ({"type": "provider.no-route", "message": "Model unavailable: p/zz"}, ErrorClass.OTHER),
                 ({"type": "x", "message": "overloaded", "status": 529}, ErrorClass.TRANSIENT),
                 ({"type": "x", "message": "Your credit balance is too low"}, ErrorClass.BILLING)]
        for i, (err, want) in enumerate(cases):
            self.user(f"x0123456789a{i}")
            self.idle(outcome="failed", error=err)
            end = self.of(self.drain(a), TurnEnded)[0]
            self.assertEqual((end.status, end.error), (TurnStatus.ERROR, want), err)

    def test_interrupted_is_stopped(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        self.idle(outcome="interrupted")
        self.assertEqual(self.of(self.drain(a), TurnEnded)[0].status, TurnStatus.STOPPED)

    def test_compaction_is_session_changed_and_no_turn(self):
        a, _ = self.started()
        self.drain(a)
        self.assertTrue(a.compact())
        self.fake.rec("ses_1", "compaction", status="completed", summary="s")
        self.idle()
        ev = self.drain(a)
        self.assertEqual([e.change for e in self.of(ev, SessionChanged)], [SessionChange.COMPACTED])
        self.assertFalse(self.of(ev, TurnStarted) or self.of(ev, TurnEnded))


class TestJobs(Base):
    def synthetic(self, shell, exit_code=0):
        return self.fake.rec("ses_1", "synthetic", text="<shell/>",
                             metadata={"source": "shell", "shellID": shell, "state": "completed", "exit": exit_code})

    def test_JOB_2_job_listed_before_turn_end_is_started_and_filtered_by_session_and_ghost(self):
        a, _ = self.started()
        self.drain(a)
        self.fake.shells = {"sh_a"}
        self.fake.jobs = [
            {"id": "sh_a", "sessionID": "ses_1", "status": "running", "command": "sleep 9", "time": {"started": 5000}},
            {"id": "sh_ghost", "sessionID": "ses_1", "status": "running", "command": "x", "time": {"started": 1}},
            {"id": "sh_o", "sessionID": "ses_other", "status": "running", "command": "y", "time": {"started": 1}}]
        self.fake.shells |= {"sh_o"}
        self.user("x0123456789ab")
        self.reply("started")
        self.idle()
        ev = self.drain(a)
        self.assertEqual([e.job.id for e in self.of(ev, JobStarted)], ["worker-1/sh_a"])
        self.assertEqual([type(e).__name__ for e in ev][-2:], ["JobStarted", "TurnEnded"])
        self.assertEqual([j.id for j in a.jobs()], ["worker-1/sh_a"])

    def test_JOB_4_server_wake_is_a_turn_after_job_ended(self):
        a, _ = self.started()
        self.drain(a)
        self.synthetic("sh_a", 3)
        self.reply("exit 3")
        self.idle()
        ev = self.drain(a)
        self.assertEqual([type(e).__name__ for e in ev][:2], ["JobEnded", "TurnStarted"])
        self.assertEqual(self.of(ev, JobEnded)[0].exit_code, 3)
        self.assertEqual(self.of(ev, TurnStarted)[0].message_ids, ())
        self.assertEqual(a.jobs()[0].exit_code, 3)

    def test_JOB_4_job_end_inside_a_running_turn_is_noticed_not_a_new_turn(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        self.drain(a)
        self.synthetic("sh_a")
        ev = self.drain(a)
        self.assertEqual([type(e).__name__ for e in ev], ["JobEnded", "JobNoticed"])

    def test_JOB_5_lost_job_is_not_listed_after_restart(self):
        """A ghost entry (listed running, shell 404) is left out, so the supervisor can mark it lost."""
        a, _ = self.started()
        self.fake.jobs = [{"id": "sh_dead", "sessionID": "ses_1", "status": "running", "command": "x", "time": {}}]
        self.assertEqual(a.jobs(), [])


class TestRestart(Base):
    def test_REC_7_reconnect_reads_only_what_was_missed_and_no_event_repeats(self):
        a, proc = self.started()
        self.user("x0123456789ab")
        self.reply("first")
        self.idle()
        first = self.drain(a)
        pos = [e.pos for e in first if e.pos][-1]
        keys = {e.key for e in first}
        a.detach()
        self.user("x0123456789ac")
        self.reply("second")
        self.idle()
        b = self.adapter()
        b.fake = self.fake
        proc.read_pos = pos
        self.assertTrue(b.reconnect(proc))
        ev = self.drain(b)
        self.assertEqual([e.text for e in self.of(ev, TurnEnded)], ["second"])
        self.assertFalse(keys & {e.key for e in ev})

    def test_REC_7_a_turn_open_at_the_restart_ends_once(self):
        a, proc = self.started()
        self.user("x0123456789ab")
        ev = self.drain(a)
        pos = [e.pos for e in ev if e.pos][-1]
        self.assertEqual(pos.split(":")[1], "1")
        a.detach()
        self.reply("done")
        self.idle()
        b = self.adapter()
        proc.read_pos = pos
        self.assertTrue(b.reconnect(proc))
        ev = self.drain(b)
        self.assertEqual([type(e).__name__ for e in ev if not isinstance(e, (Text, ModelReported))], ["TurnEnded"])

    def test_REC_8_start_joins_the_server_an_earlier_start_left(self):
        a, proc = self.started()
        ports = len([c for c in self.fake.calls if c[1] == "/api/session" and c[0] == "POST"])
        a.detach()
        b = self.adapter()
        proc2 = b.start(self.spec, SessionRef("ses_1"))
        self.assertEqual((proc2.pid, proc2.endpoint), (proc.pid, proc.endpoint))
        self.assertEqual(ports, 1)
        self.assertIsNone(b.sleeper)  # nothing new was spawned

    def test_REC_7_dead_server_is_reported_once(self):
        a, proc = self.started()
        self.user("x0123456789ab")
        self.drain(a)
        a.sleeper.kill()
        a.sleeper.wait()
        ev = self.drain(a)
        self.assertEqual([type(e).__name__ for e in ev], ["TurnEnded", "ProcessExited"])
        self.assertTrue(ev[1].reported)
        self.assertEqual(self.drain(a), [])

    def test_reconnect_to_a_gone_server_gives_false_and_the_exit(self):
        a, proc = self.started()
        a.stop()
        proc.read_pos = "0:0"
        b = self.adapter()
        (Path(self.tmp) / "cli" / "worker-1").mkdir(parents=True, exist_ok=True)
        self.assertFalse(b.reconnect(proc))
        self.assertEqual([type(e).__name__ for e in self.drain(b)], ["ProcessExited"])

    def test_stop_ends_the_open_turn_as_stopped_and_exit_asked(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        self.drain(a)
        a.stop()
        ev = self.drain(a)
        self.assertEqual([(type(e).__name__, getattr(e, "status", None)) for e in ev],
                         [("TurnEnded", TurnStatus.STOPPED), ("ProcessExited", None)])
        self.assertTrue(ev[1].asked)


class TestSession(Base):
    def test_SES_2_session_deleted_while_running_is_lost(self):
        a, _ = self.started()
        self.drain(a)
        del self.fake.sessions["ses_1"]
        self.assertEqual([e.change for e in self.of(self.drain(a), SessionChanged)], [SessionChange.LOST])
        self.assertEqual(self.drain(a), [])

    def test_SES_2_session_deleted_in_an_open_turn_ends_it_as_session_lost(self):
        a, _ = self.started()
        self.user("x0123456789ab")
        self.drain(a)
        del self.fake.sessions["ses_1"]
        end = self.of(self.drain(a), TurnEnded)[0]
        self.assertEqual(end.error, ErrorClass.SESSION_LOST)


class TestTerminal(Base):
    def test_TERM_4_terminal_command_joins_the_server_and_hides_the_password(self):
        a, proc = self.started()
        cmd = a.terminal_command(self.spec, SessionRef("ses_1", cwd=self.tmp))
        pw = (Path(self.tmp) / "cli" / "worker-1" / "password").read_text()
        self.assertNotIn(pw, " ".join(cmd))
        self.assertIn("--server http://127.0.0.1:", cmd[-1])
        self.assertIn("--session ses_1", cmd[-1])
        self.assertIn("OPENCODE_SERVER_PASSWORD", cmd[-1])

    def test_transcript_reads_user_and_agent_lines(self):
        a, _ = self.started()
        self.user("x0123456789ab", "question")
        self.reply("answer")
        said = a.transcript(SessionRef("ses_1"), 0)
        self.assertEqual([(s.by, s.text) for s in said], [(USER, "question"), ("worker-1", "answer")])


class TestCatalog(unittest.TestCase):
    def test_MOD_2_variants_are_the_efforts_and_settings_are_dropped(self):
        data = {"data": [{"modelID": "m1", "providerID": "p", "name": "M1", "enabled": True,
                          "variants": [{"id": "low", "settings": {"apiKey": "SECRET"}}, {"id": "high"}]},
                         {"modelID": "m2", "providerID": "p", "enabled": False}]}
        out = catalog_from(data)
        self.assertEqual([(m.id, m.efforts) for m in out], [("p/m1", ["low", "high"])])
        self.assertNotIn("SECRET", repr(out))


if __name__ == "__main__":
    unittest.main()
