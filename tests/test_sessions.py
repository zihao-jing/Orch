"""B7: sessions and context (mock backend only).

1. a milestone start gives the critic and the worker a new session with a short natural instruction, nothing pasted
   from earlier rounds; the manager keeps its session;
2. the worker keeps its session across the steps of a milestone and starts a new one at the next milestone;
3. `compact_between_steps`: a compact between steps (never mid-turn), recorded as its own entry;
4. a turn cut off after its message reached the session is resumed with "continue" only;
5. every session reset is recorded with its reason (turn record, events, board system line);
6. cost: a board-chat turn between two orch turns in the same session is an entry of its own, and the next orch
   turn is charged only its own increase.
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.backends import MockBackend, TurnResult  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.recorder import Recorder, live_event, now_iso, read_json  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

M1_WORK = "M1-WORKER-REPORT: rewrote section two and reran the tables."
M1_REVIEW = "M1-REVIEW-NOTE: the second table still lacks totals."


def _events(rec, kind):
    return [e for e in (json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines())
            if e.get("event") == kind]


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.sdir = self.tmp / "agents"
        MockBackend.compacts.clear()

    def cfg(self, critic=None, worker=None, mgr=None, writer=None):
        m = managed(mgr=mgr or {"milestones": 2}, critic=critic, worker=worker)
        for ag in m["agents"]:
            ag["state_file"] = str(self.sdir / ag["name"] / "state.md")
        m["agents"][2].update(writer or {})
        return make_cfg(self.tmp, [m])

    def stepped_critic(self):
        """A critic whose checklist file has two steps: step 1 finishes at the first review, step 2 at the second."""
        ck = str(self.sdir / "critic" / "checklist-m1.md")
        ck2 = str(self.sdir / "critic" / "checklist-m2.md")
        two = "## Step 1: tables\n- [{a}] totals added\n\n## Step 2: prose\n- [{b}] intro rewritten\n"
        return {
            "replies": {"brief": ["Start with the tables: add the totals."],
                        "verify": [M1_REVIEW + " Step 1 is done; now rewrite the intro.\n<verdict>FAIL</verdict>",
                                   "All done.\n<verdict>PASS</verdict>"]},
            "files": {"brief": [{ck: two.format(a=" ", b=" ")}, {ck2: two.format(a=" ", b=" ")}],
                      "verify": [{ck: two.format(a="x", b=" ")}, {ck: two.format(a="x", b="x")},
                                 {ck2: two.format(a="x", b=" ")}, {ck2: two.format(a="x", b="x")}]}}

    # 1 + 5
    def test_milestone_start_is_short_and_carries_no_past_rounds(self):
        cfg = self.cfg(worker={"replies": {"work": [M1_WORK, "Done."]}},
                       critic={"replies": {"verify": [M1_REVIEW + "\n<verdict>FAIL</verdict>",
                                                      "Fine now.\n<checklist>\n- [x] ok\n</checklist>\n<verdict>PASS</verdict>"],
                                           "brief": ["Start with the tables."]}})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        crit2 = [t for t in ts if t["agent"] == "critic" and t.get("milestone") == 1]
        work2 = [t for t in ts if t["agent"] == "writer" and t.get("milestone") == 1]
        start_c, start_w = crit2[0], work2[0]
        self.assertEqual(start_c["purpose"], "brief")
        for t in (start_c, start_w):
            self.assertFalse(t["resumed_session"], t["turn_id"])  # system prompt + a short instruction
            self.assertLess(len(t["input"]), 2500, t["turn_id"])
            for past in (M1_WORK, M1_REVIEW, "M1-", "Your latest review", "Your working state", "Your latest report"):
                self.assertNotIn(past, t["input"], t["turn_id"])
            self.assertIn("fresh conversation", t["input"])
            self.assertIn("milestone 2 starts", t["session_reset"][0]["reason"])  # 5: the reset and why
        crit1 = [t for t in ts if t["agent"] == "critic" and t.get("milestone") == 0]
        self.assertNotEqual(start_c["session_id"], crit1[-1]["session_id"])
        boss = [t for t in ts if t["agent"] == "boss"]  # the manager keeps its session
        self.assertEqual(len({t["session_id"] for t in boss}), 1)
        self.assertTrue(all(t["resumed_session"] for t in boss[1:]))
        notes = _events(rec, "session_note")
        self.assertEqual(sorted(e["agent"] for e in notes), ["critic", "writer"])
        from orch.board import Board
        d = Board("m", lambda: rec.run_dir, load_config(cfg).monitor("m")).data()
        lines = [m for m in d["messages"] if m["kind"] == "system"]
        self.assertEqual(len(lines), 2)
        self.assertIn("new session: milestone 2 starts", lines[0]["text"])
        row = next(r for r in d["turns"] if r["turn_id"] == start_w["turn_id"])
        self.assertIn("milestone 2 starts", row["session_reset"])
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_fresh_session_never_keeps_the_session_across_milestones(self):
        status, rec = run(self.cfg(writer={"fresh_session": "never"}))
        self.assertEqual(status, "completed")
        w = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertEqual(len({t["session_id"] for t in w}), 1)

    # 2
    def test_worker_keeps_its_session_across_steps_and_not_across_milestones(self):
        status, rec = run(self.cfg(critic=self.stepped_critic()))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertTrue(any(e.get("done") == 1 for e in _events(rec, "plan_step")))  # a step finished in between
        w1 = [t for t in ts if t["agent"] == "writer" and t.get("milestone") == 0]
        w2 = [t for t in ts if t["agent"] == "writer" and t.get("milestone") == 1]
        self.assertGreaterEqual(len(w1), 2)
        self.assertEqual(len({t["session_id"] for t in w1}), 1)
        self.assertTrue(all(t["resumed_session"] for t in w1[1:]))
        self.assertEqual(len({t["session_id"] for t in w2}), 1)
        self.assertNotEqual(w1[0]["session_id"], w2[0]["session_id"])
        self.assertFalse(MockBackend.compacts)  # off by default

    # 3
    def test_compact_between_steps_only(self):
        cfg = self.cfg(critic=self.stepped_critic(), writer={"compact_between_steps": True},
                       mgr={"milestones": 1})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        self.assertEqual(len(MockBackend.compacts), 1)
        c = MockBackend.compacts[0]
        self.assertEqual(c["agent"], "writer")
        self.assertFalse(c["mid_turn"])  # never while a turn runs in that session
        ts = sorted(turns(rec), key=lambda t: (t["started_at"], t["round"], t["seq"]))
        order = [(t["agent"], t["purpose"]) for t in ts]
        k = order.index(("writer", "compact"))
        self.assertEqual(order[k - 1], ("critic", "verify"))  # right after the review that finished step 1
        self.assertEqual(order[k + 1], ("writer", "work"))  # before the worker's next step
        comp, nxt = ts[k], ts[k + 1]
        self.assertEqual(comp["input"], "/compact")
        self.assertEqual(comp["session_id"], nxt["session_id"])  # the same session goes on, compacted
        self.assertTrue(nxt["resumed_session"])
        self.assertEqual(nxt["session_reset"][0]["reason"], "compacted between steps")
        first = [t for t in ts if t["agent"] == "writer" and t["purpose"] == "work"][0]
        self.assertLessEqual(first["ended_at"], comp["started_at"])
        self.assertEqual([e["change"] for e in _events(rec, "session_note")], ["compact"])

    def test_a_backend_that_cannot_compact_is_skipped_with_one_note(self):
        status, rec = run(self.cfg(critic=self.stepped_critic(), worker={"no_compact": True},
                                   writer={"compact_between_steps": True}, mgr={"milestones": 1}))
        self.assertEqual(status, "completed")
        self.assertFalse(any(t["purpose"] == "compact" for t in turns(rec)))
        self.assertEqual(len(_events(rec, "compact_skipped")), 1)
        self.assertIn("compact between steps is not available for `mock`", (rec.run_dir / "progress.md").read_text())

    # 4
    def test_a_cut_off_turn_resumes_with_continue_only(self):
        from orch.managed import ManagedRunner
        import orch.controller as ctl
        cfg = load_config(self.cfg(mgr={"milestones": 1}))
        mon = cfg.monitor("m")
        status, rec = run(self.cfg(mgr={"milestones": 1}))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        st["managed"].update(phase="work", ms=0, ms_round=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st["managed"].pop("in_work", None)
        st.update(round=0, status="running", fwd={})
        rec2.state.update(st)
        rec2.save()
        sid = st["sessions"]["writer"]["session_id"]
        real, stop, sent = ctl.get_backend, threading.Event(), []

        class Cut:  # the message reaches the session (the backend names it), then a restart cuts the turn off
            def run(self, req, should_stop):
                sent.append(req.prompt)
                req.on_event({"k": "session", "s": req.session_id})
                stop.set()
                return TurnResult("stopped", "", req.session_id, -1, "", [], {})
        ctl.get_backend = lambda name: Cut()
        try:
            ManagedRunner(mon, stop, resume=True, log=lambda s: None).run()
        finally:
            ctl.get_backend = real
        self.assertIn("Make the tables.", sent[0])
        ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None).run()
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        w = [t for t in turns(rec3) if t["agent"] == "writer"]
        self.assertEqual(w[0]["status"], "interrupted")
        redo = w[1]
        self.assertEqual(redo["input"], "continue")  # nothing resent: the session has the message
        self.assertEqual((redo["session_id"], redo["continues"]), (sid, w[0]["turn_id"]))
        self.assertIsNone(rec3.state["sessions"]["writer"].get("cut_off"))

    def test_a_retry_in_the_same_session_sends_continue_not_the_message_again(self):
        from orch.controller import MonitorRunner
        from test_orch import pipeline
        m = pipeline()
        m["agents"][0].update(retries=1, retry_waits=[])
        cfg = load_config(make_cfg(self.tmp, [m]))
        r = MonitorRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        import orch.controller as ctl
        real, sent = ctl.get_backend, []

        class Flaky:  # the first attempt reaches the session and dies half way; the retry continues it
            def run(self, req, should_stop):
                sent.append(req.prompt)
                if len(sent) == 1:
                    req.on_event({"k": "tool", "s": "edit"})
                    return TurnResult("error", "", req.session_id, 1, "crashed", [], {})
                return TurnResult("ok", "done", req.session_id, 0, "", [], {})
        rec = Recorder.create(cfg.output_dir, "m", cfg.monitor("m").snapshot())
        r.rec = rec
        rec.update_session("w1", session_id="s-1", turns=1)
        ctl.get_backend = lambda name: Flaky()
        try:
            out = r.turn(cfg.monitor("m").agent("w1"), "Please make the tables now.", 1, "work")
        finally:
            ctl.get_backend = real
        self.assertEqual(out["status"], "ok")
        self.assertEqual(sent, ["Please make the tables now.", "continue"])

    # 5
    def test_a_refresh_records_its_reason(self):
        from orch.managed import ManagedRunner
        cfg = self.cfg(mgr={"milestones": 1})
        status, rec = run(cfg)
        r = ManagedRunner(load_config(cfg).monitor("m"), threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        r.reset_sessions(["writer"])
        e = _events(rec, "session_note")[-1]
        self.assertEqual((e["agent"], e["reason"], e["change"]), ("writer", "sessions reset (refresh)", "reset"))
        self.assertEqual(rec.state["sessions"]["writer"]["resets"][-1]["reason"], "sessions reset (refresh)")

    # 6
    def test_a_chat_turn_between_two_orch_turns_is_charged_on_its_own(self):
        rec = Recorder.create(str(self.tmp / "out"), "m", {"controller": {"mode": "managed"}, "goal": "", "workspace": "",
                                                          "working_dir": "", "max_rounds": 3})

        def turn(seq, total):
            t = {"turn_id": f"r001-t0{seq}-boss", "round": 1, "seq": seq, "agent": "boss", "role": "manager",
                 "status": "ok", "session_id": "S", "started_at": now_iso(), "ended_at": now_iso(), "input": "",
                 "output": "", "meta": {"total_cost_usd": total}}
            rec.write_turn(t)
            return read_json(rec.run_dir / "rounds" / "r001" / f"t0{seq}-boss.json")
        a = turn(1, 0.12)
        with open(rec.run_dir / "chat.jsonl", "a") as f:  # a board chat in the manager's session: running total 2.11
            f.write(json.dumps({"id": "chat-001", "role": "manager", "status": "running", "ts": now_iso()}) + "\n")
            f.write(json.dumps({"id": "chat-001", "role": "manager", "status": "ok", "text": "fine", "ts": now_iso(),
                                "session_id": "S", "cost": 2.11}) + "\n")
        b = turn(2, 2.23)
        self.assertEqual(a["meta"]["turn_cost_usd"], 0.12)
        self.assertEqual(rec.state["chat_costs"]["chat-001"]["cost_usd"], 1.99)  # its own entry
        self.assertEqual(b["meta"]["turn_cost_usd"], 0.12)  # not 2.11: the chat is not charged to it
        self.assertAlmostEqual(rec.state["cost_usd"], 2.23)
        self.assertEqual(len(_events(rec, "chat_cost")), 1)
        rec.charge_chats()  # taken once
        self.assertAlmostEqual(rec.state["cost_usd"], 2.23)
        from orch.board import _turn_costs
        from orch.report import render_run_report
        rows = [{"session_id": "S", "cost": 0.12, "ended_at": a["ended_at"], "status": "ok"},
                {"session_id": "S", "cost": 2.23, "ended_at": b["ended_at"] + "z", "status": "ok"}]
        chat = [{"role": "agent", "who": "critic", "session_id": "C", "cost": 0.5, "ts": a["ended_at"], "status": "ok"}]
        self.assertEqual(_turn_costs(rows, chat), 2.73)  # a chat with the critic counts too
        self.assertIn("$2.23", render_run_report(rec.run_dir))

    def test_a_managed_run_charges_board_chats_to_themselves(self):
        cfg = self.cfg(mgr={"milestones": 1, "cost_per_turn": 0.1})
        from orch.managed import ManagedRunner
        mon = load_config(cfg).monitor("m")
        r = ManagedRunner(mon, threading.Event(), log=lambda s: None)
        orig = r.say
        done = []

        def say(agent, prompt, purpose):  # a board chat in the manager's session right after its kickoff turn
            out = orig(agent, prompt, purpose)
            if agent.role == "manager" and not done:
                done.append(1)
                from orch.backends import TurnRequest
                res = MockBackend().run(TurnRequest("boss", "manager", "How is it going?", "", out["session_id"],
                                                    str(self.tmp), 10, {"cost_per_turn": 0.1},
                                                    context={"purpose": "chat"}), lambda: False)
                with open(r.rec.run_dir / "chat.jsonl", "a") as f:
                    f.write(json.dumps({"id": "chat-001", "role": "manager", "status": "ok", "ts": now_iso(),
                                        "session_id": res.session_id, "cost": res.meta["total_cost_usd"]}) + "\n")
            return out
        r.say = say
        self.assertEqual(r.run(), "completed")
        boss = [t for t in turns(r.rec) if t["agent"] == "boss"]
        self.assertTrue(all(abs(t["meta"]["turn_cost_usd"] - 0.1) < 1e-9 for t in boss), [t["meta"] for t in boss])
        self.assertAlmostEqual(r.rec.state["chat_costs"]["chat-001"]["cost_usd"], 0.1)


if __name__ == "__main__":
    unittest.main()
