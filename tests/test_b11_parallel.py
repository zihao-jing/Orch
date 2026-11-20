"""B11: parallel workers. The critic proposes several workers for independent steps; the manager approves within its
budget (more goes to me at a gate); copies of the worker run at the same time, each with its own session, lane, job
tag and monitor; the critic hands out steps by id and reviews each step on its own, one review at a time; orch keeps
the work board. All with the mock backend: no live agents."""

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import parallel as PW  # noqa: E402
from orch.backends import MockBackend, TurnResult  # noqa: E402
from orch.recorder import Recorder, read_json  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_b6_b9 import Live  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

EVERY = 0.005  # minutes between progress checks (0.3 s)

CK_OPEN = ("# Checklist\n\n## Step 1: tables\n- [ ] table A done\n\n## Step 2: figures\n- [ ] figure B done\n\n"
           "## Step 3: appendix\n- [ ] appendix C done\n")
CK_DONE = CK_OPEN.replace("- [ ]", "- [x]")
PROPOSE = ('Here is the plan; the checklist file has it.\n\n'
           '<workers n="3" steps="1,2,3">Steps 1, 2 and 3 touch different files.</workers>')
ASSIGN = ('<to worker="writer" step="1" out="out/a">Make table A in out/a.</to>\n'
          '<to worker="writer-2" step="2" out="out/b">Make figure B in out/b.</to>\n'
          '<to worker="writer-3" step="3" out="out/c">Make appendix C in out/c.</to>')


def events(rec):
    return [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]


def ts(t, key):
    return datetime.fromisoformat(t[key]).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.sdir = self.tmp / "agents"

    @property
    def ck(self):
        return self.sdir / "critic" / "checklist-m1.md"

    def cfg(self, controller=None, mgr=None, critic=None, worker=None, monitor=None, worker_cfg=None):
        ck = str(self.ck)
        crit = {"replies": {"brief": [PROPOSE], "par_assign": [ASSIGN],
                            "par_review": ["Checked it against the file. <verdict>PASS</verdict>"],
                            "verify": ["All steps hold. <verdict>PASS</verdict>"]},
                "files": {"brief": [{ck: CK_OPEN}], "par_review": [{ck: CK_DONE}]}}
        crit.update(critic or {})
        wk = {"delays": {"work": 0.6}, "replies": {"work": ["Done: wrote my part in out/{agent}.txt."]},
              "files": {"work": [{"out/{agent}.txt": "part of {agent}"}]}}
        wk.update(worker or {})
        mg = {"milestones": 1, "replies": {"incoming": ['<workers n="3">Approved: they are independent.</workers>',
                                                        "Noted."]}}
        mg.update(mgr or {})
        m = managed(controller=controller, mgr=mg, critic=crit, worker=wk)
        if worker_cfg:
            m["agents"][2].update(worker_cfg)
        if monitor is not None:
            m["agents"].append({"name": "monitor", "role": "monitor", "backend_options": monitor})
        for ag in m["agents"]:
            ag["state_file"] = str(self.sdir / ag["name"] / "state.md")
        return make_cfg(self.tmp, [m])


