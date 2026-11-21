"""E2E fixes 4: from a live run of B6/B9/B9b/B10/B11 (opus manager, haiku critic/workers/monitors, three parallel
workers with 2–3 minute background jobs). Mock backend only."""

from protocol_fixtures import worker_instruction

import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.config import load_config  # noqa: E402
from test_b11_parallel import ASSIGN, CK_DONE, CK_OPEN, Base  # noqa: E402
from test_b6_b9 import Live, unit  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

# the manager approves the workers at kickoff, before the critic's brief (as the live manager did)
KICKOFF = ('<message to="Jordan">Build tables, figures and appendix; they are independent.</message>\n'
           '<rubric>tables right\nfigures right\nappendix right</rubric>\n'
           '<workers n="3">Independent parts, one person each.</workers>')
# the live critic's brief: a step for its one worker plus its plan (it did not know about the others)
BRIEF = 'Plan in the checklist file.\n<message to="Sam">Step 1 of 3: make table A.</message>'


class TestPlanRevise(unittest.TestCase):
    def test_a_sent_back_plan_is_still_the_plan(self):
        """The live manager, sent back on the plan, started milestone 1 in that turn (instruction, rubric, workers)
        while the gate was still open."""
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, [managed(controller={"approve_plan": True}, mgr={"milestones": 1})])
        L = Live(self, cfg)
        st, _rd = L.gate("plan")
        key = st["managed"]["gate"]["key"]
        L.control({"kind": "gate", "gate": key, "decision": "revise", "message": "Add the Python version."}, "g1")
        st, _rd = L.gate("plan", not_key=key)
        L.control({"kind": "gate", "gate": st["managed"]["gate"]["key"], "decision": "approve", "message": ""}, "g2")
        _status, rec = L.done()
        plans = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertIn("Add the Python version.", plans[1]["input"])
        self.assertIn("nothing starts until I approve it", plans[1]["input"])

    def test_board_refuses_a_click_for_a_gate_that_is_not_open(self):
        """A send back posted with the gate's kind ("plan") instead of its key was answered "ok" and silently
        dropped by the run, which kept waiting."""
        from orch.board import Board, serve
        tmp = Path(tempfile.mkdtemp())
        r = unit(tmp)
        r.mg["gate"] = {"kind": "plan", "key": "plan-0-0", "title": "Milestone plan"}
        r.rec.save()
        cfg = load_config(str(tmp / "cfg.json"))
        srv, url = serve({"m": Board("m", lambda: r.rec.run_dir, cfg.monitor("m"))}, port=8897)
        try:
            req = urllib.request.Request(url + "api/gate", data=json.dumps(
                {"gate": "plan", "decision": "revise", "message": "x"}).encode())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 409)
            self.assertIn("plan-0-0", e.exception.read().decode())
            req = urllib.request.Request(url + "api/gate", data=json.dumps(
                {"gate": "plan-0-0", "decision": "revise", "message": "x"}).encode())
            self.assertEqual(json.loads(urllib.request.urlopen(req).read()), {"ok": True})
        finally:
            srv.shutdown()
            srv.server_close()


class TestMessageAtTheMilestoneGate(unittest.TestCase):
    def test_comments_at_the_gate_do_not_start_the_next_milestone(self):
        """Review-file comments sent back while milestone 1 waited at its gate: the manager's message turn sent the
        critic milestone 2's instruction, and the critic gave it to the worker before the gate was approved."""
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, [managed(controller={"approve_milestones": True}, mgr={"milestones": 1})])
        L = Live(self, cfg)
        st, _rd = L.gate("milestone")
        L.control({"to": ["boss"], "message": "My comments: put the table first."}, "note")
        for _ in range(200):  # its message turn runs while the gate waits
            _st, rd = L.state()
            if any("My comments" in (p.read_text() if p.exists() else "")
                   for p in (rd / "rounds").glob("r*/t*-boss.input.md")):
                break
            time.sleep(0.05)
        st, rd = L.state()
        L.control({"kind": "gate", "gate": st["managed"]["gate"]["key"], "decision": "approve", "message": ""}, "g")
        _status, rec = L.done()
        inc = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "incoming"]
        self.assertTrue(inc)
        self.assertIn("My comments: put the table first.", inc[0]["input"])
        self.assertIn("Milestone 1 is waiting for my OK at the gate", inc[0]["input"])


class TestMissingVerdict(unittest.TestCase):
    def test_a_review_without_a_verdict_is_asked_for_it_once(self):
        """The haiku critic's milestone 2 review ended without <verdict>: the run stopped as "failed" with no error
        recorded."""
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, [managed(controller={"checklist": False}, mgr={"milestones": 1}, critic={"replies": {
            "verify": ["Step 1 passes; the README matches the code."], "verdict": ["<verdict>PASS</verdict>"]}})])
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        asked = [t for t in turns(rec) if t["purpose"] == "verdict"]
        self.assertEqual(len(asked), 1)
        self.assertIn("ended without a verdict", asked[0]["input"])
        (tmp / "b").mkdir()
        cfg = make_cfg(tmp / "b", [managed(controller={"checklist": False}, mgr={"milestones": 1}, critic={
            "replies": {"verify": [worker_instruction("No tag.")], "verdict": ["Still no tag."]}})])
        status, rec = run(cfg)
        self.assertEqual(status, "failed")
        self.assertIn("no verdict", rec.state.get("error") or "")


