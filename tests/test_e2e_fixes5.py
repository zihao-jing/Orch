"""E2E fixes 5: from a live run of B11 (parallel workers) with board chat and a restart. Mock backend only."""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import audit  # noqa: E402
from orch.backends import MockBackend, TurnResult  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.managed import ManagedRunner  # noqa: E402
from orch.recorder import Recorder  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_b10_audit import turn as audit_turn  # noqa: E402
from test_b11_parallel import CK_DONE, CK_OPEN, EVERY, Base  # noqa: E402
from test_b6_b9 import unit  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

TWO_OPEN = CK_OPEN.split("\n\n## Step 3")[0] + "\n"
TWO_DONE = CK_DONE.split("\n\n## Step 3")[0] + "\n"
TWO = '<to worker="writer" step="1" out="out/a">Make table A in out/a.</to>\n' \
      '<to worker="writer-2" step="2" out="out/b">Make figure B in out/b.</to>'


def ts(t, key):
    return datetime.fromisoformat(t[key]).timestamp()


def two_workers(base, critic=None, worker=None, monitor=None, worker_cfg=None, mgr=None):
    """A parallel section of two people (steps 1 and 2)."""
    crit = {"replies": {"brief": ['Plan.\n<workers n="2" steps="1,2">independent</workers>'], "par_assign": [TWO],
                        "par_review": ["Checked. <verdict>PASS</verdict>"],
                        "verify": ["All hold. <verdict>PASS</verdict>"]},
            "files": {"brief": [{str(base.ck): TWO_OPEN}], "par_review": [{}, {str(base.ck): TWO_DONE}]}}
    for k, v in (critic or {}).items():
        crit.setdefault(k, {}).update(v)
    return base.cfg(critic=crit, worker=worker, monitor=monitor, worker_cfg=worker_cfg,
                    mgr=mgr or {"replies": {"incoming": ['<workers n="2">Approved.</workers>', "Noted."]}})


