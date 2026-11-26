"""Package G, milestones (docs/redesign.md 2.5): MS-1 to 7, 10 to 14. Scenarios 5, 15 and 18 of 4.4."""
import unittest

from rt_helpers import *
from orch.runtime import milestones as M
from orch.runtime.milestones import module

K = TextKey
CK = "/run/critic/checklist-m1.md"


def ck(complete=True, digest="d1", path=CK, error=""):
    steps = [ChecklistStep("Step 1: x", complete, [(complete, "item")])] if not error else []
    return Checklist(path=path, digest=digest, steps=steps, complete=complete and not error, error=error)


def base(*records, **kw):
    """m1 current with a checklist, a critic with a state file (so the checklist path is known)."""
    kw.setdefault("workers", 1)
    recs = list(records)
    if not any(isinstance(r, Milestone) for r in recs):
        recs.append(milestone("m1", checklist_path=CK, started=T0 - 500))
    if not any(isinstance(r, Checklist) for r in recs):
        recs.append(ck())
    recs += [agent("critic", state_file="/run/critic/state.md"), RunInfo(run_dir="/run")]
    return snapshot(*recs, **kw)


def pipeline(snap, ev, names=("routing", "gates", "terminal", "discussion", "milestones", "golden")):
    """The modules in the engine's order, each given what the earlier ones decided (`prior`), as in 3.5."""
    import importlib
    prior = []
    for n in names:
        try:
            mod = importlib.import_module(f"orch.runtime.{n}").module()
        except ImportError:
            raise unittest.SkipTest(f"{n} is not merged yet")
        prior += decide(mod, snap, ev, tuple(prior))
    return prior


def ms_of(actions):
    return find(actions, SetMilestones)[0].milestones if find(actions, SetMilestones) else None


def by_id(ms, mid):
    return next(m for m in ms if m.id == mid)


class TestChecklistFile(unittest.TestCase):
    def test_MS_1_a_step_passes_by_itself_when_its_items_are_ticked(self):
        text = "## Step 1: a\n- [x] one\n- [x] two\n\n## Step 2: b\n- [x] three\n- [ ] four\n"
        c = M.read_checklist("p", text)
        self.assertEqual([s.done for s in c.steps], [True, False])
        self.assertFalse(c.complete)
        done = M.read_checklist("p", text.replace("- [ ] four", "- [x] four"))
        self.assertTrue(done.complete)
        self.assertNotEqual(done.digest, c.digest)

    def test_MS_1_standing_rules_must_be_ticked_too(self):
        c = M.read_checklist("p", "## Standing rules\n- [ ] keep\n## Step 1: a\n- [x] one\n")
        self.assertFalse(c.complete)

    def test_MS_3_steps_are_data_for_the_progress_panel(self):
        c = M.read_checklist("p", "## Step 1: a\n- [x] one\n## Step 2: b\n- [ ] two\n")
        self.assertEqual([s.title for s in c.steps], ["Step 1: a", "Step 2: b"])
        self.assertEqual(c.steps[1].items, [(False, "two")])

    def test_MS_2_missing_or_empty_file_is_not_complete(self):
        self.assertEqual(M.read_checklist("p", None).error, "missing")
        c = M.read_checklist("p", "# Checklist\n\nnothing yet\n")
        self.assertEqual((c.complete, c.error), (False, "empty"))

    def test_MS_2_comment_changes_do_not_change_the_digest(self):
        a = M.read_checklist("p", "## Step 1: a\n- [x] one\n")
        b = M.read_checklist("p", "<!-- note -->\n## Step 1: a\n\n- [x] one\n")
        self.assertEqual(a.digest, b.digest)


class TestUnreadable(unittest.TestCase):
    def snap(self, **kw):
        s = base(ck(error="missing", complete=False), **kw)
        s.turns["critic"] = turn("critic", ended=T0 - 5, status="ok")
        return s

    def test_MS_2_the_critic_is_asked_to_fix_it_once(self):
        s = self.snap()
        actions = decide(module(), s, tick(s))
        send = assert_has(self, actions, Send, recipient="critic", key=K.CHECKLIST_UNREADABLE)
        self.assertEqual(send.message.text.values["problem"], "missing")
        self.assertEqual(ms_of(actions)[0].asked_to_fix, "missing")
        s2 = snapshot(milestone("m1", checklist_path=CK, started=T0 - 500, asked_to_fix="missing"),
                      ck(error="missing", complete=False), agent("critic", state_file="/run/critic/state.md"))
        self.assertEqual(decide(module(), s2, tick(s2)), [])

    def test_MS_2_not_before_the_critic_had_a_turn(self):
        s = base(ck(error="missing", complete=False))
        assert_none(self, decide(module(), s, tick(s)), Send)

    def test_MS_2_an_unreadable_checklist_never_notifies_the_manager(self):
        s = self.snap()
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)


DONE = "r001-t01-critic"  # the turn of the critic that ended with <done/> (the default turn id of the helpers)


