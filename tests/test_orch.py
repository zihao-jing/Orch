"""End-to-end tests using the mock and command backends. Run: python -m unittest discover tests"""

import dataclasses
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
from orch.backends import TurnResult  # noqa: E402
from orch.controller import MonitorRunner, provider_error, relayed  # noqa: E402
from orch.recorder import Recorder, read_json  # noqa: E402
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

    def test_provider_refusal_is_not_kept_as_a_reply_and_retries_once(self):
        m = pipeline()
        msg = "API Error: Your account can't receive replies from Claude right now."
        m["agents"][0].update(retries=1, backend_options={"fail_turns": [2], "error_text": msg})
        m["controller"]["provider_retry_wait"] = 0
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        second = [t for t in turns(rec) if t["agent"] == "w1"][1]
        self.assertEqual(second["status"], "ok")
        self.assertEqual([a["status"] for a in second["attempts"]], ["error", "ok"])
        self.assertIn("can't receive replies", second["attempts"][0]["reason"])
        self.assertNotIn("API Error", second["output"])

    def test_a_refused_turn_that_fails_again_keeps_the_provider_message_out_of_its_output(self):
        m = pipeline()
        msg = "API Error: Your account can't receive replies from Claude right now."
        m["agents"][0].update(retries=1, backend_options={"fail_turns": [2, 3], "error_text": msg})
        m["controller"]["provider_retry_wait"] = 0
        status, rec = run(make_cfg(self.tmp, [m]))
        failed = [t for t in turns(rec) if t["agent"] == "w1"][1]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["output"], "")
        self.assertIn("can't receive replies", failed["provider_error"])

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

    def test_board_lists_audit_history_newest_first(self):
        from orch.board import Board
        run = self.tmp / "run"
        (run / "audits").mkdir(parents=True)
        (run / "audits" / "20261110-101500-r001-r010.md").write_text(
            "# Audit of rounds 1–10 (ok, $0.12)\n\n## Sent\n\nprompt\n\n## Reply\n\n1. First point\n", encoding="utf-8")
        (run / "audits" / "20261111-101500-r011-r020.md").write_text(
            "# Audit of rounds 11–20 (error, $0.00)\n\n## Sent\n\nprompt\n\n## Reply\n\n\n## stderr\n\nboom\n",
            encoding="utf-8")
        (run / "state.json").write_text(json.dumps({"monitor": "m", "status": "running"}))
        d = Board("m", lambda: run, None).data()["audit"]
        self.assertFalse(d["configured"])
        self.assertEqual(d["findings"], "")
        self.assertEqual([h["file"] for h in d["history"]],
                         ["20261111-101500-r011-r020.md", "20261110-101500-r001-r010.md"])
        self.assertEqual(d["history"][0]["status"], "error")
        self.assertEqual(d["history"][0]["reply"], "")
        self.assertEqual(d["history"][1]["rounds"], [1, 10])
        self.assertEqual(d["history"][1]["cost"], 0.12)
        self.assertEqual(d["history"][1]["reply"], "1. First point")

    def test_tell_writes_a_direct_message_and_rejects_unknown_agents(self):
        m = pipeline()
        m["agents"].insert(1, {"name": "w2", "role": "worker"})
        cfg_obj = load_config(make_cfg(self.tmp, [m]))
        from orch.recorder import Recorder
        rec = Recorder.create(cfg_obj.output_dir, "m", cfg_obj.monitor("m").snapshot())
        self.assertEqual(cli.main(["tell", str(rec.run_dir), "w2", "check the tests first", "--urgent"]), 0)
        files = list((rec.run_dir / "inbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        data = read_json(files[0])
        self.assertEqual(data["to"], ["w2"])
        self.assertTrue(data["urgent"])
        self.assertEqual(data["message"], "check the tests first")
        self.assertEqual(cli.main(["tell", str(rec.run_dir), "nobody", "hello"]), 1)
        self.assertEqual(len(list((rec.run_dir / "inbox").glob("*.json"))), 1)


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
        self.assertIn("Please break this into a short `<plan>` of steps, then give Sam the first step only", brief["input"])
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

    def test_night_mode_passes_gates_without_me(self):
        cfg = make_cfg(self.tmp, [managed(controller={"approve_plan": True, "approve_milestones": True},
                                          mgr={"milestones": 2})])
        out = self.tmp / "out" / "m"
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
        th.start()
        t0 = time.time()
        while time.time() - t0 < 10:  # the plan gate opens; I switch night mode on and leave
            runs = sorted(out.glob("*/state.json")) if out.exists() else []
            g = ((read_json(runs[-1]) or {}).get("managed") or {}).get("gate") if runs else None
            if g:
                break
            time.sleep(0.05)
        (runs[-1].parent / "inbox" / "a-night.json").write_text(json.dumps({"kind": "night", "on": True}))
        th.join(15)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        self.assertTrue(rec.state["night"])
        self.assertEqual([x["decision"] for x in rec.state["managed"]["gates"]], ["approve"] * 3)
        self.assertTrue(any(d["by"] == "manager (night mode)" for d in rec.state["decisions"]))
        mgr = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] != "plan"]
        self.assertTrue(mgr and all("Night mode is on" in t["input"] for t in mgr))

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
        with mock.patch("orch.backends.get_backend", lambda name: Fake()):
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

    def test_a_terminal_shell_started_in_the_workspace_is_not_a_job(self):
        # the round-80 case: an interactive zsh in the project folder, reparented under an editor's terminal host
        import os
        from unittest import mock
        from orch import jobs
        ws, here = os.path.realpath(self.tmp), os.getcwd()
        before = {1: (0, "launchd"), 50: (1, "node ptyHost")}
        after = {**before, 60: (50, "/bin/zsh -il"), 61: (60, "-zsh"), 62: (50, "bash"),
                 63: (1, "bash train.sh"), 64: (1, "python run.py")}
        os.chdir(ws)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {p: ws for p in pids}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual(sorted(j["pid"] for j in found), [63, 64])
        self.assertTrue(jobs.is_shell("/bin/zsh -il"))
        self.assertFalse(jobs.is_shell("bash train.sh"))

    def test_a_waiter_loop_under_a_turn_is_found(self):
        from orch import jobs
        procs = {100: (1, "claude -p --resume x"),            # the worker's turn
                 101: (100, "/bin/zsh -c eval 'cd ws; while kill -0 72926 2>/dev/null; do sleep 10; done'"),
                 102: (101, "sleep 10"),
                 103: (100, "python -m scripts.run_stage --match"),   # real work under the turn: not a waiter
                 104: (1, "bash scripts/job.sh")}                  # not under the turn at all
        self.assertEqual(jobs.waiters(100, procs), [101])
        self.assertEqual(jobs.waiters(104, procs), [])

    def test_an_editor_helper_orphaned_into_the_workspace_is_not_a_job(self):
        from orch import jobs
        from unittest import mock
        import os
        here = os.getcwd()
        ws = str(Path(tempfile.mkdtemp()).resolve())
        vscode = ("/Users/x/.vscode-server/cli/servers/Stable-abc/server/node "
                  "/Users/x/.vscode-server/extensions/markdown-language-features/dist/serverWorkerMain --node-ipc")
        before = {10: (1, "launchd")}
        after = {**before, 70: (1, vscode), 71: (1, "python build_report.py")}
        os.chdir(ws)
        try:
            with mock.patch.object(jobs, "cwds", lambda pids: {p: ws for p in pids}):
                found = jobs.detect(before, [ws], after=after)
        finally:
            os.chdir(here)
        self.assertEqual([j["pid"] for j in found], [71])
        self.assertTrue(jobs.is_tool(vscode))
        self.assertFalse(jobs.is_tool("python build_report.py"))

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

    def test_a_redone_round_keeps_a_worker_turn_that_already_finished(self):
        from orch.managed import ManagedRunner
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        rec2 = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        st = json.loads(json.dumps(rec.state))
        w = next(t for t in turns(rec) if t["agent"] == "writer")
        st["managed"].update(phase="work", ms=0, ms_round=1, in_work=1, feedback={"writer": "Make the tables."})
        st["managed"]["milestones"][0].update(status="active", score=None)
        st.update(round=0, status="running", sessions={k: v for k, v in st["sessions"].items() if k != "writer"})
        rec2.state.update(st)
        rec2.save()
        # the writer's round-1 turn finished before the crash; the round itself was not finished
        done = dict(w, status="ok", output="FINISHED-BEFORE-CRASH", round=1, seq=1, turn_id="r001-t01-writer",
                    session_id="mock-keep")
        rec2.write_turn(done)
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        self.assertEqual(r.run(), "completed")
        rec3 = Recorder(rec2.run_dir, rec2.output_root)
        writer_work_r1 = [x for x in turns(rec3) if x["agent"] == "writer" and x["round"] == 1
                          and x.get("purpose") == "work"]
        self.assertEqual([x["turn_id"] for x in writer_work_r1], ["r001-t01-writer"])  # not run a second time
        self.assertEqual(read_json(rec2.run_dir / "rounds" / "r001" / "t01-writer.json")["output"],
                         "FINISHED-BEFORE-CRASH")

    def test_resume_without_a_terminal_says_which_question_waits(self):
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [pipeline()]))
        mon = cfg.monitor("m")
        rec = Recorder.create(cfg.output_dir, "m", mon.snapshot())
        rec.set(asked={"agent": "w1", "round": 1})
        r = MonitorRunner(mon, threading.Event(), log=lambda s: None)
        r.rec = rec
        inbox = rec.run_dir / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / "unrelated.json").write_text(json.dumps({"message": "a note for nobody in particular"}))
        r.ask_unanswered()  # no terminal: the wait is written down, unrelated files don't hide it
        self.assertIn("waiting for your answer for w1", (rec.run_dir / "progress.md").read_text())
        (inbox / "answer.json").write_text(json.dumps({"to": ["w1"], "message": "use option B"}))
        before = (rec.run_dir / "progress.md").read_text()
        r.ask_unanswered()  # an answer for w1 is already queued: nothing more to say
        self.assertEqual((rec.run_dir / "progress.md").read_text(), before)

    def test_manager_preference_tag_writes_the_targets_preference_file(self):
        from orch.managed import ManagedRunner
        from orch import tags as T
        cfg = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        mgr = next(a for a in mon.agents if a.role == "manager")
        w = next(a for a in mon.agents if a.role == "worker")
        pref = self.tmp / "prefs" / "preference.md"
        w.state_file = str(self.tmp / "prefs" / "state.md")  # preference.md lives beside state.md
        text = f'<preference to="{w.name}">Keep tables under one page.</preference> Noted.'
        r.after_state_turn(mgr, {"status": "ok", "output": text})
        r.after_state_turn(mgr, {"status": "ok", "output": text})  # the same note twice is written once
        lines = pref.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("Keep tables under one page.", lines[0])
        self.assertIn(f"(from {mgr.name})", lines[0])
        self.assertEqual(T.outside(text).strip(), "Noted.")  # the tag is not passed on as text
        r.after_state_turn(mgr, {"status": "ok", "output": '<preference to="nobody">x</preference>'})
        self.assertEqual(len(pref.read_text(encoding="utf-8").splitlines()), 1)

    def test_board_talks_to_a_worker_in_its_own_conversation(self):
        import time as _t
        from orch.board import Board
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})])).monitor("m")
        b = Board("m", lambda: rec.run_dir, mon)
        w = next(a for a in mon.agents if a.role == "worker")
        self.assertIn("error", b.chat("are you there?", agent="nobody"))
        self.assertIn("error", b.chat("hi", agent=w.name, qid="Q1"))
        cid = b.chat("What did you do last?", agent=w.name)["id"]
        for _ in range(100):
            rows = [json.loads(x) for x in (rec.run_dir / "chat.jsonl").read_text().splitlines()]
            done = [r for r in rows if r.get("id") == cid and r.get("status") not in (None, "running")]
            if done:
                break
            _t.sleep(0.1)
        self.assertTrue(done, "the worker's reply did not arrive")
        self.assertEqual((done[0]["role"], done[0]["who"], done[0]["with"]), ("agent", w.name, w.name))
        human = [r for r in rows if r.get("id") == cid + "-q"][0]
        self.assertEqual((human["role"], human["with"]), ("human", w.name))

    def test_board_state_sends_previews_and_rereads_only_changed_turns(self):
        from unittest import mock
        from orch import board as B
        from orch.board import Board
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [pipeline()]))
        rec = Recorder.create(cfg.output_dir, "m", cfg.monitor("m").snapshot())
        long = "x" * 5000 + " END"
        for i in range(1, 4):
            rec.write_turn({"turn_id": f"r001-t0{i}-w1", "round": 1, "seq": i, "agent": "w1", "role": "worker",
                            "purpose": "work", "status": "ok", "output": long, "started_at": f"2026-11-11T10:0{i}:00"})
        b = Board("m", lambda: rec.run_dir, None)
        d = b.data()
        self.assertEqual(len(d["turns"]), 3)
        for t in d["turns"]:
            self.assertLessEqual(len(t["output"]), B.PREVIEW_CHARS)
            self.assertEqual(t["output_len"], len(long))
        self.assertEqual(b.turn("r001-t01-w1")["output"], long)  # the whole output is still there, on request
        # a second refresh reads only the state and config files, not the turns again
        with mock.patch("orch.board.read_json", wraps=B.read_json) as rj:
            b.data()
        self.assertFalse([c for c in rj.call_args_list if "rounds" in str(c.args[0])])
        # a changed turn is read again
        rec.write_turn({"turn_id": "r001-t01-w1", "round": 1, "seq": 1, "agent": "w1", "role": "worker",
                        "purpose": "work", "status": "ok", "output": "short now", "started_at": "2026-11-11T10:01:00"})
        t1 = next(t for t in b.data()["turns"] if t["turn_id"] == "r001-t01-w1")
        self.assertEqual((t1["output"], t1["output_len"]), ("short now", 9))

    def test_checklist_text_and_revise_flag_are_sent_without_the_whole_review(self):
        from orch.board import Board
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [pipeline()]))
        rec = Recorder.create(cfg.output_dir, "m", cfg.monitor("m").snapshot())
        review = ("A long analysis. " * 300 + "\n<checklist>\n- [x] tables done\n- [ ] figure 2 — missing legend\n</checklist>\n"
                  "<decision>REVISE</decision>")
        rec.write_turn({"turn_id": "r001-t01-critic", "round": 1, "seq": 1, "agent": "critic", "role": "verifier",
                        "purpose": "verify", "status": "ok", "output": review, "milestone": 0,
                        "started_at": "2026-11-11T10:00:00"})
        t = Board("m", lambda: rec.run_dir, None).data()["turns"][0]
        self.assertEqual(t["checklist"], [1, 2])
        self.assertIn("figure 2", t["checklist_text"])
        self.assertNotIn("long analysis", t["checklist_text"])
        self.assertTrue(t["revise"])
        self.assertLess(len(t["output"]), len(review))

    def test_chat_is_tailed_and_older_messages_load_on_request(self):
        from orch.board import Board, CHAT_TAIL
        from orch.recorder import Recorder
        cfg = load_config(make_cfg(self.tmp, [pipeline()]))
        rec = Recorder.create(cfg.output_dir, "m", cfg.monitor("m").snapshot())
        lines = []
        for i in range(150):  # manager messages, each written twice (running, then the reply) as the board does
            lines.append({"id": f"c{i:03d}", "role": "human", "text": f"q{i}", "ts": "2026-11-11T10:00:00"})
            lines.append({"id": f"c{i:03d}-r", "role": "manager", "status": "running", "ts": "2026-11-11T10:00:01"})
            lines.append({"id": f"c{i:03d}-r", "role": "manager", "status": "ok", "text": f"a{i}", "ts": "2026-11-11T10:00:02"})
        lines.append({"id": "qq", "role": "human", "text": "about Q1", "qid": "Q1", "ts": "2026-11-11T10:00:03"})
        (rec.run_dir / "chat.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        b = Board("m", lambda: rec.run_dir, None)
        d = b.data()
        mgr_rows = [c for c in d["chat"] if not c.get("qid")]
        self.assertEqual(len(mgr_rows), CHAT_TAIL)
        self.assertEqual(mgr_rows[-1]["text"], "a149")  # the latest reply, merged from its two lines
        self.assertTrue(any(c.get("qid") == "Q1" for c in d["chat"]))  # question threads are always sent
        self.assertTrue(d["chat_more"].get("manager"))
        older = b.chat_before("manager", mgr_rows[0]["id"])
        self.assertEqual(len(older["rows"]), CHAT_TAIL)
        self.assertEqual(older["rows"][-1]["id"], "c119-r")  # right before the first message the state sent
        self.assertTrue(older["more"])
        rows, cur = list(older["rows"]), older
        while cur["more"]:  # walking back through the history reaches the first message, and then stops
            cur = b.chat_before("manager", cur["rows"][0]["id"])
            rows = cur["rows"] + rows
        self.assertEqual(rows[0]["text"], "q0")
        self.assertEqual(len([c for c in rows if c["role"] == "human"]), 120)  # 120 messages before the tail

    def test_plan_shows_the_resume_without_running_anything(self):
        from orch.managed import ManagedRunner  # noqa: F401  (managed mode is what the plan describes)
        from orch.recorder import Recorder
        import contextlib
        import io
        cfg_path = make_cfg(self.tmp, [managed(mgr={"milestones": 1})])
        status, rec = run(cfg_path)
        rec.set(status="failed", error="critic verify turn ended with status error")
        before = sorted(p.name for p in rec.run_dir.parent.iterdir())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["run", cfg_path, "--resume", "--plan"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("status failed", out)
        self.assertIn("critic verify turn ended with status error", out)
        self.assertIn("writer", out)
        self.assertEqual(sorted(p.name for p in rec.run_dir.parent.iterdir()), before)  # no new run was started

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
        cfg = load_config(make_cfg(self.tmp, [managed(controller={"milestone_rounds": 5}, mgr={"milestones": 1})]))
        mon = cfg.monitor("m")
        status, rec = run(make_cfg(self.tmp, [managed(controller={"milestone_rounds": 5}, mgr={"milestones": 1})]))
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
        for f in (".gitignore", "agents/manager/agent.yaml", "agents/manager/goal.md", "agents/critic/preference.md",
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
        (d / "agents/critic/preference.md").write_text("- 2026-11-01: Prefer short reviews.\n")
        self.assertNotIn("Prefer short reviews", critic.current_system_prompt())  # read on demand, never injected
        (d / "agents/critic/notes.md").write_text("British spelling.")
        critic.memory_files = [str(d / "agents/critic/notes.md")]  # explicit memory notes are still injected
        self.assertTrue(critic.current_system_prompt().endswith("Things to keep in mind:\n\nBritish spelling."))
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


class TestRelayAndHandoverSizes(unittest.TestCase):
    def test_a_provider_refusal_is_recognised_and_an_answer_is_not(self):
        refused = TurnResult("error", "API Error: Your account can't receive replies from Claude right now.", None, 1)
        self.assertIn("can't receive replies", provider_error(refused))
        self.assertEqual(provider_error(TurnResult("error", "Here is my answer about API Errors.", None, 1)), "")
        self.assertEqual(provider_error(TurnResult("ok", "API Error: quoted text", None, 0)), "")

    def test_an_instruction_block_is_relayed_whole_and_the_rest_is_not(self):
        long_report = "x" * 9000
        reply = f"{long_report}\n<instruction>Fix the parser and rerun the tests.</instruction>\n{long_report}"
        self.assertEqual(relayed(reply, 8000), "Fix the parser and rerun the tests.")

    def test_a_reply_without_an_instruction_goes_whole_by_default(self):
        text = "a" * 9000
        self.assertEqual(relayed(text, 0), text)

    def test_a_cap_is_applied_only_when_one_is_set(self):
        out = relayed("a" * 9000, 8000)
        self.assertLess(len(out), 8100)
        self.assertIn("characters omitted", out)

    def test_the_decision_register_keeps_long_entries_whole(self):
        from orch.managed import ManagedRunner
        long_text = "\n".join(f"Step {i}: check item {i} carefully." for i in range(2000))
        fake = type("Fake", (), {})()
        fake.st = {}
        fake.rec = type("R", (), {"event": lambda self, *a, **k: None})()
        fake.write_decisions = lambda: None
        ManagedRunner.decide(fake, "Q1 ask: " + long_text, "manager", "Q1")
        self.assertEqual(fake.st["decisions"][0]["text"], "Q1 ask: " + long_text)


class TestWaiterStop(unittest.TestCase):
    """A silent agent whose turn sits in a waiter loop has the loop stopped, and the turn ends."""

    def test_a_silent_waiter_loop_is_stopped_and_the_turn_ends(self):
        import os
        from unittest import mock
        from orch import backends
        script = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done & wait"
        with mock.patch.object(backends, "WAITER_GRACE", 0.5), mock.patch.object(backends, "WAITER_CHECK", 0.2):
            start = time.monotonic()
            rc, out, err, status = backends.run_process(["sh", "-c", script], None, ".", 0, {}, lambda: False,
                                                        on_line=lambda line: None)
        self.assertLess(time.monotonic() - start, 30)
        self.assertIn("stopped a waiter loop", err)


class TestRunCost(unittest.TestCase):
    """The board's run cost must not fall when it is recomputed from cached turn rows."""

    def test_repeated_polls_give_the_same_total(self):
        from orch.board import _turn_costs
        turns = [{"session_id": "s", "cost": 1.0, "ended_at": "1", "status": "ok"},
                 {"session_id": "s", "cost": 3.0, "ended_at": "2", "status": "ok"}]  # session total 3.0 so far
        cached = list(turns)
        totals = [_turn_costs([dict(t) for t in cached], []) for _ in range(3)]
        self.assertEqual(totals, [3.0, 3.0, 3.0])


class TestMessageLog(unittest.TestCase):
    """board._message_log: conversations, transfers, check-ins and warnings in one time-ordered list."""

    def test_every_kind_of_message_is_listed_in_time_order(self):
        import tempfile
        from orch.board import _message_log
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            (rd / "inbox" / "consumed").mkdir(parents=True)
            (rd / "rounds" / "r001").mkdir(parents=True)
            rows = [
                {"id": "c1", "role": "human", "text": "Please check it.", "ts": "2026-11-12T10:00:00+08:00", "with": "critic"},
                {"id": "c1", "role": "agent", "who": "critic", "with": "critic", "status": "ok", "text": "Done.",
                 "ts": "2026-11-12T10:00:05+08:00", "problems": ['<message to="The critic"> is addressed to the sender itself; not sent']}]
            (rd / "chat.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            (rd / "inbox" / "consumed" / "20261112-100300-1-board-note.json").write_text(
                json.dumps({"to": ["worker"], "message": "Use option A.", "urgent": True}), encoding="utf-8")
            (rd / "inbox" / "20261112-100900-2-board-note.json").write_text(
                json.dumps({"to": ["critic"], "from": "manager", "message": "Waiting note."}), encoding="utf-8")
            (rd / "events.jsonl").write_text("\n".join([
                json.dumps({"ts": "2026-11-12T10:03:10+08:00", "event": "human_messages", "count": 1,
                            "files": ["20261112-100300-1-board-note.json"]}),
                json.dumps({"ts": "2026-11-12T10:06:00+08:00", "event": "route_problem", "agent": "critic",
                            "problem": "names no agent; not sent"})]), encoding="utf-8")
            (rd / "rounds" / "r001" / "t01-critic.json").write_text(json.dumps(
                {"turn_id": "r001-t01-critic", "output": "Full check-in reply."}), encoding="utf-8")
            turns = [{"turn_id": "r001-t01-critic", "agent": "critic", "purpose": "checkin", "status": "ok",
                      "started_at": "2026-11-12T10:02:00+08:00", "output": "short"}]
            queued = [{"to": "worker", "from": "critic", "message": "Stop the job.", "status": "queued",
                       "created_at": "2026-11-12T10:02:30+08:00", "source_turn": "r001-t01-critic"}]
            out = _message_log(rd, rows, turns, {"critic": "The critic", "worker": "The worker"}, "manager", queued,
                               {"critic": ["Pending decisions."]})
        kinds = [(m["kind"], m["from"], m["to"]) for m in out]
        self.assertEqual(kinds, [
            ("conversation", "you", "The critic"),
            ("conversation", "The critic", "you"),
            ("check-in", "The critic", "manager"),
            ("check-in", "The critic", "The worker"),
            ("transfer", "you", "The worker"),
            ("warning", "The critic", ""),
            ("transfer", "manager", "The critic"),
            ("transfer", "orch", "The critic"),  # held for the critic's next turn: listed last
        ])
        self.assertEqual((out[-1]["status"], out[-1]["text"]), ("waiting", "Pending decisions."))
        self.assertEqual(out[1]["problems"][0][:12], '<message to=')
        self.assertTrue(out[4]["urgent"])
        self.assertEqual(out[4]["status"], "read")
        self.assertEqual(out[6]["status"], "waiting")
        self.assertEqual(out[2]["text"], "Full check-in reply.")


class TestMessageRoutes(unittest.TestCase):
    """tags.routes: where each <message to="…"> goes, and why a block can't go anywhere."""

    def setUp(self):
        from types import SimpleNamespace as NS
        self.agents = [NS(name="critic", called="The critic", role="verifier"),
                       NS(name="worker", called="The worker", role="worker"),
                       NS(name="manager", called="The manager", role="manager"),
                       NS(name="auditor", called="The auditor", role="auditor")]

    def test_a_block_to_another_agent_is_routed_with_its_urgent_flag(self):
        from orch import tags as T
        text = '<message to="The worker" urgent="yes">Stop the job.</message> and <message to="manager">Ack.</message>'
        sent, problems = T.routes(text, "critic", self.agents)
        self.assertEqual([(t.name, b, u) for t, b, u in sent],
                         [("worker", "Stop the job.", True), ("manager", "Ack.", False)])
        self.assertEqual(problems, [])

    def test_the_role_words_reach_the_only_agent_of_that_role(self):
        from orch import tags as T
        sent, problems = T.routes('<message to="worker">Do X.</message>', "critic", self.agents)
        self.assertEqual([t.name for t, _b, _u in sent], ["worker"])
        self.assertEqual(problems, [])

    def test_a_block_addressed_to_the_sender_is_reported_not_sent(self):
        # the round-79 case: the critic addressed its own instructions to "The critic"
        from orch import tags as T
        sent, problems = T.routes('<message to="The critic">Diagnosis accepted.</message>', "critic", self.agents)
        self.assertEqual(sent, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("addressed to the sender itself", problems[0])

    def test_unknown_empty_and_auditor_blocks_are_reported(self):
        from orch import tags as T
        text = ('<message to="the team">x</message><message to="worker">  </message>'
                '<message to="auditor">y</message>')
        sent, problems = T.routes(text, "critic", self.agents)
        self.assertEqual(sent, [])
        self.assertEqual(len(problems), 3)
        self.assertIn("names no agent", problems[0])
        self.assertIn("is empty", problems[1])
        self.assertIn("auditor", problems[2])

    def test_a_block_to_the_human_is_skipped_silently(self):
        from orch import tags as T
        sent, problems = T.routes('<message to="you">Answer.</message>', "critic", self.agents)
        self.assertEqual((sent, problems), ([], []))


class TestBoardMessageDelivery(unittest.TestCase):
    """board._routes: for each <message> an agent passed on, what the recipient was actually given."""

    AGENTS = [{"name": "critic", "display_name": "The critic", "role": "verifier"},
              {"name": "worker", "display_name": "The worker", "role": "worker"},
              {"name": "manager", "display_name": "The manager", "role": "manager"}]
    LONG = "Rerun the E4 insertion arm with the corrected span.\nKeep the frozen matcher and report the ties."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def seen(self, inp="", out="", cut=()):
        from orch import tags as T
        from orch.board import _flat
        return {"input": _flat(inp), "cut_in": [(at, _flat(m)) for at, m in cut], "blocks": T.blocks(out, "message")}

    def routes(self, turns, seen, wm=(), events=()):
        from orch.board import _routes
        (self.tmp / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return _routes(self.tmp, turns, seen, self.AGENTS, list(wm))

    def turn(self, tid, agent, at, status="ok", purpose="work"):
        return {"turn_id": tid, "agent": agent, "started_at": at, "ended_at": at, "status": status, "purpose": purpose}

    def test_a_message_counts_as_read_in_the_first_later_prompt_that_carries_it(self):
        turns = [self.turn("r1-t1-worker", "worker", "2026-01-01T10:00:00"),
                 self.turn("r1-t2-critic", "critic", "2026-01-01T10:01:00", purpose="verify"),
                 self.turn("r1-t3-critic", "critic", "2026-01-01T10:02:00", purpose="verify")]
        # the critic gets it wrapped and with its heading dropped, as orch passes it on
        seen = {"r1-t1-worker": self.seen(out=f'Done.\n<message to="The critic">## For you\n{self.LONG}</message>'),
                "r1-t2-critic": self.seen(inp="The worker sent this update:\n\n- " + self.LONG.replace("\n", "\n- ")),
                "r1-t3-critic": self.seen(inp=self.LONG)}
        out, received = self.routes(turns, seen)
        r = out["r1-t1-worker"][0]
        self.assertEqual((r["status"], r["turn"], r["to"], r["how"]), ("read", "r1-t2-critic", "critic", "prompt"))
        self.assertEqual(received, {"r1-t2-critic": [{"from": "worker", "turn": "r1-t1-worker", "i": 0}]})

    def test_waiting_unseen_failed_and_for_you(self):
        turns = [self.turn("r1-t1-manager", "manager", "2026-01-01T10:00:00", purpose="review"),
                 self.turn("r1-t2-worker", "worker", "2026-01-01T10:01:00")]
        out_text = ('<message to="you">Here is my answer.</message><message to="critic" urgent="yes">Stop now.</message>'
                    '<message to="worker">Use the new prompt for every remaining call.</message>'
                    '<message to="the team">x</message>')
        seen = {"r1-t1-manager": self.seen(out=out_text), "r1-t2-worker": self.seen(inp="something else entirely")}
        rows = self.routes(turns, seen)[0]["r1-t1-manager"]
        self.assertEqual([r["status"] for r in rows], ["you", "waiting", "unseen", "failed"])
        self.assertTrue(rows[1]["urgent"])
        self.assertEqual(rows[2]["later"], 1)
        self.assertIn("names no agent", rows[3]["problem"])

    def test_a_problem_orch_logged_for_the_reply_is_shown_as_the_reason(self):
        turns = [self.turn("r1-t1-critic", "critic", "2026-01-01T10:00:00", purpose="verify"),
                 self.turn("r1-t2-manager", "manager", "2026-01-01T10:05:00", purpose="review")]
        seen = {"r1-t1-critic": self.seen(out='<message to="manager">Please look at the scores again.</message>'),
                "r1-t2-manager": self.seen(inp="Please look at the scores again.")}
        why = '<message to="manager"> is not the worker; a review passes messages to the worker only; not sent'
        rows = self.routes(turns, seen, events=[{"ts": "2026-01-01T10:00:00", "event": "route_problem",
                                                  "agent": "critic", "problem": why}])[0]["r1-t1-critic"]
        self.assertEqual((rows[0]["status"], rows[0]["problem"]), ("failed", why))

    def test_a_note_cut_into_a_running_turn_and_a_live_update(self):
        turns = [self.turn("r1-t1-worker", "worker", "2026-01-01T10:00:00", status="running"),
                 self.turn("r1-t2-critic", "critic", "2026-01-01T10:01:00", purpose="checkin"),
                 self.turn("r1-t3-manager", "manager", "2026-01-01T10:02:00", purpose="review"),
                 self.turn("r1-t4-critic", "critic", "2026-01-01T10:03:00", status="running")]
        seen = {"r1-t1-worker": self.seen(),
                "r1-t2-critic": self.seen(out=f'<message to="worker">{self.LONG}</message>'),
                "r1-t3-manager": self.seen(out='<message to="critic" urgent="yes">Stop the sweep now.</message>'),
                "r1-t4-critic": self.seen(cut=[("2026-01-01T10:03:30", "Stop the sweep now.")])}
        wm = [{"source_turn": "r1-t2-critic", "to": "worker", "message": self.LONG, "status": "accepted",
               "delivery_turn": "r1-t1-worker", "updated_at": "2026-01-01T10:01:30"}]
        out, _ = self.routes(turns, seen, wm=wm)
        self.assertEqual((out["r1-t3-manager"][0]["status"], out["r1-t3-manager"][0]["how"],
                          out["r1-t3-manager"][0]["at"]), ("read", "cut in", "2026-01-01T10:03:30"))
        # the update went into the worker turn that was already running, which started before the check-in
        r = out["r1-t2-critic"][0]
        self.assertEqual((r["status"], r["turn"], r["how"]), ("read", "r1-t1-worker", "live"))


class TestAuditFixes(unittest.TestCase):
    """Cost basis, a gate-held manager message, and the audit digest's view of relays, sessions and notes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_cost_is_an_estimate_behind_another_providers_endpoint_or_for_an_unknown_model(self):
        from orch.report import cost_basis, endpoint_host, turn_cost_basis, turn_model
        self.assertEqual(endpoint_host({"ANTHROPIC_BASE_URL": "https://zenmux.ai/api/anthropic", "X_TOKEN": "s"}), "zenmux.ai")
        self.assertEqual(endpoint_host({}), "")
        self.assertEqual(cost_basis("claude", ["opus"]), "reported")
        self.assertEqual(cost_basis("claude", ["claude-sonnet-5-5"], "api.anthropic.com"), "reported")
        self.assertEqual(cost_basis("claude", ["opus"], "zenmux.ai"), "estimate")  # "opus" mapped to another model
        self.assertEqual(cost_basis("claude", ["deepseek/deepseek-v4.1-flash"]), "estimate")
        self.assertEqual(cost_basis("codex", ["gpt-6-astra"]), "reported")
        old = {"backend": "claude", "stderr": '[claude-code:unrecognized_model] {"model":"deepseek/deepseek-v4.1-flash"}'}
        self.assertEqual((turn_cost_basis(old), turn_model(old)), ("estimate", "deepseek/deepseek-v4.1-flash"))
        self.assertEqual(turn_model({"model": "opus", "meta": {"models": ["claude-opus-5-5"]}}), "claude-opus-5-5")

    def test_the_claude_backend_keeps_token_counts_and_the_models_that_answered(self):
        from orch.backends import ClaudeBackend, TurnRequest
        fake = self.tmp / "claude"
        fake.write_text("#!/bin/sh\ncat >/dev/null\necho '" + json.dumps(
            {"type": "result", "result": "hi", "session_id": "s1", "total_cost_usd": 1.5,
             "usage": {"input_tokens": 10, "output_tokens": 20, "service_tier": "standard"},
             "modelUsage": {"deepseek/deepseek-v4.1-flash": {"inputTokens": 10}}}) + "'\n")
        fake.chmod(0o755)
        res = ClaudeBackend().run(TurnRequest("w", "worker", "go", "", None, str(self.tmp), 30,
                                              {"executable": str(fake), "output_format": "json"}), lambda: False)
        self.assertEqual(res.meta["usage"], {"input_tokens": 10, "output_tokens": 20})
        self.assertEqual(res.meta["models"], ["deepseek/deepseek-v4.1-flash"])

    def test_the_run_keeps_the_estimated_part_of_its_cost_apart(self):
        from orch.recorder import Recorder
        mon = load_config(make_cfg(self.tmp, [managed()])).monitor("m")
        rec = Recorder.create(str(self.tmp / "out"), "m", mon.snapshot())
        for i, (basis, cost) in enumerate([("estimate", 5.0), ("reported", 0.5)], 1):
            rec.write_turn({"turn_id": f"r001-t0{i}-x", "round": 1, "seq": i, "agent": f"a{i}", "status": "ok",
                            "session_id": f"s{i}", "cost_basis": basis, "meta": {"total_cost_usd": cost}})
        self.assertEqual((rec.state["cost_usd"], rec.state["cost_estimate_usd"]), (5.5, 5.0))

    def test_a_manager_message_held_at_a_gate_is_shown_to_the_manager_and_marked_superseded(self):
        held = "Rerun the E4 insertion arm with the corrected span before anything else."
        cfg = make_cfg(self.tmp, [managed(controller={"milestone_rounds": 1}, critic={"pass_after": 99},
                                          mgr={"milestones": 1, "replies": {"review": [
                                              f'Not done.\n\n<message to="Jordan">{held}</message>\n\n'
                                              "<score>2</score>\n<decision>REVISE</decision>",
                                              "Good.\n\n<score>5</score>\n<decision>ACCEPT</decision>"]}})])
        out, stop, holder = self.tmp / "out" / "m", threading.Event(), {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg, stop=stop)), daemon=True)
        th.start()
        t0, g = time.time(), None
        while time.time() - t0 < 15 and not g:
            runs = sorted(out.glob("*/state.json")) if out.exists() else []
            g = (((read_json(runs[-1]) if runs else None) or {}).get("managed") or {}).get("gate")
            time.sleep(0.05)
        self.assertTrue(g, "no milestone gate")
        run_dir = runs[-1].parent
        (run_dir / "inbox" / "a-gate.json").write_text(json.dumps(
            {"kind": "gate", "decision": "revise", "message": "Use the 20 returned instances.", "gate": g["key"]}))
        th.join(20)
        status, rec = holder["res"]
        self.assertEqual(status, "completed")
        fb = next(t for t in turns(rec) if t["purpose"] == "feedback")
        self.assertIn("Use the 20 returned instances.", fb["input"])
        self.assertIn(held, fb["input"])  # the held message is put before the manager to carry over
        ev = [json.loads(l) for l in (rec.run_dir / "events.jsonl").read_text().splitlines() if "message_superseded" in l]
        self.assertEqual([(e["to"], e["by"]) for e in ev], [("critic", fb["turn_id"])])
        from orch.board import Board
        rows = Board("m", lambda: rec.run_dir).data()["routes"][ev[0]["turn"]]
        self.assertEqual((rows[0]["status"], rows[0]["turn"]), ("superseded", fb["turn_id"]))

    def test_the_digest_measures_relays_against_what_orch_passes_on(self):
        from orch.audit import passable
        critic = {"agent": "critic", "role": "verifier",
                  "output": "Fix the table.\n\n<checklist>\n- [ ] a\n- [ ] b\n</checklist>\n<verdict>FAIL</verdict>"
                            '\n<message to="you">Long answer for the human.</message>'}
        text, withheld = passable(critic, {"agent": "worker", "role": "worker"})
        self.assertEqual(text, "Fix the table.")
        self.assertEqual(set(withheld), {"checklist", "verdict", "message to you"})
        mgr = {"agent": "boss", "role": "manager", "output": 'Report for you.\n<message to="critic">Do X.</message>'}
        text, withheld = passable(mgr, {"agent": "critic", "role": "verifier"})
        self.assertEqual((text, withheld), ("Do X.", {"reply to the human": len("Report for you.")}))

    def test_the_digest_names_a_notes_sender_and_shows_the_session(self):
        from orch.audit import build
        rd = self.tmp / "run"
        (rd / "rounds" / "r001").mkdir(parents=True)
        (rd / "inbox" / "consumed").mkdir(parents=True)
        note = "Approved: run the revised labeller prompt as a re-pilot on twenty disjoint traces, and freeze it first."
        (rd / "inbox" / "consumed" / "20260101-095900-board-note.json").write_text(
            json.dumps({"to": ["writer"], "message": note, "from": "critic"}))
        (rd / "rounds" / "r001" / "t01-writer.json").write_text(json.dumps(
            {"turn_id": "r001-t01-writer", "round": 1, "agent": "writer", "role": "worker", "purpose": "work",
             "status": "ok", "started_at": "2026-01-01T10:00:00", "ended_at": "2026-01-01T10:01:00",
             "resumed_session": True, "session_turn": 7, "input": note, "output": "Done." * 100}))
        (rd / "rounds" / "r001" / "t02-critic.json").write_text(json.dumps(
            {"turn_id": "r001-t02-critic", "round": 1, "agent": "critic", "role": "verifier", "purpose": "verify",
             "status": "ok", "started_at": "2026-01-01T10:02:00", "ended_at": "2026-01-01T10:03:00",
             "input": "x", "output": "ok"}))
        md, stats = build(rd, 1, 1)
        self.assertIn("| resumed #7 |", md)
        self.assertIn("note from critic", md)
        self.assertFalse([f for f in stats["flags"] if f["kind"] == "unexpected origin"])


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _w1(self, rec):
        return [t for t in turns(rec) if t["agent"] == "w1"]

    def test_config_merges_backups_over_the_primary(self):
        m = pipeline()
        m["agents"][0].update(backend="claude", backend_options={"model": "opus", "effort": "high"},
                              env={"ANTHROPIC_BASE_URL": "https://a"}, retries=2,
                              fallback=["sonnet", {"backend_options": {"effort": None}, "env": {}},
                                        {"backend": "agy", "backend_options": {"model": "gemini-x"}, "retries": 0}])
        a = load_config(make_cfg(self.tmp, [m])).monitor("m").agent("w1")
        v = a.variants()
        self.assertEqual([x.label for x in v], ["claude:opus", "claude:sonnet", "claude:opus", "agy:gemini-x"])
        self.assertEqual(v[1].options, {"model": "sonnet", "effort": "high"})
        self.assertEqual(v[1].env, {"ANTHROPIC_BASE_URL": "https://a"})
        self.assertEqual(v[1].session_key, v[0].session_key)
        self.assertEqual(v[2].options, {"model": "opus"})
        self.assertNotEqual(v[2].session_key, v[0].session_key)  # another endpoint: a new session
        self.assertEqual((v[3].options, v[3].env, v[3].retries, v[2].retries), ({"model": "gemini-x"}, {}, 0, 2))
        self.assertEqual(a.public()["fallback"][0]["env"], ["ANTHROPIC_BASE_URL"])  # no env values in logs

    def test_bad_backup_is_a_config_error(self):
        m = pipeline()
        m["agents"][0]["fallback"] = [{"backend": "nope"}]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))
        m["agents"][0]["fallback"] = [{"modle": "x"}]
        with self.assertRaises(ConfigError):
            load_config(make_cfg(self.tmp, [m]))

    def test_backup_takes_over_in_the_same_session_and_stays_for_the_cooldown(self):
        m = pipeline()
        msg = "API Error: overloaded_error"
        m["agents"][0].update(backend_options={"fail_turns": [2], "error_text": msg},
                              fallback=[{"label": "backup", "backend_options": {"fail_turns": []}}])
        m["agents"][1]["backend_options"] = {"pass_after": 3}
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        w = self._w1(rec)
        self.assertEqual(w[1]["status"], "ok")
        self.assertEqual([a["model"] for a in w[1]["attempts"]], ["mock", "backup"])
        self.assertEqual(w[1]["fallbacks"][0]["to"], "backup")
        self.assertEqual(w[1]["session_id"], w[0]["session_id"])  # same provider: the session carries on
        self.assertEqual(w[2]["model"], "backup")  # still within the cooldown
        self.assertEqual(len(w[2]["attempts"]), 1)

    def test_backup_on_another_provider_starts_a_new_session_with_a_handover(self):
        m = pipeline()
        m["agents"][0].update(backend_options={"fail_turns": [2]}, fallback_cooldown=0,
                              fallback=[{"backend_options": {"fail_turns": []}, "env": {"ANTHROPIC_BASE_URL": "x"}}])
        m["agents"][1]["backend_options"] = {"pass_after": 3}
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        w = self._w1(rec)
        self.assertNotEqual(w[1]["session_id"], w[0]["session_id"])
        self.assertIn("taking over this work", w[1]["input"])
        self.assertEqual(w[2]["model"], "mock")  # no cooldown: back on the primary
        self.assertNotEqual(w[2]["session_id"], w[1]["session_id"])  # whose provider can't continue the backup's

    def test_all_models_failing_still_fails_the_turn(self):
        m = pipeline()
        m["agents"][0].update(backend_options={"fail_turns": [2, 3]}, fallback=["other"])
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "failed")
        self.assertEqual([a["model"] for a in self._w1(rec)[1]["attempts"]], ["mock", "mock:other"])

    def test_audit_helper_tries_backups(self):
        from orch.backends import TurnRequest, run_variants
        m = pipeline()
        m["agents"][0].update(backend_options={"fail_turns": [1]}, fallback=[{"backend_options": {"fail_turns": []}}])
        a = load_config(make_cfg(self.tmp, [m])).monitor("m").agent("w1")
        res = run_variants(a, TurnRequest("w1", "worker", "hi", "", None, str(self.tmp), 10, {}), lambda: False)
        self.assertEqual(res.status, "ok")
        self.assertEqual(len(res.meta["models_tried"]), 2)


class TestAgy(unittest.TestCase):
    def test_command(self):
        from orch.backends import AgyBackend, TurnRequest
        req = TurnRequest("a", "worker", "hi", "sys", "c-1", ".", 10,
                          {"model": "gemini-3.6-pro", "effort": "high", "mode": "auto", "add_dirs": ["/x"],
                           "dangerously_skip_permissions": True})
        cmd = AgyBackend().build_command(req)
        self.assertEqual(cmd[:5], ["agy", "--output-format", "json", "--conversation", "c-1"])
        for part in (["--model", "gemini-3.6-pro"], ["--effort", "high"], ["--mode", "auto"], ["--add-dir", "/x"]):
            self.assertIn(part[1], cmd[cmd.index(part[0]):])
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_run_with_a_fake_agy(self):
        from orch.backends import AgyBackend, TurnRequest
        d = Path(tempfile.mkdtemp())
        exe = d / "agy"
        exe.write_text("#!/bin/sh\necho '{\"type\":\"init\",\"conversation_id\":\"conv-9\"}'\n"
                       "echo '{\"type\":\"result\",\"result\":\"done: '\"$#\"' args\"}'\n")
        exe.chmod(0o755)
        res = AgyBackend().run(TurnRequest("a", "worker", "hello", "be brief", None, str(d), 10,
                                           {"executable": str(exe)}), lambda: False)
        self.assertEqual((res.status, res.session_id), ("ok", "conv-9"))
        self.assertTrue(res.output.startswith("done"))
        self.assertIn("<prompt>", res.command)  # the prompt (with the system prompt in front) is not logged

    def test_unparsable_output_is_an_error(self):
        from orch.backends import AgyBackend, TurnRequest
        res = AgyBackend().run(TurnRequest("a", "worker", "hi", "", None, ".", 10,
                                           {"executable": "echo"}), lambda: False)
        self.assertEqual(res.status, "error")


class TestManagerChatSession(unittest.TestCase):
    """A board chat with the critic must not move the manager into the critic's session."""

    def test_only_the_managers_own_chat_replies_set_its_session(self):
        from orch.board import manager_chat_session
        st = {"sessions": {"manager": {"session_id": "M1"}, "critic": {"session_id": "C1"}}}
        rows = [{"role": "manager", "session_id": "M2", "ts": "2026-01-01T10:00:00"},
                {"role": "agent", "who": "critic", "session_id": "C1", "ts": "2026-01-01T11:00:00"}]
        self.assertEqual(manager_chat_session(rows, "", st, "manager"), "M2")
        self.assertIsNone(manager_chat_session(rows, "2026-01-01T10:30:00", st, "manager"))
        # a manager row that names another agent's session (left by the old bug) is not followed either
        rows.append({"role": "manager", "session_id": "C1", "ts": "2026-01-01T12:00:00"})
        self.assertEqual(manager_chat_session(rows, "", st, "manager"), "M2")


def _plan(*done):
    steps = ["STEP-ALPHA validate the first label", "STEP-BETA validate the second label", "STEP-GAMMA redo the table"]
    return "<plan>\n" + "\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in zip(done, steps)) + "\n</plan>"


def _review(prose, plan, ck_done, verdict):
    return (f"{prose}\n\n{plan}\n<checklist>\n- [{'x' if ck_done else ' '}] the step's result is checked\n"
            f"- [x] protected files unchanged\n</checklist>\n<verdict>{verdict}</verdict>")


class TestCriticPlan(unittest.TestCase):
    """The critic keeps a <plan> and hands the worker one step at a time: the worker never sees the plan, a PASS
    needs every step done, a finished step resets the stall limit, and check-ins come from the plan."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def critic_replies(self):
        return {"brief": ["Start with the first label: validate it on held-out cases.\n\n" + _plan(0, 0, 0)
                          + "\n<checklist>\n- [ ] the step's result is checked\n</checklist>"],
                "verify": [_review("The first label holds. Next: validate the second label.", _plan(1, 0, 0), False, "FAIL"),
                           _review("Not there yet: the held-out cases are missing.", _plan(1, 0, 0), False, "FAIL"),
                           _review("The second label holds. Next: redo the table.", _plan(1, 1, 0), False, "FAIL"),
                           _review("Looks done to me.", _plan(1, 1, 0), True, "PASS"),
                           _review("The table is redone and checked.", _plan(1, 1, 1), True, "PASS")]}

    def test_steps_reset_the_stall_limit_and_pass_needs_every_step(self):
        m = managed(controller={"milestone_rounds": 2}, critic={"replies": self.critic_replies()})
        status, rec = run(make_cfg(self.tmp, [m]))
        ts = turns(rec)
        ev = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]
        steps = [e for e in ev if e["event"] == "plan_step"]
        self.assertEqual([(e["done"], e["total"]) for e in steps][:3], [(1, 3), (2, 3), (3, 3)])
        verifies = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "verify" and t.get("milestone") == 0]
        self.assertEqual(len(verifies), 5)  # 5 rounds with milestone_rounds 2: progress kept resetting the limit
        self.assertTrue(any(t["purpose"] == "report" for t in ts if t["agent"] == "critic"))
        rounds = [read_json(p) for p in sorted((rec.run_dir / "rounds").glob("r*/round.json"))]
        self.assertTrue(any(r.get("plan_open") == 1 for r in rounds))  # the PASS with a step open was overruled
        self.assertFalse(any("limit" in str(r.get("decision")) for r in rounds))
        for t in ts:  # the worker never sees the plan or the checklist
            if t["agent"] == "writer":
                self.assertNotIn("STEP-", t["input"])
                self.assertNotIn("<plan>", t["input"])
                self.assertNotIn("protected files unchanged", t["input"])
        nudged = verifies[4]["input"]
        self.assertIn("plan still has open steps", nudged)
        self.assertIn("STEP-GAMMA", nudged)
        review = next(t for t in ts if t["agent"] == "boss" and t["purpose"] == "review")
        self.assertIn("3 of 3 steps done", review["input"])
        self.assertNotIn("<plan>", review["input"])
        self.assertEqual(status, "completed")

    def test_without_progress_the_stall_limit_still_stops_the_milestone(self):
        stuck = _review("Still missing the held-out cases.", _plan(0, 0, 0), False, "FAIL")
        m = managed(controller={"milestone_rounds": 2},
                    critic={"replies": {"brief": [self.critic_replies()["brief"][0]], "verify": [stuck]}})
        status, rec = run(make_cfg(self.tmp, [m]))
        ts = turns(rec)
        report = next(t for t in ts if t["agent"] == "critic" and t["purpose"] == "report")
        self.assertIn("2 rounds have passed without a step of your plan finishing", report["input"])
        self.assertEqual(len([t for t in ts if t["agent"] == "critic" and t["purpose"] == "verify" and t.get("milestone") == 0]), 2)

    def test_checkins_come_from_the_plan_without_model_turns(self):
        m = managed(controller={"checkin_every": 1, "milestone_rounds": 9}, critic={"replies": self.critic_replies()})
        status, rec = run(make_cfg(self.tmp, [m]))
        ts = turns(rec)
        self.assertFalse([t for t in ts if t["purpose"] in ("checkin", "ack")])
        ev = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]
        lines = [e["text"] for e in ev if e["event"] == "checkin"]
        self.assertTrue(lines and all(e.get("how") == "plan" for e in ev if e["event"] == "checkin"))
        self.assertIn("step 2 of 3", lines[0])
        review = next(t for t in ts if t["agent"] == "boss" and t["purpose"] == "review")
        self.assertEqual(review["input"].count("(orch, from the critic's plan)"), 1)  # only the newest one waits

    def test_a_restart_keeps_the_plan_and_rebuilds_the_checklist(self):
        m = managed(controller={"milestone_rounds": 9}, critic={"replies": self.critic_replies()})
        cfg = make_cfg(self.tmp, [m])
        _status, rec = run(cfg)
        r = MonitorRunner(load_config(cfg).monitor("m"), threading.Event(), log=lambda s: None)
        r.rec = Recorder(rec.run_dir, rec.output_root)
        r.mg["ms"] = 1  # as if the last milestone were still running
        plan = r.latest_plan()
        self.assertIn("- [x] STEP-GAMMA", plan)
        top = r.handover(r.critic, top_down=True)
        self.assertIn("Your plan so far", top)
        self.assertIn("write a new one for the step you hand out next", top)
        self.assertNotIn("keep the items as they are", top)
        same = r.handover(r.critic)
        self.assertIn("STEP-ALPHA", same)  # from the critic's state file (plan section) or the records
        r.write_critic_state()
        self.assertIn("## Your plan", r.read_state(r.critic)[0])
        r.mg["ms"] = 2  # a new milestone starts without the old plan
        self.assertEqual(r.latest_plan(), "")
        r.rec.release()

    def test_plan_lines_are_not_checklist_items_and_never_reach_the_worker(self):
        from orch.managed import checklist_of, plan_status, plan_text
        from orch.report import checklist_items, review_checklist
        out = _review("Next: the second label.", _plan(1, 0, 0), False, "FAIL")
        self.assertEqual(checklist_of(out), (1, 1))
        self.assertEqual(plan_status(plan_text(out)), (1, 3, "STEP-BETA validate the second label"))
        self.assertEqual(len(checklist_items(review_checklist(out))), 2)
        untagged = "Review.\n" + _plan(1, 0, 0) + "\n- [x] one item\n<verdict>FAIL</verdict>"
        self.assertEqual(checklist_of(untagged), (1, 0))  # a forgotten <checklist> tag: still not the plan's lines
        cfg = load_config(make_cfg(self.tmp, [managed()]))
        r = MonitorRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
        self.assertNotIn("STEP-", r.clean_critic(out))
        self.assertIn("Next: the second label.", r.clean_critic(out))


class TestOneControllerPerRun(unittest.TestCase):
    """A second `orch run --resume` must not drive a run another process is driving, and a resume keeps the
    run's own round limit unless --max-rounds says otherwise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_second_controller_backs_off_without_touching_the_run(self):
        m = pipeline(max_rounds=2)
        m["agents"][1]["backend_options"] = {"pass_after": 99}
        cfg = make_cfg(self.tmp, [m])
        status, rec = run(cfg)
        self.assertEqual(status, "max_rounds_reached")
        holder = Recorder(rec.run_dir, rec.output_root)
        self.assertIsNone(holder.claim())  # stands in for the other process
        self.assertIsNone(holder.claim())  # idempotent
        before = (rec.run_dir / "events.jsonl").read_text(), (rec.run_dir / "state.json").read_text()
        status, _ = run(cfg, resume=True)
        self.assertEqual(status, "busy")
        self.assertEqual(before, ((rec.run_dir / "events.jsonl").read_text(), (rec.run_dir / "state.json").read_text()))
        holder.release()
        status, _ = run(cfg, resume=True)
        self.assertEqual(status, "max_rounds_reached")
        later = Recorder(rec.run_dir, rec.output_root)
        self.assertIsNone(later.claim())  # released when the run ended
        later.release()

    def test_a_resume_keeps_the_runs_own_round_limit(self):
        m = pipeline(max_rounds=2)
        m["agents"][1]["backend_options"] = {"pass_after": 99}
        cfg = make_cfg(self.tmp, [m])
        mon = load_config(cfg).monitor("m")
        r = MonitorRunner(dataclasses.replace(mon, max_rounds=3), threading.Event(), log=lambda s: None,
                          max_rounds_given=True)
        self.assertEqual(r.run(), "max_rounds_reached")
        self.assertEqual(r.rec.state["round"], 3)
        self.assertEqual(r.rec.state["max_rounds"], 3)
        # raised to 5 for this run, then resumed without the flag: the config's 2 must not end it
        r = MonitorRunner(dataclasses.replace(mon, max_rounds=5), threading.Event(), resume=True,
                          log=lambda s: None, max_rounds_given=True)
        self.assertEqual(r.run(), "max_rounds_reached")
        self.assertEqual(r.rec.state["round"], 5)
        rec = r.rec
        rec.set(status="stopped", round=4, completed_round=4)
        status, rec = run(cfg, resume=True)
        self.assertEqual((status, rec.state["max_rounds"]), ("max_rounds_reached", 5))
        self.assertEqual(rec.state["round"], 5)
        # an explicit --max-rounds lowers it
        r = MonitorRunner(dataclasses.replace(mon, max_rounds=1), threading.Event(), resume=True,
                          log=lambda s: None, max_rounds_given=True)
        r.run()
        self.assertEqual(r.rec.state["max_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
