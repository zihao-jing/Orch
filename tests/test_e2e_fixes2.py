"""Fixes after a live end-to-end run (mock backend only):

1. the worker found orch's own process and run folder and reported orch's goal: fresh-session texts point only at the
   project and the agent's own jobs, the protocol tells the critic and worker to ignore `.orch/` and the processes
   that run them, job lists never show orch's processes, and the board flags a reply that mentions them;
2. the critic's plain notes up started manager turns that answered "nothing needed": a note wakes the manager only
   with a question, a pause or an urgent mark; otherwise it is held for the manager's next regular turn;
3. the worker got the critic's review and its amended instruction for the same step: the amend replaces it;
4. `audit_every: 0` runs no automatic audit, the final one included;
5. board chat with the manager carries the voice reminder on the first chat message and then at intervals.
"""

from protocol_fixtures import worker_instruction

import json
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

from orch import jobs  # noqa: E402
from orch.backends import TurnResult  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.templates import DEFAULT_TEMPLATES, render  # noqa: E402
from role_view_checks import assert_no_role_leaks, leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402


def _events(rec, kind):
    return [e for e in (json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines())
            if e.get("event") == kind]


class TestHideOrch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_fresh_session_texts_point_at_the_project_and_own_jobs_only(self):
        # live run: "read the relevant files, logs and any running jobs" sent the worker to `ps`, where it found orch
        for k in ("milestone_fresh", "backend_switched", "turn_interrupted", "worker_timeout"):
            text = render(DEFAULT_TEMPLATES[k], cwd="/w", agent="Sam")
            self.assertNotIn("any running jobs", text, k)
            self.assertNotIn("any logs or running jobs", text, k)
            self.assertRegex(text, r"jobs you started|background jobs of this work", k)
        m = managed(mgr={"milestones": 2})
        m["agents"][2]["fresh_session"] = "milestone"
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        mon = load_config(str(self.tmp / "cfg.json")).monitor("m")
        w2 = [t for t in turns(rec) if t["agent"] == "writer" and t.get("milestone") == 1][0]
        self.assertIn("background jobs you started for this work", w2["input"])
        self.assertNotIn("running jobs", w2["input"])
        for name in ("writer", "critic"):  # both are told to leave the tooling alone, without naming it
            sp = mon.agent(name).current_system_prompt()
            self.assertIn("The `.orch/` folder and the processes that run this conversation are tooling", sp)
            role = mon.agent(name).role
            self.assertEqual(leaks(sp, role), [], name)
        assert_no_role_leaks(self, rec, mon)

    def test_job_lists_never_show_orch_itself(self):
        ws = str(self.tmp)
        before = {1: (0, "launchd")}
        after = {1: (0, "launchd"),
                 501: (1, "/opt/envs/orch/bin/python -m orch run . --serve"),
                 502: (1, "orch run /p --serve"),
                 503: (1, "python -m orch.hooks.terminal_relay"),
                 504: (1, "/opt/envs/orch/bin/python train.py --out runs/orch_eval")}
        with mock.patch.object(jobs, "cwds", lambda pids: {int(p): ws for p in pids}), \
                mock.patch.object(jobs, "tag_check", lambda pids, tags: {}):
            found = jobs.detect(before, [ws], after=after)
        self.assertEqual([j["pid"] for j in found], [504])
        self.assertTrue(jobs.is_tool("python -m orch run . --serve"))
        self.assertFalse(jobs.is_orch("python scripts/orchestra.py"))

    def test_the_board_flags_a_reply_that_mentions_orch(self):
        leak = ("Done. I also saw `python -m orch run . --serve` running and read {rd}/state.json: the goal is to "
                "revise the plan.")
        clean = "Done. My notes are in `.orch/agents/critic/state.md`."
        m = managed(mgr={"milestones": 1}, worker={"replies": {"work": ["Done, tests pass."]}},
                    critic={"replies": {"brief": [worker_instruction(clean)]}})
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(_events(rec, "tooling_mention"), [])  # its own state file is not a mention
        mon = load_config(str(self.tmp / "cfg.json")).monitor("m")
        from orch.managed import ManagedRunner
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        r.check_tooling(mon.agent("writer"), {"turn_id": "r009-t01-writer", "output": leak.format(rd=rec.run_dir)})
        ev = _events(rec, "tooling_mention")
        self.assertEqual(len(ev), 1)
        self.assertIn(str(rec.run_dir), ev[0]["problem"])
        self.assertIn("-m orch run", ev[0]["problem"])
        from orch.board import Board
        data = json.dumps(Board("m", lambda: rec.run_dir, mon).data())
        self.assertIn("mentions the tooling that runs it", data)  # a warning line on the board


