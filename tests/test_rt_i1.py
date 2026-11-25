"""Package I, discussion (docs/redesign.md DISC-1 to 6). Scenario 13 and the night-mode part of 18 of 4.4."""
import unittest

from rt_helpers import *
from orch.runtime.discussion import module

K = TextKey


def draft_sends():
    """What a critic's reply carries: a message to a worker, its report to the manager, and one for the user."""
    return [Send(message("d1", "worker-1", "critic", status="waiting")),
            Send(message("d2", "manager", "critic", status="waiting", key=K.CRITIC_REPORT)),
            ToUser(message("d3", USER, "critic", status="waiting", needs_answer=False))]


class TestStart(unittest.TestCase):
    def test_scenario_13_DISC_1_discussion_starts_while_the_critic_writes(self):
        s = snapshot(agent("critic", status="writing"), turn("critic", status="running"))
        actions = decide(module(), s, event(DiscussionStart, s, "critic"))
        d = assert_has(self, actions, SetDiscussion, agent="critic").discussion
        self.assertEqual((d.phase, d.by_terminal), ("open", False))
        assert_has(self, actions, CommandReply, ok=True)

    def test_scenario_13_DISC_1_what_it_writes_to_others_is_a_draft(self):
        s = snapshot(Discussion("critic"))
        s, ev = turn_ended(s, "critic", "Done.")
        actions = decide(module(), s, ev, prior=draft_sends())
        assert_has(self, actions, Hold, message_id="d1", reason=HoldReason.DRAFT)
        assert_has(self, actions, Hold, message_id="d2", reason=HoldReason.DRAFT)
        self.assertEqual(len(find(actions, Hold)), 2)  # the user's message is no draft

    def test_DISC_1_a_discussion_that_ended_while_the_turn_wrote_drops_its_drafts(self):
        s, ev = turn_ended(snapshot(), "critic", "Done.", draft=True)
        actions = decide(module(), s, ev, prior=draft_sends())
        assert_has(self, actions, Drop, message_id="d1", reason=DropReason.DRAFT_ENDED)
        assert_none(self, actions, Hold)

    def test_DISC_1_a_normal_turn_has_no_holds(self):
        s, ev = turn_ended(snapshot(), "critic", "Done.")
        self.assertEqual(decide(module(), s, ev, prior=draft_sends()), [])

    def test_DISC_1_the_manager_too(self):
        s = snapshot(Discussion("manager"))
        s, ev = turn_ended(s, "manager", "Plan.")
        send = Send(message("d1", "critic", "manager", status="waiting"))
        assert_has(self, decide(module(), s, ev, prior=[send]), Hold, message_id="d1")

    def test_DISC_5_in_a_critic_discussion_nothing_reaches_the_manager(self):
        s = snapshot(Discussion("critic"))
        s, ev = turn_ended(s, "critic", "Summary for the manager.")
        prior = draft_sends()
        actions = prior + decide(module(), s, ev, prior=prior)
        self.assertEqual(sent_to(actions, "manager"), [])
        self.assertEqual(sent_to(actions, "worker-1"), [])
        self.assertEqual(len(sent_to(actions, USER)), 1)

    def test_DISC_6_a_worker_has_no_discussion_mode(self):
        s = snapshot()
        actions = decide(module(), s, event(DiscussionStart, s, "worker-1"))
        assert_has(self, actions, CommandReply, ok=False)
        assert_none(self, actions, SetDiscussion)
        self.assertEqual(actions[0].text.values["why"], "wrong_role")

    def test_DISC_6_a_reply_of_a_worker_is_no_draft(self):
        s, ev = turn_ended(snapshot(Discussion("critic")), "worker-1", "Done.")
        self.assertEqual(decide(module(), s, ev, prior=[Send(message("w", "critic", "worker-1"))]), [])

    def test_a_discussion_is_started_once(self):
        s = snapshot(Discussion("manager"))
        self.assertEqual(decide(module(), s, event(DiscussionStart, s, "manager"))[0].text.values["why"], "already")
        s = snapshot(agent("manager", lifecycle="closed"))
        self.assertEqual(decide(module(), s, event(DiscussionStart, s, "manager"))[0].text.values["why"], "closed")