class TestChatQuestion(unittest.TestCase):
    """The manager asked me a <question> in its board chat reply: it was never registered, so the work that depends
    on it went on and night mode could be switched on while it was open."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_question_in_a_chat_reply_is_open_blocking_and_holds_night_mode(self):
        from orch.board import Board
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1})])
        _status, rec = run(cfg)
        mon = load_config(cfg).monitor("m")
        reply = ("I'd rather check with you first.\n<question>Which split do we report? A) test (my default) "
                 "B) dev</question>")

        def fake(backend, req, should_stop):
            return TurnResult("ok", reply, req.session_id or "mock-chat", 0, "", ["mock"], {})
        b = Board("m", lambda: rec.run_dir, mon)
        with mock.patch.object(MockBackend, "run", fake):
            cid = b.chat("Can we report the numbers today?")["id"]
            for _ in range(200):
                if b.chat_question_pending():
                    break
                time.sleep(0.02)
        self.assertTrue(b.chat_question_pending(), "the chat reply's question was not handed to the run")
        r = ManagedRunner(mon, threading.Event(), log=lambda s: None)
        r.rec = Recorder(rec.run_dir, rec.output_root)
        r.check_control()
        q = [x for x in r.st["questions"] if x.get("turn") == cid]
        self.assertEqual(len(q), 1)
        q = q[0]
        self.assertEqual((q["from"], q["to"], q["status"], q["blocking"]), ("boss", "human", "open", True))
        self.assertIn("Which split do we report?", q["text"])
        self.assertEqual(r.held_by("work"), f"question {q['id']}")  # the work that depends on it waits
        r.switch_night(True)  # night mode is refused while it is open, as for a round question
        self.assertFalse(r.st.get("night"))
        self.assertTrue(r.st.get("night_queued"))
        r.check_control()  # handed over once only
        self.assertEqual(len([x for x in r.st["questions"] if x.get("turn") == cid]), 1)

    def test_a_question_written_in_the_terminal_is_registered_too(self):
        r = unit(self.tmp)
        r.message_control({"kind": "reply", "agent": "boss", "via": "terminal", "source": "term-1",
                           "text": "<question>Keep the old baseline? A) yes B) no</question>"})
        q = [x for x in r.st.get("questions") or [] if x.get("turn") == "term-1"]
        self.assertEqual([(x["to"], x["status"], x["blocking"]) for x in q], [("human", "open", True)])


class TestDroppedMessages(Base):
    """Messages to workers that had stopped (the parallel section ended) and messages from a final review stayed
    "sent" for good: the board and the auditor read them as workers waiting 26 minutes."""

    def test_a_message_to_a_copy_that_stopped_is_dropped(self):
        r = unit(self.tmp)
        base = r.workers[0]
        c2 = r.par_clone(base, 2, None)
        r.mg["par"] = {"active": True, "slots": {base.name: {"status": "done"}, c2.name: {"status": "done"}},
                       "queue": [], "reviews": []}
        m = r.deliver(r.critic, c2, "Also add units to the axis.", source="r005-t01-critic")
        self.assertEqual(m["status"], "sent")
        r.par_end(r.mg)
        self.assertEqual(m["status"], "dropped")
        self.assertIn("parallel section ended", m["reason"])
        wm = next(x for x in r.st["worker_messages"] if x["id"] == m["carrier"]["wm"])
        self.assertEqual(wm["status"], "dropped")

    def test_the_final_reviews_message_is_dropped_and_not_counted_as_a_wait(self):
        cfg = two_workers(self, critic={"replies": {
            "verify": ['<message to="Sam">Tidy the captions when you can.</message>\nAll hold. <verdict>PASS</verdict>']}})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        st = rec.state
        m = next(x for x in st["messages"] if x["body"] == "Tidy the captions when you can.")
        self.assertEqual(m["status"], "dropped")
        self.assertIn("review passed", m["reason"])
        self.assertFalse([x for x in st["messages"] if x["status"] == "sent"])
        led, counts, flags = audit.ledger(rec.run_dir, 0, time.time() + 3600)
        line = next(x for x in led if x.startswith(f"- {m['id']} "))
        self.assertIn("dropped", line)
        self.assertNotIn("still waiting", line)
        self.assertFalse([f for f in flags if f["line"].startswith(m["id"] + " ")])
        self.assertEqual(counts.get("dropped"), 1)
        meant = counts["messages"] - counts.get("held by design", 0) - counts.get("superseded", 0) - 1
        self.assertEqual(counts["delivered_share"], counts.get("delivered", 0) / meant)
        assert_no_role_leaks(self, rec)

    def test_board_shows_dropped(self):
        from orch.board import _ledger_row
        row = _ledger_row({"id": "M3", "to": "writer-2", "status": "dropped", "reason": "the run ended",
                           "dropped_at": "2026-01-01T10:00:00"}, [], [])
        self.assertEqual((row["status"], row["reason"]), ("dropped", "the run ended"))
        self.assertIn("case 'dropped'", (ROOT / "orch" / "board.html").read_text())

    def test_a_long_dropped_message_is_no_wait_in_the_digest(self):
        rd = Path(tempfile.mkdtemp()) / "run"
        rd.mkdir()
        audit_turn(rd, "r001-t01-crit", "crit", "verifier", "10:00:00", "10:01:00", "Review it. " * 30, "ok " * 200)
        audit_turn(rd, "r001-t02-wk", "wk", "worker", "11:00:00", "11:05:00", "Go on. " * 30, "done " * 200)
        (rd / "state.json").write_text(json.dumps({"messages": [
            {"id": "M1", "from": "crit", "to": "wk-2", "body": "Also add units.", "status": "dropped",
             "reason": "the parallel section ended: they work no more", "created_at": "2026-01-01T10:01:00",
             "dropped_at": "2026-01-01T10:30:00"},
            {"id": "M2", "from": "crit", "to": "wk", "body": "Go on.", "status": "delivered", "turn": "r001-t02-wk",
             "created_at": "2026-01-01T10:01:00", "delivered_at": "2026-01-01T10:02:00"}]}))
        md, stats = audit.build(rd, 1, 1)
        self.assertIn("M1 crit → wk-2 sent 10:01:00, dropped 10:30:00: the parallel section ended", md)
        self.assertFalse([f for f in stats["flags"] if f["kind"] == "message waited"])
        self.assertEqual(stats["ledger"]["delivered_share"], 1.0)


class TestNoWorkerIdsAcrossLanes(Base):
    """The critic named "writer-3" to another worker, who must not know about the others."""

    def test_the_protocol_keeps_each_section_to_its_person(self):
        text = (ROOT / "orch" / "prompts" / "protocol" / "critic.md").read_text()
        self.assertIn("Each person reads only their own section", text)
        self.assertIn('never mention anyone else, another person\'s id or step', text)

    def test_ids_of_the_others_are_replaced_and_flagged(self):
        r = unit(self.tmp)
        base = r.workers[0]
        c2, c3 = r.par_clone(base, 2, None), r.par_clone(base, 3, None)
        out, hit = r.scrub_others(c2, "Match the style of `writer-3`'s table, as the writer-3 did; see writer-30.")
        self.assertEqual(out, "Match the style of another task's table, as another task did; see writer-30.")
        self.assertEqual(hit, ["writer-3"])
        out, hit = r.scrub_others(c2, "Ask Sam about it; `writer` has the file.")  # the first worker's own names
        self.assertEqual(out, "Ask Sam about it; another task has the file.")
        self.assertEqual(hit, ["Sam", "writer"])
        self.assertEqual(r.scrub_others(c3, "Your own id is writer-3.")[0], "Your own id is writer-3.")
        m = r.deliver(r.critic, c3, "Use the same seed as writer-2.")
        self.assertEqual(m["text"], "Use the same seed as another task.")
        ev = [json.loads(x) for x in (r.rec.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertTrue([e for e in ev if e.get("event") == "role_leak" and e.get("agent") == c3.name
                         and e.get("replaced")])

    def test_a_step_naming_another_worker_reaches_its_worker_without_the_id(self):
        assign = TWO.replace("Make figure B in out/b.", "Make figure B in out/b, in the style writer-3 uses.")
        assign = assign.replace("Make table A in out/a.", "Make table A in out/a; writer-2 does the figure.")
        status, rec = run(two_workers(self, critic={"replies": {"par_assign": [assign]}}))
        self.assertEqual(status, "completed")
        tt = turns(rec)
        w1 = next(t for t in tt if t["agent"] == "writer" and t["purpose"] == "work")["input"]
        self.assertNotIn("writer-2", w1)
        self.assertIn("another task does the figure", w1)
        progress = (rec.run_dir / "progress.md").read_text()
        self.assertIn("named another worker (writer-2)", progress)
        assert_no_role_leaks(self, rec)


class TestLaneLabels(Base):
    """Lanes and status lines read "the worker-2": a copy without a name of its own was called "the <id>"."""

    def test_a_copy_is_called_by_its_id(self):
        cfg = json.loads(Path(two_workers(self)).read_text())
        cfg["monitors"][0]["agents"][2].pop("display_name")
        Path(self.tmp / "cfg.json").write_text(json.dumps(cfg))
        status, rec = run(str(self.tmp / "cfg.json"))
        self.assertEqual(status, "completed")
        extra = {a["name"]: a for a in rec.state["agents_extra"]}
        self.assertEqual(extra["writer-2"]["display_name"], "writer-2")
        board = (self.sdir / "critic" / "work-m1.md").read_text()
        self.assertIn("| (`writer-2`) |", board)
        self.assertNotIn("the writer-2", board)
        self.assertNotIn("the writer-2", (rec.run_dir / "progress.md").read_text())
        from orch.board import Board
        mon = load_config(str(self.tmp / "cfg.json")).monitor("m")
        agents = {a["name"]: a for a in Board("m", lambda: rec.run_dir, mon)._model()["agents"]}
        self.assertEqual(agents["writer-2"]["called"], "writer-2")


class TestChecksDuringAReview(Base):
    """A long critic review of one worker's step held up the progress checks of the others: checks ran only in the
    main loop, which waited for the critic's turn."""

    def test_another_workers_monitor_checks_while_the_critic_reviews(self):
        cfg = two_workers(self, critic={"delays": {"par_review": 2.5}},
                          worker={"delay_for": {"writer": {"work": 0.2}, "writer-2": {"work": 5.0}}},
                          monitor={"replies": {"check": ["the log grows <status>ok</status>"]}},
                          worker_cfg={"check_every": EVERY})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        tt = turns(rec)
        review = next(t for t in tt if t["purpose"] == "par_review" and t.get("par_worker") == "writer")
        lo, hi = ts(review, "started_at"), ts(review, "ended_at")  # whole seconds: strictly inside the review
        during = [t for t in tt if t["purpose"] == "check" and t.get("watching") == "writer-2"
                  and lo < ts(t, "started_at") < hi]
        self.assertTrue(during, "no check of writer-2 ran while the critic reviewed writer's step")
        reports = [m for m in rec.state["messages"] if m.get("kind") == "progress_check"]
        self.assertTrue(reports)
        self.assertTrue(all(m["to"] == "critic" for m in reports))  # they queue for the critic
        assert_no_role_leaks(self, rec)


