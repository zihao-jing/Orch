"""B5: progress checks. A monitor (or the critic's side session) looks at a running worker turn or job every few
minutes and reports evidence to the critic; the critic is woken only for problems; no rounds are spent polling a
job; a safety limit replaces the flat turn timeout; the board gets a live status line."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import progress as P  # noqa: E402
from orch.config import AgentConfig, load_config  # noqa: E402
from orch.managed import ManagedRunner  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

EVERY = 0.005  # minutes: a check every 0.3 s
FINE = "item 1: done, out.csv has 120 rows\nlog last written 0 min ago\n<status>ok</status>"
QUIET = "item 1: out.csv still has 0 rows\nlog quiet for 25 min\n<status>quiet</status>"


def _events(rec, kind=None):
    evs = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]
    return [e for e in evs if kind is None or e.get("event") == kind]


def _cfg(tmp, monitor=None, worker=None, critic=None, mgr=None, worker_cfg=None, controller=None):
    m = managed(controller=controller, mgr={"milestones": 1, **(mgr or {})}, critic=critic,
                worker={"delays": {"work": 1.5}, "activity": True, **(worker or {})})
    m["agents"][2].update({"check_every": EVERY, **(worker_cfg or {})})
    if monitor is not None:
        m["agents"].append({"name": "monitor", "role": "monitor", "backend_options": monitor})
    return make_cfg(tmp, [m])


class TestProgressUnit(unittest.TestCase):
    def test_status_and_report_cleaning(self):
        self.assertEqual(P.parse_status("x\n<status>off-track</status>"), ("off-track", True))
        self.assertEqual(P.parse_status("x <status>died</status>"), ("stopped", True))
        self.assertEqual(P.parse_status("log quiet for 25 min"), ("quiet", False))
        self.assertEqual(P.parse_status("rows growing"), ("ok", False))
        text, flags = P.clean_report("item 1: PASS, out.csv has 120 rows\nVERDICT: FAIL\n<verdict>PASS</verdict>\n"
                                     "the runner under .orch/runs/x is up\nlog active\n<status>ok</status>")
        self.assertEqual(text, "item 1: out.csv has 120 rows\n\nlog active")
        self.assertFalse(P.has_verdict(text))
        self.assertEqual(len(flags), 4)

    def test_turn_limit_semantics(self):
        a = AgentConfig(name="w", role="worker", timeout=7200)
        self.assertEqual(a.turn_limit(watched=True), 28800)  # a watched turn: the safety limit, not the old 2 h
        self.assertEqual(a.turn_limit(watched=False), 7200)  # nobody watches it: `timeout` as before
        a.safety_timeout = 600
        self.assertEqual((a.turn_limit(True), a.turn_limit(False)), (600, 600))


class TestProgressRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_monitor_checks_a_long_turn_and_fine_reports_wait_for_the_review(self):
        cfg = _cfg(self.tmp, monitor={"replies": {"check": [FINE]}})
        seen = []
        orig = ManagedRunner.end_watch

        def spy(self_, w):  # the board's status line, as it stood when the turn ended
            seen.append(dict((self_.st.get("status_lines") or {}).get(w.name) or {}))
            return orig(self_, w)

        with mock.patch.object(ManagedRunner, "end_watch", spy):
            status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        first_review = next(t for t in ts if t["agent"] == "critic" and t["purpose"] == "verify")
        checks = [t for t in ts if t["agent"] == "monitor" and t["purpose"] == "check"
                  and t["round"] == first_review["round"]]
        self.assertGreaterEqual(len(checks), 3)  # every 0.3 s of a 1.5 s turn
        self.assertTrue(all(t.get("watching") == "writer" and t.get("watch_kind") == "turn" for t in checks))
        self.assertIn("Quick check: how is the run going?", checks[0]["input"])
        self.assertIn("Latest activity in the turn's output", checks[-1]["input"])
        # all fine: no extra critic turn; the newest report waits for the review, the older ones are replaced
        self.assertFalse([t for t in ts if t["agent"] == "critic" and t["purpose"] == "incoming"])
        self.assertEqual(first_review["input"].count("Progress check,"), 1)
        self.assertIn("out.csv has 120 rows", first_review["input"])
        msgs = [m for m in rec.state["messages"] if m.get("kind") == "progress_check"]
        self.assertEqual({m["status"] for m in msgs}, {"superseded", "delivered"})
        self.assertTrue(all(m["to"] == "critic" and m["from"] == "monitor" for m in msgs))
        # the board's live line, and it goes when the turn ends
        self.assertTrue(seen and seen[0]["text"].startswith("Sam: "), seen[:1])
        self.assertIn("running", seen[0]["text"])
        self.assertIn("log active", seen[0]["text"])
        self.assertEqual(rec.state.get("status_lines"), {})
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_verdict_words_and_tooling_are_kept_from_the_critic(self):
        verdict = "item 1: PASS, out.csv has 120 rows\nVERDICT: PASS\nsaw .orch/runs/x/state.json\n<status>ok</status>"
        status, rec = run(_cfg(self.tmp, monitor={"replies": {"check": [verdict]}}))
        self.assertEqual(status, "completed")
        msgs = [m for m in rec.state["messages"] if m.get("kind") == "progress_check"]
        self.assertTrue(msgs)
        for m in msgs:
            self.assertFalse(P.has_verdict(m["text"]), m["text"])
            self.assertNotIn(".orch", m["text"])
            self.assertIn("out.csv has 120 rows", m["text"])
        flags = _events(rec, "check_flag")
        self.assertTrue(any("verdict" in f["problem"] for f in flags))
        self.assertTrue(any("tooling" in f["problem"] for f in flags))

    def test_a_quiet_run_wakes_the_critic_who_messages_the_worker(self):
        critic = {"replies": {"incoming": ['<message to="Sam">Your log has been quiet for a while; check whether '
                                           'train.py is still running and tell me.</message>']}}
        cfg = _cfg(self.tmp, monitor={"replies": {"check": [QUIET, FINE]}}, critic=critic, worker={"delays": {"work": 1.2}})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        woke = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "incoming"]
        self.assertTrue(woke)
        self.assertIn("Progress check,", woke[0]["input"])
        self.assertIn("log quiet for 25 min", woke[0]["input"])
        self.assertIn("found something that may need you", woke[0]["input"])
        # the critic's message reaches the worker; nothing of this goes up to the manager
        self.assertTrue(any("log has been quiet" in w["message"] for w in rec.state.get("worker_messages") or []))
        self.assertFalse([m for m in rec.state["messages"] if m["from"] == "critic" and m["to"] == "boss"
                          and m.get("purpose") == "incoming"])
        self.assertTrue(any(e.get("wakes") for e in _events(rec, "check_report")))
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_a_silent_turn_is_noticed_within_one_interval_even_if_the_monitor_says_ok(self):
        cfg = _cfg(self.tmp, monitor={"replies": {"check": [FINE]}}, worker={"delays": {"work": 0.9}, "activity": False})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        first = next(t for t in ts if t["purpose"] == "check")
        self.assertIn("Nothing written in the turn's output", first["input"])
        self.assertTrue(any(e["status"] == "quiet" and e.get("wakes") for e in _events(rec, "check_report")))
        self.assertTrue([t for t in ts if t["agent"] == "critic" and t["purpose"] == "incoming"])

    def test_without_a_monitor_the_critic_checks_in_a_side_session(self):
        cfg = _cfg(self.tmp, critic={"replies": {"check": [FINE]}})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        side = [t for t in ts if t["agent"] == "critic-check"]
        main = [t for t in ts if t["agent"] == "critic"]
        self.assertGreaterEqual(len(side), 2)
        self.assertTrue(all(t["role"] == "monitor" and t["purpose"] == "check" for t in side))
        self.assertIn("# Quick checks", side[0]["system_prompt"])  # the monitor's protocol, not the critic's
        self.assertEqual(len({t["session_id"] for t in side}), len(side))  # a fresh session for each check
        self.assertEqual(len({t["session_id"] for t in main}), 1)  # the critic's own conversation is untouched
        self.assertFalse(set(t["session_id"] for t in side) & set(t["session_id"] for t in main))
        review = next(t for t in main if t["purpose"] == "verify")
        self.assertIn("out.csv has 120 rows", review["input"])
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_the_critic_can_check_in_its_own_session(self):
        m = managed(mgr={"milestones": 1}, critic={"replies": {"check": [
            '<message to="Sam">train.log has been quiet; check that the run is alive.</message>', "All moving."]}},
            worker={"delays": {"work": 1.2}, "activity": True})
        m["agents"][1]["check_session"] = "own"
        m["agents"][2]["check_every"] = EVERY
        cfg = make_cfg(self.tmp, [m])
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        checks = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "check"]
        self.assertTrue(checks)
        self.assertEqual(len({t["session_id"] for t in ts if t["agent"] == "critic"}), 1)
        self.assertIn("Quick check: how is the run going?", checks[0]["input"])
        self.assertTrue(any("train.log has been quiet" in w["message"] for w in rec.state.get("worker_messages") or []))
        self.assertFalse([x for x in rec.state["messages"] if x.get("kind") == "progress_check"])  # it read it itself
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_checks_watch_a_job_without_polling_rounds(self):
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", "sleep 1.73"]))
        worker = {"delays": {}, "spawn": "sleep 1.73",
                  "replies": {"work": ["Started the full run in the background.", "The run finished; out.csv is in."]}}
        status, rec = run(_cfg(self.tmp, monitor={"replies": {"check": [FINE]}}, worker=worker))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        seq = [(t["agent"], t["purpose"]) for t in ts if t["purpose"] in ("work", "verify", "incoming")]
        self.assertEqual(seq[:4], [("writer", "work"), ("critic", "verify"), ("writer", "work"), ("critic", "verify")])
        job_checks = [t for t in ts if t["purpose"] == "check" and t.get("watch_kind") == "job"]
        self.assertGreaterEqual(len(job_checks), 2)
        self.assertIn("background job", job_checks[0]["input"])
        self.assertIn("sleep 1.73", job_checks[0]["input"])
        self.assertIn("has finished", [t for t in ts if t["purpose"] == "work"][1]["input"])

    def test_safety_limit_replaces_the_flat_timeout(self):
        # an older `timeout` no longer cuts a watched worker turn ...
        status, rec = run(_cfg(self.tmp, worker={"delays": {"work": 0.8}, "activity": False},
                               worker_cfg={"timeout": 0.3}))
        self.assertEqual(status, "completed")
        self.assertTrue(all(t["status"] == "ok" for t in turns(rec) if t["agent"] == "writer"))
        # ... the safety limit does
        (self.tmp / "b").mkdir()
        cfg = _cfg(self.tmp / "b", worker={"delays": {"work": 5}}, worker_cfg={"safety_timeout": 0.4},
                   controller={"stop_on_error": True})
        holder = {}
        th = threading.Thread(target=lambda: holder.update(res=run(cfg)), daemon=True)
        th.start()
        th.join(60)
        status, rec = holder["res"]
        w = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertEqual(w[0]["status"], "timeout")
        self.assertIn("ran past the time limit", w[1]["input"])
        self.assertTrue(_events(rec, "safety_timeout"))

    def test_manager_gets_a_status_line_at_its_own_interval(self):
        m = managed(mgr={"milestones": 1}, worker={"delays": {"work": 1.5}, "activity": True})
        m["agents"][2]["check_every"] = EVERY
        m["agents"][0]["check_every"] = 0.01  # 0.6 s
        m["agents"].append({"name": "monitor", "role": "monitor", "backend_options": {"replies": {"check": [FINE]}}})
        status, rec = run(make_cfg(self.tmp, [m]))
        self.assertEqual(status, "completed")
        self.assertTrue(_events(rec, "manager_check"))
        review = next(t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "review")
        self.assertEqual(review["input"].count("(orch, progress check)"), 1)  # only the newest waits
        self.assertIn("Sam: ", review["input"])
        self.assertFalse([t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "check"])  # no turn

    def test_checks_off(self):
        status, rec = run(_cfg(self.tmp, monitor={"replies": {"check": [FINE]}}, worker_cfg={"check_every": 0}))
        self.assertEqual(status, "completed")
        self.assertFalse([t for t in turns(rec) if t["purpose"] == "check"])


if __name__ == "__main__":
    unittest.main()
