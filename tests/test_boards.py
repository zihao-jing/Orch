"""B3: boards. The golden board reaches every agent (once per change, neutrally), its size is watched; the critic's
step checklist lives in a file that PASS reads; agents edit files instead of re-writing them in every reply."""

from protocol_fixtures import worker_instruction

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import boards as B  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.recorder import read_json  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

RULE = "- Tables before prose."
NEW_RULE = "- Round every percentage to one decimal."


def _events(rec):
    return [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]


class TestBoardsUnit(unittest.TestCase):
    def test_parse_steps_standing_rules_and_status(self):
        ck = B.parse_checklist("# Checklist\n<!-- note - [ ] not an item -->\n## Standing rules\n- [x] nothing committed\n"
                               "## Step 1: labels\n- [x] 40 labels checked\n## Step 2: table\n- [ ] totals add up\n"
                               "## Step 3: write-up\n")
        self.assertEqual([s["title"] for s in ck["steps"]], ["Step 1: labels", "Step 2: table", "Step 3: write-up"])
        self.assertEqual(B.current_index(ck), 1)
        self.assertEqual(B.plan_lines(ck), "- [x] Step 1: labels\n- [ ] Step 2: table\n- [ ] Step 3: write-up")
        self.assertEqual(B.current_list(ck), "- [x] nothing committed\n- [ ] totals add up")
        plain = B.parse_checklist("- [x] a\n- [ ] b")  # a plain list is the current step, and no plan
        self.assertEqual(B.plan_lines(plain), "")
        self.assertEqual(B.current_list(plain), "- [x] a\n- [ ] b")
        self.assertTrue(B.step_done(B.parse_checklist("## [x] Step 1: done already\n")["steps"][0]))

    def test_older_protocol_blocks_merge_into_the_file(self):
        ck = B.merge_blocks(None, "- [x] STEP-A first\n- [ ] STEP-B second", "- [x] item one\n- [ ] item two")
        again = B.parse_checklist(B.render_checklist(ck))
        self.assertEqual(B.plan_lines(again), "- [x] STEP-A first\n- [ ] STEP-B second")
        self.assertEqual(B.current_list(again), "- [x] item one\n- [ ] item two")
        # the plan's open mark holds even when every item of that step is ticked
        ck = B.merge_blocks(again, "- [x] STEP-A first\n- [ ] STEP-B second", "- [x] item one\n- [x] item two")
        self.assertEqual(B.plan_lines(B.parse_checklist(B.render_checklist(ck))).split("\n")[1], "- [ ] STEP-B second")

    def test_editing_standing_rules_cannot_match_the_header_reminder(self):
        board = B.parse_checklist("## Standing rules\n- [x] source files unchanged\n"
                                  "## Step 1: labels\n- [x] labels checked\n"
                                  "## Step 2: table\n- [x] totals checked\n")
        text = B.render_checklist(board)
        # Reproduce the critic's edit: the old header's example heading matched first,
        # so replacing this section removed the closing comment and hid the entire checklist.
        start, end = text.index("## Standing rules"), text.index("## Step 1")
        edited = text[:start] + "## Standing rules\n- [x] protected files unchanged\n\n" + text[end:]
        parsed = B.parse_checklist(edited)
        self.assertEqual(parsed["standing"], [(True, "protected files unchanged")])
        self.assertEqual(parsed["steps"], board["steps"])
        self.assertEqual(B.plan_lines(parsed), "- [x] Step 1: labels\n- [x] Step 2: table")

    def test_golden_body_drops_the_title_and_comments(self):
        self.assertEqual(B.golden_body(B.GOLDEN_HEAD), "")
        self.assertEqual(B.golden_body(B.GOLDEN_HEAD + RULE + "\n"), "## Boundaries\n\n\n" + RULE)
        self.assertEqual(B.boundaries("## Rules\n\n- a\n\n## Boundaries\n\n- b\n\n## More\n\n- c"), "- b")
        self.assertEqual(B.boundaries("## Rules\n\n- a"), "")


class TestBoardsRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.golden = self.tmp / "golden.md"
        self.sdir = self.tmp / "agents"

    def cfg(self, controller=None, mgr=None, critic=None, worker=None):
        c = {"golden_board": str(self.golden)}
        c.update(controller or {})
        m = managed(controller=c, mgr=mgr, critic=critic, worker=worker)
        for ag in m["agents"]:
            ag["state_file"] = str(self.sdir / ag["name"] / "state.md")
        return make_cfg(self.tmp, [m])

    @property
    def ck(self):
        return self.sdir / "critic" / "checklist-m1.md"

    def test_the_golden_board_reaches_every_agent_and_each_change_once(self):
        self.golden.write_text(B.GOLDEN_HEAD + RULE + "\n")
        mgr = {"milestones": 2, "files": {"review": [{str(self.golden): B.GOLDEN_HEAD + RULE + "\n" + NEW_RULE + "\n"}]}}
        cfg = self.cfg(controller={"reminders": {"golden_board": {"every": 100}}}, mgr=mgr)
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        by = {a: [t for t in ts if t["agent"] == a] for a in ("boss", "critic", "writer")}
        for a, its in by.items():
            if a == "boss":
                self.assertNotIn(RULE, its[0]["input"])
            else:
                self.assertIn(RULE, its[0]["input"], a)
            self.assertIn("golden_board", its[0].get("reminders") or [], a)
        self.assertIn("My standing rules for this work", by["critic"][0]["input"])
        self.assertIn("My boundaries for this work", by["writer"][0]["input"])  # the default board puts RULE under Boundaries
        self.assertIn(f"Read the golden board at `{self.golden}`", by["boss"][0]["input"])
        self.assertIn(str(self.golden), by["boss"][0]["system_prompt"])  # the manager knows where it is
        # the change reaches the critic and the worker once; the manager wrote it, so it is not sent back to it
        self.assertEqual(sum(NEW_RULE in t["input"] for t in by["critic"]), 1)
        self.assertEqual(sum(NEW_RULE in t["input"] for t in by["writer"]), 1)
        self.assertEqual(sum(NEW_RULE in t["input"] for t in by["boss"]), 0)
        for a in ("critic", "writer"):  # in between, nothing is resent
            self.assertEqual(sum(RULE in t["input"] for t in by[a]), 2, a)
        self.assertTrue(any(e["event"] == "golden_changed" for e in _events(rec)))
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))
        from orch.board import Board
        d = Board("m", lambda: rec.run_dir, load_config(cfg).monitor("m")).data()
        self.assertIn(NEW_RULE, d["golden"]["text"])
        self.assertFalse(d["golden"]["over"])

    def test_a_golden_board_naming_a_layer_above_is_held_from_it(self):
        self.golden.write_text(B.GOLDEN_HEAD + "- Alex wants tables before prose.\n")
        cfg = self.cfg(mgr={"milestones": 1})
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        for t in ts:
            if t["agent"] in ("critic", "writer"):
                self.assertNotIn("Alex wants", t["input"])
        boss = [t for t in ts if t["agent"] == "boss"]
        self.assertIn(str(self.golden), boss[0]["input"])
        self.assertNotIn("Alex wants", boss[0]["input"])
        self.assertTrue(any("The golden board names Alex" in t["input"] for t in boss))
        self.assertTrue(any(e["event"] == "board_warning" and e.get("what") == "golden_leak" for e in _events(rec)))
        assert_no_role_leaks(self, rec, load_config(cfg).monitor("m"))

    def test_size_warning_and_one_prune_reminder_per_growth(self):
        self.golden.write_text(B.GOLDEN_HEAD + RULE + " Keep each table to one page and cite every number.\n")
        bigger = B.GOLDEN_HEAD + RULE + " Keep each table to one page and cite every number.\n" + NEW_RULE + "\n"
        mgr = {"milestones": 2, "files": {"review": [{str(self.golden): bigger}]}}
        status, rec = run(self.cfg(controller={"golden_limit": 60}, mgr=mgr))
        self.assertEqual(status, "completed")
        boss = [t for t in turns(rec) if t["agent"] == "boss"]
        pruned = [t for t in boss if "golden_prune" in (t.get("reminders") or [])]
        self.assertEqual(len(pruned), 2)  # once at the start, once more after it grew again
        self.assertEqual(pruned[0]["turn_id"], boss[0]["turn_id"])
        self.assertIn("over the 60 it should stay under", pruned[0]["input"])
        sizes = [e for e in _events(rec) if e["event"] == "board_warning" and e.get("what") == "golden_size"]
        self.assertEqual(len(sizes), 2)
        self.assertIn("over its 60", (rec.run_dir / "progress.md").read_text())
        for t in turns(rec):  # the prune reminder is the manager's alone
            if t["agent"] != "boss":
                self.assertNotIn("golden_prune", t.get("reminders") or [])

    def test_pass_is_read_from_the_checklist_file_not_from_a_block_in_the_reply(self):
        ck = str(self.ck)
        critic = {
            "replies": {
                "brief": [worker_instruction("Check the labels first: all 40, against the source.")],
                "verify": [worker_instruction("Two labels are still wrong; fix them.\n<verdict>FAIL</verdict>"),
                           '<message to="Sam">Check the totals next.</message>All good.\n<checklist>\n- [x] labels checked\n</checklist>\n<verdict>PASS</verdict>',
                           "Done now.\n<verdict>PASS</verdict>"]},
            "files": {
                "brief": [{ck: "## Standing rules\n- [x] nothing committed\n\n## Step 1: labels\n- [ ] labels checked\n"}],
                "verify": [{},
                           {ck: "## Standing rules\n- [x] nothing committed\n\n## Step 1: labels\n- [x] labels checked\n"
                                "- [ ] totals checked\n"},
                           {ck: "## Standing rules\n- [x] nothing committed\n\n## Step 1: labels\n- [x] labels checked\n"
                                "- [x] totals checked\n"}]}}
        status, rec = run(self.cfg(mgr={"milestones": 1}, critic=critic))
        self.assertEqual(status, "completed")
        ts = turns(rec)
        verifies = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "verify"]
        self.assertEqual(len(verifies), 3)
        rounds = [read_json(p) for p in sorted((rec.run_dir / "rounds").glob("r*/round.json"))]
        self.assertEqual(sum(1 for r in rounds if r.get("checklist_overruled")), 1)  # the stale block did not count
        self.assertTrue(any(e["event"] == "checklist_block_ignored" for e in _events(rec)))
        self.assertIn("still has 1 open item", verifies[2]["input"])
        self.assertIn(str(self.ck), [t for t in ts if t["purpose"] == "brief"][0]["input"])
        self.assertEqual(verifies[1]["checklist_file"]["checklist"], "- [x] nothing committed\n- [x] labels checked\n"
                                                                      "- [ ] totals checked")
        report = next(t for t in ts if t["agent"] == "boss" and t["purpose"] == "review")
        self.assertIn("- [x] totals checked", report["input"])  # the manager sees the list from the file
        for t in ts:  # the worker never sees the checklist or its file
            if t["agent"] == "writer":
                for s in ("labels checked", "totals checked", "nothing committed", "checklist-m1"):
                    self.assertNotIn(s, t["input"])

    def test_older_protocol_blocks_still_work_and_land_in_the_file(self):
        status, rec = run(self.cfg(mgr={"milestones": 1}))  # the mock critic still writes <checklist> blocks
        self.assertEqual(status, "completed")
        text = self.ck.read_text()
        self.assertIn("- [x] the change is made", text)
        self.assertTrue(any(e["event"] == "checklist_from_reply" for e in _events(rec)))

    def test_a_checklist_file_from_an_earlier_run_is_moved_aside(self):
        self.ck.parent.mkdir(parents=True)
        self.ck.write_text("## Step 1: old\n- [x] stale item\n")
        status, rec = run(self.cfg(mgr={"milestones": 1}))
        self.assertEqual(status, "completed")
        self.assertNotIn("stale item", self.ck.read_text())
        self.assertTrue(list(self.ck.parent.glob("checklist-m1.earlier-*.md")))

    def test_a_long_checklist_gets_one_note_to_merge(self):
        items = "\n".join(f"- [ ] item {i}" for i in range(27))
        critic = {"replies": {"brief": [worker_instruction("Start with the tables.")],
                              "verify": [worker_instruction("More to do.\n<verdict>FAIL</verdict>"), "Done.\n<verdict>PASS</verdict>"]},
                  "files": {"brief": [{str(self.ck): "## Step 1: tables\n" + items + "\n"}],
                            "verify": [{}, {str(self.ck): "## Step 1: tables\n" + items.replace("[ ]", "[x]") + "\n"}]}}
        status, rec = run(self.cfg(mgr={"milestones": 1}, critic=critic))
        self.assertEqual(status, "completed")
        crit = [t for t in turns(rec) if t["agent"] == "critic"]
        notes = [t for t in crit if "has 27 items; keep it to about 25" in t["input"]]
        self.assertEqual(len(notes), 1)
        self.assertTrue(any(e["event"] == "board_warning" and e.get("what") == "checklist_size" for e in _events(rec)))

    def test_decisions_register_is_a_record_not_a_briefing(self):
        critic = {"replies": {"brief": [worker_instruction("<question>Should the tables use percentages?\nA) yes\nB) no</question>\n\n"
                                        "Start with the tables; assume percentages for now.")]}}
        status, rec = run(self.cfg(critic=critic))
        self.assertEqual(status, "completed")
        self.assertTrue(rec.state["decisions"])
        self.assertTrue((rec.run_dir / "decisions.md").exists())
        for t in turns(rec):
            self.assertNotIn("Decisions so far", t["input"])
            self.assertNotIn("decisions.md", t["input"])

    def test_the_manager_may_edit_its_state_file_in_place(self):
        state = self.sdir / "boss" / "state.md"
        mgr = {"milestones": 1, "replies": {"kickoff": ['<message to="Jordan">Write the tables.</message>']},
               "files": {"kickoff": [{str(state): "Standing: write the tables, one page each."}]}}
        status, rec = run(self.cfg(mgr=mgr))
        self.assertEqual(status, "completed")
        self.assertIn("one page each", (self.sdir / "critic" / "state.md").read_text())
        for t in turns(rec):
            self.assertNotIn("I edited your working notes", t["input"])  # its own edit, not mine


if __name__ == "__main__":
    unittest.main()
