"""B2: messages from every reply are routed; delivery is activation; each message has a status checked against the
recipient's actual prompt; newer messages supersede older waiting ones; a terminal session holds the agent's turns."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.backends import MockBackend, TurnResult  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.recorder import agent_lock, read_json  # noqa: E402
from role_view_checks import role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

HOOK = ROOT / "orch" / "hooks" / "terminal_relay.py"


def _runner(mon, rec):
    from orch.managed import ManagedRunner
    r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
    r.rec = rec
    r._main_ident = threading.get_ident()
    return r


def _last(rec, agent):
    return [t for t in turns(rec) if t["agent"] == agent][-1]


def _ledger(rec):
    return read_json(rec.run_dir / "state.json")["messages"]


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _done_run(self, **kw):
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1, **kw.get("mgr", {})}, critic=kw.get("critic"),
                                          worker=kw.get("worker"))])
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        return rec, load_config(cfg).monitor("m")

    def _chat(self, rec, mon, who, reply):
        from orch.board import Board

        def fake(backend, req, should_stop):
            return TurnResult("ok", reply, req.session_id or "mock-chat", 0, "", ["mock"], {})
        b = Board("m", lambda: rec.run_dir, mon)
        with mock.patch.object(MockBackend, "run", fake):
            cid = b.chat("hello", agent=who)["id"]
            for _ in range(100):
                rows = [json.loads(x) for x in (rec.run_dir / "chat.jsonl").read_text().splitlines()]
                if any(x.get("id") == cid and x.get("status") not in (None, "running") for x in rows):
                    return b, cid
                time.sleep(0.05)
        self.fail("the chat reply did not arrive")

    # ---- board chat replies reach their recipient without waiting for a round; idle recipients are activated

    def test_a_critic_chat_note_up_starts_an_idle_managers_turn(self):
        rec, mon = self._done_run()
        b, cid = self._chat(rec, mon, "critic", 'Fine.\n<message to="Alex">The tables need a source column.</message>')
        r = _runner(mon, rec)
        r.activate("wait")
        ts = turns(rec)
        last = ts[-1]
        self.assertEqual((last["agent"], last["purpose"]), ("boss", "incoming"))
        self.assertIn("The tables need a source column.", last["input"])
        m = next(x for x in r.ledger.rows if x["source"] == cid)
        self.assertEqual((m["status"], m["turn"], m["how"], m["via"]), ("delivered", last["turn_id"], "prompt", "chat"))
        rel = b.data()["relays"][cid]  # the board shows the status the ledger confirmed
        self.assertEqual((rel[0]["status"], rel[0]["turn"]), ("delivered", last["turn_id"]))

    def test_a_manager_chat_message_starts_an_idle_critics_turn_and_its_reply_goes_up(self):
        rec, mon = self._done_run(critic={"replies": {"incoming": [
            'I will. <message to="Sam">Use short tables from now on.</message>']}})
        b, cid = self._chat(rec, mon, None, 'Sure.\n<message to="Jordan">Use short tables.</message>')
        r = _runner(mon, rec)
        r.activate("between")  # a chat message is not round traffic: it starts its turn even between phases
        ts = turns(rec)
        crit = next(t for t in ts if t["purpose"] == "incoming" and t["agent"] == "critic")
        self.assertIn("Use short tables.", crit["input"])
        self.assertNotRegex(crit["input"], r"(?i)\b(manager|orch|board|Alex)\b")
        self.assertTrue(any(t["agent"] == "boss" and t["purpose"] == "incoming" and "I will." in t["input"]
                            for t in ts))  # the critic's answer to its user starts the manager's turn
        L = r.ledger.rows
        self.assertEqual(next(m for m in L if m["source"] == cid)["status"], "delivered")
        w = next(m for m in L if m["source"] == crit["turn_id"] and m["to"] == "writer")
        self.assertEqual((w["status"], list(w["carrier"])), ("sent", ["wm"]))  # a worker gets it at its next turn
        self.assertEqual(role_leaks(rec, mon), [])

    def test_a_busy_recipient_gets_the_message_after_its_turn(self):
        rec, mon = self._done_run()
        _b, cid = self._chat(rec, mon, None, '<message to="Jordan">Check the units.</message>')
        r = _runner(mon, rec)
        n = len(turns(rec))
        with agent_lock(rec.run_dir, "critic"):  # mid-turn (a chat or a turn of the run holds the lock)
            r.activate("wait")
            self.assertEqual(len(turns(rec)), n)
            self.assertEqual(next(m for m in r.ledger.rows if m["source"] == cid)["status"], "sent")
        r.activate("wait")
        self.assertEqual(_last(rec, "critic")["purpose"], "incoming")
        self.assertEqual(next(m for m in r.ledger.rows if m["source"] == cid)["status"], "delivered")

    def test_a_critic_message_to_a_worker_mid_turn_reaches_its_next_prompt(self):
        """A critic chat reply sent while the writer works: delivered at the writer's next turn, status from it."""
        rd = {}

        def drop():
            for _ in range(400):
                for t in (self.tmp / "out").glob("**/rounds/r*/t*-writer.json"):
                    if (read_json(t) or {}).get("status") == "running":
                        rd["run"] = t.parents[2]
                        (t.parents[2] / "inbox" / "20990101-000000-chat.json").write_text(json.dumps(
                            {"to": ["writer"], "from": "critic", "message": "Also add a units column.",
                             "source": "chat-001-1", "i": 0, "via": "chat"}))
                        return
                time.sleep(0.05)
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        rec, _mon = self._done_run(worker={"delay": 2})
        th.join(timeout=5)
        w = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertNotIn("Also add a units column.", w[0]["input"])
        self.assertIn("Also add a units column.", w[1]["input"])
        m = next(x for x in _ledger(rec) if x["source"] == "chat-001-1")
        self.assertEqual((m["status"], m["turn"]), ("delivered", w[1]["turn_id"]))

    def test_an_urgent_message_cuts_in_and_is_marked_delivered_by_the_cut_in(self):
        def drop():
            for _ in range(400):
                for t in (self.tmp / "out").glob("**/rounds/r*/t*-writer.json"):
                    if (read_json(t) or {}).get("status") == "running":
                        time.sleep(0.3)
                        (t.parents[2] / "inbox" / "20990101-000000-urgent.json").write_text(json.dumps(
                            {"to": ["writer"], "message": "Stop checking provenance.", "urgent": True}))
                        return
                time.sleep(0.05)
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        rec, _mon = self._done_run(worker={"delay": 3})
        th.join(timeout=5)
        first = [t for t in turns(rec) if t["agent"] == "writer"][0]
        self.assertTrue(first.get("interruptions"))
        m = next(x for x in _ledger(rec) if x["body"] == "Stop checking provenance.")
        self.assertEqual((m["status"], m["how"], m["turn"]), ("delivered", "cut in", first["turn_id"]))

    def test_a_note_for_the_critic_is_answered_while_the_worker_works(self):
        def drop():
            for _ in range(400):
                for t in (self.tmp / "out").glob("**/rounds/r*/t*-writer.json"):
                    if (read_json(t) or {}).get("status") == "running":
                        (t.parents[2] / "inbox" / "20990101-000000-note.json").write_text(json.dumps(
                            {"to": ["critic"], "from": "boss", "message": "Is the summary short enough?"}))
                        return
                time.sleep(0.05)
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        rec, _mon = self._done_run(worker={"delay": 2})
        th.join(timeout=5)
        ts = turns(rec)
        w = [t for t in ts if t["agent"] == "writer"][0]
        inc = next(t for t in ts if t["agent"] == "critic" and t["purpose"] == "incoming")
        self.assertIn("Is the summary short enough?", inc["input"])
        self.assertLess(inc["started_at"], w["ended_at"])  # activated, not left for the critic's next phase
        self.assertFalse(w.get("interruptions"))

    # ---- supersede

    def test_a_newer_message_supersedes_the_older_waiting_one_unless_additive(self):
        rec, mon = self._done_run()
        r = _runner(mon, rec)
        boss, critic = mon.agent("boss"), mon.agent("critic")
        r.route_text(boss, '<message to="Jordan">Use long tables.</message>', "terminal-a", "terminal")
        r.route_text(boss, '<message to="Jordan">Use short tables.</message>', "terminal-b", "terminal")
        r.route_text(boss, '<message to="Jordan" additive="yes">Also cite sources.</message>', "terminal-c", "terminal")
        L = {m["source"]: m for m in r.ledger.rows}
        self.assertEqual(L["terminal-a"]["status"], "superseded")
        self.assertEqual(L["terminal-a"]["by"], L["terminal-b"]["id"])
        self.assertEqual(L["terminal-b"]["status"], "sent")
        q = r.st["fwd"][critic.name]
        self.assertFalse(any("Use long tables." in x for x in q))
        self.assertTrue(any("Use short tables." in x for x in q) and any("Also cite sources." in x for x in q))
        r.activate("wait")
        crit = _last(rec, "critic")
        self.assertNotIn("Use long tables.", crit["input"])
        self.assertEqual([L[k]["status"] for k in ("terminal-b", "terminal-c")], ["delivered", "delivered"])
        # a note up is never superseded
        r.route_text(critic, '<message to="Alex">one</message>', "c1", "terminal")
        r.route_text(critic, '<message to="Alex">two</message>', "c2", "terminal")
        self.assertEqual([m["status"] for m in r.ledger.rows if m["source"] in ("c1", "c2")], ["sent", "sent"])

    def test_the_kickoff_instruction_supersedes_a_message_written_with_the_plan(self):
        # live run: the manager put an instruction for the critic in its plan reply, then wrote the real one at
        # kickoff ("This replaces my earlier message"); the critic's brief carried both
        plan = ("1. Part 1 — the part is done\n\n| criterion | what a 5 looks like |\n|---|---|\n| c | ok |\n\n"
                '<message to="Jordan">Plan-time draft: write the module first.</message>')
        kick = '<message to="Jordan">This replaces my earlier message. Final brief: write the module and its tests.</message>'
        rec, mon = self._done_run(mgr={"replies": {"plan": [plan], "kickoff": [kick]}})
        brief = next(t for t in turns(rec) if t["purpose"] == "brief")
        self.assertIn("Final brief: write the module and its tests.", brief["input"])
        self.assertNotIn("Plan-time draft", brief["input"])
        L = {m["source"]: m for m in _ledger(rec) if m["from"] == "boss" and m["to"] == "critic"}
        plan_turn = next(t["turn_id"] for t in turns(rec) if t["purpose"] == "plan")
        kick_turn = next(t["turn_id"] for t in turns(rec) if t["purpose"] == "kickoff")
        self.assertEqual(L[plan_turn]["status"], "superseded")
        self.assertEqual(L[plan_turn]["by"], L[kick_turn]["id"])
        self.assertEqual((L[kick_turn]["status"], L[kick_turn]["turn"]), ("delivered", brief["turn_id"]))

    def test_a_superseded_inbox_note_is_never_delivered(self):
        rec, mon = self._done_run()
        _b, c1 = self._chat(rec, mon, None, '<message to="Jordan" urgent="yes">Drop table 3.</message>')
        _b, c2 = self._chat(rec, mon, None, '<message to="Jordan" urgent="yes">Keep table 3, drop table 4.</message>')
        r = _runner(mon, rec)
        r.scan_inbox()
        L = {m["source"]: m for m in r.ledger.rows}
        self.assertEqual((L[c1]["status"], L[c2]["status"]), ("superseded", "sent"))
        self.assertTrue((rec.run_dir / "inbox" / "superseded" / L[c1]["carrier"]["inbox"]).is_file())
        r.activate("wait")
        crit = _last(rec, "critic")
        self.assertIn("Keep table 3, drop table 4.", crit["input"])
        self.assertNotIn("Drop table 3.", crit["input"])
        rel = _b.data()["relays"]
        self.assertEqual(rel[c1][0]["status"], "superseded")

    # ---- nested questions, held by design, address errors

    def test_a_nested_question_to_the_recipient_is_delivered_whole(self):
        rec, mon = self._done_run()
        r = _runner(mon, rec)
        r.route_text(mon.agent("boss"), '<message to="Jordan">Before you go on:\n<question>Short or long tables?'
                                        '</question></message>', "t1", "terminal")
        r.activate("wait")
        crit = _last(rec, "critic")
        self.assertIn("<question>Short or long tables?</question>", crit["input"])
        self.assertEqual(next(m for m in r.ledger.rows if m["source"] == "t1")["status"], "delivered")

    def test_the_checklist_and_verdict_are_held_by_design_and_shown(self):
        from orch.board import Board
        rec, mon = self._done_run()
        held = [m for m in _ledger(rec) if m.get("by_design")]
        self.assertTrue(held)
        self.assertEqual({m["kind"] for m in held}, {"checklist", "verdict"})
        self.assertTrue(all(m["status"] == "held" and m["to"] == "writer" and m["reason"] for m in held))
        w = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertFalse(any("- [ ] the change is made" in t["input"] for t in w))
        data = Board("m", lambda: rec.run_dir, mon).data()
        self.assertTrue(data["held"])
        self.assertIn('heldLine', (ROOT / "orch" / "board.html").read_text())

    def test_a_bad_address_in_a_chat_reply_is_corrected_at_the_next_turn(self):
        rec, mon = self._done_run()
        _b, cid = self._chat(rec, mon, "critic", 'Ok.\n<message to="Taylor">hello</message>')
        r = _runner(mon, rec)
        r.take_messages()
        self.assertTrue(any('Not delivered: <message to="Taylor">.' in x for x in r.st["fwd"]["critic"]))

    def test_worker_chat_tags_go_nowhere(self):
        rec, mon = self._done_run()
        _b, cid = self._chat(rec, mon, "writer", 'Done.\n<message to="Jordan">ping</message>')
        r = _runner(mon, rec)
        r.activate("wait")
        self.assertFalse(any(m["source"] == cid and m["status"] != "held" for m in r.ledger.rows))
        self.assertTrue(r.st["fwd"].get("writer"))  # its one-line correction

    # ---- the terminal relay hook

    def _hook(self, run_dir, event, sid, transcript=None, env=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        e.update(ORCH_RUN_DIR=str(run_dir), **(env or {}))
        data = {"session_id": sid, "hook_event_name": event, "cwd": str(self.tmp)}
        if transcript:
            data["transcript_path"] = str(transcript)
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(data), text=True, env=e,
                           capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(time.monotonic() - t0, 2.0)  # fast: no heavy imports

    def test_the_hook_marks_busy_relays_replies_and_orch_holds_the_manager(self):
        rec, mon = self._done_run()
        rd = rec.run_dir
        sid = rec.state["sessions"]["boss"]["session_id"]
        tr = self.tmp / "t.jsonl"
        rows = [{"type": "user", "message": {"role": "user", "content": "tell the critic"}},
                {"type": "assistant", "uuid": "u1", "message": {"content": [{"type": "text", "text": "Sure."}]}},
                {"type": "assistant", "uuid": "u2", "message": {"content": [
                    {"type": "tool_use", "name": "x"}, {"type": "text", "text": '<message to="Jordan">Add units.</message>'}]}}]
        tr.write_text("\n".join(json.dumps(x) for x in rows))
        self._hook(rd, "SessionStart", sid)
        self.assertTrue((rd / "busy" / "boss.json").is_file())
        self._hook(rd, "Stop", sid, tr)
        self._hook(rd, "Stop", sid, tr)  # the same reply twice: handed over once
        replies = [json.loads(p.read_text()) for p in (rd / "inbox").glob("*-terminal-reply.json")]
        self.assertEqual(len(replies), 1)
        self.assertIn('<message to="Jordan">Add units.</message>', replies[0]["text"])
        self.assertIn("Sure.", replies[0]["text"])
        self._hook(rd, "Stop", sid, tr, env={"ORCH_JOB": "x"})  # orch's own turns are ignored
        self.assertEqual(len(list((rd / "inbox").glob("*-terminal-reply.json"))), 1)

        r = _runner(mon, rec)
        boss = mon.agent("boss")
        n = len(turns(rec))
        done = {}

        def mgr_turn():
            done["r"] = r.turn(boss, "Anything new?", 9, "ack")
            done["t"] = time.time()
        th = threading.Thread(target=mgr_turn, daemon=True)
        th.start()
        time.sleep(1.0)
        self.assertNotIn("r", done)  # held while the terminal is open
        r.activate("wait")  # meanwhile the terminal's message reaches the critic
        crit = _last(rec, "critic")
        self.assertEqual((crit["agent"], crit["purpose"]), ("critic", "incoming"))
        self.assertIn("Add units.", crit["input"])
        released = time.time()
        self._hook(rd, "SessionEnd", sid)
        self.assertFalse((rd / "busy" / "boss.json").exists())
        th.join(timeout=10)
        self.assertEqual(done["r"]["status"], "ok")
        self.assertGreaterEqual(done["t"], released)
        self.assertGreater(len(turns(rec)), n + 1)

    def test_a_turn_right_after_the_terminal_closes_continues_the_terminals_session(self):
        # live run: SessionEnd removed the mark while orch was in another agent's turn; the manager's next turn
        # started before the "end" note was read, so a session the terminal moved to (/clear) would be missed
        rec, mon = self._done_run()
        rd = rec.run_dir
        old = rec.state["sessions"]["boss"]["session_id"]
        self._hook(rd, "SessionStart", old)
        self._hook(rd, "SessionEnd", "terminal-new-session")
        r = _runner(mon, rec)
        seen = {}
        real = MockBackend.run

        def spy(backend, req, should_stop):
            seen.setdefault(req.agent, req.session_id)
            return real(backend, req, should_stop)
        with mock.patch.object(MockBackend, "run", spy):
            res = r.turn(mon.agent("boss"), "Anything new?", 9, "ack")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(seen["boss"], "terminal-new-session")
        self.assertFalse(list((rd / "inbox").glob("*-terminal-end.json")))

    def test_a_stale_terminal_mark_does_not_hold_the_run(self):
        rec, mon = self._done_run()
        rd = rec.run_dir
        (rd / "busy").mkdir(exist_ok=True)
        (rd / "busy" / "boss.json").write_text(json.dumps({"session_id": "s", "touched": time.time() - 10}))
        mon.controller.terminal_busy_timeout = 5
        r = _runner(mon, rec)
        t0 = time.time()
        res = r.turn(mon.agent("boss"), "Anything new?", 9, "ack")
        self.assertEqual(res["status"], "ok")
        self.assertLess(time.time() - t0, 3)
        self.assertIn("is stale", (rd / "progress.md").read_text())

    def test_the_hook_install_snippet_writes_no_settings(self):
        from orch import cli
        s = cli.hook_settings()
        self.assertEqual(set(s["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"})
        self.assertIn("terminal_relay.py", s["hooks"]["Stop"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
