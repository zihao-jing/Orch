"""The writing rules: a project's own file (its `.orch/writing.md`, or a path) at the end of the manager's, critic's
and workers' prompts. orch ships none: its general rules are in runtime/prompts/style.md."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.config import load_config  # noqa: E402
from role_view_checks import leaks  # noqa: E402
from test_orch import make_cfg, managed, turns

RULES = "# Writing rules\n\n- Tables before prose."


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "mine.md"
        self.file.write_text(RULES + "\n")

    def mon(self, controller=None):
        m = managed(controller=controller)
        m["agents"].append({"name": "auditor", "role": "auditor"})
        return load_config(make_cfg(self.tmp, [m])).monitor("m")

    def test_none_unless_the_project_has_its_own(self):
        mon = self.mon()
        for name in ("boss", "critic", "writer"):
            self.assertNotIn("# Writing rules", mon.agent(name).current_system_prompt(), name)

    def test_the_project_rules_end_the_team_prompts_and_nobody_elses(self):
        mon = self.mon({"writing": str(self.file)})
        for name in ("boss", "critic", "writer"):
            sp = mon.agent(name).current_system_prompt()
            self.assertTrue(sp.endswith(RULES), name)
            self.assertEqual(sp.count("# Writing rules"), 1, name)
            self.assertEqual(leaks(sp, mon.agent(name).role), [], name)
        self.assertNotIn("# Writing rules", mon.agent("auditor").current_system_prompt())

    def test_an_edit_reaches_the_next_system_prompt(self):
        mon = self.mon({"writing": str(self.file)})
        self.file.write_text("# Writing rules\n\n- Shorter.\n<!-- a note for me -->\n")
        sp = mon.agent("writer").current_system_prompt()
        self.assertTrue(sp.endswith("- Shorter."))
        self.assertNotIn("a note for me", sp)

    def test_off_means_none(self):
        mon = self.mon({"writing": "off"})
        self.assertNotIn("# Writing rules", mon.agent("boss").current_system_prompt())



if __name__ == "__main__":
    unittest.main()
