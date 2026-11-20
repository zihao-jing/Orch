"""The writing rules: orch's own file (or the project's) at the end of the manager's, critic's and workers' prompts."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.config import PROMPTS_DIR, load_config  # noqa: E402
from role_view_checks import leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

RULES = (PROMPTS_DIR / "writing.md").read_text(encoding="utf-8").strip()


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def mon(self, controller=None):
        m = managed(controller=controller)
        m["agents"].append({"name": "auditor", "role": "auditor"})
        return load_config(make_cfg(self.tmp, [m])).monitor("m")

    def test_orchs_rules_end_the_team_prompts_and_nobody_elses(self):
        mon = self.mon()
        for name in ("boss", "critic", "writer"):
            sp = mon.agent(name).current_system_prompt()
            self.assertTrue(sp.endswith(RULES), name)
            self.assertEqual(sp.count("# Writing rules"), 1, name)
            self.assertEqual(leaks(sp, mon.agent(name).role), [], name)
        self.assertNotIn("# Writing rules", mon.agent("auditor").current_system_prompt())

    def test_an_edit_reaches_the_next_system_prompt(self):
        f = self.tmp / "mine.md"
        f.write_text("# Writing rules\n\n- Short.\n")
        mon = self.mon({"writing": str(f)})
        self.assertTrue(mon.agent("writer").current_system_prompt().endswith("- Short."))
        f.write_text("# Writing rules\n\n- Shorter.\n<!-- a note for me -->\n")
        sp = mon.agent("writer").current_system_prompt()
        self.assertTrue(sp.endswith("- Shorter."))
        self.assertNotIn("a note for me", sp)

    def test_off_means_none(self):
        mon = self.mon({"writing": "off"})
        self.assertNotIn("# Writing rules", mon.agent("boss").current_system_prompt())

    def test_a_run_sends_them_once_with_the_system_prompt(self):
        status, rec = run(make_cfg(self.tmp, [managed()]))
        self.assertEqual(status, "completed")
        ts = [t for t in turns(rec) if t["agent"] in ("boss", "critic", "writer")]
        for name in ("boss", "critic", "writer"):  # each agent's first turn carries them in its system prompt
            first = next(t for t in ts if t["agent"] == name)
            self.assertIn("# Writing rules", first.get("system_prompt") or "", name)
        for t in ts:
            self.assertNotIn("# Writing rules", t["input"], t["turn_id"])  # not repeated in the messages


if __name__ == "__main__":
    unittest.main()