class TestReviewTrigger(unittest.TestCase):
    def test_MS_4_a_done_turn_tells_the_manager_once(self):
        s = base(milestone("m1", checklist_path=CK, started=T0 - 500, done_turn=DONE))
        n = assert_has(self, decide(module(), s, tick(s)), NotifyReviewReady, milestone_id="m1", digest=DONE,
                       recipient="manager")
        self.assertEqual(n.message.key, K.CHECKLIST_COMPLETE)
        self.assertEqual(n.message.text.values["milestone"], "m1")
        self.assertEqual(n.message.text.values["rubric_path"], "/run/rubric-1.md")
        s2 = base(milestone("m1", checklist_path=CK, notified=DONE, done_turn=DONE))
        assert_none(self, decide(module(), s2, tick(s2)), NotifyReviewReady)

    def test_MS_4_a_ticked_checklist_without_a_done_turn_tells_nothing(self):
        s = base()  # the checklist is complete, the critic never said <done/>
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)

    def test_MS_4_a_critic_done_turn_starts_the_review(self):
        s = base(milestone("m1", checklist_path=CK, started=T0 - 500))
        s, ev = turn_ended(s, "critic", reply=Reply(untagged="All done.", done=True))
        actions = decide(module(), s, ev)
        self.assertEqual(ms_of(actions)[0].done_turn, DONE)
        assert_has(self, actions, NotifyReviewReady, milestone_id="m1", digest=DONE)

    def test_MS_4_a_done_turn_before_the_milestone_started_does_not_count(self):
        s = base(milestone("m1", checklist_path=CK, started=T0 - 10))
        s, ev = turn_ended(s, "critic", reply=Reply(untagged="All done.", done=True))  # started at T0 - 30
        self.assertEqual(find(decide(module(), s, ev), NotifyReviewReady), [])

    def test_MS_4_a_second_done_turn_asks_again_and_no_other_turn_does(self):
        s = base(milestone("m1", checklist_path=CK, started=T0 - 500, notified=DONE, done_turn=DONE))
        s, ev = turn_ended(s, "critic", reply=Reply(untagged="Not yet.", done=False))
        self.assertEqual(find(decide(module(), s, ev), NotifyReviewReady), [])
        s, ev = turn_ended(s, "critic", reply=Reply(untagged="All done.", done=True))
        s.turns["critic"].id = "r002-t01-critic"
        actions = decide(module(), s, ev)
        self.assertEqual(ms_of(actions)[0].done_turn, "r002-t01-critic")
        assert_has(self, actions, NotifyReviewReady, digest="r002-t01-critic")

    def test_MS_4_waits_while_a_worker_writes_or_a_message_waits(self):
        s = base(milestone("m1", checklist_path=CK, done_turn=DONE), agent("worker-1", status="writing"))
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)
        s = base(milestone("m1", checklist_path=CK, done_turn=DONE),
                 message("w1", "worker-1", "critic", status="waiting", sent_at=0.0))
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)

    def test_MS_4_running_jobs_do_not_block_and_are_shown(self):
        s = base(milestone("m1", checklist_path=CK, done_turn=DONE), job("worker-1/j1"))
        n = assert_has(self, decide(module(), s, tick(s)), NotifyReviewReady)
        self.assertEqual(n.message.text.values["jobs"], ["worker-1: train.py"])

    def test_MS_4_not_while_paused_or_after_the_manager_accepted(self):
        s = base(paused=True)
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)
        s = base(milestone("m1", checklist_path=CK, accepted=True))
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)

    def test_scenario_5_MS_4_silent_done_reply_still_offers_the_review(self):
        s = base(milestone("m1", checklist_path=CK, started=T0 - 500))
        reply = Reply(untagged="All done.", silent=True, done=True, messages=[OutMessage("worker-1", "Thanks.")])
        s, ev = turn_ended(s, "critic", reply=reply)
        assert_has(self, decide(module(), s, ev), NotifyReviewReady, milestone_id="m1")

    def test_MS_5_an_ordinary_critic_message_never_starts_a_review(self):
        s = base(ck(complete=False))
        s, ev = turn_ended(s, "critic", "Is this what you meant?")
        self.assertEqual(decide(module(), s, ev), [])

    def test_MS_5_no_review_for_rounds_without_progress(self):
        s = base(ck(complete=False), rounds=4000)
        self.assertEqual(decide(module(), s, tick(s)), [])


def review_snap(extra=(), **kw):
    """The manager has been told; its reply to that is the event."""
    m = message("n1", "manager", key=K.CHECKLIST_COMPLETE,
                values={"milestone": "m1", "title": "Milestone m1", "text": "It works."})
    return base(milestone("m1", checklist_path=CK, notified="d1", rubric=["works"]), m, *extra, **kw)


def manager_reply(s, **kw):
    return turn_ended(s, "manager", reply=Reply(untagged="Verdict.", **kw), started_by=["n1"])