class TestParallelUnit(unittest.TestCase):
    def test_tags_and_budget(self):
        t = PW.workers_tag('x <workers n="3" steps="2,3,4">steps 2,3,4 are independent</workers>')
        self.assertEqual((t["n"], t["steps"]), (3, [2, 3, 4]))
        t = PW.workers_tag("<workers>steps 2-4 are independent</workers>")
        self.assertEqual((t["n"], t["steps"]), (3, [2, 3, 4]))
        secs = PW.to_sections('<to worker="w-2" step="3" out="o/">do it</to> rest <to worker="w">go</to>')
        self.assertEqual([(s["worker"], s["step"], s["out"], s["text"]) for s in secs],
                         [("w-2", 3, "o/", "do it"), ("w", None, "", "go")])
        self.assertEqual(PW.decide(3, [1, 2, 3], 3, 6, 10, 0, False)[:2], ("approved", 3))
        self.assertEqual(PW.decide(5, [], 3, 6, 10, 0, False)[:2], ("gate", 5))
        self.assertEqual(PW.decide(8, [], 3, 6, 10, 0, True)[:2], ("approved", 6))  # night: the hard cap
        self.assertEqual(PW.decide(3, [1, 2], 3, 6, 10, 0, False)[:2], ("approved", 2))  # no more than the steps
        self.assertEqual(PW.decide(3, [], 3, 6, 2, 0, False)[0], "gate")  # fewer rounds left than workers
        self.assertEqual(PW.decide(3, [], 3, 6, 10, 2, False)[0], "gate")  # parallel_turns below n
        self.assertEqual(PW.decide(1, [1, 2], 3, 6, 10, 0, False)[0], "declined")

    def test_board_keeps_the_shared_limits(self):
        par = {"slots": {"w": {"status": "working", "step": 1, "out": "o/a", "turns": 1, "round": 3},
                         "w-2": {"status": "done", "done_steps": [2]}}, "queue": [3]}
        ck = {"steps": [{"title": "Step 1: a", "items": []}, {"title": "Step 2: b", "items": []},
                        {"title": "Step 3: c", "items": []}]}
        b = PW.render_board(par, {"w": "Sam", "w-2": "Sam-2"}, ck)
        self.assertIn("| Sam (`w`) | Step 1: a | working (round 3) | o/a |", b)
        self.assertIn("Steps not handed out yet: Step 3: c.", b)
        edited = b.replace(PW.LIMITS_EMPTY, "- one GPU: only one training job at a time")
        again = PW.render_board(par, {"w": "Sam", "w-2": "Sam-2"}, ck, edited)
        self.assertIn("- one GPU: only one training job at a time", again)
        self.assertNotIn(PW.LIMITS_EMPTY, again)


class TestParallelRun(Base):
    def test_proposal_approval_concurrent_workers_reviews_and_completion(self):
        status, rec = run(self.cfg(monitor={"replies": {"check": ["the log grows <status>ok</status>"]}},
                                   worker_cfg={"check_every": EVERY}))
        self.assertEqual(status, "completed")
        st = rec.state
        tt = turns(rec)
        work = [t for t in tt if t["purpose"] == "work"]
        self.assertEqual(sorted(t["agent"] for t in work), ["writer", "writer-2", "writer-3"])
        # each its own session and lane
        self.assertEqual(len({t["session_id"] for t in work}), 3)
        # the turns overlapped in time
        spans = [(ts(t, "started_at"), ts(t, "started_at") + float(t["duration_s"])) for t in work]
        self.assertLess(max(a for a, _b in spans), min(b for _a, b in spans), spans)
        # routed by id: each worker got its own instruction, and no other's
        for t, mine, other in [(w, *{"writer": ("table A", "figure B"), "writer-2": ("figure B", "appendix C"),
                                     "writer-3": ("appendix C", "table A")}[w["agent"]]) for w in work]:
            self.assertIn(mine, t["input"])
            self.assertNotIn(other, t["input"])
        assert_no_role_leaks(self, rec)
        for t in work:  # no worker reads about the others
            for n in ("writer-2", "writer-3", "Sam-2", "Sam-3") if t["agent"] == "writer" else ("writer-3",) if t["agent"] == "writer-2" else ("writer-2",):
                self.assertNotIn(n, t["input"])
        # one review per finished step, one at a time
        revs = [t for t in tt if t["purpose"] == "par_review"]
        self.assertEqual(sorted(t["par_worker"] for t in revs), ["writer", "writer-2", "writer-3"])
        spans = sorted((ts(t, "started_at"), ts(t, "started_at") + float(t["duration_s"])) for t in revs)
        for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0 + 1.0)  # started_at has one-second resolution
        ev = events(rec)
        self.assertTrue(any(e.get("event") == "workers_decision" and e.get("status") == "approved" for e in ev))
        # one monitor per worker
        checkers = {e.get("agent"): e.get("checker") for e in ev if e.get("event") == "progress_check"}
        self.assertEqual(checkers, {"writer": "monitor", "writer-2": "monitor-2", "writer-3": "monitor-3"})
        # the work board
        board = (self.sdir / "critic" / "work-m1.md").read_text()
        self.assertIn("`writer-3`", board)
        self.assertIn("out/c", board)
        self.assertIn("Steps done here:", board)
        self.assertIn(PW.LIMITS_HEAD, board)
        # the section ended, the copies are gone, the milestone completed after the critic took stock
        mg = st["managed"]
        self.assertIsNone(mg.get("par"))
        self.assertEqual({s["status"] for s in mg["par_last"]["slots"].values()}, {"done"})
        self.assertTrue(any(t["purpose"] == "verify" and "finished" in t["input"] for t in tt))
        self.assertEqual(mg["milestones"][0]["status"], "done")
        for n in ("writer", "writer-2", "writer-3"):
            self.assertTrue((self.tmp / "ws" / "out" / f"{n}.txt").exists())

    def test_a_pass_on_a_step_with_open_items_is_overruled(self):
        ck = str(self.ck)
        status, rec = run(self.cfg(critic={"files": {"brief": [{ck: CK_OPEN}],
                                                     "par_review": [{}, {ck: CK_DONE}]}}))
        self.assertEqual(status, "completed")
        work = [t for t in turns(rec) if t["purpose"] == "work"]
        self.assertEqual(len(work), 4)  # the first step reviewed went back once
        self.assertIn("are not all ticked", (rec.run_dir / "progress.md").read_text())
        nudged = [t for t in turns(rec) if t["purpose"] == "par_review" and "not all `[x]`" in t["input"]]
        self.assertEqual(len(nudged), 1)