class TestMessagesInADiscussion(unittest.TestCase):
    def test_scenario_13_DISC_2_messages_and_job_ends_enter_at_once(self):
        s = snapshot(Discussion("critic"))
        s, ev = turn_ended(s, "worker-1", "Result.")
        arriving = [Send(message("w1", "critic", "worker-1", status="waiting"))]
        self.assertEqual(decide(module(), s, ev, prior=arriving), [])  # nothing holds it
        self.assertEqual(sent_to(arriving, "critic"), [arriving[0].message])

    def test_assumption_4_questions_and_decisions_of_a_draft_are_ignored_by_their_owners(self):
        s = snapshot(Discussion("manager"))
        s, ev = turn_ended(s, "manager", reply=Reply(untagged="x", questions=[Question("Which?")]))
        self.assertTrue(s.drafting("manager"))


class TestApply(unittest.TestCase):
    def test_scenario_13_DISC_3_apply_sends_one_message_when_the_reply_has_ended(self):
        draft = message("d1", "worker-1", "critic", status="waiting", holds=["draft"])
        s = snapshot(Discussion("critic"), draft)
        actions = decide(module(), s, event(DiscussionApply, s, "critic", text="Go ahead."))
        send = assert_has(self, actions, Send, recipient="critic", key=K.DISCUSSION_APPLY)
        self.assertEqual(send.message.text.values, {"request": "Go ahead."})
        self.assertTrue(send.message.when_idle)  # waits until its current reply has ended
        d = assert_has(self, actions, SetDiscussion, agent="critic").discussion
        self.assertEqual((d.phase, d.apply_message), ("applying", send.message.id))
        assert_has(self, actions, Drop, message_id="d1", reason=DropReason.DRAFT_ENDED)  # the new message replaces it
        assert_has(self, actions, CommandReply, ok=True)

    def test_DISC_3_apply_without_a_discussion_is_refused(self):
        s = snapshot()
        self.assertEqual(decide(module(), s, event(DiscussionApply, s, "critic"))[0].text.values["why"], "not_open")

    def test_DISC_3_applying_twice_is_refused(self):
        s = snapshot(Discussion("critic", phase="applying", apply_message="ap"))
        self.assertEqual(decide(module(), s, event(DiscussionApply, s, "critic"))[0].text.values["why"], "already")

    def applying(self, *extra):
        ap = message("ap", "critic", USER, key=K.DISCUSSION_APPLY, values={"request": ""})
        return snapshot(Discussion("critic", phase="applying", apply_message="ap"), ap, *extra)

    def test_scenario_13_DISC_3_the_reply_to_the_apply_message_follows_the_normal_rules(self):
        s, ev = turn_ended(self.applying(), "critic", reply=Reply(untagged="Report.", messages=[OutMessage("worker-1", "Do X.")]),
                           started_by=["ap"])
        prior = [Send(message("a1", "worker-1", "critic", status="waiting")),
                 Send(message("a2", "manager", "critic", status="waiting", key=K.CRITIC_REPORT))]
        actions = decide(module(), s, ev, prior=prior)
        assert_none(self, actions, Hold)  # not a draft: the worker and the manager get their parts
        assert_has(self, actions, SetDiscussion, agent="critic", discussion=None)

    def test_DISC_3_a_turn_that_began_before_applying_stays_a_draft(self):
        s, ev = turn_ended(self.applying(), "critic", "Late draft.")
        actions = decide(module(), s, ev, prior=[Send(message("a1", "worker-1", "critic", status="waiting"))])
        assert_has(self, actions, Hold, message_id="a1")
        assert_none(self, actions, SetDiscussion)

    def test_DISC_3_drafts_still_held_when_the_apply_reply_ends_are_dropped(self):
        left = message("d9", "worker-1", "critic", status="waiting", holds=["draft"])
        s, ev = turn_ended(self.applying(left), "critic", "Report.", started_by=["ap"])
        assert_has(self, decide(module(), s, ev), Drop, message_id="d9", reason=DropReason.DRAFT_ENDED)

    def test_DISC_3_a_failed_apply_turn_does_not_end_the_discussion(self):
        s, ev = turn_ended(self.applying(), "critic", error="transient", started_by=["ap"])
        self.assertEqual(decide(module(), s, ev), [])

    def test_DISC_3_apply_waits_while_a_terminal_is_open(self):
        s = snapshot(Discussion("manager", by_terminal=True), Terminal("manager"))
        self.assertEqual(decide(module(), s, event(DiscussionApply, s, "manager"))[0].text.values["why"], "terminal_open")