class TestWorkersBeforeTheBrief(Base):
    def test_brief_knows_the_people_and_its_step_for_one_worker_is_dropped(self):
        """Approved at kickoff, the workers were unknown to the critic's brief: it gave step 1 to its one worker and
        asked whether it could have more. That step message stayed "sent" for good (the section handed steps out
        afresh), and the job wait took it for a message: the worker got a turn every 3 s while its job ran."""
        status, rec = run(self.cfg(mgr={"replies": {"kickoff": [KICKOFF]}},
                                   critic={"replies": {"brief": [worker_instruction(BRIEF)], "par_assign": [ASSIGN],
                                                       "par_review": ["Checked. <verdict>PASS</verdict>"],
                                                       "verify": ["All hold. <verdict>PASS</verdict>"]},
                                           "files": {"brief": [{str(self.ck): CK_OPEN}],
                                                     "par_review": [{str(self.ck): CK_DONE}]}}))
        self.assertEqual(status, "completed")
        tt = turns(rec)
        brief = next(t for t in tt if t["purpose"] == "brief")["input"]
        self.assertIn("3 people can work on this at the same time", brief)
        self.assertEqual(sorted(t["agent"] for t in tt if t["purpose"] == "work"), ["writer", "writer-2", "writer-3"])
        step1 = next(m for m in rec.state["messages"] if m["body"].startswith("Step 1 of 3"))
        self.assertEqual(step1["status"], "superseded")
        self.assertFalse([m for m in rec.state["messages"] if m["status"] == "sent"])

    def test_a_phase_message_never_wakes_a_worker_whose_job_runs(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.ledger.add("critic", "writer", "Step 1.", carrier={"phase": "brief"})
        self.assertFalse(r.message_for_job_turn("writer"))  # only the review's prompt carries it
        r.ledger.add("critic", "writer", "Also check the log.", carrier={"inbox": "n1.json"})
        self.assertTrue(r.message_for_job_turn("writer"))


class TestParallelReview(Base):
    def test_review_message_reaches_its_worker_once(self):
        """In a parallel review, the critic's <message to="the worker"> went into the worker's next prompt twice:
        once in the review text and once as a separate update."""
        rv = ['<message to="Sam">Fix the caption of table A.</message>\n<verdict>FAIL</verdict>',
              "Checked. <verdict>PASS</verdict>"]
        status, rec = run(self.cfg(critic={"replies": {"brief": [
            'Plan.\n<workers n="3" steps="1,2,3">independent</workers>'], "par_assign": [ASSIGN],
            "par_review": rv, "verify": ["All hold. <verdict>PASS</verdict>"]},
            "files": {"brief": [{str(self.ck): CK_OPEN}], "par_review": [{}, {str(self.ck): CK_DONE}]}},
            # Sam (writer) finishes first, so the first review (the one with the message to Sam) is of Sam's step
            worker={"delay_for": {"writer-2": {"work": 1.2}, "writer-3": {"work": 1.2}}}))
        self.assertEqual(status, "completed")
        tt = turns(rec)
        first = next(t for t in tt if t["purpose"] == "par_review")
        self.assertEqual(first["par_worker"], "writer")
        who = first["par_worker"]
        nxt = [t for t in tt if t["agent"] == who and t["purpose"] == "work"][1]["input"]
        self.assertEqual(nxt.count("Fix the caption of table A."), 1)
        m = next(x for x in rec.state["messages"] if x["body"] == "Fix the caption of table A.")
        self.assertEqual(m["status"], "delivered")
        self.assertFalse([x for x in rec.state["messages"] if x["status"] == "sent"])

    def test_a_message_turn_during_a_job_does_not_end_the_wait(self):
        """A message for a worker whose job ran gave it a turn; that turn's "still running" reply then went to the
        critic as a finished step, and every FAIL review started another such turn (11 rounds in the live run)."""
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", "sleep 2.47"]))
        tmp = self.tmp
        cfg = self.cfg(worker={"delays": {"work": 0.2}, "spawn": "sleep 2.47",
                               "replies": {"work": ["Started the run in the background.", "Noted.",
                                                    "The run finished; out is in."]}},
                       critic={"replies": {"brief": ['Plan.\n<workers n="2" steps="1,2">independent</workers>'],
                                           "par_assign": [ASSIGN.rsplit("\n", 1)[0]],
                                           "par_review": ["Checked. <verdict>PASS</verdict>"],
                                           "verify": ["All hold. <verdict>PASS</verdict>"]},
                               "files": {"brief": [{str(self.ck): CK_OPEN.split("\n\n## Step 3")[0] + "\n"}],
                                         "par_review": [{str(self.ck): CK_DONE.split("\n\n## Step 3")[0] + "\n"}]}},
                       mgr={"replies": {"incoming": ['<workers n="2">Approved.</workers>', "Noted."]}})
        L = Live(self, cfg)
        L.until(lambda st: any(s.get("status") == "job" for s in ((st["managed"].get("par") or {}).get("slots")
                                                                    or {}).values()), "a job wait", timeout=20)
        # the wait comes from the critic's review (<wait-job/>), not from orch on its own
        L.control({"to": ["writer"], "message": "Also note the seed in the log."}, "note")
        status, rec = L.done(timeout=40)
        self.assertEqual(status, "completed")
        tt = turns(rec)
        mine = [t for t in tt if t.get("par_worker") == "writer" or (t["agent"] == "writer" and t["purpose"] == "work")]
        reviews = [t for t in mine if t["purpose"] == "par_review"]
        waits = [t for t in reviews if "<wait-job/>" in t["output"]]
        self.assertGreaterEqual(len(waits), 1)  # the critic saw the running job and waited (again after the message turn)
        self.assertEqual(len(reviews) - len(waits), 1, [(t["agent"], t["purpose"]) for t in mine])
        self.assertIn("has finished", [t for t in mine if t["purpose"] == "work"][-1]["input"])
        self.assertIn("Also note the seed in the log.", "\n".join(t["input"] for t in mine if t["purpose"] == "work"))


if __name__ == "__main__":
    unittest.main()
