"""Package H: prompts, message texts, reminders. Pure; no CLI, no network."""
from __future__ import annotations

import random
import re

from rt_helpers import *
from orch.runtime import reminders, texts
from orch.runtime.contracts import TEXT_VALUES, GoldenBoard
from orch.runtime.reminders import PROMPTS

# what an agent must never read; "orch" alone, as `.orch/` is a folder of the project
NEVER = {
    "critic": r"\bmanager|\bmilestone|\brubric|\bscore\b|(?<!\.)\borch\b|\bupper|\bverifier",
    "worker-1": r"\bmanager|\bmilestone|\brubric|\bscore\b|(?<!\.)\borch\b|\bcritic|\bgolden|\bchecklist|\bworker-?2|"
                r"other workers?|\bverifier",
}
REMOVED = r"verdict|wait-job|<to\b|<workers|urgent|additive|blocking=|preference\.md|review file|step board|<plan>|" \
          r"<checklist>|CONTINUE\b|pipeline|controller"
VALUES = {
    "worker": "worker-1", "agent": "worker-1", "by": "critic", "monitor": "monitor", "sender": "critic",
    "jobs": ["train.py"], "available": ["worker-1"], "label": "<message>", "problem": "unclosed", "why": "billing",
    "assignment": "Check the labels", "question": "Done?", "to": "worker-9", "turns_dir": "/run/rounds", "since": T0,
    "latest": "r003-t01-manager", "goal": "Build it.", "milestone": "m1",
    "title": "Data", "text": "The data is clean.", "checklist_path": "/run/c.md", "last_title": "Setup", "attempt": 1,
    "attempts": 3, "rubric": ["Correct"], "checklist": ["- [x] Labels checked"], "decide_again": "",
    "answers": [{"kind": "question", "question": "A or B?", "answer": "A"}], "unsent": [], "path": "/run/golden.md",
    "job": "train.py", "minutes": 40, "status": "writing", "live_file": "/run/live.md", "items": ["- [ ] Labels checked"],
    "health": "quiet", "request": "", "open_report": False, "idle_since": T0 - 720, "workers": ["worker-1", "worker-2"],
    "chars": 9000, "limit": 8000, "words": ["boss"], "gates": ["question (A or B?)"], "rules": ["Be brief."],
    "milestones": ["1. Data"], "score": 4.5, "pass_score": 4.8, "rounds": 5000, "choices": ["opus", "gpt"], "choice": "opus",
    "new_session": True, "files": ["a.yaml"], "milestone_change": False, "approved_plan": False,
}
TO_WORKER = ("job_wakeup", "job_lost", "retry_continue", "restart_continue", "message_again", "session_return",
             "session_fresh", "handover_write", "handover_instruction")
TO_CRITIC = ("worker_final", "worker_intermediate", "worker_to_critic", "worker_failed", "direct_message_notice",
             "label_returned", "unknown_worker", "handover_request_critic", "checklist_unreadable", "nobody_watches",
             "job_no_progress", "jobs_stopped", "monitor_report", "discussion_apply", "rem_worker_line", "rem_workers",
             "rem_labels", "rem_unwatched", "rem_golden_use", "rem_golden_path", "rem_discussion", "rem_discussion_over",
             "rem_checklist") + TO_WORKER
BOARD = GoldenBoard("/run/golden.md", "d1", chars=100)


def values(key: TextKey) -> dict:
    out = {n: VALUES[n] for n in TEXT_VALUES[key]}
    if key == TextKey.JOB_WAKEUP:
        out["jobs"] = [{"job": "train.py", "exit_code": 1}]
    if key == TextKey.MILESTONE_CHANGE_REQUEST:
        out["milestones"] = [{"id": "m1", "title": "Data", "text": "Clean.", "status": "current"}]
    return out


class Fixed:
    """A random source that always (or never) picks."""

    def __init__(self, on: bool):
        self.on = on

    def random(self) -> float:
        return 0.0 if self.on else 0.999

    def sample(self, xs, n):
        return list(xs)[:n]


def keys(snap, msg, on=False):
    refs, given = reminders.lines(snap, msg, Fixed(on))
    return [str(r.key) for r in refs], given


def give(snap, agent_id, **given):
    snap.agents[agent_id].session.given.update(given)
    return snap


