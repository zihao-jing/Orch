"""B6 (the manager's job) and B9 (gates), mock backend only:

B6: per-milestone rubrics (3–8, parsed from <rubric>, shown in the review); discussion mode (the manager's messages
held, one batch after it closes, supersede); milestone fallback (state and checklist file); the golden board sent
again after a compacted session.
B9: send back on the wait gate; night mode can't switch while a question for me is open; a manager question blocks
new work until answered (unless blocking="no"); gate summary; review file -> comments back to the manager.
"""

from protocol_fixtures import worker_instruction

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch import boards as B  # noqa: E402
from orch import routing as RT  # noqa: E402
from orch.config import load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402
from orch.managed import ManagedRunner, parse_rubric  # noqa: E402
from orch.recorder import Recorder, read_json  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402


def unit(tmp, **kw):
    cfg = load_config(make_cfg(tmp, [managed(mgr={"milestones": 1}, **kw)]))
    r = ManagedRunner(cfg.monitor("m"), threading.Event(), log=lambda s: None)
    r.rec = Recorder.create(str(tmp / "out"), "m", cfg.monitor("m").snapshot())
    r.st.setdefault("fwd", {})
    return r


class Live:
    """A run in a thread, and helpers to wait on its state and drop control files in its inbox."""

    def __init__(self, tc, cfg):
        self.tc, self.out, self.holder = tc, Path(json.loads(Path(cfg).read_text())["output_dir"]) / "m", {}
        self.th = threading.Thread(target=lambda: self.holder.update(res=run(cfg)), daemon=True)
        self.th.start()

    def state(self):
        runs = sorted(self.out.glob("*/state.json")) if self.out.exists() else []
        return (read_json(runs[-1]) or {}, runs[-1].parent) if runs else ({}, None)

    def until(self, pred, what, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, rd = self.state()
            try:
                if st and pred(st):
                    return st, rd
            except (KeyError, TypeError, IndexError):
                pass
            time.sleep(0.05)
        self.tc.fail(f"timed out waiting for {what}")

    def control(self, data, name):
        _st, rd = self.state()
        (rd / "inbox" / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.json").write_text(json.dumps(data))

    def gate(self, kind, not_key=None):
        return self.until(lambda st: (st["managed"].get("gate") or {}).get("kind") == kind
                          and st["managed"]["gate"]["key"] != not_key, f"a {kind} gate")

    def done(self, timeout=20):
        self.th.join(timeout)
        self.tc.assertFalse(self.th.is_alive(), "the run did not finish")
        return self.holder["res"]


# --------------------------------------------------------------------------- B6


class TestRubrics(unittest.TestCase):
    def test_parse_counts_from_three_to_eight(self):
        for n in (3, 5, 8):
            items = parse_rubric("<rubric>\n" + "\n".join(f"{i}. criterion {i}" for i in range(1, n + 1)) + "\n</rubric>")
            self.assertEqual(items, [f"criterion {i}" for i in range(1, n + 1)])
        self.assertEqual(parse_rubric("<rubric>\n- [ ] **each reviewer concern** is strongly supported\n* tables "
                                      "self-explaining\n</rubric>"),
                         ["each reviewer concern is strongly supported", "tables self-explaining"])
        self.assertEqual(parse_rubric("<rubric>\n| criterion | what a 5 looks like |\n|---|---|\n| concerns answered "
                                      "| all |\n| no n/a cells | none |\n| verdicts first | 5 |\n</rubric>"),
                         ["concerns answered", "no n/a cells", "verdicts first"])
        self.assertEqual(parse_rubric("no rubric here"), [])

    def test_kickoff_rubric_is_kept_and_shown_at_the_review(self):
        status, rec = run(make_cfg(Path(tempfile.mkdtemp()), [managed(mgr={"milestones": 1, "replies": {"kickoff": [
            '<message to="Jordan">Answer each concern.</message>\n<rubric>\n1. each reviewer concern is strongly '
            'supported\n2. every table is self-explaining\n3. no superseded numbers\n4. verdict first in each '
            'section\n</rubric>']}})]))
        self.assertEqual(status, "completed")
        ms = rec.state["managed"]["milestones"][0]
        self.assertEqual(len(ms["rubric"]), 4)
        review = next(t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "review")
        self.assertIn("Your rubric for this milestone:\n\n1. each reviewer concern is strongly supported", review["input"])
        self.assertIn("4. verdict first in each section", review["input"])
        self.assertFalse(any("rubric" in x for x in (rec.state.get("fwd") or {}).get("boss", [])))
        assert_no_role_leaks(self, rec, also=["each reviewer concern"])

    def test_too_few_criteria_or_none_are_flagged_and_asked_again(self):
        status, rec = run(make_cfg(Path(tempfile.mkdtemp()), [managed(mgr={"milestones": 2, "replies": {"kickoff": [
            '<message to="Jordan">Go.</message>\n<rubric>\n- correct\n- clear\n</rubric>',
            '<message to="Jordan">Go on.</message>']}})]))
        self.assertEqual(status, "completed")
        ts = [t for t in turns(rec) if t["agent"] == "boss"]
        notes = [t for t in ts if "Your rubric for milestone 1 has 2 criteria; set 3–8" in t["input"]]
        self.assertTrue(notes, "the manager is asked to set 3–8")
        self.assertTrue(any("Your rubric for milestone 2 has 0 criteria" in t["input"] for t in ts))
        warn = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("what") == "rubric_count" for e in warn))
        review2 = [t for t in ts if t["purpose"] == "review"][-1]
        self.assertIn("You set no rubric for this milestone", review2["input"])