class TestDecision(unittest.TestCase):
    def test_MS_6_accept_opens_the_approval_gate_with_rubric_and_score(self):
        s, ev = manager_reply(review_snap(), decision="ACCEPT", score=4.5, summary="Good.")
        actions = pipeline(s, ev)
        g = assert_has(self, actions, OpenGate)
        self.assertEqual((g.gate.kind, g.gate.milestone, g.gate.words.key), ("milestone", "m1", K.GATE_MILESTONE))
        m = ms_of(actions)[0]
        self.assertEqual((m.accepted, m.score, m.summary, m.status), (True, 4.5, "Good.", "current"))

    def test_MS_6_accept_without_a_rubric_asks_for_it(self):
        s = review_snap()
        s.milestones[0].rubric = []
        s, ev = manager_reply(s, decision="ACCEPT", score=4.0)
        actions = pipeline(s, ev)
        assert_has(self, actions, Send, recipient="manager", key=K.REVIEW_RUBRIC_MISSING)
        assert_none(self, actions, OpenGate)
        # the answer to that message is a review: its rubric is kept and its ACCEPT opens the gate (live run S)
        ask = assert_has(self, actions, Send, recipient="manager", key=K.REVIEW_RUBRIC_MISSING).message
        s.messages["rm"] = message("rm", "manager", key=ask.key, values=ask.text.values)
        s, ev = turn_ended(s, "manager", reply=Reply(untagged="Verdict.", decision="ACCEPT", score=4.8,
                                                     rubric=["works"]), started_by=["rm"])
        actions = pipeline(s, ev)
        self.assertEqual(assert_has(self, actions, OpenGate).gate.kind, "milestone")
        self.assertEqual(ms_of(actions)[0].rubric, ["works"])

    def test_MS_6_the_rubric_of_a_reply_is_kept(self):
        s, ev = manager_reply(review_snap(), decision="ACCEPT", rubric=["a", "b"], score=3.0)
        self.assertEqual(ms_of(pipeline(s, ev))[0].rubric, ["a", "b"])

    def test_MS_6_a_score_below_the_agreed_one_is_not_accepted_and_the_manager_is_asked(self):
        s, ev = manager_reply(review_snap(), decision="ACCEPT", score=2.0)
        s.settings.pass_score = 3.0
        actions = pipeline(s, ev)
        self.assertEqual([g for g in find(actions, OpenGate) if g.gate.kind == "milestone"], [])
        low = assert_has(self, actions, Send, recipient="manager", key=K.REVIEW_SCORE_LOW)
        self.assertEqual(low.message.text.values, {"milestone": "m1", "title": "Milestone m1",
                                                   "score": 2.0, "pass_score": 3.0})
        self.assertFalse(ms_of(actions)[0].accepted)

    def test_MS_7_revise_changes_nothing_and_the_review_waits_for_a_new_done(self):
        s, ev = manager_reply(review_snap(), decision="REVISE", score=2.0)
        actions = pipeline(s, ev)
        assert_none(self, actions, OpenGate)
        assert_none(self, actions, Send, recipient="critic")
        self.assertFalse(ms_of(actions)[0].accepted)
        s2 = base(milestone("m1", checklist_path=CK, notified=DONE, done_turn=DONE, score=2.0))
        self.assertEqual(decide(module(), s2, tick(s2)), [])
        s3 = base(milestone("m1", checklist_path=CK, notified=DONE, done_turn="r002-t01-critic"))
        assert_has(self, decide(module(), s3, tick(s3)), NotifyReviewReady, digest="r002-t01-critic")

    def test_MS_8_pause_decision_is_held_by_gates_and_decided_again_after_it(self):
        s, ev = manager_reply(review_snap(), decision="PAUSE")
        actions = pipeline(s, ev)
        assert_has(self, actions, HoldDecision, milestone_id="m1", decision=Decision.PAUSE)
        self.assertEqual([g for g in find(actions, OpenGate) if g.gate.kind == "milestone"], [])
        self.assertFalse(ms_of(actions)[0].accepted if ms_of(actions) else False)
        # the pause is over: the answers come with decide_again, and the new decision counts
        again = message("n2", "manager", key=K.GATE_ANSWERS, values={"answers": [], "unsent": [], "decide_again": "Milestone m1"})
        s2 = review_snap([again], paused=False)
        s2.milestones[0].held_decision = Decision.PAUSE
        s2, ev2 = turn_ended(s2, "manager", reply=Reply(untagged="Ok.", decision="ACCEPT", score=5.0), started_by=["n2"])
        self.assertEqual(len([g for g in find(pipeline(s2, ev2), OpenGate) if g.gate.kind == "milestone"]), 1)

    def test_MS_9_a_held_decision_is_not_acted_on(self):
        s, ev = manager_reply(review_snap(), decision="ACCEPT", score=4.5)
        hold = HoldDecision(milestone_id="m1", decision=Decision.ACCEPT)
        assert_none(self, decide(module(), s, ev, prior=[hold]), OpenGate)

    def test_DISC_1_a_decision_written_in_a_discussion_has_no_effect(self):
        s, ev = manager_reply(review_snap([Discussion("manager")]), decision="ACCEPT", score=4.5)
        self.assertEqual(decide(module(), s, ev), [])


class TestApproval(unittest.TestCase):
    def snap(self, **kw):
        return snapshot(milestone("m1", checklist_path=CK, accepted=True, started=T0 - 500, rubric=["r"]),
                        milestone("m2", "pending"), gate("g1", "milestone", milestone="m1"),
                        agent("critic", state_file="/run/critic/state.md"), ck(), RunInfo(run_dir="/run"), **kw)

    def approve(self, s, ok=True):
        return pipeline(s, event(GateAnswer, s, "", gate_id="g1", approve=ok))

    def test_MS_6_approval_finishes_it_and_starts_the_next_like_any_other(self):
        actions = self.approve(self.snap())
        ms = ms_of(actions)
        self.assertEqual([m.status for m in ms], ["done", "current"])
        self.assertEqual(ms[1].checklist_path, "/run/checklist-2.md")
        start = assert_has(self, actions, Send, recipient="manager", key=K.MILESTONE_START)
        self.assertEqual(start.message.text.values["last_title"], "Milestone m1")
        self.assertEqual(start.message.text.values["checklist_path"], "/run/checklist-2.md")

    def test_MS_6_asking_for_changes_keeps_it_open_and_not_accepted(self):
        actions = self.approve(self.snap(), ok=False)
        self.assertEqual((ms_of(actions)[0].status, ms_of(actions)[0].accepted), ("current", False))
        assert_none(self, actions, Send, recipient="critic")

    def test_MS_14_after_the_last_milestone_the_run_stays_open_as_finished(self):
        s = snapshot(milestone("m1", checklist_path=CK, accepted=True), gate("g1", "milestone", milestone="m1"), ck())
        actions = self.approve(s)
        self.assertEqual(ms_of(actions)[0].status, "done")
        assert_has(self, actions, SetFinished, finished=True)
        assert_has(self, actions, ToUser, key=K.NOTE_FINISHED)

    def test_MS_14_a_finished_run_gets_no_review_trigger(self):
        s = snapshot(milestone("m1", "done", checklist_path=CK, notified=""), ck(), finished=True)
        self.assertEqual(decide(module(), s, tick(s)), [])

    def test_assumption_3_next_milestone_compacts_the_settled_critic(self):
        s = snapshot(milestone("m1", "done", ended=T0 - 10), milestone("m2", checklist_path="/run/checklist-2.md",
                                                        started=T0, refreshed=["manager"]),
                     agent("critic", state_file="/run/critic/state.md"), workers=1)
        s.checklist = Checklist(path="/run/checklist-2.md")
        actions = decide(module(), s, tick(s))
        assert_has(self, actions, RefreshSession, agent="critic", mode="compact")
        assert_has(self, actions, RefreshSession, agent="worker-1", mode="compact")
        self.assertEqual(sorted(ms_of(actions)[1].refreshed), ["critic", "manager", "worker-1"])


