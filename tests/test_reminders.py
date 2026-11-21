"""B4: short reminders at intervals from a registry in the config; no fixed reminder on every turn."""

import json
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import audit  # noqa: E402
from orch import reminders as R  # noqa: E402
from orch.config import ConfigError, load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402
from orch.templates import render  # noqa: E402
from role_view_checks import assert_no_role_leaks, leaks  # noqa: E402
from test_config_reload import FakeRec  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

OLD_ROUTING = "If I asked you a question or requested an explanation"  # the fixed block on every critic review


def sends(rem, n, rng=None, state=None):
    state = {} if state is None else state
    return [i for i in range(1, n + 1) if R.due(state, rem, rng or random.Random(1))]


class TestSchedule(unittest.TestCase):
    def test_every_n(self):
        self.assertEqual(sends({"id": "a", "every": 3}, 10), [3, 6, 9])
        self.assertEqual(sends({"id": "a", "every": 1}, 3), [1, 2, 3])

    def test_random_gaps_stay_in_range(self):
        rem = {"id": "p", "random": [3, 10]}
        for seed in range(30):
            rng = random.Random(seed)
            state = {}
            hits = [i for i in range(1, 200) if R.due(state, rem, rng)]
            gaps = [b - a for a, b in zip([0] + hits, hits)]
            self.assertTrue(gaps and all(3 <= g <= 10 for g in gaps), gaps)
        self.assertEqual(sends({"id": "p", "random": [4, 4]}, 12), [4, 8, 12])

    def test_counters_carry_on_after_a_save_and_load(self):
        rem = {"id": "a", "every": 4}
        state = {}
        first = [i for i in range(1, 6) if R.due(state, rem)]
        state = json.loads(json.dumps(state))  # the run state is saved as JSON and read back on resume
        later = [i for i in range(6, 13) if R.due(state, rem)]
        self.assertEqual(first + later, [4, 8, 12])

    def test_changed_schedule_counts_from_now(self):
        state = {}
        sends({"id": "a", "every": 5}, 3, state=state)
        self.assertEqual([i for i in range(4, 10) if R.due(state, {"id": "a", "every": 2})], [5, 7, 9])

    def test_strip(self):
        text = "Fix the labels, then reply.\n\n<system-reminder>\nNight mode is on.\n</system-reminder>\n"
        self.assertEqual(R.strip(text), "Fix the labels, then reply.")
        self.assertEqual(R.strip("a\n\n" + R.wrap("x") + "\n\nb"), "a\n\nb")


def cfg_with(tmp, controller):
    return make_cfg(tmp, [managed(controller=controller, mgr={"milestones": 1})])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def rems(self, controller):
        return {r["id"]: r for r in load_config(cfg_with(self.tmp, controller)).monitor("m").controller.reminders}

    def test_defaults(self):
        r = self.rems({"tag_reminder": None})
        self.assertEqual(set(r), {"concise", "protocol", "report_length", "chat_voice", R.STYLE_RULES, R.GOLDEN_BOARD})
        self.assertEqual(r["protocol"]["random"], [3, 10])
        self.assertEqual(r["report_length"]["to"], ["worker"])
        self.assertEqual(r["report_length"]["every"], 3)
        for rem in r.values():  # one or two sentences (the style reminder has no fixed text)
            for t in (rem["text"].values() if isinstance(rem["text"], dict) else [rem["text"]]):
                self.assertLess(len(t), 220)

    def test_overrides_and_new_ones(self):
        r = self.rems({"tag_reminder": None, "reminders": {
            "concise": {"every": 4}, "report_length": False,
            "cite": {"text": "Name the file behind every number.", "to": ["worker", "critic"], "random": [2, 5]}}})
        self.assertEqual(r["concise"]["every"], 4)
        self.assertNotIn("random", r["concise"])
        self.assertIn("manager", r["concise"]["text"])  # the text is kept
        self.assertNotIn("report_length", r)
        self.assertEqual(r["cite"]["to"], ["worker", "critic"])
        self.assertEqual(r["cite"]["random"], [2, 5])

    def test_all_off_and_tag_reminder_compatibility(self):
        self.assertEqual(self.rems({"reminders": False}), {})
        self.assertNotIn("protocol", self.rems({"tag_reminder": 0}))
        self.assertEqual(self.rems({"tag_reminder": 1})["protocol"]["every"], 1)
        self.assertEqual(self.rems({"tag_reminder": 0.2})["protocol"]["random"], [2, 8])
        # an explicit schedule wins over the older chance
        self.assertEqual(self.rems({"tag_reminder": 0, "reminders": {"protocol": {"every": 7}}})["protocol"]["every"], 7)

    def test_bad_settings_are_refused(self):
        for bad in ({"x": {"text": "t", "every": 2, "random": [1, 3]}}, {"x": {"text": "t", "random": [5, 2]}},
                    {"x": {"text": "t", "every": 0}}, {"x": {"every": 3}}, {"x": {"text": "t", "when": 3}}, ["a"]):
            with self.assertRaises(ConfigError, msg=bad):
                self.rems({"reminders": bad})

    def test_old_tag_reminder_template_overrides_still_load(self):
        mon = load_config(cfg_with(self.tmp, {"templates": {"tag_reminder_critic": "old text"}})).monitor("m")
        self.assertNotIn("tag_reminder_critic", mon.controller.templates)

    def test_hot_reload_of_a_changed_reminder(self):
        path = Path(cfg_with(self.tmp, {"reminders": {"concise": {"every": 2}}}))
        mon = load_config(str(path)).monitor("m")
        r = MonitorRunner(mon, threading.Event(), log=lambda s: None)
        r.rec = FakeRec()
        r.rec.state = {"sessions": {}, "fwd": {}}
        cfg = json.loads(path.read_text())
        cfg["monitors"][0]["controller"]["reminders"] = {"concise": {"every": 2, "text": "Short, please."}}
        path.write_text(json.dumps(cfg))
        r.refresh_agent(mon.agents[2])
        rem = next(x for x in mon.controller.reminders if x["id"] == "concise")
        self.assertEqual(rem["text"], "Short, please.")
        self.assertEqual(r.rec.events[-1], ("reminders_reloaded", {"changed": ["concise"]}))
        r.rec.events.clear()
        r.refresh_agent(mon.agents[2])  # no change, nothing reported
        self.assertEqual(r.rec.events, [])


class TestManagedRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_no_fixed_reminder_on_consecutive_critic_reviews(self):
        status, rec = run(make_cfg(self.tmp, [managed(controller={"tag_reminder": None},
                                                      mgr={"milestones": 1}, critic={"pass_after": 5})]))
        self.assertEqual(status, "completed")
        reviews = [t for t in turns(rec) if t["agent"] == "critic" and t["purpose"] == "verify"]
        self.assertGreaterEqual(len(reviews), 4)
        for t in reviews:
            self.assertNotIn(OLD_ROUTING, t["input"])
            self.assertNotIn("reply_routing", t.get("reminders") or [])
        # the critic's first two turns (brief, first review) come before the 3+ turn reminders are due; only the
        # style reminder (every 2-3 turns) can be there by the first review
        self.assertFalse(set(reviews[0].get("reminders") or []) - {"style_rules", "communication_protocol"})
        self.assertLess(sum(bool(t.get("reminders")) for t in reviews), len(reviews))  # not on every review
        sent = [t.get("reminders") or [] for t in turns(rec)]
        self.assertTrue(any(sent))  # some reminders do go out, at intervals
        for t in turns(rec):
            if t.get("reminders"):
                self.assertTrue(t["input"].rstrip().endswith("</system-reminder>"))  # at the end of the message
                self.assertEqual(t["input"].count("<system-reminder>"), len(t["reminders"]))
        worker = [t for t in turns(rec) if t["agent"] == "writer"]
        self.assertIn("report_length", worker[2].get("reminders") or [])  # every 3rd worker turn
        self.assertIn("Keep the report short; put details in files.", worker[2]["input"])
        self.assertNotIn("report_length", (worker[0].get("reminders") or []) + (worker[1].get("reminders") or []))
        # the counters are kept in the run state, per agent
        st = json.loads((rec.run_dir / "state.json").read_text())
        self.assertEqual(st["reminders"]["writer"]["report_length"]["n"], len(worker))

    def test_counters_survive_resume(self):
        cfg_path = make_cfg(self.tmp, [managed(mgr={"milestones": 1})])
        status, rec = run(cfg_path)
        from orch.managed import ManagedRunner
        saved = json.loads((rec.run_dir / "state.json").read_text())
        n = saved["reminders"]["writer"]["report_length"]["n"]
        r = ManagedRunner(load_config(cfg_path).monitor("m"), threading.Event(), resume=True, log=lambda s: None)
        rec.state = saved
        r.rec = rec
        sent = [r.add_reminders(r.workers[0], "Do the step.", "work")[1] for _ in range(3)]
        self.assertEqual(rec.state["reminders"]["writer"]["report_length"]["n"], n + 3)
        self.assertEqual(sum("report_length" in s for s in sent), 1)  # every 3rd, counted on from the saved count

    def test_reminders_never_name_a_layer_above(self):
        names = {"critic": "Jordan", "worker": "Sam"}
        for rem in R.parse(None):
            for role, above in (("critic", ["Alex"]), ("worker", ["Alex", "Jordan"])):
                text = render(R.text_for(rem, role), **names)
                self.assertFalse(leaks(text, {"critic": "verifier"}.get(role, role), above), (rem["id"], role, text))
        every = {k: {"every": 1} for k in ("concise", "protocol", "report_length")}
        status, rec = run(make_cfg(self.tmp, [managed(
            controller={"reminders": every, "checkin_every": 1},
            mgr={"milestones": 1, "replies": {"ack": ['<message to="Jordan">Please explain the table to me.</message>']}},
            critic={"pass_after": 3})]))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertTrue(any("reply_routing" in (t.get("reminders") or []) for t in ts))
        self.assertTrue(all("concise" in (t.get("reminders") or []) for t in ts if t["role"] != "auditor"))
        assert_no_role_leaks(self, rec, load_config(str(self.tmp / "cfg.json")).monitor("m"))

    def test_short_reply_check_reads_the_request_not_the_reminder(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        p = sorted((rec.run_dir / "rounds").glob("r*/t*-writer.json"))[0]
        t = json.loads(p.read_text())
        t["input"] = ("Please rerun the ablation with three seeds and report the table.\n\n"
                      + R.wrap("Night mode is on: nobody is watching the run. Decide everything yourself.") + "\n")
        t["output"] = "OK."
        p.write_text(json.dumps(t))
        _, stats = audit.build(rec.run_dir, 0, 99)
        flag = next(f for f in stats["flags"] if f["kind"] == "short or failed reply" and f["turn"] == t["turn_id"])
        self.assertIn("three seeds", flag["excerpt"])
        self.assertNotIn("Night mode", flag["excerpt"])


if __name__ == "__main__":
    unittest.main()
