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
from orch.templates import render  # noqa: E402
from role_view_checks import assert_no_role_leaks, leaks  # noqa: E402
from test_orch import make_cfg, managed, turns

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





if __name__ == "__main__":
    unittest.main()