class TestPlan(unittest.TestCase):
    LIST = "Plan:\n1. Parse — files are read\n2. Check — results agree\n3. Report - a summary exists\n"

    def snap(self, **kw):
        s = snapshot(milestones=[], **kw)
        s.run.goal = "Do it"
        return s

    def test_MS_6_the_manager_is_asked_for_a_plan_once(self):
        s = self.snap()
        actions = decide(module(), s, event(OrchStarted, s))
        send = assert_has(self, actions, Send, recipient="manager", key=K.PLAN_REQUEST)
        self.assertEqual(send.message.text.values["goal"], "Do it")
        s.messages["p"] = message("p", "manager", key=K.PLAN_REQUEST, values={"goal": "Do it"})
        self.assertEqual(decide(module(), s, tick(s)), [])

    def reply(self, text, **kw):
        s = self.snap()
        s.messages["p"] = message("p", "manager", key=K.PLAN_REQUEST, values={"goal": "g"})
        return turn_ended(s, "manager", reply=Reply(text=text, untagged=text, **kw), started_by=["p"])

    def test_MS_11_the_plan_waits_for_approval_in_a_gate(self):
        s, ev = self.reply(self.LIST, summary="Three parts.")
        actions = decide(module(), s, ev)
        g = assert_has(self, actions, OpenGate).gate
        self.assertEqual((g.kind, [m.id for m in g.new], g.new[0].title), ("plan", ["m1", "m2", "m3"], "Parse"))
        self.assertIsNone(ms_of(actions))

    def test_MS_6_an_approved_plan_starts_the_first_milestone(self):
        s, ev = self.reply(self.LIST)
        g = find(decide(module(), s, ev), OpenGate)[0].gate
        s.gates[g.id] = g
        actions = pipeline(s, event(GateAnswer, s, "", gate_id=g.id, approve=True))
        self.assertEqual([(m.id, m.status) for m in ms_of(actions)], [("m1", "current"), ("m2", "pending"), ("m3", "pending")])
        start = assert_has(self, actions, Send, recipient="manager", key=K.MILESTONE_START)
        self.assertEqual(start.message.text.values["milestone"], "m1")
        # a bare approval is not sent on its own: the start message says it, so the manager gets one turn
        answers = assert_has(self, actions, Send, recipient="manager", key=K.GATE_ANSWERS)
        assert_has(self, actions, Drop, message_id=answers.message.id)
        self.assertTrue(start.message.text.values["approved_plan"])
        # with a note, the answer goes as it is
        actions = pipeline(s, event(GateAnswer, s, "", gate_id=g.id, approve=True, text="Keep it short."))
        assert_none(self, actions, Drop)

    def test_MS_6_a_reply_without_a_list_is_asked_again(self):
        s, ev = self.reply("I think we should begin.")
        assert_has(self, decide(module(), s, ev), Send, recipient="manager", key=K.PLAN_LIST_MISSING)

    def test_scenario_18_night_mode_plan_and_milestones_act_at_once(self):
        s = self.snap(night=True)
        s.messages["p"] = message("p", "manager", key=K.PLAN_REQUEST, values={"goal": "g"})
        s, ev = turn_ended(s, "manager", reply=Reply(text=self.LIST, untagged=self.LIST), started_by=["p"])
        actions = decide(module(), s, ev)
        assert_none(self, actions, OpenGate)
        assert_has(self, actions, RecordDecision)
        self.assertEqual(ms_of(actions)[0].status, "current")
        # a milestone the manager accepts at night is approved at once and the next one starts
        s2 = snapshot(milestone("m1", checklist_path=CK, notified="d1", rubric=["r"]), milestone("m2", "pending"),
                      message("n1", "manager", key=K.CHECKLIST_COMPLETE, values={"milestone": "m1", "title": "Milestone m1", "text": "It works."}),
                      ck(), night=True)
        s2, ev2 = manager_reply(s2, decision="ACCEPT", score=5.0)
        actions = decide(module(), s2, ev2)
        assert_none(self, actions, OpenGate)
        self.assertEqual([m.status for m in ms_of(actions)], ["done", "current"])
        start = assert_has(self, actions, Send, key=K.MILESTONE_START)
        self.assertEqual(start.message.text.values["milestone"], "m2")

    def test_parse_list_reads_ids_and_wrapped_lines(self):
        got = M.parse_list("Intro\n1. [m1] Parse - read\n   more words\n2. **m2** New: x\n3. Fresh one\n")
        self.assertEqual([(i, t) for i, t, _ in got], [("m1", "Parse"), ("m2", "New"), ("", "Fresh one")])
        self.assertIn("more words", got[0][2])