class TestEnd(unittest.TestCase):
    def test_scenario_13_DISC_4_ending_drops_drafts_and_never_incoming_messages(self):
        draft = message("d1", "worker-1", "critic", status="waiting", holds=["draft"])
        incoming = message("w1", "critic", "worker-1", status="waiting")
        s = snapshot(Discussion("critic"), draft, incoming)
        actions = decide(module(), s, event(DiscussionEnd, s, "critic"))
        assert_has(self, actions, Drop, message_id="d1", reason=DropReason.DRAFT_ENDED)
        assert_none(self, actions, Drop, message_id="w1")
        assert_has(self, actions, SetDiscussion, agent="critic", discussion=None)
        assert_has(self, actions, CommandReply, ok=True)

    def test_DISC_4_a_draft_still_being_written_is_dropped_when_it_ends(self):
        s = snapshot(Discussion("critic"))
        end = decide(module(), s, event(DiscussionEnd, s, "critic"))
        self.assertTrue(find(end, SetDiscussion))
        s2, ev = turn_ended(snapshot(), "critic", "Done.", draft=True)  # Turn.draft stays after the discussion
        assert_has(self, decide(module(), s2, ev, prior=draft_sends()), Drop, message_id="d2")

    def test_DISC_4_the_other_agents_drafts_are_kept(self):
        mine = message("m1", "worker-1", "manager", status="waiting", holds=["draft"])
        theirs = message("c1", "worker-1", "critic", status="waiting", holds=["draft"])
        s = snapshot(Discussion("manager"), mine, theirs)
        actions = decide(module(), s, event(DiscussionEnd, s, "manager"))
        assert_has(self, actions, Drop, message_id="m1")
        assert_none(self, actions, Drop, message_id="c1")

    def test_DISC_4_cannot_end_while_applying(self):
        s = snapshot(Discussion("critic", phase="applying", apply_message="ap"))
        self.assertEqual(decide(module(), s, event(DiscussionEnd, s, "critic"))[0].text.values["why"], "already")


class TestTerminalAndMilestones(unittest.TestCase):
    def test_TERM_2_a_terminal_on_the_manager_or_critic_starts_a_discussion(self):
        s = snapshot()
        prior = [OpenTerminal(Terminal("critic", T0), "c1")]
        d = assert_has(self, decide(module(), s, event(TerminalOpen, s, "critic"), prior=prior), SetDiscussion).discussion
        self.assertTrue(d.by_terminal)

    def test_TERM_3_a_terminal_on_a_worker_starts_none(self):
        s = snapshot()
        prior = [OpenTerminal(Terminal("worker-1", T0), "c1")]
        self.assertEqual(decide(module(), s, event(TerminalOpen, s, "worker-1"), prior=prior), [])

    def test_TERM_2_an_open_discussion_stays_as_it_is(self):
        s = snapshot(Discussion("critic"))
        prior = [OpenTerminal(Terminal("critic", T0), "c1")]
        self.assertEqual(decide(module(), s, event(TerminalOpen, s, "critic"), prior=prior), [])

    def test_MS_11_a_change_request_inside_a_discussion_is_remembered(self):
        s = snapshot(Discussion("manager"))
        prior = [Send(message("q", "manager", USER, key=K.MILESTONE_CHANGE_REQUEST, status="waiting",
                              values={"milestones": []})), CommandReply("c1")]
        d = assert_has(self, decide(module(), s, event(ChangeMilestones, s, "", text="x"), prior=prior), SetDiscussion).discussion
        self.assertTrue(d.milestone_change)

    def test_scenario_18_night_mode_changes_nothing_for_a_discussion(self):
        s = snapshot(Discussion("manager"), night=True)
        s, ev = turn_ended(s, "manager", "Plan.")
        assert_has(self, decide(module(), s, ev, prior=[Send(message("d1", "critic", "manager", status="waiting"))]),
                   Hold, reason=HoldReason.DRAFT)


if __name__ == "__main__":
    unittest.main()