class TestParallelResume(Base):
    def fail_assignment(self, cfg, stop=None):
        original = MockBackend.run

        def fail(backend, req, should_stop):
            if req.context.get("purpose") == "par_assign":
                if stop is not None:
                    stop.set()
                return TurnResult("stopped" if stop is not None else "timeout", "", req.session_id)
            return original(backend, req, should_stop)

        with mock.patch.object(MockBackend, "run", fail):
            status, rec = run(cfg, stop=stop)
        self.assertEqual(status, "stopped" if stop is not None else "failed")
        self.assertEqual(rec.state["managed"]["phase"], "parallel")
        self.assertFalse(any(t["purpose"] == "work" for t in turns(rec)))
        return rec

    def assert_completed_once(self, cfg):
        status, rec = run(cfg, resume=True)
        self.assertEqual(status, "completed", rec.state.get("error"))
        work = [t for t in turns(rec) if t["purpose"] == "work"]
        self.assertEqual(sorted(t["agent"] for t in work), ["writer", "writer-2", "writer-3"])
        self.assertNotIn("par", rec.state["managed"])
        return rec

    def test_assignment_timeout_keeps_assignment_pending_on_resume(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}})
        rec = self.fail_assignment(cfg)
        par = read_json(rec.run_dir / "state.json")["managed"]["par"]
        self.assertTrue(par["active"])
        self.assertTrue(par["assign_due"])
        self.assertEqual({s["status"] for s in par["slots"].values()}, {PW.IDLE})
        rec = self.assert_completed_once(cfg)
        assignments = [t for t in turns(rec) if t["purpose"] == "par_assign"]
        self.assertEqual([t["status"] for t in assignments], ["timeout", "ok"])
        self.assertEqual(assignments[0]["session_id"], assignments[1]["session_id"])

    def test_stop_during_assignment_keeps_assignment_pending(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}})
        rec = self.fail_assignment(cfg, stop=threading.Event())
        self.assertTrue(rec.state["managed"]["par"]["assign_due"])
        self.assert_completed_once(cfg)

    def test_assignment_failure_preserves_worktrees_until_completion(self):
        git_repo(self.tmp / "ws")
        cfg = self.cfg(controller={"worker_worktrees": True},
                       worker={"files": {}, "delays": {"work": 0.01}})
        rec = self.fail_assignment(cfg)
        paths = [Path(s["worktree"]["path"]) for s in rec.state["managed"]["par"]["slots"].values()
                 if s.get("worktree")]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(p.is_dir() for p in paths))
        self.assertFalse(any(e["event"] == "worktree_end" for e in events(rec)))
        rec = self.assert_completed_once(cfg)
        self.assertFalse(any(p.exists() for p in paths))
        self.assertEqual(len([e for e in events(rec) if e["event"] == "worktree_end"]), 2)

    def archive_unstarted(self, rec):
        mg = rec.state["managed"]
        last = mg.pop("par")
        last.update(active=False, assign_due=False, ended_at="2026-11-20T00:00:00Z")
        mg["par_last"] = last
        rec.save()
        return last

    def test_legacy_unstarted_archive_is_recovered(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}})
        rec = self.fail_assignment(cfg)
        self.archive_unstarted(rec)
        rec = self.assert_completed_once(cfg)
        self.assertTrue(any(e["event"] == "parallel_recovered" for e in events(rec)))

    def test_unsafe_or_missing_legacy_archive_is_rejected_without_work(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}})
        rec = self.fail_assignment(cfg)
        pristine = copy.deepcopy(self.archive_unstarted(rec))
        cases = [{}, {**pristine, "ms": 99}, {**pristine, "turns": 1},
                 {**pristine, "worktrees": True}, {**pristine, "reviews": ["writer"]}]
        for change in ({"status": PW.DONE}, {"step": 1}, {"turns": 1}, {"worktree": {"path": "gone"}},
                       {"done_steps": [1]}):
            last = copy.deepcopy(pristine)
            last["slots"]["writer"].update(change)
            cases.append(last)
        for last in cases:
            with self.subTest(archive=last):
                rec.state["managed"]["par_last"] = last
                rec.save()
                status, resumed = run(cfg, resume=True)
                self.assertEqual(status, "failed")
                self.assertIn("cannot safely restore", resumed.state.get("error", ""))
                self.assertNotIn("Traceback", resumed.state["error"])
                self.assertNotIn("par", resumed.state["managed"])
                self.assertEqual(resumed.state["managed"]["par_last"], last)
                self.assertFalse(any(t["purpose"] == "work" for t in turns(resumed)))

    def test_review_timeout_keeps_reports_and_collects_workers_before_resume(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}})
        original = MockBackend.run
        review_failed = threading.Event()
        finished = []

        def fail(backend, req, should_stop):
            purpose = req.context.get("purpose")
            if purpose == "par_review":
                self.assertEqual(finished, ["writer"])
                review_failed.set()
                return TurnResult("timeout", "", req.session_id)
            if purpose == "work" and req.agent != "writer":
                self.assertTrue(review_failed.wait(10), "first worker was never reviewed")
            result = original(backend, req, should_stop)
            if purpose == "work":
                finished.append(req.agent)
            return result

        with mock.patch.object(MockBackend, "run", fail):
            status, rec = run(cfg)
        self.assertEqual(status, "failed")
        self.assertIn("critic par_review turn ended with status timeout", rec.state["error"])
        par = read_json(rec.run_dir / "state.json")["managed"]["par"]
        self.assertEqual(sorted(par["reviews"]), ["writer", "writer-2", "writer-3"])
        for name, slot in par["slots"].items():
            self.assertEqual(slot["status"], PW.REVIEW)
            self.assertIn(f"out/{name}.txt", slot["report"])
        rec = self.assert_completed_once(cfg)
        reviews = [t for t in turns(rec) if t["purpose"] == "par_review"]
        self.assertEqual(len(reviews), 4)
        for review in reviews:
            self.assertIn(f"out/{review['par_worker']}.txt", review["input"])

    def test_worker_error_during_failed_review_does_not_discard_other_results(self):
        cfg = self.cfg(worker={"delays": {"work": 0.01}}, worker_cfg={"retry_waits": []})
        original = MockBackend.run
        review_failed = threading.Event()

        def fail(backend, req, should_stop):
            purpose = req.context.get("purpose")
            if purpose == "par_review":
                review_failed.set()
                return TurnResult("timeout", "", req.session_id)
            if purpose == "work" and req.agent != "writer":
                self.assertTrue(review_failed.wait(10))
                if req.agent == "writer-2":
                    return TurnResult("error", "worker failed", req.session_id)
            return original(backend, req, should_stop)

        with mock.patch.object(MockBackend, "run", fail):
            status, rec = run(cfg)
        self.assertEqual(status, "failed")
        self.assertIn("critic par_review turn ended with status timeout", rec.state["error"])
        par = rec.state["managed"]["par"]
        self.assertEqual(sorted(par["reviews"]), ["writer", "writer-3"])
        self.assertTrue(par["slots"]["writer-2"]["prompt"])
        self.assertTrue(any(e["event"] == "parallel_worker_failed" and e["agent"] == "writer-2"
                            for e in events(rec)))
        status, rec = run(cfg, resume=True)
        self.assertEqual(status, "completed", rec.state.get("error"))
        work = [t for t in turns(rec) if t["purpose"] == "work"]
        self.assertEqual(sorted(t["agent"] for t in work), ["writer", "writer-2", "writer-2", "writer-3"])