LISTING = "1. [m1] Milestone m1 — It works.\n2. [m2] Second — better\n3. Brand new — added\n"


def change_snap(**kw):
    s = base(milestone("m1", checklist_path=CK, started=T0 - 500),
             milestone("m2", "pending", title="Second", text="Second — old"),
             milestone("m3", "pending", title="Third", text="Third — old"),
             message("q1", "manager", USER, key=K.MILESTONE_CHANGE_REQUEST, values={"milestones": []}), **kw)
    return s


class TestChange(unittest.TestCase):
    def test_MS_10_the_button_sends_the_list_with_ids_in_one_message(self):
        s = change_snap()
        actions = decide(module(), s, event(ChangeMilestones, s, "", text="Add a part."))
        send = assert_has(self, actions, Send, recipient="manager", sender=USER, key=K.MILESTONE_CHANGE_REQUEST)
        self.assertEqual([m["id"] for m in send.message.text.values["milestones"]], ["m1", "m2", "m3"])
        self.assertEqual(send.message.body, "Add a part.")
        assert_has(self, actions, CommandReply, ok=True)
        refused = decide(module(), s, event(ChangeMilestones, s, "", text="  "))
        assert_has(self, refused, CommandReply, ok=False)

    def test_MS_10_only_the_reply_to_that_message_is_read_as_a_change(self):
        s = base(milestone("m1", checklist_path=CK), milestone("m2", "pending"))
        s, ev = turn_ended(s, "manager", reply=Reply(text=LISTING, untagged=LISTING))
        assert_none(self, decide(module(), s, ev), OpenGate)
        s = change_snap()
        s, ev = turn_ended(s, "manager", reply=Reply(text=LISTING, untagged=LISTING), started_by=["q1"])
        self.assertEqual(find(decide(module(), s, ev), OpenGate)[0].gate.kind, "milestone_change")

    def test_MS_11_old_and_new_lists_side_by_side_and_nothing_changes_before_approval(self):
        s = change_snap()
        s, ev = turn_ended(s, "manager", reply=Reply(text=LISTING, untagged=LISTING, summary="s"), started_by=["q1"])
        actions = decide(module(), s, ev)
        g = assert_has(self, actions, OpenGate).gate
        self.assertEqual([m.id for m in g.old], ["m1", "m2", "m3"])
        self.assertEqual([(m.id, m.status) for m in g.new], [("m1", "current"), ("m2", "pending"), ("m4", "pending"), ("m3", "dropped")])
        self.assertIsNone(ms_of(actions))

    def test_MS_11_applying_a_discussion_that_changes_milestones_opens_the_same_gate(self):
        d = Discussion("manager", phase="applying", apply_message="ap", milestone_change=True)
        s = base(milestone("m1", checklist_path=CK), milestone("m2", "pending"), d, message("ap", "manager", USER, key=K.DISCUSSION_APPLY, values={"request": "", "milestone_change": True}))
        s, ev = turn_ended(s, "manager", reply=Reply(text=LISTING, untagged=LISTING), started_by=["ap"])
        self.assertEqual(find(decide(module(), s, ev), OpenGate)[0].gate.kind, "milestone_change")

    def test_MS_11_an_unreadable_reply_is_asked_again_once(self):
        s = change_snap()
        s, ev = turn_ended(s, "manager", reply=Reply(text="Sure.", untagged="Sure."), started_by=["q1"])
        assert_has(self, decide(module(), s, ev), Send, recipient="manager", key=K.MILESTONE_CHANGE_UNREADABLE)

    def approve(self, s, g):
        s.gates[g.id] = g
        return decide(module(), s, event(GateAnswer, s, "", gate_id=g.id, approve=True),
                      prior=[CloseGate(g.id, Answer(approve=True))])

    def proposal(self, s, text, **kw):
        s, ev = turn_ended(s, "manager", reply=Reply(text=text, untagged=text, **kw), started_by=["q1"])
        return s, find(decide(module(), s, ev), OpenGate)[0].gate

    def test_scenario_15_MS_12_ids_stay_dropped_ones_are_kept_and_nothing_is_sent(self):
        s, g = self.proposal(change_snap(), LISTING)
        actions = self.approve(s, g)
        ms = ms_of(actions)
        self.assertEqual([(m.id, m.status) for m in ms],
                         [("m1", "current"), ("m2", "pending"), ("m4", "pending"), ("m3", "dropped")])
        self.assertEqual(by_id(ms, "m3").text, "Third — old")  # kept
        self.assertEqual(by_id(ms, "m2").title, "Second")
        self.assertEqual(by_id(ms, "m2").text, "better")
        assert_none(self, actions, Send, recipient="critic")  # no message is forced (MS-12)

    def test_MS_12_finished_milestones_stay_as_they_are(self):
        s = snapshot(milestone("m1", "done", title="Old", text="Old — done"), milestone("m2", checklist_path=CK),
                     milestone("m3", "pending"), ck(), message("q1", "manager", USER, key=K.MILESTONE_CHANGE_REQUEST, values={"milestones": []}))
        s, g = self.proposal(s, "1. [m1] Rewritten — no\n2. [m2] Milestone m2 — It works.\n")
        ms = ms_of(self.approve(s, g))
        self.assertEqual((by_id(ms, "m1").title, by_id(ms, "m1").status), ("Old", "done"))
        self.assertEqual(by_id(ms, "m3").status, "dropped")

    def test_MS_12_the_current_milestone_is_rewritten_in_place_checklist_untouched(self):
        text = "1. [m1] Better name — new test\n2. [m2] Second — old\n"
        s, g = self.proposal(change_snap(), text, rubric=["new criterion"])
        actions = self.approve(s, g)
        m = by_id(ms_of(actions), "m1")
        self.assertEqual((m.title, m.id, m.rubric, m.checklist_path, m.status),
                         ("Better name", "m1", ["new criterion"], CK, "current"))
        assert_none(self, actions, Send, recipient="critic")

    def test_MS_13_an_open_review_or_approval_of_the_changed_milestone_is_withdrawn(self):
        s = change_snap()
        s.milestones[0].accepted, s.milestones[0].notified = True, "d1"
        s.gates["gm"] = gate("gm", "milestone", milestone="m1")
        s, g = self.proposal(s, "1. [m1] Better — new test\n2. [m2] Second — old\n", rubric=["c"])
        actions = self.approve(s, g)
        assert_has(self, actions, CloseGate, gate_id="gm", withdrawn=True)
        assert_has(self, actions, ToUser, key=K.NOTE_REVIEW_WITHDRAWN)
        m = by_id(ms_of(actions), "m1")
        self.assertEqual((m.accepted, m.rubric, m.score), (False, ["c"], None))

    def test_MS_14_milestones_added_after_the_last_start_like_any_other(self):
        s = snapshot(milestone("m1", "done", title="One"), ck(path=""),
                     message("q1", "manager", USER, key=K.MILESTONE_CHANGE_REQUEST, values={"milestones": []}),
                     finished=True)
        s.checklist = Checklist()
        s, g = self.proposal(s, "1. [m1] One — done\n2. Next part — more\n")
        actions = self.approve(s, g)
        self.assertEqual([(m.id, m.status) for m in ms_of(actions)], [("m1", "done"), ("m2", "current")])
        assert_has(self, actions, SetFinished, finished=False)
        start = assert_has(self, actions, Send, recipient="manager", key=K.MILESTONE_START)
        self.assertEqual((start.message.text.values["milestone"], start.message.text.values["last_title"]), ("m2", "One"))

    def test_MS_14_an_old_completed_run_can_be_reopened(self):
        s = snapshot(milestone("m1", "done", checklist_path=CK, done_turn=DONE), ck(), finished=True)
        actions = decide(module(), s, event(ReopenMilestone, s, "", milestone_id="m1"))
        m = ms_of(actions)[0]
        self.assertEqual((m.status, m.done_turn), ("current", ""))
        assert_has(self, actions, SetFinished, finished=False)
        assert_has(self, actions, CommandReply, ok=True)
        assert_none(self, actions, NotifyReviewReady)  # an old <done/> does not count
        again = snapshot(milestone("m1", "current", checklist_path=CK, notified="@reopened"), ck())
        assert_none(self, decide(module(), again, tick(again)), NotifyReviewReady)
        again = snapshot(milestone("m1", "current", checklist_path=CK, notified="@reopened", done_turn="r002-t01-critic"),
                         ck())
        assert_has(self, decide(module(), again, tick(again)), NotifyReviewReady, digest="r002-t01-critic")

    def test_MS_14_only_a_finished_milestone_can_be_reopened(self):
        s = snapshot(milestone("m1", checklist_path=CK), ck())
        assert_has(self, decide(module(), s, event(ReopenMilestone, s, "", milestone_id="m1")), CommandReply, ok=False)
        assert_has(self, decide(module(), s, event(ReopenMilestone, s, "", milestone_id="m9")), CommandReply, ok=False)

    def test_scenario_15_a_new_proposal_withdraws_the_open_one(self):
        s = change_snap()
        s.gates["old"] = gate("old", "milestone_change")
        s, ev = turn_ended(s, "manager", reply=Reply(text=LISTING, untagged=LISTING), started_by=["q1"])
        assert_has(self, decide(module(), s, ev), CloseGate, gate_id="old", withdrawn=True)