class TestDiscussion(unittest.TestCase):
    @staticmethod
    def discuss_with_a_held_message(tc, L):
        """Open a discussion, have the manager write to the critic from a board chat (held), and end it."""
        L.control({"kind": "discussion", "on": True}, "discussion-on")
        L.until(lambda st: st.get("discussion"), "the discussion switch")
        L.control({"to": ["critic"], "message": "HELD-MARK: use long tables.", "from": "boss", "via": "chat",
                   "source": "chat-001"}, "board-note")
        st, rd = L.until(lambda st: any(m.get("reason") == RT.DISCUSSION for m in st.get("messages") or []),
                         "a held message")
        held = next(m for m in st["messages"] if m.get("reason") == RT.DISCUSSION)
        tc.assertEqual((held["status"], held["to"]), ("held", "critic"))
        tc.assertTrue((rd / "inbox" / "held").is_dir())
        L.control({"kind": "discussion", "on": False}, "discussion-off")
        st, _ = L.until(lambda st: not st.get("discussion") and (st["managed"].get("discussion_batch") or {}).get("gate"),
                        "the discussion closed at the gate")
        time.sleep(0.5)  # a batch sent at once would have replaced the held message by now
        st, _ = L.state()
        tc.assertEqual(next(m for m in st["messages"] if m["id"] == held["id"])["status"], "held")  # not sent yet
        return held

    def test_closed_at_the_plan_gate_goes_with_the_first_instruction(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(controller={"approve_plan": True}, mgr={"milestones": 1, "replies": {
            "kickoff": ['We agreed on short tables.\n\n<message to="Jordan">BATCH-MARK: keep every table short; '
                        'get the first part done.</message>\n\n<rubric>\n- tables short\n- part done\n- checked\n'
                        '</rubric>']}})])
        L = Live(self, cfg)
        L.gate("plan")
        held = self.discuss_with_a_held_message(self, L)
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": L.gate("plan")[0]["managed"]["gate"]["key"]},
                  "approve")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertFalse([t for t in ts if t["purpose"] == "discussion"])  # no turn of its own: it went with the kickoff
        kick = next(t for t in ts if t["agent"] == "boss" and t["purpose"] == "kickoff")
        self.assertIn("HELD-MARK", kick["input"])  # the manager sees what was held
        self.assertIn("We closed our discussion before this decision", kick["input"])
        old = next(m for m in rec.state["messages"] if m["id"] == held["id"])
        self.assertEqual(old["status"], "superseded")
        self.assertNotIn("discussion_carry", rec.state["managed"])
        crit = [t for t in ts if t["agent"] == "critic"]
        self.assertTrue(any("BATCH-MARK" in t["input"] for t in crit))
        self.assertFalse(any("HELD-MARK" in t["input"] for t in crit))
        assert_no_role_leaks(self, rec, also=["HELD-MARK", "We agreed on short tables", "discussion"])

    def test_closed_at_the_milestone_gate_then_sent_back_is_one_message(self):
        """The case from a real run: the discussion closed at a milestone gate, then my note sent it back. Before,
        the critic got two messages (one per event) and the worker both; now the manager writes one."""
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(controller={"approve_milestones": True}, mgr={
            "milestones": 1, "replies": {"feedback": [
                '<message to="Jordan">BATCH-MARK: keep every table short, and finish the summary first.</message>']}})])
        L = Live(self, cfg)
        st, _ = L.gate("milestone")
        key = st["managed"]["gate"]["key"]
        held = self.discuss_with_a_held_message(self, L)
        L.control({"kind": "gate", "decision": "revise", "message": "NOTE-R: finish the summary first.", "gate": key},
                  "sendback")
        st, _ = L.gate("milestone", not_key=key)
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": st["managed"]["gate"]["key"]}, "ok")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertFalse([t for t in ts if t["purpose"] in ("discussion", "incoming")])
        fb = [t for t in ts if t["agent"] == "boss" and t["purpose"] == "feedback"]
        self.assertEqual(len(fb), 1)  # one manager turn carries my note and the discussion
        for mark in ("NOTE-R", "HELD-MARK", "We closed our discussion before this decision"):
            self.assertIn(mark, fb[0]["input"])
        old = next(m for m in rec.state["messages"] if m["id"] == held["id"])
        self.assertEqual(old["status"], "superseded")
        ev = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual([e.get("at_gate") for e in ev if e.get("event") == "discussion_batch"], [True])
        guid = [t for t in ts if t["agent"] == "critic" and t["purpose"] == "guidance"]
        self.assertEqual(len(guid), 1)
        self.assertIn("BATCH-MARK", guid[0]["input"])
        crit = [t for t in ts if t["agent"] == "critic"]
        self.assertFalse(any("HELD-MARK" in t["input"] for t in crit))
        assert_no_role_leaks(self, rec, also=["HELD-MARK", "NOTE-R", "discussion"])

    def test_reminder_and_phases_wait_while_on(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.set_discussion(True)
        _p, sent = r.add_reminders(r.manager, "hello", "incoming")
        self.assertIn("discussion", sent)
        self.assertEqual(r.held_by("kickoff"), "discussion")
        self.assertEqual(r.held_by("work"), "")  # the critic and the worker go on
        m = r.deliver(r.manager, r.critic, "Do X.")
        self.assertEqual((m["status"], m["reason"]), ("held", RT.DISCUSSION))
        self.assertNotIn("Do X.", " ".join(r.st["fwd"].get("critic", [])))
        r.set_discussion(False)
        self.assertTrue(r.mg.get("discussion_batch"))
        self.assertNotIn("gate", r.mg["discussion_batch"])  # no gate open: the batch goes at once, as before
        self.assertEqual(r.gate_discussion(), "")  # and a later gate does not take it
        self.assertEqual(r.held_by("kickoff"), "")


class TestCriticMessageTurn(unittest.TestCase):
    """The critic's message turn says what the worker is actually doing (it said "may still be working" always,
    also at a gate, so the critic wrote the idle worker an update that waited and stacked with its next step)."""

    def test_prompt_follows_what_the_worker_does(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.mg.update(phase="gate", gate={"kind": "milestone", "key": "milestone-0-1"})
        p = r.critic_turn_prompt("wait")
        self.assertIn("Sam is not working right now: the run is waiting for my decision at a gate", p)
        self.assertIn("Don't write to Sam now", p)
        self.assertNotIn('<message to="Sam">', p)
        self.assertNotIn("may still be working", p)
        r.mg.update(phase="job", gate=None, job={"since": "x"})
        self.assertIn("its background job is still running", r.critic_turn_prompt("wait"))
        r.mg.update(phase="work", job=None)  # the step is given; the worker's turn comes next
        p = r.critic_turn_prompt("between")
        self.assertIn("its next turn starts with the step you\nalready gave", p)
        self.assertIn('<message to="Sam">', p)
        p = r.critic_turn_prompt("stage")  # the worker's turn runs
        self.assertIn("Sam is working: leave that work running", p)
        self.assertIn('<message to="Sam">', p)

    def test_message_at_a_milestone_gate_gets_the_idle_prompt(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(controller={"approve_milestones": True}, mgr={"milestones": 1})])
        L = Live(self, cfg)
        st, _ = L.gate("milestone")
        L.control({"to": ["critic"], "message": "GATE-NOTE: the tables can stay long.", "from": "boss", "via": "chat",
                   "source": "chat-001"}, "board-note")
        L.until(lambda st: any("GATE-NOTE" in m.get("body", "") and m["status"] == "delivered"
                               for m in st.get("messages") or []), "the note delivered to the critic")
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": st["managed"]["gate"]["key"]}, "ok")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        inc = [t for t in turns(rec) if t["agent"] == "critic" and t["purpose"] == "incoming"]
        self.assertEqual(len(inc), 1)
        self.assertIn("GATE-NOTE", inc[0]["input"])
        self.assertIn("is not working right now: the run is waiting for my decision at a gate", inc[0]["input"])
        self.assertNotIn("may still be working", inc[0]["input"])


class TestMilestoneFallback(unittest.TestCase):
    def test_reopen_marks_state_and_checklist(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.mg.update(milestones=[{"title": "Tables", "status": "done"}, {"title": "Prose", "status": "active"}], ms=1)
        p = r.agent_dir(r.critic) / "checklist-m1.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(B.CHECKLIST_HEAD + "\n## Step 1: tables\n\n- [x] totals added\n")
        r.control({"kind": "milestone", "action": "reopen", "ms": 0})
        r.check_control()
        ms = r.mg["milestones"][0]
        self.assertEqual(ms["status"], "open")
        ck = B.parse_checklist(p.read_text())
        self.assertEqual(ck["steps"][-1]["title"], "Step 2: Reopened")
        self.assertFalse(B.step_done(ck["steps"][-1]))
        self.assertIn("- [ ] reopened", p.read_text())
        self.assertEqual(r.mg["milestones"][1]["status"], "active")  # the current one is untouched
        self.assertFalse(r.st["fwd"].get("boss"))  # no prompt to the manager
        r.reopen_milestone(1)  # not finished: nothing to reopen
        self.assertEqual(r.mg["milestones"][1]["status"], "active")


class TestGoldenResend(unittest.TestCase):
    """A scheduled resend is one line when the board is unchanged, the changed lines when it changed; the worker
    gets only the Boundaries section."""

    def setUp(self):
        self.r = unit(Path(tempfile.mkdtemp()))
        self.r.setup_boards()
        self.r.golden_path().write_text(
            B.GOLDEN_HEAD.split("## Boundaries")[0] + "## Rules\n\n- Tables first\n- Short prose\n\n"
            "## Boundaries\n\n- Sources are read-only\n")
        self.r.check_golden()

    def sent(self, agent, role, due=True):
        """One turn of the agent: the board reminder, then the bookkeeping after the turn."""
        out = self.r.golden_board_reminder(agent, role, "Intro:", due)
        self.r.rec.update_session(agent.name, session_id="s1")
        if out:
            self.r.golden_after_turn(agent, {"reminders": ["golden_board"], "session_id": "s1"}, None)
        return out

    def test_unchanged_then_diff(self):
        c = self.r.critic
        first = self.r.golden_board_reminder(c, "critic", "Intro:", False)
        self.assertIn("- Tables first", first)
        self.assertIn("- Sources are read-only", first)
        self.sent(c, "critic")
        path = str(self.r.golden_path())
        self.assertEqual(self.r.golden_board_reminder(c, "critic", "Intro:", True), f"Standing rules unchanged: `{path}`")
        self.assertEqual(self.r.golden_board_reminder(c, "critic", "Intro:", False), "")
        self.r.golden_path().write_text(self.r.golden_path().read_text().replace("- Short prose\n", "- Plain prose\n"))
        self.r.check_golden()
        out = self.r.golden_board_reminder(c, "critic", "Intro:", False)
        self.assertIn(path, out)
        self.assertIn("- - Short prose", out)
        self.assertIn("+ - Plain prose", out)
        self.assertNotIn("Tables first", out)

    def test_worker_gets_only_boundaries(self):
        w = self.r.workers[0]
        out = self.sent(w, "worker")
        self.assertIn("Sources are read-only", out)
        self.assertNotIn("Tables first", out)
        self.assertEqual(self.r.golden_board_reminder(w, "worker", "", True), f"Standing rules unchanged: `{self.r.golden_path()}`")
        # a change outside Boundaries is no change for the worker
        self.r.golden_path().write_text(self.r.golden_path().read_text().replace("Tables first", "Tables last"))
        self.r.check_golden()
        self.assertEqual(self.r.golden_board_reminder(w, "worker", "", False), "")

    def test_no_boundaries_section_sends_nothing_to_the_worker(self):
        self.r.golden_path().write_text("## Rules\n\n- Tables first\n")
        self.r.check_golden()
        self.assertEqual(self.r.golden_board_reminder(self.r.workers[0], "worker", "Intro:", True), "")

    def test_night_reminder_schedule(self):
        self.r.st["night"] = True
        got = []
        for _ in range(11):
            _, sent = self.r.add_reminders(self.r.manager, "Do it.", "round")
            got.append("night_mode" in sent)
        self.assertEqual([i for i, x in enumerate(got) if x], [0, 5, 10])
        self.r.switch_night(False)
        self.r.switch_night(True)  # turned on again: the next manager turn has it
        _, sent = self.r.add_reminders(self.r.manager, "Do it.", "round")
        self.assertIn("night_mode", sent)


class TestStyleRules(unittest.TestCase):
    def test_each_role_gets_its_own_rules(self):
        r = unit(Path(tempfile.mkdtemp()))
        from orch import reminders as R
        text = r.style_rules_text(r.manager)
        pools = {role: set(R.style_pool(text, role)) for role in ("manager", "critic", "worker")}
        self.assertTrue(all(pools.values()))
        self.assertFalse(pools["manager"] & pools["critic"] or pools["critic"] & pools["worker"])
        self.assertTrue(any("text for me" in x for x in pools["manager"]))  # what is written for me stays the manager's
        self.assertTrue(any("concise" in x for x in pools["worker"]))
        self.assertEqual(R.style_pool(text, "verifier"), R.style_pool(text, "critic"))
        for role, pool in pools.items():
            groups = R.style_groups(text, role)
            self.assertEqual([g[0] for g in groups], R.style_pool(text, role))
            seen = set()
            for _ in range(40):
                lines = [x[2:] for x in R.style_text(text, role).splitlines()]
                self.assertIn(len(lines), {min(len(groups), 2), min(len(groups), 3)})
                hit = [next(i for i, g in enumerate(groups) if x in g) for x in lines]
                self.assertEqual(len(hit), len(set(hit)))  # never two wordings of one rule together
                seen.update(lines)
            self.assertTrue(seen - set(pool), role)  # the other wordings are used too
        g = R.style_groups("## Worker\n- Short.\n  - Brief.\n- Plain.\n", "worker")
        self.assertEqual(g, [["Short.", "Brief."], ["Plain."]])
        self.assertEqual(R.style_pool("## All\n- Shared.\n\n## Worker\n- Own.\n", "worker"), ["Shared.", "Own."])

    def test_the_system_prompt_has_the_role_rules_with_names(self):
        r = unit(Path(tempfile.mkdtemp()))
        c, w = r.critic.current_system_prompt(), r.workers[0].current_system_prompt()
        self.assertIn("How I want you to write and communicate:", c)
        self.assertIn(f"Keep messages to {r.workers[0].called} brief", c)
        self.assertNotIn("{{worker}}", c)
        self.assertIn("Keep your replies to me concise.", w)
        self.assertNotIn("Keep messages to", w)

    def test_sent_every_two_to_five_turns_and_can_be_turned_off(self):
        r = unit(Path(tempfile.mkdtemp()))
        hits = [i for i in range(30) if "style_rules" in r.add_reminders(r.workers[0], "Go.", "round")[1]]
        self.assertTrue(hits and all(2 <= b - a <= 5 for a, b in zip(hits, hits[1:])), hits)
        self.assertLessEqual(hits[0], 5)
        r.mon.controller.reminders = [x for x in r.mon.controller.reminders if x["id"] != "style_rules"]
        self.assertFalse(any("style_rules" in r.add_reminders(r.workers[0], "Go.", "round")[1] for _ in range(12)))


class TestGoldenAfterCompact(unittest.TestCase):
    def test_board_is_sent_again_after_a_compact(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.setup_boards()
        r.golden_path().write_text(B.GOLDEN_HEAD + "\n- Tables before prose\n")
        g = r.check_golden()
        r.rec.update_session("critic", session_id="s1")
        r.st.setdefault("golden_seen", {})["critic"] = {"hash": g["hash"], "session": "s1", "text": r.golden_body()}
        self.assertEqual(r.golden_board_reminder(r.critic, "critic", "", False), "")
        r.after_compact(r.critic, "compact between steps")
        self.assertIn("Tables before prose", r.golden_board_reminder(r.critic, "critic", "", False))

    def test_auto_compact_in_a_turn_counts(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.setup_boards()
        r.st.setdefault("golden_seen", {})["writer"] = {"hash": "h", "session": "s1"}
        rec = {"status": "ok", "output": "Done.", "turn_id": "r001-t01-writer", "meta": {"compacted": True}}
        with mock.patch.object(MonitorRunner, "turn", return_value=rec):
            r.turn(r.workers[0], "go", 1, "work")
        self.assertNotIn("writer", r.st["golden_seen"])


# --------------------------------------------------------------------------- B9


class TestWaitGateSendBack(unittest.TestCase):
    def test_send_back_goes_to_the_manager(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(
            mgr={"milestones": 1, "replies": {"pause": [
                "Please run the cluster job.\n\n<decision>PAUSE</decision>",
                '<message to="Jordan">Meanwhile, draft the tables from the pilot.</message>\n<decision>CONTINUE</decision>']}},
            critic={"replies": {"verify": [
                worker_instruction("<pause>Someone has to run the cluster job.</pause>\n<checklist>- [ ] job run</checklist>\n<verdict>FAIL</verdict>"),
                "Good.\n<checklist>- [x] job run</checklist>\n<verdict>PASS</verdict>"]}})])
        L = Live(self, cfg)
        st, _ = L.gate("wait")
        L.control({"kind": "gate", "decision": "revise", "message": "NOTE-X: I can't run it this week.",
                   "gate": st["managed"]["gate"]["key"]}, "sendback")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        pauses = [t for t in turns(rec) if t["agent"] == "boss" and t["purpose"] == "pause"]
        self.assertEqual(len(pauses), 2)
        self.assertIn("NOTE-X", pauses[1]["input"])
        self.assertIn("I'm sending it back", pauses[1]["input"])
        self.assertEqual(rec.state["managed"]["gates"][0]["decision"], "revise")
        g = next(t for t in turns(rec) if t["agent"] == "critic" and t["purpose"] == "guidance")
        self.assertIn("draft the tables from the pilot", g["input"])
        assert_no_role_leaks(self, rec, also=["NOTE-X"])


class TestNightSwitchDuringQuestion(unittest.TestCase):
    def test_switch_waits_until_the_question_closes(self):
        r = unit(Path(tempfile.mkdtemp()))
        q = r.new_question(r.manager, "human", "Which model?\nA) one\nB) two", "t0")
        r.control({"kind": "night", "on": True})
        r.check_control()
        self.assertFalse(r.st.get("night"))
        self.assertTrue(r.st.get("night_queued"))
        r.control({"kind": "answer", "qid": q["id"], "message": "A"})
        r.check_control()
        self.assertEqual(q["status"], "answered")
        self.assertTrue(r.st.get("night"))
        self.assertNotIn("night_queued", r.st)

    def test_board_refuses_the_switch(self):
        from orch.board import Board, serve
        import urllib.error
        import urllib.request
        tmp = Path(tempfile.mkdtemp())
        r = unit(tmp)
        r.new_question(r.manager, "human", "Which model?", "t0")
        r.rec.save()
        cfg = load_config(str(tmp / "cfg.json"))
        srv, url = serve({"m": Board("m", lambda: r.rec.run_dir, cfg.monitor("m"))}, port=8893)
        try:
            req = urllib.request.Request(url + "api/night", data=json.dumps({"on": True}).encode())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 409)
            self.assertFalse([p for p in (r.rec.run_dir / "inbox").glob("*.json")
                              if json.loads(p.read_text()).get("kind") == "night"])
        finally:
            srv.shutdown()
            srv.server_close()


class TestBlockingQuestions(unittest.TestCase):
    def test_blocking_question_holds_new_work_until_answered(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(
            mgr={"milestones": 1, "replies": {"answer": [
                "<question>Do we cite the new run?\nA) yes\nB) no\nI recommend A.</question>"]}},
            critic={"replies": {"verify": [
                worker_instruction("<question>Do we cite the new run?</question>\n<checklist>- [ ] cited</checklist>\n<verdict>FAIL</verdict>"),
                "Good.\n<checklist>- [x] cited</checklist>\n<verdict>PASS</verdict>"]}})])
        L = Live(self, cfg)
        st, rd = L.until(lambda st: (st["managed"].get("held") or {}).get("why", "").startswith("question"), "the hold")
        n = len(list((rd / "rounds").glob("r*/t*.json")))
        time.sleep(0.6)
        self.assertEqual(len(list((rd / "rounds").glob("r*/t*.json"))), n)  # no new turn while it waits
        q = next(x for x in st["questions"] if x["to"] == "human")
        self.assertTrue(q["blocking"])
        L.control({"kind": "answer", "qid": q["id"], "message": "A"}, "answer")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        amend = next(t for t in turns(rec) if t["purpose"] == "amend")
        self.assertIn("A) yes", amend["input"])  # my answer reached the critic before it went on
        self.assertNotIn("held", rec.state["managed"])

    def test_non_blocking_question_lets_the_work_go_on(self):
        status, rec = run(make_cfg(Path(tempfile.mkdtemp()), [managed(
            mgr={"milestones": 1, "replies": {"answer": [
                '<question blocking="no">Do we cite the new run?\nA) yes\nB) no</question>\n'
                '<message to="Jordan">Assume yes for now.</message>']}},
            critic={"replies": {"verify": [
                worker_instruction("<question>Do we cite it?</question>\n<checklist>- [ ] cited</checklist>\n<verdict>FAIL</verdict>"),
                "Good.\n<checklist>- [x] cited</checklist>\n<verdict>PASS</verdict>"]}})]))
        self.assertEqual(status, "completed")
        q = next(x for x in rec.state["questions"] if x["to"] == "human")
        self.assertEqual((q["status"], q["blocking"]), ("open", False))

    def test_night_mode_decides_and_records(self):
        r = unit(Path(tempfile.mkdtemp()))
        r.st["night"] = True
        r.after_manager({"output": "I chose A.\n<question>Which model?\nA) one\nB) two</question>", "turn_id": "t1"},
                        relay=False)
        q = r.st["questions"][-1]
        self.assertEqual(q["status"], "night")
        self.assertEqual(r.held_by("work"), "")
        self.assertTrue(any(d["by"] == "manager (night mode)" for d in r.st["decisions"]))


class TestGateSummary(unittest.TestCase):
    def test_summary_at_plan_and_milestone_gates(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(controller={"approve_plan": True, "approve_milestones": True},
                                                          mgr={"milestones": 1})])
        L = Live(self, cfg)
        st, _ = L.gate("plan")
        self.assertIn("checked against the goal", st["managed"]["gate"]["summary"])
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": st["managed"]["gate"]["key"]}, "ok1")
        st, _ = L.gate("milestone")
        g = st["managed"]["gate"]
        self.assertIn("Changed: the part is done", g["summary"])
        self.assertNotIn("<summary>", g["text"])
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": g["key"]}, "ok2")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        ts = turns(rec)
        self.assertIn("`<summary>`", next(t for t in ts if t["purpose"] == "plan")["input"])
        review = next(t for t in ts if t["purpose"] == "review")
        self.assertIn("Score it against the rubric and decide.", review["input"])  # the gate rules are in the protocol
        self.assertIn("Changed: the part is done", rec.state["final_report"])
        assert_no_role_leaks(self, rec, also=["Changed: the part is done"])