def seen_all(snap):
    pool = ",".join(w.id for w in snap.workers)
    give(snap, "critic", workers=pool, golden=snap.golden.digest, checklist=snap.checklist.path)
    return give(snap, "manager", golden=snap.golden.digest, checklist=snap.checklist.path)


class TestTexts(unittest.TestCase):
    def test_PRM_1_every_key_renders_with_its_values_and_leaves_no_placeholder(self):
        for key in TextKey:
            for reader in (USER, "manager", "critic", "worker-1"):
                t = texts._Text(snapshot(workers=2), C.TextRef(key, values(key)), reader, "The body.")
                out = texts._compose(t)
                self.assertTrue(out, key)
                self.assertNotRegex(out, r"\{[a-z_]+\}|None|TextKey|Problem\.", f"{key} to {reader}")
                self.assertIn("The body.", out, key)

    def test_PRM_1_each_text_uses_the_values_that_carry_content(self):
        free = {"agent", "attempts", "state_file", "turns_dir", "latest", "since", "available", "milestone",
                "decide_again", "unsent", "request", "open_report", "idle_since", "new_session", "jobs", "workers",
                "last_title", "score", "approved_plan"}
        for key in TextKey:
            if key in (TextKey.COMMAND_REFUSED, TextKey.HANDOVER_WRITE):
                continue
            for name in set(TEXT_VALUES[key]) - free:
                v = values(key)
                one = texts.words(C.TextRef(key, v), snapshot())
                other = {"problem": "empty", "why": "frozen", "health": "stopped", "status": "idle", "sender": "worker-1",
                         "worker": "worker-2", "by": "manager", "monitor": "worker-2", "minutes": 77}.get(name)
                v[name] = other or (["x1"] if isinstance(v[name], list) and not isinstance(v[name][0], dict) else
                                    [{**v[name][0], "answer": "B", "title": "Zzz"}] if isinstance(v[name], list) else 1234)
                if key == TextKey.MONITOR_CHECK and name == "status":
                    v[name] = "waiting_job"
                self.assertNotEqual(one, texts.words(C.TextRef(key, v), snapshot()), f"{key}: {name}")

    def test_PRM_1_critic_texts_name_nothing_above_it(self):
        for k in TO_CRITIC:
            t = texts._Text(snapshot(workers=2), C.TextRef(TextKey(k), values(TextKey(k))), "critic", "Body.")
            self.assertNotRegex(texts._compose(t).lower(), NEVER["critic"], k)

    def test_WRK_7_worker_texts_name_no_one(self):
        for k in TO_WORKER:
            t = texts._Text(snapshot(workers=2), C.TextRef(TextKey(k), values(TextKey(k))), "worker-1", "Body.")
            self.assertNotRegex(texts._compose(t).lower(), NEVER["worker-1"], k)

    def test_LAB_5_broken_message_to_the_critic_does_not_name_its_sender(self):
        ref = C.TextRef(TextKey.LABEL_BROKEN_PASSED, {"sender": "manager", "label": "<message>", "problem": "unclosed"})
        out = texts._compose(texts._Text(snapshot(), ref, "critic", "Do it."))
        self.assertNotIn("manager", out)
        self.assertIn("From the critic", texts._compose(texts._Text(snapshot(), C.TextRef(
            ref.key, {**ref.values, "sender": "critic"}), "manager", "Do it.")))

    def test_a_body_is_passed_on_untouched_also_with_braces(self):
        m = message("x1", "critic", "worker-1", "Set {worker} and {{x}}.", key=TextKey.WORKER_FINAL,
                    values={"worker": "worker-1"})
        self.assertIn("Set {worker} and {{x}}.", texts.render(snapshot(), m).text)
        m = message("x1", "worker-1", "critic", "Do {this}.")
        self.assertTrue(texts.render(snapshot(), m).text.startswith("Do {this}."))

    def test_JOB_3_intermediate_reply_is_marked(self):
        m = message("x1", "critic", "worker-1", "Started.", key=TextKey.WORKER_INTERMEDIATE,
                    values={"worker": "worker-1", "jobs": ["train.py"]})
        out = texts.render(snapshot(), m).text
        self.assertIn("worker-1, job still running (`train.py`), not ready for review", out)

    def test_REC_10_monitor_report_gives_the_status_once(self):
        out = texts._compose(texts._Text(snapshot(), C.TextRef(TextKey.MONITOR_REPORT, {
            "worker": "worker-1", "health": "off_track", "minutes": 9}), "critic", "off-track\nIt edits the docs."))
        self.assertEqual(out, "Progress check on worker-1, 9 minutes into its run: off track.\n\nIt edits the docs.")

    def test_MS_6_an_accept_below_the_agreed_score_goes_back(self):
        out = texts.words(C.TextRef(TextKey.REVIEW_SCORE_LOW, {"milestone": "m1", "title": "Data", "score": 3.5,
                                                               "pass_score": 4.0}),
                          snapshot())
        self.assertEqual(out, "Your score of 3.5 out of 5 is below the 4.0 we agreed on, so “Data” is not accepted. "
                              "Write the critic what to fix and to update its checklist.")

    def test_SIL_3_silence_check(self):
        out = texts.words(C.TextRef(TextKey.SILENCE_CHECK, {"attempt": 2, "attempts": 3}), snapshot())
        for part in ("check 2 of 3", "Find out why", "write the critic", "pause only if it really needs me"):
            self.assertIn(part, out)

    def test_PRM_9_review_texts_full_once_per_session_then_short(self):
        snap = snapshot()
        for who, key in (("critic", TextKey.WORKER_FINAL), ("manager", TextKey.CHECKLIST_COMPLETE)):
            m = message("x1", who, "worker-1", "", key=key, values=values(key))
            first = texts.render(snap, m)
            self.assertEqual(first.given["review"], "1")
            give(snap, who, review="1")
            again = texts.render(snap, m)
            self.assertNotIn("review", again.given)  # only the full text counts as shown
            self.assertLess(len(again.text), len(first.text))
            self.assertIn("Labels checked" if who == "manager" else "ready for review", again.text)
        self.assertIn("Correct", again.text)  # the manager's review keeps checklist and rubric

    def test_REC_10_monitor_sees_the_checklist_once_then_only_changes(self):
        snap = snapshot(agent("monitor"))
        v = {**values(TextKey.MONITOR_CHECK), "items": ["- [ ] Labels checked", "- [x] Files read"]}
        m = message("x1", "monitor", key=TextKey.MONITOR_CHECK, values=v, body="")
        out = texts.render(snap, m)
        self.assertIn("Labels checked", out.text)
        self.assertNotIn("system-reminder", out.text)
        snap.agents["monitor"].session.given.update(out.given)
        m.text.values["items"] = ["- [x] Labels checked", "- [x] Files read"]
        out = texts.render(snap, m)
        self.assertIn("Changed in the checklist:\n- [x] Labels checked", out.text)
        self.assertNotIn("Files read", out.text)

    def test_SES_4_the_critic_and_workers_are_not_sent_into_others_turns(self):
        ref = C.TextRef(TextKey.SESSION_RETURN, {"turns_dir": "/run/rounds", "since": T0})
        self.assertIn("/run/rounds/**/t*-critic.*.md", texts._compose(texts._Text(snapshot(), ref, "critic")))
        self.assertNotIn("/run", texts._compose(texts._Text(snapshot(), ref, "worker-1")))