CV = {"milestone": "m1", "title": "Milestone m1", "text": "It works."}


class TestReview1(unittest.TestCase):
    """The review round (MS-6, 9, 13) with the real modules in the engine's order."""

    def test_MS_6_a_late_accept_never_decides_a_later_milestone(self):
        old = message("n1", "manager", key=K.CHECKLIST_COMPLETE, values=CV)  # asked about m1
        s = base(milestone("m1", "done"), milestone("m2", checklist_path=CK, notified="d9", rubric=["r"]), old)
        s, ev = manager_reply(s, decision="ACCEPT", score=5.0)
        actions = pipeline(s, ev)
        self.assertEqual(find(actions, OpenGate), [])
        self.assertFalse(any(m.accepted for m in (ms_of(actions) or [])))

    def test_MS_13_an_answer_about_a_rewritten_milestone_is_ignored(self):
        old = message("n1", "manager", key=K.CHECKLIST_COMPLETE, values={**CV, "title": "Before"})
        s = review_snap([old])
        s, ev = manager_reply(s, decision="ACCEPT", score=5.0) if False else turn_ended(
            s, "manager", reply=Reply(untagged="V.", decision="ACCEPT", score=5.0), started_by=["n1"])
        self.assertEqual(find(pipeline(s, ev), OpenGate), [])

    def test_MS_13_nor_one_after_the_review_was_withdrawn(self):
        s = review_snap()
        s.milestones[0].notified = ""
        s, ev = manager_reply(s, decision="ACCEPT", score=5.0)
        self.assertEqual(find(pipeline(s, ev), OpenGate), [])

    def test_MS_6_a_second_accept_of_an_accepted_milestone_opens_no_second_gate(self):
        s = review_snap()
        s.milestones[0].accepted = True
        s, ev = manager_reply(s, decision="ACCEPT", score=5.0)
        self.assertEqual(find(pipeline(s, ev), OpenGate), [])

    def test_MS_6_the_rubric_of_the_start_reply_is_kept(self):
        start = message("st", "manager", key=K.MILESTONE_START,
                        values={**CV, "checklist_path": CK, "last_title": ""})
        s = base(milestone("m1", checklist_path=CK, started=T0 - 5), start)
        s, ev = turn_ended(s, "manager", reply=Reply(untagged="Rubric.", rubric=["a"]), started_by=["st"])
        self.assertEqual(ms_of(pipeline(s, ev))[0].rubric, ["a"])

    def test_MS_6_a_rubric_in_a_chat_reply_changes_nothing(self):
        s, ev = turn_ended(base(), "manager", reply=Reply(untagged="Chat.", rubric=["a"]))
        self.assertIsNone(ms_of(pipeline(s, ev)))

    def test_MS_6_approval_needs_the_message_that_carries_it_and_the_milestone_still_accepted(self):
        s = TestApproval().snap()
        s.milestones[0].accepted = False  # withdrawn meanwhile
        actions = TestApproval().approve(s)
        self.assertEqual([m.status for m in ms_of(actions)] if ms_of(actions) else ["current"], ["current"])
        assert_none(self, actions, Send, key=K.MILESTONE_START)

    def test_MS_11_approval_waits_while_another_answer_gate_is_open(self):
        s = TestApproval().snap()
        s.gates["q1"] = gate("q1", "question")
        actions = TestApproval().approve(s)
        self.assertIsNone(ms_of(actions))  # gates sends the answers once the last gate is closed
        s.gates["g1"].closed, s.gates["g1"].answer = T0 - 5, Answer(approve=True)
        actions = pipeline(s, event(GateAnswer, s, "", gate_id="q1", text="Yes"))
        self.assertEqual([m.status for m in ms_of(actions)], ["done", "current"])

    def test_MS_14_a_session_started_later_or_none_needs_no_refresh(self):
        s = snapshot(milestone("m1", "done", ended=T0 - 10), milestone("m2", checklist_path="/run/checklist-2.md", started=T0),
                     RunInfo(run_dir="/run"), workers=2)
        s.checklist = Checklist(path="/run/checklist-2.md")
        s.agents["worker-1"].sessions = {}
        s.agents["worker-1"].session_key = ""
        s.agents["worker-2"].sessions[s.agents["worker-2"].session_key].started = T0 + 5
        actions = decide(module(), s, tick(s))
        self.assertEqual(sorted(a.agent for a in find(actions, RefreshSession)), ["critic"])
        self.assertIn("worker-1", ms_of(actions)[1].refreshed)
        self.assertIn("worker-2", ms_of(actions)[1].refreshed)

    def test_MS_14_a_busy_agent_is_refreshed_later(self):
        for busy in (dict(holds=["terminal"]), dict(retry=RetryState()), dict(switch=Switch("critic"))):
            s = snapshot(milestone("m1", "done", ended=T0 - 10), milestone("m2", checklist_path="/run/checklist-2.md", started=T0,
                                                                         refreshed=["manager", "worker-1"]),
                         agent("critic", **busy))
            s.checklist = Checklist(path="/run/checklist-2.md")
            self.assertEqual(find(decide(module(), s, tick(s)), RefreshSession), [])
        s = snapshot(milestone("m1", "done", ended=T0 - 10), milestone("m2", checklist_path="/run/checklist-2.md", started=T0,
                                                                     refreshed=["manager", "worker-1"]),
                     Terminal("critic"))
        s.checklist = Checklist(path="/run/checklist-2.md")
        self.assertEqual(find(decide(module(), s, tick(s)), RefreshSession), [])
        s.terminals.clear()
        s.discussions["critic"] = Discussion("critic")
        self.assertEqual(find(decide(module(), s, tick(s)), RefreshSession), [])

    def test_MS_4_the_review_waits_while_the_manager_is_in_a_discussion(self):
        s = base(Discussion("manager"))
        assert_none(self, decide(module(), s, tick(s)), NotifyReviewReady)