class TestReviewFile(unittest.TestCase):
    def test_request_write_comment_send_back(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), [managed(controller={"approve_plan": True}, mgr={"milestones": 1, "replies": {
            "review_file": ["REVIEW-MARK\n\n## Overview\n\nOne milestone, not started: verdict pending.\n\n"
                            "## Plan\n\nThe plan covers the goal."],
            "incoming": ["Thanks: I will shorten the overview."]}})])
        L = Live(self, cfg)
        st, rd = L.gate("plan")
        L.control({"kind": "review"}, "review")
        st, _ = L.until(lambda st: (st.get("review_files") or [{}])[-1].get("status") == "written", "the review file")
        e = st["review_files"][-1]
        p = Path(e["path"])
        self.assertEqual(p.parent.name, "reviews")
        self.assertIn("REVIEW-MARK", p.read_text())
        req = next(t for t in turns_dir(rd) if t["purpose"] == "review_file")
        self.assertIn(str(p), req["input"])
        self.assertIn("Overview first", req["input"])
        p.write_text(p.read_text().replace("The plan covers the goal.",
                                           "The plan covers the goal.\n\n> COMMENT-MARK: say which tables come first."))
        L.control({"kind": "review_comments", "path": str(p)}, "comments")
        L.until(lambda st: any(t for t in (st.get("messages") or []) if "COMMENT-MARK" in t.get("body", "")
                               and t["status"] == "delivered"), "the comments delivered")
        L.control({"kind": "gate", "decision": "approve", "message": "", "gate": L.gate("plan")[0]["managed"]["gate"]["key"]},
                  "approve")
        status, rec = L.done()
        self.assertEqual(status, "completed")
        got = next(t for t in turns(rec) if t["agent"] == "boss" and "COMMENT-MARK" in t["input"])
        self.assertIn("+> COMMENT-MARK: say which tables come first.", got["input"])
        self.assertEqual(rec.state["review_files"][-1]["status"], "comments sent")
        assert_no_role_leaks(self, rec, also=["REVIEW-MARK", "COMMENT-MARK", "review file"])

    def test_board_lists_the_file_with_a_link(self):
        from orch.board import Board
        tmp = Path(tempfile.mkdtemp())
        r = unit(tmp)
        p = r.reviews_dir() / "20261119-120000.md"
        p.parent.mkdir(parents=True)
        p.write_text("# Review\n")
        r.st["review_files"] = [{"path": str(p), "status": "written"}]
        r.rec.save()
        d = Board("m", lambda: r.rec.run_dir, load_config(str(tmp / "cfg.json")).monitor("m")).data()
        f = d["review_files"][0]
        self.assertTrue(f["exists"])
        self.assertEqual(f["href"], f"log/m/reviews/{p.name}")


def turns_dir(rd):
    out = []
    for p in sorted((Path(rd) / "rounds").glob("r*/t*.json")):
        out.append(read_json(p))
    return out


if __name__ == "__main__":
    unittest.main()