class TestPrompts(unittest.TestCase):
    def prompts(self, **kw):
        snap = snapshot(agent("monitor"), BOARD, workers=2, **kw)
        return {i: texts.system_prompt(snap, i) for i in ("manager", "critic", "worker-1", "monitor")}

    def test_PRM_1_shorter_than_before_and_without_removed_controls(self):
        p = self.prompts()
        self.assertLess(len(p["critic"]), 12500 * 0.6)
        self.assertLess(len(p["manager"]), 10500 * 0.65)
        for who, text in p.items():
            self.assertNotRegex(text, r"\{[a-z_]+\}", who)
            self.assertNotRegex(text, REMOVED, who)
            self.assertNotRegex(text, r"NIW|authors|rebuttal|ICLR", who)
            lines = [x.strip() for x in text.splitlines() if len(x.strip()) > 30]
            self.assertEqual(len(lines), len(set(lines)), who)

    def test_LAB_1_2_3_labels_per_role(self):
        p = self.prompts()
        names = lambda t: set(re.findall(r"<([a-z-]+)(?=[ >/])", t))  # noqa: E731
        self.assertEqual(names(p["critic"]), {"message", "silent"})
        self.assertEqual(names(p["manager"]), {"message", "question", "pause", "rubric", "score", "decision", "summary",
                                               "state"})
        self.assertEqual(names(p["worker-1"]), set())
        self.assertNotRegex(p["critic"], r'to="(?!worker-1)')
        self.assertNotRegex(p["manager"], r'to="worker')  # allowed, never prompted

    def test_labels_are_not_shown_inside_code_where_they_would_not_be_read(self):
        for text in self.prompts().values():
            self.assertNotRegex(text, r"`<(?:message|silent|question)")
        self.assertNotIn("`<", texts.words(C.TextRef(TextKey.REM_LABELS, {"workers": ["worker-1"]}), snapshot()))

    def test_PRM_5_WRK_7_who_knows_whom(self):
        p = self.prompts()
        self.assertNotRegex(p["critic"].lower(), NEVER["critic"])
        self.assertNotRegex(p["worker-1"].lower(), NEVER["worker-1"])
        self.assertIn("/run/golden.md", p["manager"])
        self.assertNotIn("/run/golden.md", p["critic"])  # the critic is never pointed to the board
        self.assertIn("golden board", p["manager"].lower())

    def test_WRK_9_10_11_rules_are_in_the_prompts(self):
        p = self.prompts()
        self.assertIn("parts that don't depend on each other", p["critic"])
        self.assertIn("one task at a time", p["critic"])  # WRK-8
        for part in ("Files may change while you work", "Don't undo changes you didn't make", "git worktree",
                     "merge your work back", "remove the worktree"):
            self.assertIn(part, p["worker-1"])
        self.assertRegex(p["worker-1"], r"ask me in plain text and end your turn. Don't guess")

    def test_MS_7_PRM_8(self):
        p = self.prompts()
        self.assertIn("never just to acknowledge", p["manager"])
        self.assertIn("to update its checklist", p["manager"])
        self.assertRegex(p["critic"], r"untick it when it no longer does, and make every problem or change I ask for")
        self.assertIn("at most 3 lines", p["monitor"])

    def test_JOB_1_one_line_for_the_backend_of_the_worker(self):
        marks = {"claude": "run_in_background: true`.", "qoder": "up to 7200000", "qwen": "is_background: true",
                 "agy": "WaitMsBeforeAsync: 0", "codex": "yield_time_ms", "opencode": "`background: true`"}
        for backend in marks:
            p = texts.system_prompt(snapshot(agent("worker-1", choice=C.Choice(backend=backend))), "worker-1")
            self.assertIn("nohup, setsid, a trailing &", p)
            self.assertEqual([b for b, m in marks.items() if m in p], [backend])
        p = texts.system_prompt(snapshot(agent("worker-1", choice=C.Choice(backend="command"))), "worker-1")
        self.assertIn("background option", p)

    def test_the_manager_uses_display_names(self):
        p = texts.system_prompt(snapshot(agent("critic", display="Mira")), "manager")
        self.assertIn("guide Mira", p)
        self.assertIn('<message to="critic">', p)

    def test_PRM_7_research_rules_are_an_asset_and_in_no_prompt(self):
        rules = (ROOT / "assets" / "research-rules.md").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"(?m)^- ", rules)), 10)
        for text in self.prompts().values():
            self.assertNotIn("Purpose first", text)

    def test_PRM_2_PRM_6_prompts_come_only_from_orch(self):
        self.assertTrue((PROMPTS / "protocol" / "critic.md").exists())
        self.assertTrue(all("preference.md" not in f.read_text(encoding="utf-8") for f in PROMPTS.rglob("*.md")))


