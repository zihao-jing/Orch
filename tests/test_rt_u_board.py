"""Read-only data needed by the dashboard's timeline, activity, and audit history."""

import json
import tempfile
import unittest
from pathlib import Path

from rt_helpers import C
from orch.runtime.board import Board


class TimelineRuntime:
    def __init__(self, messages):
        self.rows = messages

    def state(self):
        return {"agents": {"lead": {"role": "manager"}, "reviewer": {"role": "critic"},
                           "builder": {"role": "worker"}, "lookout": {"role": "monitor"}}}

    def messages(self, before=0, limit=50, q="", agent=""):
        rows = [m for m in reversed(self.rows) if (not before or m.seq < before)
                and (not q or q.lower() in m.body.lower()) and (not agent or agent in (m.sender, m.recipient))]
        return rows[:limit], len(rows) > limit


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)

    def test_UI_2_timeline_counts_filtered_rows_before_the_cursor(self):
        rows = [C.Message(str(n), "lead", "reviewer", "Sample", seq=n) for n in range(1, 506)]
        rows.append(C.Message("silence", "orch", "lead", "Silence", seq=506, text=C.TextRef(C.TextKey.SILENCE_CHECK)))
        rows.append(C.Message("worker", "reviewer", "builder", "Build", seq=507))
        rows.append(C.Message("monitor", "lookout", "reviewer", "Health", seq=508))
        board = Board(TimelineRuntime(rows), self.path)
        args = {"timeline": ["1"], "limit": ["30"], "levels": ["critic"], "q": ["sample"], "agent": ["lead"]}
        page = board.messages(args)
        self.assertEqual([m["seq"] for m in page["messages"]], list(range(476, 506)))
        self.assertEqual(page["remaining"], 475)
        older = board.messages({**args, "before": ["476"]})
        self.assertEqual([m["seq"] for m in older["messages"]], list(range(446, 476)))
        self.assertEqual(older["remaining"], 445)
        self.assertEqual(board.messages({"timeline": ["1"], "levels": ["you"], "notes": ["0"]})["messages"], [])
        monitor = board.messages({"timeline": ["1"], "levels": ["monitor"]})
        self.assertEqual([m["id"] for m in monitor["messages"]], ["monitor"])

    def test_UI_2_summary_has_model_and_character_counts_without_full_text(self):
        turn = "r001-t01-lead"
        folder = self.path / "rounds" / "r001"
        folder.mkdir(parents=True)
        (folder / "t01-lead.json").write_text(json.dumps(
            {"turn_id": turn, "input": "a" * 30001, "output": "done", "model": "provider/model",
             "system_prompt": "system instructions"}), encoding="utf-8")
        row = C.Message("routed", "lead", "reviewer", "Work", turn=turn, seq=1, rendered="Delivered prompt")
        board = Board(TimelineRuntime([row]), self.path)
        page, = board.messages({"timeline": ["1"]})["messages"]
        self.assertEqual(page["written_by"], {"model": "provider/model", "input_chars": 30001, "output_chars": 4})
        self.assertFalse({"input", "output", "rendered", "system_prompt"} & set(page))
        self.assertNotIn("written_by", board.messages({})["messages"][0])
        self.assertEqual(board.turn(turn)["system_prompt"], "system instructions")

    def test_UI_1_recent_activity_uses_latest_then_pages_older(self):
        directory = self.path / "live"
        directory.mkdir()
        rows = [json.dumps({"k": "text", "s": str(n)}) for n in range(40)]
        (directory / "builder.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        board = Board(TimelineRuntime([]), self.path)
        latest = board.live("builder", 0, tail=15)
        self.assertEqual([r["n"] for r in latest["items"]], list(range(25, 40)))
        self.assertEqual(latest["next"], 40)
        self.assertTrue(latest["more"])
        older = board.live("builder", 0, tail=15, before=latest["items"][0]["n"])
        self.assertEqual([r["n"] for r in older["items"]], list(range(10, 25)))
        self.assertTrue(older["more"])

    def test_UI_4_older_audits_are_indexed_and_opened_separately(self):
        directory = self.path / "audits"
        directory.mkdir()
        for key, report in (("20260101-170100", "Earlier reply"), ("20260101-170900", "Latest reply")):
            (directory / (key + ".md")).write_text("## Reply\n\n" + report, encoding="utf-8")
        board = Board(TimelineRuntime([]), self.path)
        current = board.audit(history=True)
        self.assertEqual(current["report"], "Latest reply")
        self.assertEqual([r["id"] for r in current["records"]], ["20260101-170900", "20260101-170100"])
        self.assertEqual(board.audit("20260101-170100")["report"], "Earlier reply")
        self.assertEqual(board.audit("../state"), {"error": "not found"})
