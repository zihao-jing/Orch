"""Review turns without repeated instructions: with `checklist: true` the critic's checklist file decides the verdict
(no tag needed, an explicit FAIL still holds the work back), and the full review instructions go out once per session;
later reviews get a short line."""

from protocol_fixtures import worker_instruction

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orch import make_cfg, managed, run, turns  # noqa: E402

OPEN = "## Standing rules\n- [x] nothing committed\n\n## Step 1: labels\n- [ ] labels checked\n"
DONE = "## Standing rules\n- [x] nothing committed\n\n## Step 1: labels\n- [x] labels checked\n"


class TestReviewTrim(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.sdir = self.tmp / "agents"

    def cfg(self, critic):
        m = managed(controller={"checklist": True}, mgr={"milestones": 1}, critic=critic)
        for ag in m["agents"]:
            ag["state_file"] = str(self.sdir / ag["name"] / "state.md")
        return make_cfg(self.tmp, [m])

    @property
    def ck(self):
        return str(self.sdir / "critic" / "checklist-m1.md")

    def verifies(self, rec):
        return [t for t in turns(rec) if t["agent"] == "critic" and t["purpose"] == "verify"]

    def test_the_checklist_file_decides_without_a_tag(self):
        critic = {"replies": {"brief": [worker_instruction("Check all 40 labels.")],
                              "verify": [worker_instruction("Two labels are still wrong; fix them."),
                                         "All labels hold now."]},
                  "files": {"brief": [{self.ck: OPEN}], "verify": [{}, {self.ck: DONE}]}}
        status, rec = run(self.cfg(critic))
        self.assertEqual(status, "completed")
        self.assertEqual(len(self.verifies(rec)), 2)
        self.assertFalse([t for t in turns(rec) if t["purpose"] == "verdict"])  # no turn spent asking for a tag

    def test_an_explicit_fail_holds_the_work_back(self):
        critic = {"replies": {"brief": [worker_instruction("Check all 40 labels.")],
                              "verify": [worker_instruction("Recheck label 7 against the source.\n"
                                                            "<verdict>FAIL</verdict>"),
                                         "Label 7 holds."]},
                  "files": {"brief": [{self.ck: DONE}]}}
        status, rec = run(self.cfg(critic))
        self.assertEqual(status, "completed")
        self.assertEqual(len(self.verifies(rec)), 2)  # the ticked file did not override the FAIL

    def test_full_instructions_once_then_a_short_line(self):
        critic = {"replies": {"brief": [worker_instruction("Check all 40 labels.")],
                              "verify": [worker_instruction("Two labels are still wrong; fix them."),
                                         "All labels hold now."]},
                  "files": {"brief": [{self.ck: OPEN}], "verify": [{}, {self.ck: DONE}]}}
        status, rec = run(self.cfg(critic))
        self.assertEqual(status, "completed")
        first, second = self.verifies(rec)
        self.assertIn("If every item is now ticked", first["input"])
        self.assertIn("Same as before", second["input"])
        self.assertNotIn("If every item is now ticked", second["input"])


if __name__ == "__main__":
    unittest.main()