class TestReview(unittest.TestCase):
    def test_GATE_4_approval_gates_say_approved_or_not_without_quoting_the_reply(self):
        out = texts.words(C.TextRef(TextKey.GATE_ANSWERS, {"answers": [
            {"kind": "plan", "question": "1. Data\n2. Model", "answer": "", "approve": True},
            {"kind": "milestone", "question": "long reply", "answer": "More tests.", "approve": False},
            {"kind": "pause", "question": "Stop?", "answer": "", "approve": None}], "unsent": [], "decide_again": ""}),
            snapshot())
        self.assertIn("- The plan: approved", out)
        self.assertIn("- The milestone: not approved: More tests.", out)
        self.assertIn("- The pause (“Stop?”): done", out)
        self.assertNotIn("long reply", out)

    def test_MS_10_dropped_milestones_are_left_out_and_a_new_rubric_asked_for(self):
        out = texts.words(C.TextRef(TextKey.MILESTONE_CHANGE_REQUEST, {"milestones": [
            {"id": "m1", "title": "Data", "text": "Clean.", "status": "current"},
            {"id": "m2", "title": "Old", "text": "", "status": "dropped"}]}), snapshot())
        self.assertIn("1. [m1] Data — Clean. (current)", out)
        self.assertNotIn("Old", out)
        self.assertIn("add its new <rubric>", out)

    def test_WRK_failed_and_stopped_workers_keep_a_line_but_get_no_label(self):
        snap = seen_all(snapshot(agent("worker-1", status="failed"), agent("worker-2"), workers=2))
        snap.agents["worker-2"].stopped_by_user = True
        refs, _ = reminders.lines(snap, message("x1", "critic"), Fixed(False))
        out = texts.reminder(snap, "critic", refs)
        self.assertIn("worker-1: failed", out)
        self.assertNotIn('to="worker-1"', out)
        self.assertNotIn('to="worker-2"', out)

    def test_PRM_9_the_manager_is_told_the_checklist_once(self):
        snap = snapshot()
        snap.checklist.path = "/run/checklist-2.md"
        refs, given = reminders.lines(snap, message("x1", "manager"), Fixed(False))
        self.assertIn("The critic's checklist is `/run/checklist-2.md`.", texts.reminder(snap, "manager", refs))
        give(snap, "manager", **given)
        self.assertNotIn("rem_checklist", keys(snap, message("x2", "manager"))[0])

    def test_discussion_apply_asks_the_critic_for_a_plain_summary(self):
        snap = snapshot(C.Discussion("critic", T0))
        out = texts.render(snap, message("x1", "critic", key=TextKey.DISCUSSION_APPLY, values={"request": ""})).text
        self.assertIn("without <silent/>", out)