class TestNotesUp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_plain_note_up_waits_for_the_managers_next_turn(self):
        # live run: "Passed on: …" notes started manager turns that replied "nothing needed" (3x in 7 rounds)
        fail = ("Round {round} review: add the units column.\n\n"
                '<message to="you">Passed on: the units column, as you asked.</message>\n\n'
                "<checklist>\n- [ ] units column\n</checklist>\n<verdict>FAIL</verdict>")
        ok = "All done.\n\n<checklist>\n- [x] units column\n</checklist>\n<verdict>PASS</verdict>"
        m = managed(mgr={"milestones": 1}, critic={"replies": {"verify": [worker_instruction(fail), worker_instruction(fail), worker_instruction(ok)]}})
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertFalse([t for t in ts if t["agent"] == "boss" and t["purpose"] == "incoming"])
        notes = [x for x in rec.state["messages"] if x["from"] == "critic" and x["to"] == "boss"
                 and x["body"].startswith("Passed on:")]
        self.assertEqual(len(notes), 2)
        mgr_next = [t for t in ts if t["agent"] == "boss" and t["purpose"] == "review"][0]
        for n in notes:  # held while waiting, then delivered in the manager's regular turn
            self.assertEqual((n["status"], n["reason"], n["turn"]), ("delivered", "waits for next turn",
                                                                      mgr_next["turn_id"]))
        self.assertIn("Passed on: the units column", mgr_next["input"])

    def test_wake_rule(self):
        from orch.managed import ManagedRunner
        self.assertFalse(ManagedRunner.wakes_manager("Passed on: the units column."))
        self.assertTrue(ManagedRunner.wakes_manager("Which one? <question>A) x B) y</question>"))
        self.assertTrue(ManagedRunner.wakes_manager("<pause>a GPU quota from you</pause>"))
        self.assertTrue(ManagedRunner.wakes_manager("Stop the run.", urgent=True))


class TestOneInstructionPerStep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_amend_replaces_the_queued_review(self):
        # live run: the worker got the review's feedback and then the amended text for the same step
        review = ("Fix the old totals in table 2.\n\n<message to=\"Sam\">Also rename the column.</message>\n\n"
                  "<question>Recompute from raw data or patch? A) recompute (default) B) patch</question>\n\n"
                  "<checklist>\n- [ ] totals fixed\n</checklist>\n<verdict>FAIL</verdict>")
        amend = ("Drop the fix of table 2; recompute every total from the raw data now.\n\n"
                 "<checklist>\n- [ ] totals recomputed\n</checklist>\n<verdict>FAIL</verdict>")
        ok = "Good.\n\n<checklist>\n- [x] totals recomputed\n</checklist>\n<verdict>PASS</verdict>"
        m = managed(mgr={"milestones": 1}, critic={"replies": {"verify": [worker_instruction(review), worker_instruction(ok)], "amend": [worker_instruction(amend)]}})
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        am = next(t for t in ts if t["purpose"] == "amend")
        self.assertIn("it replaces that review, so they get one instruction", am["input"])
        w2 = [t for t in ts if t["agent"] == "writer"][1]
        self.assertIn("recompute every total from the raw data", w2["input"])
        self.assertNotIn("Fix the old totals in table 2", w2["input"])
        self.assertNotIn("Also rename the column", w2["input"])
        old = next(x for x in rec.state["messages"] if x["body"] == "Also rename the column.")
        self.assertEqual(old["status"], "superseded")
        self.assertTrue(_events(rec, "feedback_superseded"))
        mon = load_config(str(self.tmp / "cfg.json")).monitor("m")
        assert_no_role_leaks(self, rec, mon)

    def test_an_amend_with_nothing_for_the_worker_keeps_the_review(self):
        review = ("Fix the old totals in table 2.\n\n"
                  "<question>Recompute or patch? A) recompute (default) B) patch</question>\n\n"
                  "<checklist>\n- [ ] totals fixed\n</checklist>\n<verdict>FAIL</verdict>")
        amend = "<checklist>\n- [ ] totals fixed\n</checklist>\n<verdict>FAIL</verdict>"
        ok = "Good.\n\n<checklist>\n- [x] totals fixed\n</checklist>\n<verdict>PASS</verdict>"
        m = managed(mgr={"milestones": 1}, critic={"replies": {"verify": [worker_instruction(review), worker_instruction(ok)], "amend": [worker_instruction(amend)]}})
        status, rec = run(make_cfg(self.tmp, [m]))
        w2 = [t for t in turns(rec) if t["agent"] == "writer"][1]
        self.assertIn("Fix the old totals in table 2", w2["input"])


class TestAuditEveryZero(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, every):
        m = managed(controller={"audit_every": every}, mgr={"milestones": 1})
        m["agents"].append({"name": "auditor", "role": "auditor",
                            "backend_options": {"replies": {"audit": ["<points>\n1. x\n</points>\n<findings>\n</findings>"]}}})
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        return rec

    def test_zero_means_no_automatic_audit_not_even_the_final_one(self):
        self.assertEqual(_events(self._run(0), "audit"), [])

    def test_otherwise_the_run_ends_with_one(self):
        self.assertEqual(len(_events(self._run(50), "audit")), 1)  # fewer rounds than audit_every: the final one


class TestChatReminder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_voice_reminder_goes_with_the_first_chat_message_then_every_third(self):
        from orch import board as B
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = load_config(str(self.tmp / "cfg.json")).monitor("m")
        seen = []

        class Fake:
            def run(self, req, stop):
                seen.append(req.prompt)
                return TurnResult("ok", "Fine.", req.session_id or "s1")
        b = B.Board("m", lambda: rec.run_dir, mon)
        with mock.patch("orch.backends.get_backend", lambda name: Fake()):
            for i in range(5):
                b.chat(f"message {i}")
                for _ in range(200):
                    if len(seen) > i and '"status": "ok"' in (rec.run_dir / "chat.jsonl").read_text().split(
                            f"message {i}")[-1]:
                        break
                    time.sleep(0.02)
        marker = 'write as "I" and give everything as your own'
        self.assertEqual([marker in p for p in seen], [True, False, False, True, False])
        self.assertTrue(all(p.count("<system-reminder>") <= 1 for p in seen))


if __name__ == "__main__":
    unittest.main()