class TestLists(unittest.TestCase):
    def test_MS_6_numbered_lists_inside_labels_are_no_plan(self):
        s = snapshot(milestones=[])
        s.messages["p"] = message("p", "manager", key=K.PLAN_REQUEST, values={"goal": "g"})
        s, ev = turn_ended(s, "manager", reply=Reply(text="x\n" + TestPlan.LIST, untagged="Thinking.",
                                                      rubric=["1. a", "2. b"]), started_by=["p"])
        actions = pipeline(s, ev)
        self.assertEqual(find(actions, OpenGate), [])
        assert_has(self, actions, Send, recipient="manager", key=K.PLAN_LIST_MISSING)

    def test_MS_6_a_plan_after_a_question_is_read(self):
        q = gate("q1", "question", closed=T0 - 5, answer=Answer(text="Python"), sent_in="g1")
        s = snapshot(q, message("ga", "manager", key=K.GATE_ANSWERS, values={"answers": [], "unsent": [], "decide_again": ""},
                                gates=["q1"]), milestones=[])
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=TestPlan.LIST), started_by=["ga"])
        actions = pipeline(s, ev)
        self.assertEqual(find(actions, OpenGate)[0].gate.kind, "plan")
        assert_none(self, actions, Send, key=K.PLAN_LIST_MISSING)

    def test_MS_6_a_reply_with_a_question_is_no_plan(self):
        s = snapshot(milestones=[])
        s.messages["p"] = message("p", "manager", key=K.PLAN_REQUEST, values={"goal": "g"})
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=TestPlan.LIST, questions=[Question("Which?")]), started_by=["p"])
        self.assertEqual([g for g in find(pipeline(s, ev), OpenGate) if g.gate.kind == "plan"], [])

    def test_MS_11_a_change_list_after_a_question_is_read(self):
        asked = message("q1", "manager", USER, key=K.MILESTONE_CHANGE_REQUEST, values={"milestones": []},
                        reply_turn="r001-t01-manager")
        q = gate("qg", "question", closed=T0 - 5, answer=Answer(text="Yes"), source_turn="r001-t01-manager")
        ga = message("ga", "manager", key=K.GATE_ANSWERS, values={"answers": [], "unsent": [], "decide_again": ""}, gates=["qg"])
        s = base(milestone("m2", "pending"), asked, q, ga)
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=LISTING), started_by=["ga"])
        self.assertEqual(find(pipeline(s, ev), OpenGate)[0].gate.kind, "milestone_change")

    def test_MS_11_a_change_list_after_a_question_in_an_applied_discussion_is_read(self):
        ap = message("ap", "manager", USER, key=K.DISCUSSION_APPLY, values={"request": "", "milestone_change": True},
                     reply_turn="r001-t01-manager")
        q = gate("qg", "question", closed=T0 - 5, answer=Answer(text="Yes"), source_turn="r001-t01-manager")
        ga = message("ga", "manager", key=K.GATE_ANSWERS, values={"answers": [], "unsent": [], "decide_again": ""}, gates=["qg"])
        s = base(milestone("m2", "pending"), ap, q, ga)
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=LISTING), started_by=["ga"])
        self.assertEqual(find(pipeline(s, ev), OpenGate)[0].gate.kind, "milestone_change")

    def test_MS_10_a_rubric_in_a_proposal_waits_in_the_gate(self):
        s = change_snap()
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=LISTING, rubric=["new"]), started_by=["q1"])
        actions = pipeline(s, ev)
        self.assertIsNone(ms_of(actions))
        self.assertEqual(find(actions, OpenGate)[0].gate.new[0].rubric, [])

    def test_MS_11_a_reply_with_none_of_our_ids_is_asked_again(self):
        s = change_snap()
        text = "1. Alpha — a\n2. Beta — b\n"
        s, ev = turn_ended(s, "manager", reply=Reply(untagged=text), started_by=["q1"])
        actions = pipeline(s, ev)
        self.assertEqual(find(actions, OpenGate), [])
        assert_has(self, actions, Send, key=K.MILESTONE_CHANGE_UNREADABLE)

    def test_MS_11_unreadable_is_not_sent_for_a_turn_that_took_no_change_request(self):
        s = base(milestone("m2", "pending"), message("ga", "manager", key=K.GATE_ANSWERS, gates=[],
                                                     values={"answers": [], "unsent": [], "decide_again": ""}))
        s, ev = turn_ended(s, "manager", reply=Reply(untagged="Ok."), started_by=["ga"])
        assert_none(self, pipeline(s, ev), Send, key=K.MILESTONE_CHANGE_UNREADABLE)

    def test_MS_12_milestone_text_holds_what_follows_the_title(self):
        got = M.parse_list("1. Data \u2014 clean\n2. Report\n")
        self.assertEqual([(t, x) for _, t, x in got], [("Data", "clean"), ("Report", "")])
        self.assertEqual(M.merge([], got)[0].text, "clean")

    def test_MS_12_a_reopened_milestone_tells_the_manager(self):
        s = snapshot(milestone("m1", "done", title="One", checklist_path="/run/checklist-1.md"),
                     milestone("m2", "done", title="Two", checklist_path="/run/checklist-2.md"), RunInfo(run_dir="/run"),
                     ck(), finished=True)
        actions = decide(module(), s, event(ReopenMilestone, s, "", milestone_id="m2"))
        start = assert_has(self, actions, Send, recipient="manager", key=K.MILESTONE_START)
        self.assertEqual((start.message.text.values["milestone"], start.message.text.values["last_title"]), ("m2", "One"))
        self.assertEqual(by_id(ms_of(actions), "m2").notified, "@reopened")
        actions = decide(module(), s, event(ReopenMilestone, s, "", milestone_id="m1"))
        self.assertEqual(find(actions, Send)[0].message.text.values["last_title"], "")

    def test_MS_13_a_withdrawal_only_when_a_review_was_open(self):
        s = change_snap()
        s.milestones[0].notified = "d1"  # told and answered (REVISE): nothing is open
        s, g = TestChange().proposal(s, "1. [m1] Better \u2014 new test\n2. [m2] Second \u2014 old\n")
        actions = TestChange().approve(s, g)
        assert_none(self, actions, ToUser, key=K.NOTE_REVIEW_WITHDRAWN)
        s = change_snap(**{})
        s.messages["n9"] = message("n9", "manager", key=K.CHECKLIST_COMPLETE, values=CV)
        s, g = TestChange().proposal(s, "1. [m1] Better \u2014 new test\n2. [m2] Second \u2014 old\n")
        assert_has(self, TestChange().approve(s, g), ToUser, key=K.NOTE_REVIEW_WITHDRAWN)


if __name__ == "__main__":
    unittest.main()