class TestReminders(unittest.TestCase):
    def test_PRM_4_critic_every_turn_exactly_worker_lines_and_labels(self):
        snap = seen_all(snapshot(
            agent("worker-1", idle_since=T0 - 720, open_report="x9", assignment=C.Assignment("m", "Check the labels.")),
            agent("worker-2", status="waiting_job"), job("worker-2/j1"), BOARD, workers=2))
        refs, given = reminders.lines(snap, message("x1", "critic", "worker-2"), Fixed(False))
        self.assertEqual(given, {})
        self.assertEqual(texts.reminder(snap, "critic", refs), "\n".join([
            "<system-reminder>",
            "worker-1: idle for 12 min; assigned: Check the labels; you have not written to it since its last report",
            "worker-2: waiting for a job (`train.py`); nothing assigned yet",
            'To a worker: <message to="worker-1">…</message> or <message to="worker-2">…</message>. Plain text goes to me; '
            "add <silent/> when nothing in the reply needs me.",
            "</system-reminder>"]))

    def test_PRM_4_the_critic_gets_its_lines_with_every_message(self):
        for key in (None, TextKey.WORKER_FINAL, TextKey.DISCUSSION_APPLY, TextKey.HANDOVER_INSTRUCTION):
            m = message("x1", "critic", key=key, values=values(key) if key else {})
            got, _ = keys(snapshot(), m)
            self.assertIn("rem_worker_line", got)
            self.assertIn("rem_labels", got)

    def test_the_report_in_this_message_is_not_called_unanswered(self):
        snap = snapshot(agent("worker-1", open_report="x1"))
        self.assertNotIn("since its last report", texts.render(snap, message("x1", "critic", "worker-1")).text)

    def test_WRK_6_WRK_9_pool_is_told_when_new_or_changed(self):
        snap = snapshot(workers=2)
        got, given = keys(snap, message("x1", "critic"))
        self.assertIn("rem_workers", got)
        give(snap, "critic", **given)
        self.assertNotIn("rem_workers", keys(snap, message("x2", "critic"))[0])
        snap.agents["worker-2"].lifecycle = C.Lifecycle.CLOSED
        got, _ = keys(snap, message("x3", "critic"))
        self.assertEqual((got.count("rem_worker_line"), got.count("rem_workers")), (1, 1))

    def test_no_worker(self):
        out = texts.render(snapshot(workers=0), message("x1", "critic")).text
        self.assertIn("No worker is available", out)
        self.assertNotIn("<message", out)

    def test_PRM_4_PRM_5_golden_board_path_when_new_changed_or_compacted(self):
        snap = snapshot(BOARD)
        got, given = keys(snap, message("x1", "manager"))
        self.assertTrue({"rem_golden_path", "rem_golden_use"} <= set(got))
        give(snap, "manager", **given)
        self.assertFalse({"rem_golden_path", "rem_golden_use"} & set(keys(snap, message("x2", "manager"))[0]))
        self.assertIn("rem_golden_use", keys(snap, message("x2", "manager"), True)[0])
        snap.golden = replace(snap.golden, digest="d2")
        self.assertIn("rem_golden_path", keys(snap, message("x3", "manager"))[0])
        snap.agents["manager"].session.given.clear()  # compacted
        self.assertIn("rem_golden_path", keys(snap, message("x3", "manager"))[0])

    def test_PRM_5_never_to_workers_and_never_to_the_critic(self):
        snap = snapshot(BOARD)
        self.assertEqual([k for k in keys(snap, message("x1", "worker-1"), True)[0] if "golden" in k], [])
        self.assertEqual([k for k in keys(snap, message("x1", "critic"), True)[0] if "golden" in k], [])
        self.assertIn("rem_golden_keep", keys(snap, message("x1", "manager"), True)[0])

    def test_SES_the_session_being_opened_by_a_switch_has_seen_nothing(self):
        snap = seen_all(snapshot(BOARD))
        snap.agents["manager"].switch = C.Switch("manager", step=C.SwitchStep.CONFIRMING, new_session=True)
        self.assertIn("rem_golden_path", keys(snap, message("x1", "manager"))[0])
        snap.agents["manager"].switch.new = snap.agents["manager"].session
        self.assertNotIn("rem_golden_path", keys(snap, message("x1", "manager"))[0])

    def test_PRM_4_manager_lines(self):
        snap = seen_all(snapshot(gate("g1", text="Which split?"), BOARD))
        self.assertEqual(keys(snap, message("x1", "manager"))[0], [])
        got, _ = keys(snap, message("x1", "manager"), True)
        self.assertEqual(got, ["rem_message_critic", "rem_open_gates", "rem_golden_use", "rem_golden_keep", "rem_writing"])
        refs, _ = reminders.lines(snap, message("x1", "manager"), Fixed(True))
        self.assertIn("Still waiting for me: “Which split?”. Don't ask again", texts.reminder(snap, "manager", refs))

    def test_PRM_4_in_a_discussion_only_the_board_is_kept(self):
        snap = seen_all(snapshot(gate("g1"), BOARD, C.Discussion("manager", T0)))
        got, given = keys(snap, message("x1", "manager"), True)
        self.assertFalse({"rem_message_critic", "rem_open_gates"} & set(got))
        self.assertIn("rem_golden_keep", got)
        self.assertIn("rem_discussion", got)
        self.assertEqual(given["discussion"], str(T0))

    def test_DISC_4_an_agent_is_told_once_that_the_discussion_ended_unapplied(self):
        snap = give(seen_all(snapshot(BOARD)), "manager", discussion="1")
        got, given = keys(snap, message("x1", "manager"))
        self.assertEqual((got, given), (["rem_discussion_over"], {"discussion": ""}))
        m = message("x1", "manager", key=TextKey.DISCUSSION_APPLY, values={"request": ""})
        self.assertEqual(keys(snap, m), ([], {"discussion": ""}))

    def test_golden_size_is_told_once_per_growth(self):
        big = GoldenBoard("/run/golden.md", "d1", chars=9000)
        snap = seen_all(snapshot(big))
        got, given = keys(snap, message("x1", "manager"))
        self.assertEqual(got, ["rem_golden_size"])
        give(snap, "manager", **given)
        self.assertEqual(keys(snap, message("x2", "manager"))[0], [])
        snap.golden = GoldenBoard("/run/golden.md", "d1", chars=100)
        give(snap, "manager", **keys(snap, message("x3", "manager"))[1])
        snap.golden = big
        self.assertEqual(keys(snap, message("x4", "manager"))[0], ["rem_golden_size"])

    def test_night_mode_start_and_end(self):
        snap = seen_all(snapshot(night=True))
        got, given = keys(snap, message("x1", "manager"))
        self.assertEqual(got, ["rem_night"])
        give(snap, "manager", **given)
        self.assertEqual(keys(snap, message("x2", "manager"))[0], [])
        snap.night = False
        self.assertEqual(keys(snap, message("x3", "manager"))[0], ["rem_night_over"])

    def test_WRK_11_PRM_4_worker_is_reminded_rarely(self):
        snap = snapshot()
        n = sum("ask me in plain text" in texts.render(snap, message(f"x{i}", "worker-1")).text for i in range(2000))
        self.assertTrue(120 < n < 300, n)

    def test_PRM_4_each_random_item_has_its_own_chance(self):
        snap = seen_all(snapshot(BOARD))
        got = [texts.render(snap, message(f"x{i}", "manager")).text for i in range(2000)]
        for text, chance in (("Only <message", 0.6), ("Keep the golden board", 0.25), ("Be brief", 0.25)):
            self.assertAlmostEqual(sum(text in x for x in got) / 2000, chance, delta=0.05, msg=text)

    def test_PRM_4_random_picks_come_from_the_given_source_and_repeat_on_replay(self):
        snap = snapshot(BOARD, workers=2)
        for i in range(50):
            m = message(f"x{i}", "critic")
            self.assertEqual(texts.render(snap, m), texts.render(copy.deepcopy(snap), m))
            self.assertEqual(reminders.lines(snap, m, random.Random(i)), reminders.lines(snap, m, random.Random(i)))

    def test_PRM_3_one_tag_at_the_end_one_line_each_no_duplicates(self):
        snap = snapshot(gate("g1"), GoldenBoard("/run/golden.md", "d1", 9000), night=True, workers=3)
        for i in range(200):
            for who in ("manager", "critic", "worker-1"):
                text = texts.render(snap, message(f"x{i}", who, body="Hello.")).text
                self.assertLessEqual(text.count("<system-reminder>"), 1)
                if "<system-reminder>" in text:
                    self.assertTrue(text.endswith("\n</system-reminder>"))
                    lines = text.split("<system-reminder>\n")[1].splitlines()[:-1]
                    self.assertEqual(len(lines), len(set(lines)))
                    self.assertTrue(all(x.strip() for x in lines))

    def test_PRM_4_writing_rules_one_brevity_line_and_one_sampled(self):
        refs, _ = reminders.lines(snapshot(), message("x1", "worker-1"), Fixed(True))
        rules = refs[-1].values["rules"]
        self.assertEqual(len(rules), 2)
        self.assertTrue(rules[0].startswith("Be brief"))
        brief = [r for w in (Role.MANAGER, Role.CRITIC, Role.WORKER) for r in reminders.writing_rules(w) if "brief" in r]
        self.assertEqual(len(set(brief)), 1)

    def test_critic_is_told_where_its_checklist_is_once(self):
        snap = seen_all(snapshot(C.Checklist("/run/c1.md")))
        self.assertNotIn("rem_checklist", keys(snap, message("x1", "critic"))[0])
        snap.checklist = C.Checklist("/run/c2.md")
        self.assertIn("rem_checklist", keys(snap, message("x1", "critic"))[0])

    def test_SES_5_a_handover_is_asked_for_without_a_reminder(self):
        m = message("x1", "worker-1", key=TextKey.HANDOVER_WRITE, values={"agent": "worker-1"}, body="")
        self.assertNotIn("reminder", texts.render(snapshot(), m).text)

    def test_PRM_4_reminders_to_each_role_name_nothing_it_must_not_know(self):
        snap = snapshot(gate("g1"), BOARD, C.Checklist("/run/c1.md"), workers=2)
        for who in ("critic", "worker-1"):
            refs, _ = reminders.lines(snap, message("x1", who), Fixed(True))
            self.assertNotRegex(texts.reminder(snap, who, refs).lower(), NEVER[who])


if __name__ == "__main__":
    unittest.main()