class TestRedoAfterRestart(unittest.TestCase):
    """The auditor reported a turn redone after a restart as a duplicate of the turn the restart cut off."""

    def test_redone_turns_are_marked_and_not_flagged_as_repeats(self):
        rd = Path(tempfile.mkdtemp()) / "run"
        rd.mkdir()
        long = "Run the second arm with the corrected span and write the rows to results/arm2.json. " * 30
        audit_turn(rd, "r001-t01-crit", "crit", "verifier", "10:00:00", "10:01:00", "Plan it. " * 30, "ok " * 200)
        audit_turn(rd, "r001-t02-wk", "wk", "worker", "10:02:00", "10:20:00", long, "partial")
        p = rd / "rounds" / "r001" / "t02-wk.json"
        t = json.loads(p.read_text())
        t["status"] = "interrupted"
        p.write_text(json.dumps(t))
        audit_turn(rd, "r001-t03-wk", "wk", "worker", "10:30:00", "10:40:00", long, "done " * 200)
        (rd / "events.jsonl").write_text(json.dumps({"event": "run_resumed", "ts": "2026-01-01T10:29:00"}) + "\n")
        (rd / "state.json").write_text("{}")
        md, stats = audit.build(rd, 1, 1)
        self.assertIn("| r001-t03-wk | 10:30:00–10:40:00 | work | ok · redo of r001-t02-wk after a restart |", md)
        self.assertIn("interrupted · cut off by a restart, redone in r001-t03-wk", md)
        self.assertFalse([f for f in stats["flags"] if f["kind"] == "repeated content"])
        self.assertIn("redo of X after a restart", md)  # the legend says what the mark means
        prompt = (ROOT / "orch" / "prompts" / "auditor.md").read_text()
        self.assertIn('"redo of <turn> after a restart"', prompt)

    def test_resume_between_two_turns_of_one_round_marks_a_redo(self):
        turns_ = [{"turn_id": "r002-t01-wk", "agent": "wk", "purpose": "work", "round": 2, "status": "ok",
                   "started_at": "2026-01-01T10:00:00"},
                  {"turn_id": "r002-t02-wk", "agent": "wk", "purpose": "work", "round": 2, "status": "ok",
                   "started_at": "2026-01-01T10:10:00"},
                  {"turn_id": "r003-t01-wk", "agent": "wk", "purpose": "work", "round": 3, "status": "ok",
                   "started_at": "2026-01-01T10:20:00"}]
        resumed = [datetime.fromisoformat("2026-01-01T10:05:00").timestamp()]
        self.assertEqual(audit.redos(turns_, resumed), {"r002-t02-wk": "r002-t01-wk"})
        self.assertEqual(audit.redos(turns_, []), {})


if __name__ == "__main__":
    unittest.main()
