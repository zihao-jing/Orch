"""End-to-end tests using the mock and command backends. Run: python -m unittest discover tests"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.config import ConfigError, load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402
from orch.recorder import read_json  # noqa: E402
from orch import cli  # noqa: E402


def make_cfg(tmp: Path, monitors: list, defaults=None) -> str:
    (tmp / "ws").mkdir(exist_ok=True)
    cfg = {"output_dir": str(tmp / "out"), "defaults": defaults or {"backend": "mock"}, "monitors": monitors}
    p = tmp / "cfg.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def pipeline(name="m", **kw):
    m = {"name": name, "workspace": "ws", "goal": "g", "max_rounds": 4,
         "controller": {"mode": "pipeline"},
         "agents": [{"name": "w1", "role": "worker", "task": "do it"},
                    {"name": "v1", "role": "verifier", "backend_options": {"pass_after": 2}}]}
    m.update(kw)
    return m


def run(cfg_path, name="m", resume=False, stop=None):
    cfg = load_config(cfg_path)
    r = MonitorRunner(cfg.monitor(name), stop or threading.Event(), resume=resume, log=lambda s: None)
    return r.run(), r.rec


def turns(rec):
    out = []
    for p in sorted((rec.run_dir / "rounds").glob("r*/t*.json")):
        out.append(read_json(p))
    return out


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_feedback_loop_and_sessions(self):
        status, rec = run(make_cfg(self.tmp, [pipeline()]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertEqual([t["agent"] for t in ts], ["w1", "v1", "w1", "v1"])
        w = [t for t in ts if t["agent"] == "w1"]
        self.assertFalse(w[0]["resumed_session"])
        self.assertTrue(w[1]["resumed_session"])
        self.assertEqual(w[0]["session_id"], w[1]["session_id"])
        self.assertIn("add input validation", w[1]["input"])
        self.assertTrue((rec.run_dir / "report.html").is_file())
        self.assertTrue((rec.run_dir.parent.parent / "index.html").is_file())
        self.assertEqual(rec.state["rounds"][0]["decision"], "feedback → w1")

    def test_max_rounds(self):
        m = pipeline(max_rounds=2)
        m["agents"][1]["backend_options"] = {"pass_after": 99}
        status, _ = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "max_rounds_reached")

    def test_routing_reviews_and_feedback_to(self):
        m = pipeline()
        m["agents"] = [
            {"name": "a", "role": "worker"}, {"name": "b", "role": "worker"},
            {"name": "va", "role": "verifier", "reviews": ["a"], "backend_options": {"pass_after": 2}},
            {"name": "vb", "role": "verifier", "reviews": ["b"], "backend_options": {"pass_after": 1}},
        ]
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        r2 = [t["agent"] for t in turns(rec) if t["round"] == 2]
        self.assertEqual(r2, ["a", "va", "vb"])  # b got no feedback, so it is skipped in round 2
        va = [t for t in turns(rec) if t["agent"] == "va"][0]
        self.assertIn("handled", va["input"])
        self.assertNotIn("Report from b", va["input"])

    def test_unknown_verdict_counts_as_fail(self):
        m = pipeline(max_rounds=1)
        m["agents"][1]["backend_options"] = {"script": ["looks fine I guess"]}
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "failed")  # no verdict line → stop instead of feeding noise to the writer
        self.assertEqual(read_json(rec.run_dir / "rounds/r001/round.json")["verdicts"], {"v1": "UNKNOWN"})

    def test_empty_reply_stops_run(self):
        m = pipeline(max_rounds=3)
        m["agents"][1]["backend_options"] = {"script": [""]}
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "failed")
        self.assertEqual(len([t for t in turns(rec) if t["agent"] == "w1"]), 1)  # writer never got empty feedback

    def test_question_for_human_pauses_run(self):
        m = pipeline(max_rounds=3)
        m["agents"][1]["backend_options"] = {"script": ["QUESTIONS FOR HUMAN: which venue?\nVERDICT: FAIL"]}
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "needs_human")
        self.assertIn("which venue", (rec.run_dir / "NEEDS_HUMAN.md").read_text())

    def test_answer_reaches_worker_and_reviewer(self):
        m = pipeline(max_rounds=2)
        m["agents"][0]["backend_options"] = {"script": ["Done most of it. Which venue should I target?\n\nA) ICLR\nB) NeurIPS",
                                                        "Switched to NeurIPS."]}
        m["agents"][1]["backend_options"] = {"pass_after": 1}
        cfg = make_cfg(self.tmp, [m])
        status, rec = run(cfg)
        self.assertEqual(status, "needs_human")
        (rec.run_dir / "inbox" / "a-answer.json").write_text(json.dumps({"to": ["w1"], "message": "B) NeurIPS"}))
        status, rec = run(cfg, resume=True)
        self.assertEqual(status, "completed")
        ws = [t for t in turns(rec) if t["agent"] == "w1"]
        self.assertEqual(ws[-1]["input"].strip(), "B) NeurIPS")  # the worker gets only the answer
        v = [t for t in turns(rec) if t["agent"] == "v1"][0]
        self.assertIn("I decided: B) NeurIPS", v["input"])  # and the reviewer hears about it
        self.assertFalse(read_json(rec.run_dir / "state.json").get("forward"))

    def test_last_verdict_wins(self):
        m = pipeline(max_rounds=1)
        m["agents"][1]["backend_options"] = {"script": ["Earlier: VERDICT: FAIL\nfixed now\nVERDICT: **PASS**"]}
        status, _ = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")

    def test_error_retry_then_ok(self):
        m = pipeline()
        m["agents"][0].update(retries=1, backend_options={"fail_turns": [2]})  # 2nd turn of the session fails once
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        second = [t for t in turns(rec) if t["agent"] == "w1"][1]
        self.assertEqual(second["status"], "ok")
        self.assertEqual([a["status"] for a in second["attempts"]], ["error", "ok"])
        self.assertTrue(second["resumed_session"])

    def test_stop_on_error(self):
        m = pipeline()
        m["controller"]["stop_on_error"] = True
        m["agents"][0]["backend_options"] = {"fail_turns": [1]}
        status, _ = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "failed")

    def test_timeout(self):
        m = pipeline(max_rounds=1)
        m["agents"][0].update(backend="command", timeout=0.5,
                              backend_options={"command": [sys.executable, "-c", "import time; time.sleep(10)"]})
        t0 = time.time()
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(turns(rec)[0]["status"], "timeout")

    def test_stop_file_and_resume_continue_sessions(self):
        m = pipeline()
        m["agents"][1]["backend_options"] = {"pass_after": 3}
        m["agents"][0]["backend_options"] = {"delay": 0.3}
        cfg = make_cfg(self.tmp, [m])
        holder = {}

        def go():
            holder["res"] = run(cfg)

        t = threading.Thread(target=go)
        t.start()
        out = self.tmp / "out" / "m"
        deadline = time.time() + 10
        while time.time() < deadline:
            runs = list(out.glob("*/rounds/r002"))
            if runs:
                (runs[0].parent.parent / "STOP").write_text("x")
                break
            time.sleep(0.05)
        t.join(15)
        status, rec = holder["res"]
        self.assertEqual(status, "stopped")
        sid_before = rec.state["sessions"]["w1"]["session_id"]
        status2, rec2 = run(cfg, resume=True)
        self.assertEqual(status2, "completed")
        self.assertEqual(rec2.run_dir, rec.run_dir)
        later = [x for x in turns(rec2) if x["agent"] == "w1" and x["round"] >= 2 and x["status"] == "ok"]
        self.assertTrue(later and all(x["session_id"] == sid_before for x in later))
        self.assertIn("add input validation", later[-1]["input"])

    def test_inject_human_message_targeted(self):
        m = pipeline()
        m["agents"].insert(1, {"name": "w2", "role": "worker"})
        cfg = make_cfg(self.tmp, [m])
        cfg_obj = load_config(cfg)
        r = MonitorRunner(cfg_obj.monitor("m"), threading.Event(), log=lambda s: None)
        # pre-create the run so we can drop a message into the inbox before round 1
        from orch.recorder import Recorder
        rec = Recorder.create(cfg_obj.output_dir, "m", cfg_obj.monitor("m").snapshot())
        cli.main(["inject", str(rec.run_dir), "use tabs please", "--to", "w2"])
        rec.set(status="stopped")
        r.resume = True
        self.assertEqual(r.run(), "completed")
        ts = turns(r.rec)
        w2 = [t for t in ts if t["agent"] == "w2"][0]
        w1 = [t for t in ts if t["agent"] == "w1"][0]
        self.assertIn("use tabs please", w2["input"])
        self.assertNotIn("use tabs please", w1["input"])
        self.assertTrue(list((r.rec.run_dir / "inbox" / "consumed").iterdir()))


class TestLLMController(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def llm(self, ctrl_opts, **kw):
        m = {"name": "m", "workspace": "ws", "goal": "g", "max_rounds": 4,
             "controller": {"mode": "llm", "parallel": True, "agent": {"backend_options": ctrl_opts}},
             "agents": [{"name": "a", "role": "worker"}, {"name": "b", "role": "critic"}]}
        m.update(kw)
        return make_cfg(self.tmp, [m])

    def test_dispatch_and_done(self):
        status, rec = run(self.llm({"done_after": 3}))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertEqual(sum(t["agent"] == "controller" for t in ts), 3)
        self.assertEqual(sorted(t["agent"] for t in ts if t["round"] == 1), ["a", "b", "controller"])
        c2 = [t for t in ts if t["agent"] == "controller"][1]
        self.assertIn("results of your last dispatch", c2["input"])
        self.assertIn("## a — ok", c2["input"])
        self.assertEqual(rec.state["final_report"], "Goal met after 3 rounds.")

    def test_bad_json_repair(self):
        good = '```json\n{"dispatch": [], "done": true, "final_report": "ok"}\n```'
        status, rec = run(self.llm({"script": ["no json here", good]}))
        self.assertEqual(status, "completed")
        self.assertEqual([t["purpose"] for t in turns(rec)], ["control", "control-repair"])

    def test_unknown_agent_rejected(self):
        bad = '```json\n{"dispatch": [{"agent": "zed", "message": "x"}]}\n```'
        status, rec = run(self.llm({"script": [bad]}, max_rounds=1))
        self.assertEqual(status, "failed")
        self.assertIn("unknown agent", read_json(rec.run_dir / "rounds/r001/round.json")["error"])


class TestCommandBackendAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_checklist_gates_pass(self):
        from orch.controller import checklist_state
        self.assertEqual(checklist_state("- [x] a\n- [ ] b\n* [X] c\n1. [ ] d\nno box"), (2, 2))
        script = self.tmp / "critic.py"
        script.write_text(
            "import sys; p=sys.stdin.read()\n"
            "if 'verifier' not in sys.argv[1]: print('did work'); sys.exit()\n"
            "print('- [x] item one\\n- [x] item two' if 'checklist still has open' in p else '- [x] item one\\n- [ ] item two')\n"
            "print('VERDICT: PASS')\n")
        cmd = lambda tag: {"command": [sys.executable, str(script), tag]}
        m = pipeline()
        m["controller"] = {"mode": "pipeline", "checklist": True}
        m["agents"] = [{"name": "w", "role": "worker", "backend": "command", "backend_options": cmd("w")},
                       {"name": "v", "role": "verifier", "backend": "command", "backend_options": cmd("verifier")}]
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        st = read_json(rec.run_dir / "state.json")
        self.assertEqual(st["completed_round"], 2)  # round-1 PASS had an open item, so it counted as FAIL
        w2 = [t for t in turns(rec) if t["agent"] == "w"][1]
        self.assertNotIn("[ ]", w2["input"])  # the worker never sees the checklist
        html_ = (rec.run_dir / "report.html").read_text()
        self.assertIn("Reviewer checklist", html_)
        self.assertIn("overruled", html_)
        from orch.controller import strip_checklist
        self.assertEqual(strip_checklist("Fix A.\n\n**Checklist**\n- [x] a\n- [ ] b\n\nThanks."), "Fix A.\n\nThanks.")

    def test_command_backend_session_and_stdin(self):
        script = self.tmp / "agent.py"
        script.write_text("import sys; p=sys.stdin.read(); print('SID='+sys.argv[1]+' LEN='+str(len(p)))\n"
                          "print('VERDICT: PASS' if 'verifier' in sys.argv[2] else 'did work')\n")
        opts = {"command": [sys.executable, str(script), "{session_id}", "{agent}-verifier"]}
        m = pipeline()
        m["agents"] = [{"name": "w", "role": "worker", "backend": "command",
                        "backend_options": {"command": [sys.executable, str(script), "{session_id}", "w"]}},
                       {"name": "v", "role": "verifier", "backend": "command", "backend_options": opts}]
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        t = turns(rec)[0]
        self.assertIn("SID=" + t["session_id"], t["output"])
        self.assertIn("<system_instructions>", t["input"]) if t["system_prompt"] else None

    def test_validation_errors(self):
        bad = pipeline()
        bad["agents"].append({"name": "v2", "role": "verifier", "reviews": ["nobody"]})
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [bad]))
        bad = pipeline()
        bad["agents"][0]["colour"] = "red"
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [bad]))
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [pipeline(), pipeline()]))

    def test_text_files_and_defaults_merge(self):
        (self.tmp / "goal.md").write_text("from a file")
        m = pipeline()
        del m["goal"]
        m["goal_file"] = "goal.md"
        m["defaults"] = {"backend_options": {"b": 2}}
        cfg = load_config(make_cfg(self.tmp, [m], defaults={"backend": "mock", "backend_options": {"a": 1}}))
        mon = cfg.monitor("m")
        self.assertEqual(mon.goal, "from a file")
        self.assertEqual(mon.agents[0].backend_options, {"a": 1, "b": 2})

    def test_concurrent_monitors_via_cli(self):
        cfg = make_cfg(self.tmp, [pipeline("one"), pipeline("two")])
        rc = cli.main(["run", cfg])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list((self.tmp / "out").glob("*/*/state.json"))), 2)
        idx = (self.tmp / "out" / "index.html").read_text()
        self.assertIn("one", idx)
        self.assertIn("two", idx)


def managed(name="m", controller=None, mgr=None, critic=None, worker=None):
    c = {"mode": "managed", "checklist": True, "approve_plan": False, "approve_milestones": False, "tag_reminder": 0}
    c.update(controller or {})
    return {"name": name, "workspace": "ws", "goal": "Revise the plan.", "max_rounds": 12, "controller": c,
            "agents": [{"name": "boss", "role": "manager", "display_name": "Alex", "backend_options": mgr or {}},
                       {"name": "critic", "role": "verifier", "display_name": "Jordan", "backend_options": critic or {}},
                       {"name": "writer", "role": "worker", "task": "Edit the plan.", "display_name": "Sam",
                        "backend_options": worker or {}}]}


class TestManaged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_parsers(self):
        from orch.managed import parse_criteria, parse_milestones, parse_score
        ms = parse_milestones("Plan:\n\n1. **Scope** — the scope is fixed\n2. Tables: numbers checked\n   and sourced\n\n"
                              "Rubric\n1. something else")
        self.assertEqual([m["title"] for m in ms], ["Scope", "Tables"])
        self.assertEqual(ms[1]["done_when"], "numbers checked and sourced")
        self.assertEqual(parse_score("| a | 4 |\n\n<score>8/10</score>"), 4.0)
        self.assertEqual(parse_score("<score>4.5</score>"), 4.5)
        self.assertIsNone(parse_score("Overall score: 4/5"))  # only the tag counts
        self.assertEqual(parse_criteria("| criterion | score |\n|---|---|\n| clarity | 4/5 |\n| depth | **3** |\n| Overall | 3.5 |"),
                         [["clarity", 4.0], ["depth", 3.0]])

    def test_milestones_flow_and_worker_question_answered_by_critic(self):
        status, rec = run(make_cfg(self.tmp, [managed(worker={"replies": {"work": [
            "Changed it.\n\n<question>Should the headers be bold?\nA) yes\nB) no</question>", "Fixed the rest."]}})]))
        self.assertEqual(status, "completed")
        st = rec.state
        ms = st["managed"]["milestones"]
        self.assertEqual([m["status"] for m in ms], ["done", "done"])
        self.assertEqual(ms[0]["score"], 4.5)
        ts = turns(rec)
        purposes = [(t["agent"], t["purpose"]) for t in ts]
        self.assertEqual(purposes[:4], [("boss", "plan"), ("boss", "kickoff"), ("critic", "brief"), ("writer", "work")])
        self.assertIn(("critic", "report"), purposes)
        self.assertIn(("boss", "review"), purposes)
        q = st["questions"][0]
        self.assertEqual((q["from"], q["to"], q["status"], q["answered_by"]), ("writer", "critic", "answered", "critic"))
        for t in ts:  # the worker never sees the critic's checklist, the manager never talks to the worker
            if t["agent"] == "writer":
                self.assertNotIn("[ ]", t["input"])
                self.assertNotIn("Alex", t["input"])
        for t in ts:  # the critic never hears about milestones, the rubric or scores
            if t["agent"] == "critic":
                self.assertNotIn("milestone", t["input"].lower(), t["turn_id"])
                self.assertNotIn("score", t["input"].lower(), t["turn_id"])
        brief = next(t for t in ts if t["purpose"] == "brief")
        self.assertIn("Please turn this into concrete instructions for Sam", brief["input"])
        self.assertTrue(all(t.get("milestone") is not None for t in ts if t["purpose"] != "plan"))

    def test_revise_passes_only_the_part_for_the_critic(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1, "replies": {"review": [
            "| criterion | score |\n|---|---|\n| depth | 2 |\n\nMilestone 1 is not met.\n\n"
            "<message to=\"Jordan\">The comparison table still lacks the new baseline; add it.</message>\n\n"
            "<score>2</score>\n<decision>REVISE</decision>",
            "<score>5</score>\n<decision>ACCEPT</decision>"]}})]))
        self.assertEqual(status, "completed")
        g = next(t for t in turns(rec) if t["purpose"] == "guidance")
        self.assertIn("lacks the new baseline", g["input"])
        for word in ("Milestone", "score", "decision", "| depth"):
            self.assertNotIn(word, g["input"])

    def test_checkins_and_notes_up(self):
        status, rec = run(make_cfg(self.tmp, [managed(
            controller={"checkin_every": 1}, mgr={"milestones": 1},
            critic={"pass_after": 3, "replies": {"verify": [
                "Add the units.\n\n<message to=\"Alex\">The reviewers' tables use different seeds.</message>\n\n"
                "<checklist>\n- [ ] units\n</checklist>\n<verdict>FAIL</verdict>",
                "Nearly there.\n\n<checklist>- [ ] units</checklist>\n<verdict>FAIL</verdict>",
                "Done.\n\n<checklist>- [x] units</checklist>\n<verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        seq = [(t["agent"], t["purpose"]) for t in ts]
        self.assertEqual(seq.count(("critic", "checkin")), 2)  # after each failing review, not after the pass
        self.assertEqual(seq.count(("boss", "ack")), 2)
        i = seq.index(("critic", "checkin"))
        self.assertEqual(seq[i + 1], ("boss", "ack"))
        self.assertEqual(seq[i + 2], ("writer", "work"))
        ack = next(t for t in ts if t["purpose"] == "ack")
        self.assertIn("Jordan mentioned: The reviewers' tables use different seeds.", ack["input"])
        w2 = [t for t in ts if t["agent"] == "writer"][1]
        self.assertIn("Add the units.", w2["input"])
        self.assertNotIn("seeds", w2["input"])  # the note was for the manager only
        self.assertNotIn("report", [p for a, p in seq if a == "critic"][:3])

    def test_manager_side_question_in_a_review_has_explicit_reply_routing(self):
        request = "Please explain the ledger columns to me. Reply directly; no new experiments."
        answer = "The ledger counts attempts, including retries."
        status, rec = run(make_cfg(self.tmp, [managed(
            controller={"checkin_every": 1, "tag_reminder": 0},
            mgr={"milestones": 1, "replies": {"ack": [
                f'<message to="Jordan">{request}</message>', "Thanks."]}},
            critic={"replies": {"verify": [
                "Fix the labels.\n<checklist>- [ ] labels</checklist><verdict>FAIL</verdict>",
                f'<message to="you">{answer}</message>\nFinish checking the labels.\n'
                '<checklist>- [ ] labels</checklist><verdict>FAIL</verdict>',
                "Done.\n<checklist>- [x] labels</checklist><verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        reviews = [t for t in ts if t["purpose"] == "verify"]
        self.assertIn(request, reviews[1]["input"])
        self.assertTrue(reviews[1]["resumed_session"])
        self.assertIn('complete answer inside `<message to="you">', reviews[1]["input"])
        self.assertIn("If I asked you a question", reviews[1]["input"])
        self.assertNotRegex(reviews[1]["input"], r"(?i)\b(manager|user|human|Alex)\b")
        self.assertFalse(reviews[1].get("tag_reminder"))  # routing does not depend on random reminders
        ack = [t for t in ts if t["purpose"] == "ack"][1]
        self.assertIn(answer, ack["input"])
        worker = [t for t in ts if t["purpose"] == "work"][2]
        self.assertIn("Finish checking the labels.", worker["input"])
        self.assertNotIn(answer, worker["input"])
        for t in ts:
            if t["purpose"] in ("report", "checkin") or t["role"] != "verifier":
                self.assertNotIn("complete answer inside", t["input"])

    def test_reply_routing_reminder_respects_protocol_opt_out(self):
        status, rec = run(make_cfg(self.tmp, [managed(controller={"protocol": False}, mgr={"milestones": 1})]))
        self.assertEqual(status, "completed")
        for t in turns(rec):
            self.assertNotIn("complete answer inside", t["input"])
            self.assertNotIn('speak as "I"', t["input"])

    def test_worker_timeout_is_not_fatal(self):
        m = managed(mgr={"milestones": 1})
        m["agents"][2].update(backend="command", timeout=0.5, retries=2, backend_options={
            "command": [sys.executable, "-c", "import sys,time; p=sys.stdin.read(); time.sleep(5) if 'brief' not in p and 'ran past the time limit' not in p else 0; print('done')"]})
        status, rec = run(make_cfg(self.tmp, [m]))
        ts = turns(rec)
        w = [t for t in ts if t["agent"] == "writer"]
        self.assertEqual(w[0]["status"], "timeout")
        self.assertEqual(len(w[0]["attempts"]), 1)  # not re-run
        self.assertIn("ran past the time limit", w[1]["input"])
        self.assertEqual(status, "completed")

    def test_question_chain_up_to_me_does_not_block(self):
        cfg = make_cfg(self.tmp, [managed(
            mgr={"milestones": 1, "replies": {"answer": ["<question>Do we cite the new run?\nA) yes\nB) no</question>\n\n"
                                                         "<message to=\"Jordan\">Assume yes for now.</message>"]}},
            critic={"replies": {"verify": ["<question>Do we cite the new run in the main text?</question>\n\n"
                                           "<checklist>- [ ] cited</checklist>\n<verdict>FAIL</verdict>",
                                           "Good.\n\n<checklist>- [x] cited</checklist>\n<verdict>PASS</verdict>"]}})])
        status, rec = run(cfg)
        self.assertEqual(status, "completed")  # finished although my answer never came
        qs = rec.state["questions"]
        self.assertEqual([(q["from"], q["to"], q["status"]) for q in qs],
                         [("critic", "boss", "passed_up"), ("boss", "human", "open")])
        amend = next(t for t in turns(rec) if t["purpose"] == "amend")
        self.assertIn("Assume yes for now.", amend["input"])
        self.assertNotIn("<question>", amend["input"])

    def test_human_answer_is_relayed_to_manager_and_critic(self):
        from orch.managed import ManagedRunner
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        r = ManagedRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        from orch.recorder import Recorder
        r.rec = Recorder.create(str(self.tmp / "out"), "m", cfg.monitor("m").snapshot())
        q = r.new_question(r.manager, "human", "QUESTIONS FOR YOU:\nWhich model?\nA) Gemini\nB) Ring", "t0")
        r.control({"kind": "answer", "qid": "q1", "message": "a"})
        r.check_control()
        self.assertEqual((q["status"], q["answered_by"], q["answer"]), ("answered", "human", "A) Gemini"))
        self.assertIn('My answer about "Which model?": A) Gemini', r.st["fwd"]["critic"][0])
        self.assertIn("About your question (Which model?)", r.st["fwd"]["boss"][0])

    def test_gates_approve_and_send_back(self):
        cfg = make_cfg(self.tmp, [managed(controller={"approve_plan": True, "approve_milestones": True},
                                          mgr={"milestones": 1})])
        from orch.config import load_config as lc
        mon = lc(cfg).monitor("m")
        out = self.tmp / "out" / "m"
        stop = threading.Event()
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg, stop=stop)), daemon=True)
        th.start()

        def wait_gate(kind, timeout=10, not_key=None):
            t0 = time.time()
            while time.time() - t0 < timeout:
                runs = sorted(out.glob("*/state.json")) if out.exists() else []
                st = read_json(runs[-1]) if runs else None
                g = ((st or {}).get("managed") or {}).get("gate")
                if g and g["kind"] == kind and g["key"] != not_key:
                    return runs[-1].parent, g
                time.sleep(0.05)
            self.fail(f"no {kind} gate")

        run_dir, g = wait_gate("plan")
        (run_dir / "inbox" / "a-gate.json").write_text(json.dumps({"kind": "gate", "decision": "revise",
                                                                  "message": "Fewer milestones please.", "gate": g["key"]}))
        run_dir, g2 = wait_gate("plan", not_key=g["key"])
        self.assertNotEqual(g["key"], g2["key"])
        self.assertEqual(cli.main(["approve", str(run_dir)]), 0)
        run_dir, g3 = wait_gate("milestone")
        self.assertEqual(cli.main(["approve", str(run_dir), "--note", "nice"]), 0)
        th.join(10)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        plans = [t for t in turns(rec) if t["purpose"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertIn("Fewer milestones please.", plans[1]["input"])
        self.assertEqual([x["decision"] for x in rec.state["managed"]["gates"]], ["revise", "approve", "approve"])

    def test_board_data_and_server(self):
        status, rec = run(make_cfg(self.tmp, [managed()]))
        from orch.board import Board, serve
        import urllib.request
        cfg = load_config(str(self.tmp / "cfg.json"))
        b = Board("m", lambda: rec.run_dir, cfg.monitor("m"))
        d = b.data()
        self.assertEqual(d["status"], "completed")
        self.assertTrue(d["series"] and d["reviews"] and d["can_chat"])
        srv, url = serve({"m": b}, port=8890)
        try:
            page = urllib.request.urlopen(url).read().decode()
            self.assertIn("Talk to the manager", page)
            # Relative URLs must stay under a reverse proxy's external prefix.
            from urllib.parse import urljoin
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", 8890)
            conn.request("GET", "/log/run?m=m")
            response = conn.getresponse()
            self.assertEqual(response.status, 302)
            location = response.getheader("Location")
            self.assertEqual(
                urljoin("https://example.test/proxy/8765/log/run?m=m", location),
                f"https://example.test/proxy/8765/log/m/{rec.run_dir.name}/report.html",
            )
            response.read()
            conn.close()
            st = json.loads(urllib.request.urlopen(url + "api/state").read())
            self.assertEqual(st["run_id"], rec.run_dir.name)
            req = urllib.request.Request(url + "api/chat", data=json.dumps({"message": "How is it going?"}).encode(),
                                         headers={"Content-Type": "application/json"})
            self.assertIn("id", json.loads(urllib.request.urlopen(req).read()))
            for _ in range(100):
                chat = [json.loads(x) for x in (rec.run_dir / "chat.jsonl").read_text().splitlines()]
                if any(c.get("status") == "ok" for c in chat):
                    break
                time.sleep(0.05)
            self.assertEqual(chat[-1]["text"], "Things are on track; nothing needs you right now.")
            req = urllib.request.Request(url + "api/answer", data=json.dumps({"qid": "Q9", "message": "x"}).encode())
            urllib.request.urlopen(req).read()
            self.assertTrue(any(json.loads(p.read_text()).get("kind") == "answer" for p in (rec.run_dir / "inbox").glob("*.json")))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_question_settled_in_conversation(self):
        from orch.board import Board
        from orch.managed import ManagedRunner
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        cfg = load_config(str(self.tmp / "cfg.json"))
        r = ManagedRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        r.rec = rec
        q = r.new_question(r.manager, "human", "QUESTIONS FOR YOU:\nWhere do the placement tests go?", "t0")
        rec.save()
        b = Board("m", lambda: rec.run_dir, cfg.monitor("m"))

        def wait_reply(n):
            for _ in range(200):
                rows = [json.loads(x) for x in (rec.run_dir / "chat.jsonl").read_text().splitlines()]
                if sum(1 for c in rows if c.get("qid") and c["role"] == "manager" and c.get("status") == "ok") >= n:
                    return rows
                time.sleep(0.05)
            self.fail("no reply")
        self.assertIn("id", b.chat("Which table do you mean?", qid="Q1"))
        rows = wait_reply(1)
        self.assertEqual(rows[0]["text"], "Which table do you mean?")  # I see my words; the manager gets the context
        self.assertIn("id", b.chat("", qid="Q1", settle=True))
        wait_reply(2)
        self.assertIn("error", b.chat("again", qid="Q1", settle=True))  # one settle per question
        r.check_control()
        self.assertEqual((q["status"], q["answered_by"], q.get("via")), ("answered", "human", "conversation"))
        self.assertIn("Here is what I decided", r.st["fwd"]["critic"][0])
        self.assertNotIn("boss", r.st.get("fwd") or {})  # the manager was in the conversation

    def test_settle_with_my_answer_sends_it_to_the_manager(self):
        from unittest import mock
        from orch import board as B
        from orch.backends import TurnResult
        from orch.managed import ManagedRunner
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        cfg = load_config(str(self.tmp / "cfg.json"))
        r = ManagedRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        r.rec = rec
        r.new_question(r.manager, "human", "Real labels or test exports? A) Real B) Test C) Ask the authors", "t0")
        rec.save()
        seen = []

        class Fake:
            def run(self, req, stop):
                seen.append(req.prompt)
                return TurnResult("ok", "Count them as real labels.", req.session_id or "s1")
        b = B.Board("m", lambda: rec.run_dir, cfg.monitor("m"))
        with mock.patch.object(B, "get_backend", lambda name: Fake()):
            b.chat("These are the real labels.", qid="Q1", settle=True)
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.05)
        self.assertIn("These are the real labels.", seen[0])
        self.assertIn("not by your earlier recommendation", seen[0])
        self.assertIn('speak as "I" and address them as "you"', seen[0])

    def test_claude_can_skip_my_hooks(self):
        from orch.backends import ClaudeBackend, TurnRequest
        req = lambda o: TurnRequest("a", "worker", "hi", "", None, ".", 10, o)
        cmd, _ = ClaudeBackend().build_command(req({"disable_hooks": True}))
        self.assertIn('{"disableAllHooks": true}', cmd)
        cmd, _ = ClaudeBackend().build_command(req({}))
        self.assertNotIn("--settings", cmd)

    def _urgent_run(self, note: dict, worker_opts: dict, critic_opts=None):
        """Run a one-milestone project; drop `note` (urgent) into the inbox while the writer's first turn runs."""
        from unittest import mock
        from orch.backends import MockBackend
        sent, orig = [], MockBackend.run

        def spy(self_, req, stop):
            sent.append((req.agent, req.session_id, req.prompt))
            return orig(self_, req, stop)

        def drop():
            for _ in range(400):
                for t in (self.tmp / "out").glob("**/rounds/r*/t*-writer.json"):
                    if (read_json(t) or {}).get("status") == "running":
                        time.sleep(0.3)
                        inbox = t.parents[2] / "inbox"
                        (inbox / "20990101-000000-urgent.json").write_text(json.dumps({**note, "urgent": True}))
                        return
                time.sleep(0.05)
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        with mock.patch.object(MockBackend, "run", spy):
            status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1}, worker=worker_opts,
                                                        critic=critic_opts)]))
        th.join(timeout=5)
        return status, rec, sent

    def test_a_system_process_whose_directory_cant_be_read_is_not_a_job(self):
        import os
        from unittest import mock
        from orch import jobs
        ws, here = os.path.realpath(self.tmp), os.getcwd()
        before = {1: (0, "launchd")}
        after = {1: (0, "launchd"), 900: (1, "/System/Library/nbstated"), 901: (1, "python run.py")}
        os.chdir(ws)  # orch runs from the project folder; 900's directory can't be read (a root daemon)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {901: ws}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual([j["pid"] for j in found], [901])

    AUDIT_REPLY = ("<points>\n1. The critic resends its whole checklist. Evidence: r001. Fix: send the change only.\n"
                   "2. Second point.\n3. Third point.\n</points>\n<findings>\n"
                   "- [repetition] the checklist is resent every round · seen 1× since 2026-11-09 · open\n</findings>")

    def _audited(self, auditor_opts, controller=None):
        m = managed(controller={"audit_every": 1, "audit_points": 2, **(controller or {})}, mgr={"milestones": 1})
        m["agents"].append({"name": "auditor", "role": "auditor", "backend_options": auditor_opts})
        return run(make_cfg(self.tmp, [m]))

    def test_the_auditor_reads_the_conversation_every_n_rounds_and_keeps_its_findings(self):
        from orch import audit
        status, rec = self._audited({"replies": {"audit": [self.AUDIT_REPLY]}})
        self.assertEqual(status, "completed")
        ts = turns(rec)
        last = max(int(t["round"]) for t in ts)
        self.assertFalse(any(t["agent"] == "auditor" for t in ts))  # outside the team: not a turn of the run
        self.assertFalse(any("resends its whole checklist" in (t["input"] or "") for t in ts))  # never reaches them
        self.assertEqual(rec.state["managed"]["audit"]["through"], last)
        evs = [json.loads(l) for l in (rec.run_dir / "events.jsonl").read_text().splitlines()]
        audits = [e for e in evs if e["event"] == "audit"]
        self.assertEqual([e["rounds"] for e in audits], [[r, r] for r in range(1, last + 1)])  # every round, then none
        f = rec.run_dir.parent / "audit-auditor.md"
        text = f.read_text()
        self.assertIn("- [repetition] the checklist is resent every round", text)
        self.assertEqual(text.count("- [repetition]"), 1)  # the list is replaced, not appended
        self.assertEqual(text.count("### "), last)
        self.assertIn("2. Second point.", text)
        self.assertNotIn("3. Third point.", text)  # at most audit_points
        sent = sorted((rec.run_dir / "audits").glob("*.md"))
        self.assertEqual(len(sent), last)
        self.assertIn("## System prompts", sent[0].read_text())
        if last > 1:  # the next audit gets the findings so far, and the prompts only if they changed
            self.assertIn("the checklist is resent every round", sent[1].read_text())
            self.assertIn("**writer**", sent[0].read_text())
            self.assertNotIn("**writer**", sent[1].read_text())  # shown once; again only when it changes
        self.assertEqual(audit.findings_lines(f), ["[repetition] the checklist is resent every round · seen 1× since "
                                                   "2026-11-09 · open"])

    def test_a_failed_audit_does_not_stop_the_run(self):
        status, rec = self._audited({"fail_turns": [1]})
        self.assertEqual(status, "completed")
        self.assertIn("did not finish", (rec.run_dir / "progress.md").read_text())
        self.assertFalse((rec.run_dir.parent / "audit-auditor.md").exists())

    def test_the_digest_traces_where_each_part_of_a_message_came_from(self):
        from orch import audit
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        last = max(int(t["round"]) for t in turns(rec))
        md, st = audit.build(rec.run_dir, 1, last)
        work = [t for t in turns(rec) if t["agent"] == "writer" and int(t["round"]) >= 1]
        for t in work:
            self.assertIn(f"| {t['turn_id']} | work |", md)
        crit = [t for t in turns(rec) if t["agent"] == "critic" and t.get("output")]
        self.assertIn("worker", st["origin"].get("critic", {}))  # the worker's report reached the critic
        self.assertIn("## System prompts", md)
        md2, _ = audit.build(rec.run_dir, 1, last, prompts_seen=st["prompts"])
        self.assertNotIn("## System prompts", md2)  # unchanged since the last audit

    def test_the_digest_flags_a_resent_message_and_a_note_that_waited(self):
        from orch import audit
        from datetime import datetime
        # Inbox filenames use local time; event timestamps must describe that same timezone.
        def local_time(value):
            return datetime.fromisoformat(value).astimezone().isoformat()
        rd = self.tmp / "run"
        (rd / "rounds" / "r001").mkdir(parents=True)
        (rd / "inbox" / "consumed").mkdir(parents=True)
        big = "\n\n".join(f"Paragraph {i}: " + "the same long instruction, sent again " * 3 for i in range(60))
        for seq, (agent, role, inp, out, t0) in enumerate([
                ("w", "worker", big, "Done the first part, details follow." * 20, local_time("2026-11-09T10:00:00")),
                ("w", "worker", big, "ok", local_time("2026-11-09T10:20:00"))], 1):
            (rd / "rounds" / "r001" / f"t{seq:02d}-{agent}.json").write_text(json.dumps({
                "turn_id": f"r001-t{seq:02d}-{agent}", "round": 1, "agent": agent, "role": role, "purpose": "work",
                "status": "ok", "started_at": t0, "ended_at": t0.replace(":00:00", ":05:00").replace(":20:00", ":25:00"),
                "input": inp, "output": out, "system_prompt": "You are the worker."}))
        (rd / "inbox" / "consumed" / "20261109-090000-1-board-note.json").write_text(
            json.dumps({"to": ["w"], "message": "Please stop the sweep."}))
        (rd / "events.jsonl").write_text(json.dumps({"ts": local_time("2026-11-09T10:20:00"), "event": "human_messages",
                                                     "files": ["20261109-090000-1-board-note.json"]}) + "\n")
        md, st = audit.build(rd, 1, 1)
        kinds = {f["kind"] for f in st["flags"]}
        self.assertTrue({"repeated content", "short or failed reply", "note waited"} <= kinds, kinds)
        self.assertIn("waited 80 min", md)

    def test_a_wait_on_a_process_outside_the_workspace_is_dropped(self):
        # a wait saved by an older orch: pid 1 (a root daemon, its directory can't be read) was taken for a job
        from unittest import mock
        from orch import managed as M
        cmd = subprocess.run(["ps", "-p", "1", "-o", "command="], capture_output=True, text=True).stdout.strip()
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1})])
        holder, calls = {}, []
        def fake_detect(*a, **k):
            calls.append(1)
            return [{"pid": 1, "command": cmd}] if len(calls) == 1 else []
        with mock.patch.object(M._jobs, "detect", fake_detect):
            th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
            th.start()
            th.join(20)
        self.assertIn("res", holder, "still waiting on pid 1")
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        self.assertIn("none of the processes I was waiting for", (rec.run_dir / "progress.md").read_text())

    def test_an_urgent_message_cuts_into_the_running_turn_of_its_agent(self):
        status, rec, sent = self._urgent_run({"to": ["writer"], "message": "Stop checking provenance."},
                                             {"delay": 3, "replies": {"work": ["Done, without the provenance checks."]}})
        self.assertEqual(status, "completed")
        w = [t for t in turns(rec) if t["agent"] == "writer"]
        first = w[0]
        self.assertEqual(first["status"], "ok")
        self.assertIn("Stop checking provenance.", first["interruptions"][0]["message"])
        self.assertEqual(first["output"], "Done, without the provenance checks.")
        calls = [x for x in sent if x[0] == "writer"]
        self.assertTrue(calls[1][1] and calls[1][1] == first["session_id"])  # carried on in the same session
        self.assertIn("I stopped you mid-turn", calls[1][2])
        self.assertIn("Stop checking provenance.", calls[1][2])
        self.assertNotIn("I stopped you mid-turn", calls[0][2])
        for t in w[1:]:  # delivered once, not again at the next turn
            self.assertNotIn("Stop checking provenance.", t["input"])
        self.assertFalse((rec.state.get("fwd") or {}).get("writer"))
        self.assertEqual(list((rec.run_dir / "inbox").glob("*.json")), [])

    def test_an_urgent_critic_checkin_leaves_the_worker_running(self):
        question = "How is the inventory check going?"
        answer = "You can see the first results; the remaining checks are pending."
        status, rec, sent = self._urgent_run({"to": ["critic"], "from": "boss", "message": question},
                                             {"delay": 3}, {"replies": {"checkin": [
                                                 f'<message to="you">{answer}</message>']}})
        self.assertEqual(status, "completed")
        ts = turns(rec)
        workers = [t for t in ts if t["agent"] == "writer"]
        checkin = next(t for t in ts if t["purpose"] == "checkin")
        ack = next(t for t in ts if t["purpose"] == "ack")
        w = workers[0]
        self.assertEqual(w["status"], "ok")
        self.assertFalse(w.get("cut_short") or w.get("interruptions"))
        self.assertGreaterEqual(w["duration_s"], 3)
        self.assertLessEqual(w["started_at"], checkin["started_at"])
        self.assertLess(ack["ended_at"], w["ended_at"])
        self.assertIn(question, checkin["input"])
        self.assertIn(answer, ack["input"])
        self.assertEqual(ack["input"].count(answer), 1)
        self.assertNotIn("manager", checkin["input"].lower())
        self.assertEqual(sum(question in t["input"] for t in ts), 1)
        self.assertEqual(len([x for x in sent if x[0] == "writer"]), len(workers))
        for t in workers:
            self.assertNotIn(question, t["input"])
            self.assertNotIn(answer, t["input"])
            self.assertEqual(t["session_id"], w["session_id"])
        review = next(t for t in ts if t["purpose"] == "verify")
        self.assertLessEqual(w["ended_at"], review["started_at"])
        self.assertEqual(list((rec.run_dir / "inbox").glob("*.json")), [])

    def test_a_failed_urgent_checkin_does_not_interrupt_the_worker(self):
        status, rec, sent = self._urgent_run({"to": ["critic"], "message": "How is it going?"},
                                             {"delay": 2}, {"replies": {"checkin": [""]}})
        self.assertEqual(status, "completed")
        workers = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertEqual(len([x for x in sent if x[0] == "writer"]), len(workers))
        self.assertTrue(all(t["status"] == "ok" and not t.get("cut_short") for t in workers))
        self.assertIn("Urgent check-in did not finish", (rec.run_dir / "progress.md").read_text())

    def _critic_codex_update(self, mode="steer"):
        from test_codex_stream import PEER
        peer = self.tmp / "peer"
        peer.write_text("#!" + sys.executable + "\n" + PEER)
        peer.chmod(0o700)
        log = self.tmp / "peer.jsonl"
        instruction = "Keep the current experiment running; also evaluate the missing independent cases."
        private = "You can track the results in the report."
        m = managed(mgr={"milestones": 1}, critic={"pass_after": 1, "replies": {"checkin": [
            f'<message to="you">{private}</message>\n<message to="writer">{instruction}</message>']}})
        m["agents"][2].update(backend="codex", timeout=8, retries=0,
            backend_options={"transport": "app-server", "executable": str(peer)},
            env={"PEER_MODE": mode, "PEER_LOG": str(log)})
        stop_drop = threading.Event()
        def drop():
            while not stop_drop.wait(0.02):
                if log.exists() and '"turn/start"' in log.read_text():
                    for p in (self.tmp / "out").glob("**/inbox"):
                        (p / "urgent.json").write_text(json.dumps({"to": ["critic"], "from": "boss",
                            "urgent": True, "message": "I want independent cases run concurrently."}))
                        return
        th = threading.Thread(target=drop, daemon=True)
        th.start()
        try:
            status, rec = run(make_cfg(self.tmp, [m]))
        finally:
            stop_drop.set()
            th.join(2)
        self.assertEqual(status, "completed")
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(Path(str(log) + '.tool').read_text(), 'finished naturally')
        return rec, calls, instruction, private

    def test_critic_update_reaches_codex_while_its_tool_is_running(self):
        from orch.board import Board
        rec, calls, instruction, private = self._critic_codex_update()
        steers = [c for c in calls if c.get("method") == "turn/steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["params"]["input"][0]["text"], instruction)
        self.assertFalse(any(c.get("method") == "turn/interrupt" for c in calls))
        self.assertEqual(sum(c.get("method") == "turn/start" for c in calls), 1)
        ts = turns(rec)
        w = next(t for t in ts if t["agent"] == "writer")
        self.assertEqual(w["status"], "ok")
        self.assertFalse(w.get("cut_short") or w.get("interruptions"))
        data = Board("m", lambda: rec.run_dir).data()
        note, = data["worker_messages"]
        checkin = next(t for t in ts if t["purpose"] == "checkin")
        self.assertEqual((note["status"], note["via"], note["delivery_turn"], note["source_turn"]),
                         ("accepted", "steer", w["turn_id"], checkin["turn_id"]))
        ack = next(t for t in ts if t["purpose"] == "ack")
        self.assertIn(private, ack["input"])
        self.assertNotIn(private, steers[0]["params"]["input"][0]["text"])

    def test_rejected_live_input_gets_a_followup_before_review(self):
        rec, calls, instruction, private = self._critic_codex_update("steer-race")
        workers = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertEqual(len(workers), 2)
        self.assertNotIn(instruction, workers[0]["input"])
        self.assertIn(instruction, workers[1]["input"])
        self.assertNotIn(private, workers[1]["input"])
        self.assertEqual(workers[0]["session_id"], workers[1]["session_id"])
        review = next(t for t in turns(rec) if t["purpose"] == "verify")
        self.assertLessEqual(workers[1]["ended_at"], review["started_at"])
        self.assertEqual(sum(c.get("method") == "turn/steer" for c in calls), 1)
        self.assertFalse(any(c.get("method") == "turn/interrupt" for c in calls))
        note, = rec.state["worker_messages"]
        self.assertEqual((note["status"], note["via"], note["delivery_turn"]),
                         ("accepted", "prompt", workers[1]["turn_id"]))

    def test_critic_update_waits_for_next_turn_on_other_backends(self):
        instruction = "Keep the existing result files and add the missing independent case."
        status, rec, sent = self._urgent_run({"to": ["critic"], "message": "Add the missing case."},
            {"delay": 2}, {"replies": {"checkin": [
                f'<message to="Sam">{instruction}</message><message to="you">I queued the change.</message>']}})
        self.assertEqual(status, "completed")
        workers = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertNotIn(instruction, workers[0]["input"])
        self.assertIn(instruction, workers[1]["input"])
        self.assertEqual(sum(instruction in t["input"] for t in workers), 1)
        self.assertTrue(all(not t.get("interruptions") for t in workers))
        note, = rec.state["worker_messages"]
        self.assertEqual((note["status"], note["via"]), ("accepted", "prompt"))
        self.assertEqual(note["delivery_turn"], workers[1]["turn_id"])

    def test_worker_message_queue_survives_restart_without_replaying_uncertain_sends(self):
        from orch.recorder import Recorder
        mon = load_config(make_cfg(self.tmp, [managed()])).monitor("m")
        rec = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        a = rec.queue_worker_message("writer", "Keep working.", "r001-t02-critic", "critic")
        rec.queue_worker_message("writer", "Keep working.", "r001-t02-critic", "critic")
        self.assertEqual(len(rec.pending_worker_messages("writer")), 1)
        rec.worker_message_status(a["id"], "sending", delivery_turn="r001-t01-writer")
        b = rec.queue_worker_message("writer", "Add a case.", "r001-t03-critic", "critic")
        rec.worker_message_status(b["id"], "deferred", delivery_turn="r001-t01-writer")
        self.assertEqual(rec.pending_worker_messages("writer", "r001-t01-writer"), [])
        restarted = Recorder(rec.run_dir, rec.output_root)
        restarted.recover_worker_messages()
        self.assertEqual(restarted.state["worker_messages"][0]["status"], "unknown")
        self.assertEqual([m["id"] for m in restarted.pending_worker_messages("writer")], [b["id"]])
        restarted.worker_message_status(b["id"], "accepted")
        self.assertEqual(restarted.pending_worker_messages("writer"), [])

    def test_the_board_shows_when_a_message_from_the_chat_was_delivered(self):
        from orch.board import _relays
        rd = self.tmp / "run"
        (rd / "inbox" / "consumed").mkdir(parents=True)
        (rd / "inbox" / "consumed" / "a-board-note.json").write_text(json.dumps({"to": ["critic"], "message": "Stop.", "from": "boss"}))
        (rd / "inbox" / "b-board-note.json").write_text(json.dumps({"to": ["critic"], "message": "Later.", "from": "boss", "urgent": True}))
        (rd / "events.jsonl").write_text(json.dumps({"ts": "2026-01-01T10:05:00", "event": "human_messages", "files": ["a-board-note.json"]}) + "\n")
        chat = [{"id": "c1", "role": "manager", "status": "ok", "text": 'OK.\n<message to="Jordan">Stop.</message>'},
                {"id": "c2", "role": "manager", "status": "ok", "text": '<message to="Jordan" urgent="yes">Later.</message>'}]
        turns_ = [{"turn_id": "r1-t1-critic", "agent": "critic", "started_at": "2026-01-01T10:00:00", "purpose": "brief"},
                  {"turn_id": "r1-t3-critic", "agent": "critic", "started_at": "2026-01-01T10:05:00", "purpose": "verify", "status": "ok"}]
        r = _relays(rd, chat, turns_)
        self.assertEqual((r["c1"][0]["status"], r["c1"][0]["turn"]), ("delivered", "r1-t3-critic"))
        self.assertEqual((r["c2"][0]["status"], r["c2"][0]["urgent"]), ("waiting", True))

    def test_the_manager_can_send_an_urgent_message_from_the_chat(self):
        from orch import tags as T
        a, body = T.blocks('<message to="Jordan" urgent="yes">Stop now.</message>', "message")[0]
        self.assertTrue(T.urgent(a))
        self.assertEqual(body, "Stop now.")
        self.assertFalse(T.urgent(T.blocks('<message to="Jordan">x</message>', "message")[0][0]))

    def test_resume_continues_an_interrupted_worker_turn(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder, live_event
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        # fake a crash in the middle of the worker's first turn of a fresh run
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        w = next(t for t in turns(rec) if t["agent"] == "writer")
        st["managed"].update(phase="work", ms=0, ms_round=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st["managed"].pop("in_work", None)
        st.update(round=1, status="running", sessions={k: v for k, v in st["sessions"].items() if k != "writer"})
        rec2.state.update(st)
        rec2.save()
        t = dict(w, status="running", session_id=None, output="", round=1, seq=1, turn_id="r001-t01-writer")
        rec2.write_turn(t)
        live_event(rec2.run_dir, t["turn_id"], {"k": "session", "s": "mock-abc"})
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        self.assertEqual(r.run(), "completed")
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        old = read_json(rec2.run_dir / "rounds" / "r001" / "t01-writer.json")
        self.assertEqual((old["status"], old["session_id"]), ("interrupted", "mock-abc"))
        redo = next(x for x in turns(rec3) if x["agent"] == "writer" and x["turn_id"] != "r001-t01-writer")
        self.assertEqual((redo["round"], redo["session_id"]), (1, "mock-abc"))  # same round, same session
        self.assertIn("cut off when the run was restarted", redo["input"])
        self.assertIn("Make the tables.", redo["input"])
        self.assertEqual(rec3.state["managed"]["milestones"][0]["rounds"], 2)  # as without the crash, not 3
        self.assertEqual(sorted({x["round"] for x in turns(rec3) if x["agent"] == "writer"}), [1, 2])

    def test_a_reset_after_a_cut_off_turn_is_not_undone_by_resume(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder, live_event
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        w = next(t for t in turns(rec) if t["agent"] == "writer")
        st["managed"].update(phase="work", ms=0, ms_round=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st["managed"].pop("in_work", None)
        st["sessions"]["writer"] = {"session_id": None, "turns": 0, "reset_at": "2026-01-02T00:00:00+00:00"}
        st.update(round=1, status="running")
        rec2.state.update(st)
        rec2.save()
        t = dict(w, status="stopped", session_id="mock-old", output="", round=1, seq=1,
                 turn_id="r001-t01-writer", started_at="2026-01-01T00:00:00+00:00")
        rec2.write_turn(t)
        live_event(rec2.run_dir, t["turn_id"], {"k": "session", "s": "mock-old"})
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        self.assertEqual(r.run(), "completed")
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        redo = next(x for x in turns(rec3) if x["agent"] == "writer" and x["turn_id"] != "r001-t01-writer")
        self.assertNotEqual(redo["session_id"], "mock-old")  # the reset stands: a new session
        self.assertNotIn("cut off when the run was restarted", redo["input"])

    def test_my_note_survives_a_restart_mid_turn(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        st["managed"].update(phase="work", ms=0, ms_round=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st["managed"].pop("in_work", None)
        st.update(round=0, status="running")
        rec2.state.update(st)
        rec2.save()
        (rec2.run_dir / "inbox").mkdir(exist_ok=True)
        (rec2.run_dir / "inbox" / "1-note.json").write_text(json.dumps({"to": ["writer"], "message": "Use British spelling."}))
        import orch.controller as ctl
        from orch.backends import TurnResult
        real = ctl.get_backend
        class Cut:  # the writer's turn is cut off by a restart while it works
            def run(self, req, should_stop):
                stop.set()
                return TurnResult("stopped", "", None, -1, "", [], {})
        stop = threading.Event()
        ctl.get_backend = lambda name: Cut()
        try:
            ManagedRunner(mon, stop, resume=True, log=lambda s: None).run()
        finally:
            ctl.get_backend = real
        rec2 = Recorder(rec2.run_dir, rec2.output_root)
        self.assertIn("Use British spelling.", "\n".join((rec2.state.get("fwd") or {}).get("writer") or []))
        r2 = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r2.run()
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        w = [x for x in turns(rec3) if x["agent"] == "writer" and x["status"] == "ok"]
        self.assertIn("Use British spelling.", w[0]["input"])

    def test_resume_redoes_a_cut_off_round_even_at_the_round_limit(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        w = next(t for t in turns(rec) if t["agent"] == "writer")
        lim = mon.controller.milestone_rounds
        st["managed"].update(phase="work", ms=0, ms_round=lim, in_work=lim, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st.update(round=lim, status="running")
        rec2.state.update(st)
        rec2.save()
        sid = st["sessions"]["writer"]["session_id"]
        rec2.write_turn(dict(w, status="running", session_id=sid, output="", round=lim, seq=1, turn_id=f"r{lim:03d}-t01-writer"))
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.run()
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        ts = turns(rec3)  # the redo is the first turn, before the limit hands the milestone to the manager
        self.assertEqual((ts[0]["agent"], ts[0]["purpose"], ts[0]["round"], ts[0]["status"]), ("writer", "work", lim, "ok"))
        self.assertIn("Make the tables.", ts[0]["input"])
        self.assertLess([t["purpose"] for t in ts].index("verify"), [t["purpose"] for t in ts].index("report"))

    def test_resume_after_backend_change_starts_a_new_session(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        w = next(t for t in turns(rec) if t["agent"] == "writer")
        st["managed"].update(phase="work", ms=0, ms_round=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st["managed"].pop("in_work", None)
        st["sessions"]["writer"] = {"session_id": "claude-xyz", "turns": 3, "backend": "claude"}
        st.update(round=1, status="running")
        rec2.state.update(st)
        rec2.save()
        rec2.write_turn(dict(w, status="running", session_id="claude-xyz", backend="claude", output="", round=1,
                             seq=1, turn_id="r001-t01-writer"))
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        self.assertEqual(r.run(), "completed")
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        redo = next(x for x in turns(rec3) if x["agent"] == "writer" and x["turn_id"] != "r001-t01-writer")
        self.assertFalse(redo["resumed_session"])  # the Claude session is not handed to the new backend
        self.assertIn("This is a fresh conversation", redo["input"])  # its handover notes, as after a reset
        self.assertIn("Make the tables.", redo["input"])
        self.assertNotIn("cut off when the run was restarted", redo["input"])
        prev = rec3.state["sessions"]["writer"]["previous"][0]
        self.assertEqual((prev["backend"], prev["session_id"]), ("claude", "claude-xyz"))

    def test_decisions_register_and_fresh_worker_session_per_milestone(self):
        m = managed(critic={"replies": {"brief": [
            "<question>Should the tables use percentages?\nA) yes\nB) no</question>\n\n"
            "Start with the tables; assume percentages for now."]}})
        m["agents"][2]["fresh_session"] = "milestone"
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        regs = rec.state["decisions"]
        self.assertEqual((regs[0]["id"], regs[0]["by"], regs[0]["source"]), ("S1", "Alex", "Q1"))
        self.assertIn("percentages", regs[0]["text"])
        self.assertTrue((rec.run_dir / "decisions.md").read_text().startswith("# Decisions"))
        ts = turns(rec)
        briefs = [t for t in ts if t["purpose"] == "brief"]
        self.assertNotIn("Decisions so far", briefs[0]["input"])
        self.assertIn("Decisions so far", briefs[1]["input"])  # milestone 2 starts with the whole register
        self.assertIn("- **S1** — Should the tables use percentages?: Go with the simpler option", briefs[1]["input"])
        self.assertNotIn("(you,", briefs[1]["input"])  # the critic gets decisions without attribution
        w2 = [t for t in ts if t["agent"] == "writer" and t.get("milestone") == 1]
        self.assertFalse(w2[0]["resumed_session"])  # a fresh worker session for the new milestone
        self.assertIn("fresh conversation", w2[0]["input"])
        self.assertNotIn("Decisions so far", w2[0]["input"])  # the worker gets the critic's instruction, not the register
        self.assertTrue(all(t["resumed_session"] for t in w2[1:]))
        self.assertEqual(len(rec.state["sessions"]["writer"]["previous"]), 1)

    def test_decisions_backfilled_on_resume(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        st = json.loads(json.dumps(rec.state))
        st.pop("decisions", None)
        st["questions"] = [{"id": "Q1", "from": "boss", "to": "human", "text": "QUESTIONS FOR YOU:\n1. Bold headers?",
                            "status": "answered", "answered_by": "human", "answer": "yes", "answered_at": "2026-01-01T10:00:00"}]
        st.update(status="running")
        st["managed"]["phase"] = "done"
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        rec2.state.update(st)
        rec2.save()
        (rec2.run_dir / "inbox" / "consumed").mkdir(parents=True)
        (rec2.run_dir / "inbox" / "consumed" / "20260101-090000-1-note.json").write_text(
            json.dumps({"to": ["critic"], "message": "Use British spelling."}))
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec2
        r.backfill_decisions()
        self.assertEqual([(d["id"], d["by"], d["text"]) for d in rec2.state["decisions"]],
                         [("S1", "you", "Use British spelling."), ("S2", "you", "Bold headers?: yes")])

    def test_critic_pauses_the_work_while_waiting_for_a_human_task(self):
        crit = {"replies": {"verify": [
            "<pause>The authors label the 20 items in `labels/`; I need the four CSV files back.</pause>\n\n"
            "Nothing more to do until the labels are in.\n\n<checklist>- [ ] labels are in</checklist>\n<verdict>FAIL</verdict>",
            "All good.\n\n<checklist>- [x] labels are in</checklist>\n<verdict>PASS</verdict>"]}}
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1}, critic=crit)])
        out = self.tmp / "out" / "m"
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
        th.start()
        t0, g = time.time(), None
        while time.time() - t0 < 10 and not g:
            runs = sorted(out.glob("*/state.json")) if out.exists() else []
            g = ((read_json(runs[-1]) if runs else {}).get("managed") or {}).get("gate")
            time.sleep(0.05)
        self.assertEqual(g["kind"], "wait")
        self.assertIn("Please do the task", g["text"])  # the manager's words for me
        st = read_json(runs[-1])
        self.assertEqual(st["status"], "awaiting_approval")
        n_rounds = st["round"]
        (runs[-1].parent / "inbox" / "a-gate.json").write_text(json.dumps(
            {"kind": "gate", "decision": "approve", "message": "Labels are in labels/v2.", "gate": g["key"]}))
        th.join(10)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        ts = turns(rec)
        res = [t for t in ts if t["purpose"] == "guidance"]
        self.assertEqual(len(res), 1)
        self.assertIn("Labels are in labels/v2.", res[0]["input"])
        self.assertIn("four CSV files", res[0]["input"])
        work = [t for t in ts if t["purpose"] == "work"]
        self.assertTrue(all("<pause>" not in t["input"] and "four CSV" not in t["input"] for t in work))
        self.assertEqual(rec.state["round"], n_rounds + 1)  # the pause spent no rounds
        self.assertIn("Labels are in labels/v2.", rec.state["decisions"][-1]["text"])
        from orch.managed import ManagedRunner
        r = ManagedRunner(load_config(cfg).monitor("m"), threading.Event(), log=lambda s: None)
        r.rec = rec
        wtext = r.decisions_text(r.workers[0])
        self.assertNotIn("Labels are in labels/v2.", wtext)  # pause messages are not the worker's
        self.assertIn("Labels are in labels/v2.", r.decisions_text(r.critic))
        self.assertNotIn("The whole run is paused", r.handover(r.workers[0]))
        pause = [t for t in ts if t["purpose"] == "pause"]
        self.assertEqual(len(pause), 1)
        self.assertIn("four CSV files", pause[0]["input"])  # the critic proposed, the manager decided
        later = [t for t in ts[ts.index(pause[0]) + 1:] if t["agent"] == "boss"]
        self.assertTrue(later, [(t["agent"], t["purpose"]) for t in ts])
        self.assertIn("Labels are in labels/v2.", later[0]["input"])  # the manager hears it too

    def test_untagged_checklist_is_gated_hidden_from_the_worker_and_shown_to_the_manager(self):
        crit = {"replies": {"verify": [
            "Fix the table header.\n\n## Checklist\n- [x] tests pass\n- [ ] table header fixed — still old\n\n<verdict>PASS</verdict>",
            "Looks right now.\n\n<checklist>\n- [x] tests pass\n- [x] table header fixed\n</checklist>\n<verdict>PASS</verdict>"]}}
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1}, critic=crit)]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        work = [t for t in ts if t["purpose"] == "work"]
        self.assertGreaterEqual(len(work), 2)  # the PASS with an open item was overruled
        self.assertIn("Fix the table header.", work[1]["input"])
        self.assertNotIn("[ ]", work[1]["input"])
        self.assertNotIn("Checklist", work[1]["input"])
        review = next(t for t in ts if t["purpose"] == "review")
        self.assertIn("working checklist at its last review (2 of 2 done)", review["input"])
        self.assertIn("- [x] table header fixed", review["input"])

    def test_manager_can_turn_down_a_proposed_pause(self):
        crit = {"replies": {"verify": [
            "<pause>The authors label the 20 items.</pause>\n\nNothing more to do.\n\n"
            "<checklist>- [ ] labels are in</checklist>\n<verdict>FAIL</verdict>",
            "All good.\n\n<checklist>- [x] labels are in</checklist>\n<verdict>PASS</verdict>"],
            "guidance": ["Write the analysis script against the dummy labels."]}}
        mgr = {"milestones": 1, "replies": {"pause": [
            "Not yet.\n\n<message to=\"the critic\">Have the worker write the analysis script on dummy labels.</message>\n\n"
            "<decision>CONTINUE</decision>"]}}
        status, rec = run(make_cfg(self.tmp, [managed(mgr=mgr, critic=crit)]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertFalse(any(g["kind"] == "wait" for g in rec.state["managed"].get("gates") or []))
        cont = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "guidance"]
        self.assertIn("analysis script on dummy labels", cont[0]["input"])
        self.assertNotIn("<decision>", cont[0]["input"])
        work = [t for t in ts if t["purpose"] == "work"]
        self.assertIn("dummy labels", work[1]["input"])

    def test_review_now_skips_the_job_wait(self):
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", "sleep 60.17"]))
        work = {"spawn": "sleep 60.17", "replies": {"work": ["Started the long run.", "Carried on."]}}
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1}, worker=work)])
        out = self.tmp / "out" / "m"
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
        th.start()
        t0, st = time.time(), {}
        while time.time() - t0 < 10 and st.get("status") != "waiting_job":
            runs = sorted(out.glob("*/state.json")) if out.exists() else []
            st = read_json(runs[-1]) if runs else {}
            time.sleep(0.05)
        self.assertIn("sleep 60.17", " ".join(st["managed"]["job"]["agents"]["writer"]["lines"]))
        (runs[-1].parent / "inbox" / "a-job.json").write_text(json.dumps({"kind": "job"}))
        th.join(10)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        self.assertEqual(subprocess.run(["pgrep", "-f", "sleep 60.17"], capture_output=True).returncode, 0)  # left alone

    def test_urgent_checkin_preserves_a_background_job_wait(self):
        from unittest import mock
        from orch.backends import MockBackend
        cfg = make_cfg(self.tmp, [managed(mgr={"milestones": 1}, critic={"replies": {"checkin": [
            '<message to="you">The experiment is still running.</message>']}})])
        stop, holder, jobs = threading.Event(), {}, []
        orig = MockBackend.run

        def spy(backend, req, cancel):
            result = orig(backend, req, cancel)
            if req.role == "worker" and not jobs:
                jobs.append(subprocess.Popen(["sleep", "30"], cwd=req.cwd))
            return result

        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            self.fail("Timed out waiting for the job/check-in")

        # Keep ownership of the real process for cleanup; simulate its discovery as a detached job.
        def detect(before, *args):
            return ([{"pid": jobs[0].pid, "command": "sleep 30"}]
                    if jobs and jobs[0].pid not in before else [])

        with mock.patch.object(MockBackend, "run", spy), mock.patch("orch.jobs.detect", side_effect=detect):
            th = threading.Thread(target=lambda: holder.update(res=run(cfg, stop=stop)), daemon=True)
            th.start()
            try:
                out = self.tmp / "out" / "m"
                wait_for(lambda: any((read_json(p) or {}).get("status") == "waiting_job"
                                     for p in out.glob("*/state.json")))
                rd = next(p.parent for p in out.glob("*/state.json"))
                before = read_json(rd / "state.json")["managed"]["job"]
                (rd / "inbox" / "urgent.json").write_text(json.dumps({
                    "to": ["critic"], "from": "boss", "urgent": True, "message": "Is the experiment still running?"}))
                wait_for(lambda: any((read_json(p) or {}).get("purpose") == "ack"
                                     and (read_json(p) or {}).get("status") == "ok"
                                     for p in rd.glob("rounds/r*/t*.json")))
                state = read_json(rd / "state.json")
                self.assertEqual(state["status"], "waiting_job")
                self.assertEqual(state["managed"]["phase"], "job")
                self.assertEqual(state["managed"]["job"], before)
                self.assertIsNone(jobs[0].poll())
                self.assertFalse(holder)
                self.assertFalse(any((read_json(p) or {}).get("purpose") == "verify"
                                     for p in rd.glob("rounds/r*/t*.json")))
                (rd / "inbox" / "release.json").write_text(json.dumps({"kind": "job"}))
                th.join(10)
                self.assertFalse(th.is_alive())
                self.assertEqual(holder["res"][0], "completed")
                self.assertIsNone(jobs[0].poll())
            finally:
                stop.set()
                for job in jobs:
                    job.terminate()
                    job.wait(timeout=5)
                th.join(5)

    def test_manager_pauses_the_work_itself(self):
        mgr = {"milestones": 1, "replies": {"ack": [
            "<pause>Please finish the dry run and tell me where the notes are.</pause>\n\nThanks.", "Carry on."]}}
        cfg = make_cfg(self.tmp, [managed(controller={"checkin_every": 1}, mgr=mgr, critic={"pass_after": 3})])
        out = self.tmp / "out" / "m"
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
        th.start()
        t0, g = time.time(), None
        while time.time() - t0 < 10 and not g:
            runs = sorted(out.glob("*/state.json")) if out.exists() else []
            g = ((read_json(runs[-1]) if runs else {}).get("managed") or {}).get("gate")
            time.sleep(0.05)
        self.assertEqual(g["kind"], "wait")
        self.assertIn("finish the dry run", g["text"])
        (runs[-1].parent / "inbox" / "a-gate.json").write_text(json.dumps(
            {"kind": "gate", "decision": "approve", "message": "Notes are in notes.md.", "gate": g["key"]}))
        th.join(10)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        ts = turns(rec)
        i_ack = [t["purpose"] for t in ts].index("ack")
        self.assertEqual(ts[i_ack + 1]["purpose"], "guidance")  # paused right after the check-in, then resumed
        self.assertIn("Notes are in notes.md.", ts[i_ack + 1]["input"])
        self.assertIn("finish the dry run", ts[i_ack + 1]["input"])
        self.assertEqual([x["kind"] for x in rec.state["managed"]["gates"]].count("wait"), 1)

    def test_protocol_is_added_to_every_role_and_can_be_turned_off(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        sp = {t["agent"]: t["system_prompt"] for t in turns(rec)}
        self.assertIn("<decision>PAUSE</decision>", sp["boss"])
        self.assertIn('<message to="you">', sp["critic"])
        self.assertNotRegex(sp["critic"], r"(?i)\b(manager|user|human|Alex)\b")
        self.assertNotRegex(sp["writer"], r"(?i)\b(manager|critic|user|human|Alex|Jordan)\b")
        self.assertIn('<no-wait pid="123"/>', sp["writer"])
        self.assertNotIn("{{", sp["writer"])
        off = managed(name="n", controller={"protocol": False}, mgr={"milestones": 1})
        (self.tmp / "b").mkdir()
        cfg = load_config(make_cfg(self.tmp / "b", [off]))
        self.assertTrue(all(not a.protocol_path for a in cfg.monitor("n").agents))

    def test_orch_finds_a_job_the_worker_left_running(self):
        work = {"spawn": "sleep 2.31", "replies": {"work": ["Started the full run in the background.",
                                                           "The run finished; results are in out.csv."]}}
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1}, worker=work)]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        seq = [(t["agent"], t["purpose"]) for t in ts if t["purpose"] in ("work", "verify")]
        self.assertEqual(seq[:3], [("writer", "work"), ("writer", "work"), ("critic", "verify")])
        done = [t for t in ts if t["purpose"] == "work"][1]["input"]
        self.assertIn("sleep 2.31", done)
        self.assertIn("has finished", done)

    def test_ignored_commands_are_not_waited_on(self):
        work = {"spawn": "sleep 4.37", "replies": {"work": ["Started a helper."]}}
        status, rec = run(make_cfg(self.tmp, [managed(controller={"job_ignore": ["sleep 4.37"]},
                                                      mgr={"milestones": 1}, worker=work)]))
        subprocess.run(["pkill", "-f", "sleep 4.37"])
        self.assertEqual(status, "completed")
        seq = [(t["agent"], t["purpose"]) for t in turns(rec) if t["purpose"] in ("work", "verify")]
        self.assertEqual(seq[:2], [("writer", "work"), ("critic", "verify")])

    def test_tag_reminder_is_added_now_and_then(self):
        status, rec = run(make_cfg(self.tmp, [managed(controller={"tag_reminder": 1}, mgr={"milestones": 1})]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertTrue(all("<system-reminder>" in t["input"] and t.get("tag_reminder") for t in ts))
        crit = next(t for t in ts if t["agent"] == "critic")["input"]
        self.assertIn('<message to="you">', crit)
        self.assertIn("not a request to add anything", crit)
        status, rec = run(make_cfg(self.tmp / "x" if (self.tmp / "x").mkdir() is None else self.tmp,
                                   [managed(controller={"tag_reminder": 0}, mgr={"milestones": 1})]))
        self.assertFalse(any(t.get("tag_reminder") for t in turns(rec)))
        for t in turns(rec):
            if "<system-reminder>" in t["input"]:
                self.assertIn(t["role"], ("verifier", "manager"))
                self.assertIn("complete answer inside" if t["role"] == "verifier" else 'speak as "I"',
                              t["input"])  # deterministic communication reminders still apply

    def test_reset_sessions_keeps_the_run_and_hands_over(self):
        cfg_path = make_cfg(self.tmp, [managed(mgr={"milestones": 2})])
        status, rec = run(cfg_path)
        self.assertEqual(status, "completed")
        from orch.managed import ManagedRunner
        mon = load_config(cfg_path).monitor("m")
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        r.decide("Use British spelling.", "you", "note")
        done = r.reset_sessions()
        self.assertEqual(sorted(done), ["boss", "critic", "writer"])
        st = rec.state
        self.assertTrue(all(st["fwd"][a][0].startswith("(handover notes") for a in done))  # written at the next turn
        for ag in (r.manager, r.critic, r.workers[0]):
            r.write_handover(ag)
        for a in ("boss", "critic", "writer"):
            self.assertIsNone(st["sessions"][a]["session_id"])
            self.assertTrue(st["sessions"][a]["reset_at"])
            self.assertEqual(len(st["sessions"][a]["previous"]) >= 1, True)
            self.assertIn("fresh conversation", st["fwd"][a][0])
        for a in ("boss", "critic"):
            self.assertIn("British spelling", st["fwd"][a][0])
        self.assertNotIn("British spelling", st["fwd"]["writer"][0])  # the critic passes on what the work must keep to
        self.assertIn("Your plan and rubric", st["fwd"]["boss"][0])
        self.assertIn("| 2 | Part 2 | done |", st["fwd"]["boss"][0])
        self.assertIn("Your latest review", st["fwd"]["critic"][0])
        self.assertEqual(st["managed"]["phase"], "done")  # the run's place is kept
        self.assertEqual(cli.main(["reset-sessions", cfg_path, "--agent", "writer"]), 0)

    def test_reset_mid_milestone_restarts_top_down(self):
        mgr = {"milestones": 2, "replies": {"restart": [
            "Picking up part 2.\n\n<message to=\"Jordan\">Part 2: finish the tables, percentages as decided.</message>"]}}
        crit = {"replies": {"brief": ["Start with table 1 in plan.md, then table 2. Use percentages."]}}
        cfg_path = make_cfg(self.tmp, [managed(mgr=mgr, critic=crit)])
        status, rec = run(cfg_path)
        from orch.managed import ManagedRunner
        mon = load_config(cfg_path).monitor("m")
        mg = rec.state["managed"]
        mg.update(phase="work", ms=1)
        mg["milestones"][1]["status"] = "active"
        rec.set(status="stopped")
        rec.save()
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        r.decide("Use percentages in every table.", "you", "note")
        r.reset_sessions()
        r.reset_sessions()  # twice before resuming: still one top-down restart
        self.assertEqual(rec.state["managed"]["phase"], "restart")
        self.assertEqual(rec.state["managed"]["restart"]["from"], "work")
        for ag in (r.critic, r.workers[0]):
            r.write_handover(ag)
        self.assertNotIn("percentages", rec.state["fwd"]["writer"][0])  # the worker's handover is only the fresh start
        self.assertNotIn("My instruction for the current work", rec.state["fwd"]["critic"][0])
        status, rec = run(cfg_path, resume=True)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        i = next(n for n, t in enumerate(ts) if t["purpose"] == "restart")
        self.assertEqual(ts[i]["agent"], "boss")
        self.assertIn("Your plan and rubric", ts[i]["input"])
        self.assertEqual((ts[i + 1]["agent"], ts[i + 1]["purpose"]), ("critic", "brief"))
        self.assertIn("Part 2: finish the tables", ts[i + 1]["input"])
        self.assertNotIn("Picking up part 2.", ts[i + 1]["input"])  # only the message reaches the critic
        w = ts[i + 2]
        self.assertEqual((w["agent"], w["purpose"]), ("writer", "work"))
        self.assertIn("Start with table 1 in plan.md", w["input"])
        self.assertIn("fresh conversation", w["input"])
        self.assertNotIn("Decisions so far", w["input"])

    def test_state_files_written_edited_and_kept_per_run(self):
        mgr = {"milestones": 1, "replies": {"kickoff": [
            "Starting.\n\n<message to=\"Jordan\">Write the tables.</message>\n\n"
            "<state>Standing: write the tables.\nHolds: percentages in every table.</state>"]}}
        crit = {"replies": {"verify": [
            "Not yet.\n\n<checklist>\n- [x] tables written\n- [ ] totals checked — missing\n</checklist>\n<verdict>FAIL</verdict>",
            "Good.\n\n<checklist>\n- [x] tables written\n- [x] totals checked\n</checklist>\n<verdict>PASS</verdict>"]}}
        cfg = managed(mgr=mgr, critic=crit)
        sdir = self.tmp / "agents"
        for ag in cfg["agents"]:
            ag["state_file"] = str(sdir / ag["name"] / "state.md")
        cfg_path = make_cfg(self.tmp, [cfg])
        status, rec = run(cfg_path)
        self.assertEqual(status, "completed")
        boss = (sdir / "boss" / "state.md").read_text()
        self.assertIn(f"run {rec.run_dir.name}", boss)
        self.assertIn("Holds: percentages in every table.", boss)
        critic = (sdir / "critic" / "state.md").read_text()
        self.assertIn("## My standing instruction", critic)
        self.assertIn("Standing: write the tables.", critic)
        self.assertIn("- [x] totals checked", critic)  # its newest checklist
        writer = (sdir / "writer" / "state.md").read_text()
        self.assertIn("written", writer)
        self.assertNotIn("critic", writer.split("-->", 1)[1].lower())

        from orch.managed import ManagedRunner
        r = ManagedRunner(load_config(cfg_path).monitor("m"), threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        h = r.handover(r.manager)
        self.assertIn("Your working state", h)
        self.assertIn("Holds: percentages in every table.", h)
        self.assertIn("latest message (report", h)  # the newest message, also when turns start in the same second
        self.assertNotIn("Not yet.", h)
        r.decide("Round to one decimal.", "you", "note")
        h = r.handover(r.manager)
        self.assertIn("Decided since those notes were last written", h)
        self.assertIn("Round to one decimal.", h)
        # my edit reaches the manager and the critic's file
        p = sdir / "boss" / "state.md"
        p.write_text(p.read_text().replace("percentages", "fractions"))
        r.sync_state(r.manager)
        self.assertTrue(any("fractions" in x and "I edited your working notes" in x for x in rec.state["fwd"]["boss"]))
        self.assertIn("fractions", (sdir / "critic" / "state.md").read_text())
        # a new run moves the old run's files aside
        status2, rec2 = run(cfg_path)
        self.assertTrue((rec.run_dir / "agent_state" / "boss.md").is_file())
        self.assertIn(f"run {rec2.run_dir.name}", (sdir / "boss" / "state.md").read_text())

    def test_managed_validation(self):
        m = managed()
        m["agents"] = m["agents"][1:]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))


class TestProjectFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_init_layout_settings_split_and_default_prompts(self):
        self.assertEqual(cli.main(["init", str(self.tmp), "--project"]), 0)
        d = self.tmp / ".orch"
        for f in (".gitignore", "agents/manager/agent.yaml", "agents/manager/goal.md", "agents/critic/memory.md",
                  "agents/worker/agent.yaml"):
            self.assertTrue((d / f).is_file(), f)
        self.assertFalse(list(d.glob("*.yaml")) + list(d.rglob("task.md")))
        self.assertEqual((d / "agents/critic/system_prompt.md").read_text(),
                         (ROOT / "orch/prompts/critic.md").read_text())  # copied from the role default
        cfg = load_config(str(self.tmp))  # the project root finds .orch/agents
        mon = cfg.monitors[0]
        self.assertEqual(mon.name, self.tmp.name.strip("-.") or mon.name)
        self.assertEqual((mon.workspace, cfg.output_dir), (str(self.tmp.resolve()), str((d / "runs").resolve())))
        c = mon.controller
        self.assertEqual((c.mode, c.approve_plan, c.checklist, c.milestone_rounds, mon.max_rounds), ("managed", True, True, 25, 100))
        self.assertTrue(mon.goal.startswith("Describe what you want done"))
        self.assertEqual([(a.name, a.role) for a in mon.agents], [("manager", "manager"), ("critic", "verifier"), ("worker", "worker"),
                                                                ("auditor", "auditor")])
        aud = mon.agent("auditor")
        self.assertFalse((d / "agents/auditor/system_prompt.md").exists())  # orch's own prompt: it audits orch
        self.assertIn("You are the auditor", aud.current_system_prompt())
        self.assertEqual((mon.controller.audit_every, mon.controller.audit_points), (10, 5))
        self.assertIn("Bash", aud.backend_options["disallowed_tools"])
        critic = mon.agent("critic")
        sp = critic.current_system_prompt()
        self.assertTrue(sp.startswith("You are the critic, a careful reviewer. I lead this work"), sp[:80])
        self.assertIn("<question>", sp)
        self.assertNotIn("{{", sp)
        self.assertNotIn("Things to keep in mind", sp)  # the memory stub is only a comment
        (d / "agents/critic/memory.md").write_text("<!-- note -->\nPrefer short reviews.")
        (d / "agents/critic/memory").mkdir()
        (d / "agents/critic/memory/style.md").write_text("British spelling.")
        self.assertTrue(critic.current_system_prompt().endswith("Things to keep in mind:\n\nPrefer short reviews.\n\nBritish spelling."))
        dsp = mon.agent("worker").current_system_prompt()  # the worker knows only "me": no other roles, no framework
        self.assertNotRegex(dsp.lower(), r"critic|manager|orch\b|agents")
        (d / "agents/worker/system_prompt.md").write_text("You are {{worker}}; {{critic}} reviews you.")
        wsp = load_config(str(d)).monitors[0].agent("worker").current_system_prompt()
        self.assertTrue(wsp.startswith("You are the worker; the critic reviews you.\n\n# How we work"), wsp[:120])

    def test_conflicting_workflow_setting_and_run(self):
        cli.main(["init", str(self.tmp), "--project"])
        d = self.tmp / ".orch"
        with open(d / "agents/worker/agent.yaml", "a") as f:
            f.write("max_rounds: 3\n")
        with self.assertRaises(ConfigError):
            load_config(str(self.tmp))
        for name, extra in (("manager", "approve_plan: false\napprove_milestones: false\nmax_rounds: 20\n"),
                            ("critic", ""), ("worker", ""), ("auditor", "")):
            y = d / "agents" / name / "agent.yaml"
            text = y.read_text().replace("backend: claude", "backend: mock").replace("approve_plan: true\n", "") \
                .replace("approve_milestones: true\n", "").replace("max_rounds: 20 ", "# ").replace("max_rounds: 3\n", "")
            y.write_text(text + extra)
        self.assertTrue(all(a.backend == "mock" for a in load_config(str(self.tmp)).monitors[0].agents))
        self.assertEqual(cli.main(["run", str(self.tmp)]), 0)
        t = next((d / "runs").glob("*/*/rounds/r000/t01-manager.json"))
        self.assertTrue(read_json(t)["system_prompt"].startswith("You are the manager, who leads this work."))
        w = next((d / "runs").glob("*/*/rounds/r001/t01-worker.json"))
        self.assertTrue(read_json(w)["input"].startswith("Here is what I need"))  # the critic's brief, no task text


if __name__ == "__main__":
    unittest.main()