CK4_OPEN = CK_OPEN + "\n## Step 4: index\n- [ ] index D done\n"
ASSIGN4 = ASSIGN + '\n<to worker="writer-4" step="4" out="out/d">Make index D in out/d.</to>'


class TestParallelGateAndStress(Base):
    def test_over_budget_proposal_becomes_my_gate(self):
        ck = str(self.ck)
        cfg = self.cfg(controller={"max_workers": 2},
                       critic={"replies": {"brief": [PROPOSE.replace('n="3" steps="1,2,3"', 'n="4" steps="1,2,3,4"')],
                                           "par_assign": [ASSIGN4],
                                           "par_review": ["Checked. <verdict>PASS</verdict>"],
                                           "verify": ["All steps hold. <verdict>PASS</verdict>"]},
                               "files": {"brief": [{ck: CK4_OPEN}], "par_review": [{ck: CK4_OPEN.replace("- [ ]", "- [x]")}]}},
                       mgr={"replies": {"incoming": ['<workers n="4">They are independent.</workers>', "Noted."]}})
        live = Live(self, cfg)
        st, _rd = live.gate("workers")
        self.assertEqual(st["managed"]["par_req"]["status"], "gate")
        self.assertEqual(st["managed"]["phase"], "workers_gate")
        self.assertFalse(any(t["agent"].startswith("writer") for t in turns_of(_rd)))  # nobody works meanwhile
        live.control({"kind": "gate", "decision": "approve", "gate": st["managed"]["gate"]["key"]}, "approve")
        status, rec = live.done()
        self.assertEqual(status, "completed")
        work = [t for t in turns(rec) if t["purpose"] == "work"]
        self.assertEqual(sorted(t["agent"] for t in work), ["writer", "writer-2", "writer-3", "writer-4"])
        gates = rec.state["managed"]["gates"]
        self.assertEqual([(g["kind"], g["decision"]) for g in gates], [("workers", "approve")])

    def test_declined_at_the_gate_one_worker_goes_on(self):
        ck = str(self.ck)
        cfg = self.cfg(controller={"max_workers": 2},
                       critic={"replies": {"brief": [PROPOSE], "verify": ["All steps hold. <verdict>PASS</verdict>"]},
                               "files": {"brief": [{ck: CK_OPEN}], "verify": [{ck: CK_DONE}]}})
        live = Live(self, cfg)
        st, _rd = live.gate("workers")
        live.control({"kind": "gate", "decision": "revise", "message": "too many", "gate": st["managed"]["gate"]["key"]},
                     "revise")
        status, rec = live.done()
        self.assertEqual(status, "completed")
        self.assertEqual({t["agent"] for t in turns(rec) if t["purpose"] == "work"}, {"writer"})
        crit = [t for t in turns(rec) if t["agent"] == "critic"]
        self.assertTrue(any("keep to one person" in t["input"] for t in crit))

    def test_stress_reviews_fail_then_pass_messages_by_id_and_safe_state(self):
        ck = str(self.ck)
        for k in range(3):
            with self.subTest(run=k):
                self.tmp = Path(tempfile.mkdtemp()).resolve()
                self.sdir = self.tmp / "agents"
                ck = str(self.ck)
                fail = "Not yet: add the caption. <verdict>FAIL</verdict>"
                cfg = self.cfg(
                    critic={"replies": {"brief": [PROPOSE], "par_assign": [ASSIGN],
                                        "par_review": [fail + '\n<message to="writer-3">Also keep the file short.</message>',
                                                       fail, fail, "Good. <verdict>PASS</verdict>"],
                                        "verify": ["All steps hold. <verdict>PASS</verdict>"]},
                            "files": {"brief": [{ck: CK_OPEN}], "par_review": [{}, {}, {ck: CK_DONE}]}},
                    worker={"delays": {"work": 0.05 * (k + 1)}})
                status, rec = run(cfg)
                self.assertEqual(status, "completed")
                st = read_json(rec.run_dir / "state.json")  # valid JSON on disk, the same as in memory
                self.assertEqual(st["round"], rec.state["round"])
                tt = turns(rec)
                work = [t for t in tt if t["purpose"] == "work"]
                self.assertGreaterEqual(len(work), 6)
                self.assertEqual(len({t["turn_id"] for t in tt}), len(tt))
                self.assertEqual(len({t["session_id"] for t in work}), 3)
                for t in work:  # each worker keeps its own session from turn to turn
                    self.assertEqual(t["session_id"], next(x["session_id"] for x in work if x["agent"] == t["agent"]))
                short = {t["agent"] for t in work if "keep the file short" in t["input"]}
                self.assertEqual(short, {"writer-3"})
                revs = [t for t in tt if t["purpose"] == "par_review"]
                # a step is reviewed when its turn ends; a message that came in meanwhile gives a turn first
                self.assertLessEqual(len(revs), len(work))
                self.assertGreaterEqual(len(revs), 6)
                assert_no_role_leaks(self, rec)
                rounds = [json.loads(p.read_text()) for p in sorted((rec.run_dir / "rounds").glob("r*/round.json"))]
                self.assertTrue(all(r.get("status") == "done" for r in rounds), rounds)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")


