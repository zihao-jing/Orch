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
from test_orch import make_cfg, managed, turns

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




if __name__ == "__main__":
    unittest.main()
