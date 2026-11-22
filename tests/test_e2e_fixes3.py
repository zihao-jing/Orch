"""Fixes after the third live end-to-end run (B3, B5, B7, B8; mock backend only):

1. a fresh run warned "changed in the config but applied only at the next start: prompt_vars" for the manager: the
   golden board's path is a prompt variable the run sets itself, so a re-read config (which lacks it) is no change;
2. a critic review that carried only a progress check report got the "answer me inside <message to=you>" reminder
   (meant for notes from its user) on top of the protocol reminder: observations don't trigger it;
3. the critic passed milestone 1 with a question; the manager's answer turn held the whole report, so the manager
   reviewed the milestone there and sent the next milestone's instruction, which the critic turned into a step of
   milestone 1 (the next milestone then started on finished work): the answer prompt now says the review comes
   next and to keep the next instruction for it.
"""

from protocol_fixtures import worker_instruction

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.config import load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402
from test_config_reload import FakeRec  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402
from test_progress import FINE, _cfg  # noqa: E402


class TestRunPromptVars(unittest.TestCase):
    def test_golden_board_path_is_not_a_config_change(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, [{"name": "m", "workspace": "ws", "goal": "g", "max_rounds": 4,
                              "controller": {"mode": "pipeline"},
                              "agents": [{"name": "w1", "role": "worker", "task": "do it"}]}])
        mon = load_config(cfg).monitor("m")
        r = MonitorRunner(mon, threading.Event(), log=lambda s: None)
        r.rec = FakeRec()
        r.rec.state = {"sessions": {}, "fwd": {}}
        w = mon.agents[0]
        w.prompt_vars["golden_board"] = "/p/.orch/golden.md"  # what ManagedRunner.board_vars does at the start
        r.refresh_agent(w)
        self.assertEqual(r.rec.progress_lines, [])
        self.assertEqual(r.rec.events, [])
        self.assertEqual(w.prompt_vars["golden_board"], "/p/.orch/golden.md")


class TestObservationsNeedNoAnswer(unittest.TestCase):
    def test_a_progress_check_alone_brings_no_reply_routing_reminder(self):
        status, rec = run(_cfg(Path(tempfile.mkdtemp()).resolve(), monitor={"replies": {"check": [FINE]}}))
        self.assertEqual(status, "completed")
        review = next(t for t in turns(rec) if t["agent"] == "critic"
                      and t["purpose"] in ("verify", "incoming") and "Progress check:" in t["input"])
        self.assertIn("Progress check:", review["input"])
        self.assertNotIn("reply_routing", review.get("reminders") or [])
        self.assertNotIn("If I asked you something", review["input"])

    def test_unit_only_notes_from_its_user_bring_it(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        tmp = Path(tempfile.mkdtemp())
        cfg = load_config(make_cfg(tmp, [managed(controller={"tag_reminder": 0, "reminders": False},
                                                 mgr={"milestones": 1})]))
        r = ManagedRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        r.rec = Recorder.create(str(tmp / "out"), "m", cfg.monitor("m").snapshot())
        r.st.setdefault("fwd", {})
        check = "Progress check, 2 min into Sam's current turn:\n\n- out.csv has 120 rows"
        r.ledger.add("monitor", "critic", "- out.csv has 120 rows", check, kind="progress_check")
        r.forward(r.critic, check)
        _p, sent = r.add_reminders(r.critic, "Please review.", "verify")
        self.assertNotIn("reply_routing", sent)  # an observation: nothing to answer
        r.forward(r.critic, "Alex wrote: which run do we cite?")  # from its user
        _p, sent = r.add_reminders(r.critic, "Please review.", "verify")
        self.assertIn("reply_routing", sent)


class TestQuestionWithAFinishedMilestone(unittest.TestCase):
    def test_the_manager_is_told_the_review_comes_next(self):
        status, rec = run(make_cfg(Path(tempfile.mkdtemp()), [managed(
            mgr={"milestones": 1, "replies": {"answer": ['<message to="Jordan">A: accept it.</message>']}},
            critic={"replies": {"verify": [
                "All done.\n\n<question>The run took 5 min 18 s. Accept it?\nA) yes\nB) rerun</question>\n\n"
                "<checklist>- [x] run finished</checklist>\n<verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        answer = next(t for t in ts if t["agent"] == "boss" and t["purpose"] == "answer")
        self.assertIn("its report comes to you next for your review", answer["input"])
        self.assertIn("next milestone's instruction", answer["input"])
        seq = [(t["agent"], t["purpose"]) for t in ts]
        self.assertLess(seq.index(("boss", "answer")), seq.index(("boss", "review")))

    def test_a_question_mid_milestone_has_no_such_note(self):
        status, rec = run(make_cfg(Path(tempfile.mkdtemp()), [managed(
            mgr={"milestones": 1, "replies": {"answer": ['<message to="Jordan">Cite the new run.</message>']}},
            critic={"replies": {"verify": [
                worker_instruction("<question>Do we cite the new run?\nA) yes\nB) no</question>\n\n"
                "<checklist>- [ ] cited</checklist>\n<verdict>FAIL</verdict>"),
                "Good.\n\n<checklist>- [x] cited</checklist>\n<verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed")
        answer = next(t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "answer")
        self.assertNotIn("report comes to you next", answer["input"])


if __name__ == "__main__":
    unittest.main()