class TestWorktrees(Base):
    def test_add_and_remove_keep_what_would_be_lost(self):
        repo = self.tmp / "repo"
        git_repo(repo)
        wt = PW.worktree_add(str(repo), "w-2", "run1")
        self.assertTrue(Path(wt["path"]).is_dir())
        self.assertEqual(git(wt["path"], "rev-parse", "--abbrev-ref", "HEAD"), "parallel/run1-w-2")
        self.assertEqual(PW.worktree_remove(wt), (True, "removed"))
        self.assertFalse(Path(wt["path"]).exists())
        self.assertNotIn("parallel/run1-w-2", git(repo, "branch"))
        wt = PW.worktree_add(str(repo), "w-3", "run1")
        (Path(wt["path"]) / "new.txt").write_text("work")
        ok, why = PW.worktree_remove(wt)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)
        git(wt["path"], "add", ".")
        git(wt["path"], "commit", "-qm", "work")
        ok, why = PW.worktree_remove(wt)  # committed but not merged: kept for the critic to merge
        self.assertFalse(ok)
        self.assertIn("not merged", why)
        git(repo, "merge", "-q", "parallel/run1-w-3")
        self.assertEqual(PW.worktree_remove(wt), (True, "removed"))
        self.assertIsNone(PW.worktree_add(str(self.tmp), "w-4", "run1"))  # not a git repository

    def test_run_gives_each_copy_its_own_worktree_and_cleans_up(self):
        git_repo(self.tmp / "ws")
        status, rec = run(self.cfg(controller={"worker_worktrees": True},
                                   worker={"files": {}, "replies": {"work": ["Done with my step."]}}))
        self.assertEqual(status, "completed")
        work = {t["agent"]: t for t in turns(rec) if t["purpose"] == "work"}
        root = self.tmp / "ws-worktrees"
        self.assertEqual(Path(work["writer"]["cwd"]).resolve(), (self.tmp / "ws").resolve())
        for n in ("writer-2", "writer-3"):
            self.assertEqual(Path(work[n]["cwd"]).resolve().parent, root.resolve())
        ends = [e for e in events(rec) if e.get("event") == "worktree_end"]
        self.assertEqual(len(ends), 2)
        self.assertTrue(all(e["removed"] for e in ends))
        self.assertFalse(root.exists())
        self.assertNotIn("parallel/", git(self.tmp / "ws", "branch"))
        board = (self.sdir / "critic" / "work-m1.md").read_text()
        self.assertIn("git worktree", board)

    def test_night_mode_clips_to_the_hard_cap(self):
        cfg = self.cfg(controller={"max_workers": 1, "workers_hard_cap": 2},
                       worker={"delay_for": {"writer-2": {"work": 1.2}}},  # writer finishes first
                       critic={"delays": {"brief": 1.0}, "replies": {
                           "brief": [PROPOSE], "par_assign": [ASSIGN, '<to worker="writer" step="3">Make appendix C.</to>'],
                           "par_review": ["Checked. <verdict>PASS</verdict>"],
                           "verify": ["All steps hold. <verdict>PASS</verdict>"]}},
                       mgr={"replies": {"incoming": ['<workers n="3">All three.</workers>', "Noted."]}})
        live = Live(self, cfg)
        live.until(lambda st: st.get("status") == "running", "the run to start")
        live.control({"kind": "night", "on": True}, "night")
        status, rec = live.done()
        self.assertEqual(status, "completed")
        self.assertEqual(sorted({t["agent"] for t in turns(rec) if t["purpose"] == "work"}), ["writer", "writer-2"])
        d = [e for e in events(rec) if e.get("event") == "workers_decision"][0]
        self.assertEqual((d["status"], d["n"], d["asked"]), ("approved", 2, 3))
        steps = rec.state["managed"]["par_last"]["slots"]["writer"]["done_steps"]
        self.assertEqual(steps, [1, 3])  # the step left over went to whoever finished first


def turns_of(rd):
    out = []
    for p in sorted((Path(rd) / "rounds").glob("r*/t*.json")):
        if not p.name.endswith(".input.json"):
            out.append(read_json(p) or {})
    return out


if __name__ == "__main__":
    unittest.main()
