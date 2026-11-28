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
        # one chip per agent: a line belongs to its writer, Orch's to the one it went to
        off = board.messages({"timeline": ["1"], "limit": ["500"], "off": ["lead,lookout"]})
        self.assertEqual({m["id"] for m in off["messages"]}, {"worker"})

    def test_hiding_discussions_filters_both_sides_before_paging_but_keeps_applied_work(self):
        turn = "r001-t01-lead"
        folder = self.path / "rounds/r001"
        folder.mkdir(parents=True)
        (folder / "t01-lead.json").write_text(json.dumps({"turn_id": turn, "draft": True, "ended_at": 30}))
        rows = [C.Message("prior", "user", "lead", seq=1, discussion_chat=False, reply_turn=turn),
                C.Message("discussion", "user", "lead", seq=2, reply_turn=turn),
                C.Message("reply", "lead", "user", seq=3, turn=turn),
                C.Message("queued", "user", "lead", seq=4, discussion_chat=True),
                C.Message("other", "builder", "reviewer", seq=5),
                C.Message("apply", "user", "lead", seq=6, text=C.TextRef(C.TextKey.DISCUSSION_APPLY))]
        board = Board(TimelineRuntime(rows), self.path)
        args = {"timeline": ["1"], "discussions": ["0"], "limit": ["2"]}
        page = board.messages(args)
        self.assertEqual(([m["id"] for m in page["messages"]], page["remaining"]), (["other", "apply"], 1))
        self.assertEqual([m["id"] for m in board.messages({**args, "before": ["5"]})["messages"]], ["prior"])
        self.assertEqual(len(board.messages({"timeline": ["1"]})["messages"]), len(rows))

    def test_finish_time_decides_which_page_contains_a_reply(self):
        turn = "r001-t01-lead"
        folder = self.path / "rounds/r001"
        folder.mkdir(parents=True)
        (folder / "t01-lead.json").write_text(json.dumps({"turn_id": turn, "ended_at": 300}))
        rows = [C.Message("slow", "lead", "user", turn=turn, seq=1, created=100),
                C.Message("sent", "user", "lead", seq=2, created=200)]
        board = Board(TimelineRuntime(rows), self.path)
        args = {"timeline": ["1"], "limit": ["1"]}
        top, = board.messages(args)["messages"]
        self.assertEqual((top["id"], top["ended"]), ("slow", 300))
        older, = board.messages({**args, "before": [str(top["seq"])]})["messages"]
        self.assertEqual(older["id"], "sent")

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


class ViewerOrchRunning(unittest.TestCase):
    """`orch serve` says whether an Orch holds the run, from the pid in its run lock."""

    def test_the_lock_names_a_live_or_a_gone_process(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path
        from orch.runtime.board import Viewer
        with tempfile.TemporaryDirectory() as d:
            v = Viewer(Path(d))
            self.assertFalse(v.orch_running())  # no lock
            (Path(d) / "locks").mkdir()
            lock = Path(d) / "locks" / "run.lock"
            lock.write_text(json.dumps({"pid": os.getpid()}))
            self.assertTrue(v.orch_running())
            gone = subprocess.Popen(["true"])
            gone.wait()
            lock.write_text(json.dumps({"pid": gone.pid}))
            self.assertFalse(v.orch_running())
            lock.write_text("")
            self.assertFalse(v.orch_running())


class BoardChecklist(unittest.TestCase):
    """Any milestone's checklist can be read, done ones too."""

    def test_a_done_milestone_checklist_is_read_from_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ck.md").write_text("## Step 1: Hello\n- [x] greets\n- [ ] tests\n")

            class R:
                def state(self):
                    return {"run": {"workspace": d}, "milestones": [
                        {"id": "m1", "status": "done", "checklist_path": "ck.md"}, {"id": "m2", "checklist_path": ""}]}
            b = Board(R(), Path(d))
            c = b.checklist("m1")
            self.assertEqual([s["title"] for s in c["steps"]], ["Step 1: Hello"])
            self.assertEqual(c["steps"][0]["items"], [[True, "greets"], [False, "tests"]])
            self.assertEqual(b.checklist("m2")["error"], "none")
            self.assertEqual(b.checklist("m9")["error"], "not found")


class ViewerRefusesWithoutOrch(unittest.TestCase):
    def test_a_request_is_never_saved_for_a_later_orch(self):
        from unittest import mock
        from orch.runtime.board import Viewer
        with tempfile.TemporaryDirectory() as d:
            v = Viewer(Path(d))
            v._store = object()
            with mock.patch.object(Viewer, "snapshot", lambda self: None), \
                    mock.patch.object(Viewer, "orch_running", lambda self: False):
                r = v.submit({"type": "WorkerAdd", "id": "x"})
            self.assertFalse(r["ok"])
            self.assertIn("No Orch is running", r["shown"])
            self.assertFalse((Path(d) / "commands").exists())


class ViewerCatalog(unittest.TestCase):
    def test_a_saved_run_without_a_model_list_shows_the_settings_models(self):
        from orch.runtime.board import Viewer
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".orch").mkdir()
            (Path(d) / ".orch" / "settings.yaml").write_text(
                "models:\n  gpt:\n    backend: codex\n    backend_options: {model: gpt-5.5, effort: high}\n")
            self.assertEqual([m.id for m in Viewer(Path(d)).catalog(Path(d))], ["gpt"])
            self.assertEqual(Viewer(Path(d)).catalog(Path(d) / "nowhere"), [])
