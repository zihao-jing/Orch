"""Audit follow-ups: a critic question in a parallel section goes through the manager's answer step and closes; a
reply the manager is asked to give again replaces the first one's message for the critic; a milestone review can
PAUSE the run. All with the mock backend."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_b11_parallel import ASSIGN, CK_DONE, CK_OPEN, PROPOSE, Base  # noqa: E402
from protocol_fixtures import worker_instruction  # noqa: E402
from test_b6_b9 import Live  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

ASK = 'Checked it. <question>Should table A use the 2025 figures?\nA) yes\nB) no</question> <verdict>PASS</verdict>'
REVIEW = ("| criterion | score |\n|---|---|\n| correctness | 5 |\n\n<summary>Done.</summary>\n\n"
          "<score>4.5</score>\n<decision>ACCEPT</decision>")


def msgs(rec, frm, to):
    return [m for m in rec.state.get("messages") or [] if m["from"] == frm and m["to"] == to]


class TestParallelQuestion(Base):
    def par_cfg(self, answer):
        ck = str(self.ck)
        return self.cfg(mgr={"milestones": 1, "replies": {"answer": [answer], "incoming": [
                            '<workers n="3">Approved: they are independent.</workers>', "Noted."]}},
                        critic={"replies": {"brief": [worker_instruction(PROPOSE)], "par_assign": [ASSIGN],
                                            "par_review": [ASK, "Checked it. <verdict>PASS</verdict>"],
                                            "verify": ["All steps hold. <verdict>PASS</verdict>"]},
                                "files": {"brief": [{ck: CK_OPEN}], "par_review": [{ck: CK_DONE}]}})

    def test_question_goes_through_the_answer_step_and_closes(self):
        status, rec = run(self.par_cfg('<message to="critic">ANS-1: yes, the 2025 figures.</message>'))
        self.assertEqual(status, "completed", rec.state.get("error"))
        q = next(x for x in rec.state["questions"] if x["from"] == "critic")
        self.assertEqual((q["status"], q["answered_by"]), ("answered", "boss"))
        tt = turns(rec)
        ans = [t for t in tt if t["agent"] == "boss" and t["purpose"] == "answer"]
        self.assertEqual(len(ans), 1)
        self.assertIn("2025 figures", ans[0]["input"])
        self.assertTrue(any(t["agent"] == "critic" and "ANS-1" in t["input"] for t in tt))
        self.assertFalse([m for m in msgs(rec, "boss", "critic") if m["status"] == "sent"])
        self.assertFalse(any(t["agent"] == "boss" and t["purpose"] == "incoming" and "2025 figures" in t["input"]
                             for t in tt))

    def test_question_the_manager_passes_up_closes_as_passed_up(self):
        status, rec = run(self.par_cfg('<question blocking="no">2025 or 2024 figures?</question>\n'
                                       '<message to="critic">ANS-2: use 2025 until I hear back.</message>'))
        self.assertEqual(status, "completed", rec.state.get("error"))
        qs = rec.state["questions"]
        q = next(x for x in qs if x["from"] == "critic")
        self.assertEqual(q["status"], "passed_up")
        up = next(x for x in qs if x["from"] == "boss")
        self.assertEqual(up["parent"], [q["id"]])
        self.assertTrue(any(t["agent"] == "critic" and "ANS-2" in t["input"] for t in turns(rec)))
        self.assertFalse([m for m in msgs(rec, "boss", "critic") if m["status"] == "sent"])


class TestReask(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_min_score_reask_replaces_the_first_message(self):
        status, rec = run(make_cfg(self.tmp, [managed(controller={"min_score": 4}, mgr={"milestones": 1, "replies": {
            "review": ['<message to="Jordan">FIRST: all good.</message>\n<score>3</score>\n<decision>ACCEPT</decision>',
                       '<message to="Jordan">SECOND: fix the table.</message>', REVIEW]}})]))
        self.assertEqual(status, "completed", rec.state.get("error"))
        first = next(m for m in msgs(rec, "boss", "critic") if "FIRST" in m["body"])
        self.assertEqual(first["status"], "superseded")
        second = next(m for m in msgs(rec, "boss", "critic") if "SECOND" in m["body"])
        self.assertEqual(second["status"], "delivered")
        self.assertEqual(first["by_turn"], second["source"])

    def test_invalid_decision_retry_replaces_the_first_message(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1, "replies": {
            "review": ['<message to="Jordan">FIRST: keep going.</message>\nNot sure yet.',
                       '<message to="Jordan">SECOND: fix the table.</message>\n<decision>REVISE</decision>',
                       REVIEW]}})]))
        self.assertEqual(status, "completed", rec.state.get("error"))
        first = next(m for m in msgs(rec, "boss", "critic") if "FIRST" in m["body"])
        self.assertEqual(first["status"], "superseded")
        self.assertTrue(any(m["status"] == "delivered" for m in msgs(rec, "boss", "critic") if "SECOND" in m["body"]))
        retry = next(t for t in turns(rec) if t["agent"] == "boss" and "Please end your reply" in t["input"])
        self.assertIn("PAUSE", retry["input"])

    def test_no_message_retry_replaces_only_when_it_gives_one(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1, "replies": {
            "answer": ["Go with the simpler option."],
            "message": ['<message to="Jordan">RETRY: the simpler option.</message>']}},
            worker={"replies": {"work": ["Changed it.\n\n<question>Bold headers?</question>", "Done."]}},
            critic={"replies": {"verify": [
                "<question>Bold headers or not?</question>\n<checklist>- [ ] x</checklist>\n<verdict>FAIL</verdict>",
                "<checklist>- [x] x</checklist>\n<verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed", rec.state.get("error"))
        self.assertTrue(any(m["status"] == "delivered" for m in msgs(rec, "boss", "critic") if "RETRY" in m["body"]))
        self.assertFalse([m for m in msgs(rec, "boss", "critic") if m["status"] == "sent"])


class TestReviewPause(unittest.TestCase):
    def test_pause_waits_for_me_and_the_review_comes_back(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(mgr={"milestones": 1, "replies": {"review": [
            '<message to="Jordan">HELD: wait for the data.</message>\nPlease send me the cluster numbers.\n'
            '<score>3</score>\n<decision>PAUSE</decision>', REVIEW]}})])
        L = Live(self, cfg)
        st, _ = L.gate("wait")
        mg = st["managed"]
        self.assertEqual(mg["phase"], "wait")
        self.assertEqual(mg["milestones"][0]["status"], "active")
        self.assertIn("cluster numbers", mg["gate"]["text"])
        self.assertEqual([r["decision"] for r in st["reviews"]], ["PAUSE"])
        held = next(m for m in st["messages"] if "HELD" in m["body"])
        self.assertEqual(held["status"], "held")
        L.control({"kind": "gate", "decision": "approve", "message": "NOTE-Y: the numbers are in data/cluster.csv.",
                   "gate": mg["gate"]["key"]}, "resume")
        status, rec = L.done()
        self.assertEqual(status, "completed", rec.state.get("error"))
        reviews = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertIn("NOTE-Y", reviews[1]["input"])
        self.assertNotIn("Please end your reply", reviews[1]["input"])
        self.assertEqual(reviews[0]["input"].count("From Jordan, on milestone 1"), 1)
        self.assertIn("From Jordan, on milestone 1", reviews[1]["input"])
        self.assertEqual([r["decision"] for r in rec.state["reviews"]], ["PAUSE", "ACCEPT"])
        self.assertEqual(next(m for m in rec.state["messages"] if "HELD" in m["body"])["status"], "superseded")
        self.assertFalse(any(t["agent"] == "critic" and "HELD" in t["input"] for t in turns(rec)))
        self.assertFalse(any(t["agent"] == "critic" and t["purpose"] == "guidance" and "NOTE-Y" in t["input"]
                             for t in turns(rec)))

    def test_send_back_returns_to_the_review(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(mgr={"milestones": 1, "replies": {"review": [
            "Please send me the cluster numbers.\n<decision>PAUSE</decision>", REVIEW]}})])
        L = Live(self, cfg)
        st, _ = L.gate("wait")
        L.control({"kind": "gate", "decision": "revise", "message": "NOTE-Z: no numbers this week.",
                   "gate": st["managed"]["gate"]["key"]}, "sendback")
        status, rec = L.done()
        self.assertEqual(status, "completed", rec.state.get("error"))
        reviews = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertIn("NOTE-Z", reviews[1]["input"])
        self.assertIn("I'm sending it back", reviews[1]["input"])
        self.assertFalse(any(t["purpose"] == "pause" for t in turns(rec)))


if __name__ == "__main__":
    unittest.main()
