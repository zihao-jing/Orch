"""Package C: labels.parse and routing (LAB-1..6, MSG-7..10, GATE-2 holds, JOB-3 marking, WRK-8)."""
from __future__ import annotations

from rt_helpers import *  # noqa: F401,F403
from rt_helpers import C

from orch.runtime import labels
from orch.runtime.routing import module

R = module()


def parsed(snap, who, text, **kw):
    role = snap.agents[who].role
    return turn_ended(snap, who, reply=labels.parse(text, role), **kw)


def routed(snap, who, text, **kw):
    s, ev = parsed(snap, who, text, **kw)
    return s, decide(R, s, ev)


class TestParse(unittest.TestCase):
    def test_LAB_5_labels_anywhere_and_code_or_quotes_are_not_labels(self):
        text = ('Before <message to="worker-1">Do it.</message> middle\n```\n<message to="worker-2">no</message>\n```\n'
                '> <message to="worker-3">quoted</message>\nUse `<silent/>` like that. <silent/> end')
        r = labels.parse(text, Role.CRITIC)
        self.assertEqual([(m.to, m.body) for m in r.messages], [("worker-1", "Do it.")])
        self.assertTrue(r.silent)
        self.assertEqual(r.broken, [])
        self.assertIn("quoted", r.untagged)
        self.assertNotIn("Do it.", r.untagged)

    def test_LAB_5_broken_labels_are_reported_not_guessed(self):
        r = labels.parse('<message to="worker-1">never closed <silent/>', Role.CRITIC)
        self.assertEqual([b.problem for b in r.broken], [Problem.UNCLOSED])
        self.assertEqual(r.messages, [])
        r = labels.parse('<message>No address.</message><message to="w-1"></message>', Role.CRITIC)
        self.assertEqual([b.problem for b in r.broken], [Problem.NO_RECIPIENT, Problem.EMPTY])
        r = labels.parse("<decision>CONTINUE</decision><score>high</score>", Role.MANAGER)
        self.assertEqual([b.problem for b in r.broken], [Problem.BAD_VALUE] * 2)
        self.assertIsNone(r.decision)

    def test_LAB_1_critic_done_label_marks_a_finished_milestone(self):
        r = labels.parse("All the work is in place.\n<done/>", Role.CRITIC)
        self.assertTrue(r.done)
        self.assertEqual((r.broken, r.untagged), ([], "All the work is in place."))
        self.assertFalse(labels.parse("Still working.", Role.CRITIC).done)
        self.assertEqual([b.problem for b in labels.parse("<done/>", Role.MANAGER).broken], [Problem.REMOVED])

    def test_LAB_1_critic_has_only_message_and_silent(self):
        text = ('<question>A?</question><pause>x</pause><wait-job/><verdict>PASS</verdict><workers n="2">x</workers>'
                '<to worker="w-1">x</to><message to="worker-1">ok</message>')
        r = labels.parse(text, Role.CRITIC)
        self.assertEqual([b.problem for b in r.broken], [Problem.REMOVED] * 6)
        self.assertEqual(len(r.messages), 1)
        self.assertEqual(r.questions, [])
        self.assertEqual(r.pauses, [])

    def test_LAB_3_manager_labels(self):
        text = ('Hello.<question>Which?\nA) one\nB) two</question><pause>wait</pause><decision> revise </decision>'
                '<score>4.5</score><rubric>- a\n- b</rubric><summary>S</summary>'
                '<message to="critic">go</message><workers n="2">x</workers>')
        r = labels.parse(text, Role.MANAGER)
        self.assertEqual((r.questions[0].text, r.questions[0].options), ("Which?", ["one", "two"]))
        self.assertEqual((r.pauses, r.decision, r.score), (["wait"], Decision.REVISE, 4.5))
        self.assertEqual((r.rubric, r.summary, r.untagged), (["a", "b"], "S", "Hello."))
        self.assertEqual([b.problem for b in r.broken], [Problem.REMOVED])

    def test_LAB_2_worker_has_no_labels_and_MSG_10_clean_text(self):
        text = 'See :codex-file-citation[codex-file-citation]{line_range_start=1 path=/a/b c.py git_url=https://x/y} now <silent/>'
        r = labels.parse(text, Role.WORKER)
        self.assertEqual(r.untagged, "See /a/b c.py now <silent/>")
        self.assertFalse(r.silent)
        self.assertEqual(r.text, r.untagged)

    def test_REC_10_monitor_health(self):
        self.assertEqual(labels.parse("Off-track: it loops.\nmore", Role.MONITOR).health, Health.OFF_TRACK)
        self.assertEqual(labels.parse("Healthy.", Role.MONITOR).health, Health.HEALTHY)
        self.assertIsNone(labels.parse("No idea.", Role.MONITOR).health)


class TestWorkerReplies(unittest.TestCase):
    def test_LAB_2_and_WRK_8_final_reply_goes_to_the_critic(self):
        snap = snapshot(message("m1", "worker-1", "critic", trigger="critic"), workers=1)
        s, acts = routed(snap, "worker-1", "Done, tests pass.", started_by=["m1"])
        m = assert_has(self, acts, Send, recipient="critic", key=TextKey.WORKER_FINAL).message
        self.assertEqual((m.body, m.sender), ("Done, tests pass.", "worker-1"))
        self.assertEqual(m.text.values, {"worker": "worker-1"})

    def test_scenario_1_MSG_7_two_workers_finish_while_the_critic_writes(self):
        snap = snapshot(agent("critic", status="writing"), workers=2)
        out = []
        for w in ("worker-1", "worker-2"):
            out += routed(snap, w, f"Result of {w}.")[1]
        sends = find(out, Send, recipient="critic")
        self.assertEqual([s.message.text.values["worker"] for s in sends], ["worker-1", "worker-2"])
        self.assertEqual(len({s.message.id for s in sends}), 2)
        self.assertFalse(find(out, Hold))

    def test_JOB_3_intermediate_reply_is_marked(self):
        snap = snapshot(job("worker-1/j1", state="running"))
        s, acts = routed(snap, "worker-1", "Started the job.")
        m = assert_has(self, acts, Send, recipient="critic", key=TextKey.WORKER_INTERMEDIATE).message
        self.assertEqual(m.text.values["jobs"], ["train.py"])

    def test_LAB_6_direction_of_a_worker_reply(self):
        snap = snapshot(message("m1", "worker-1", sender="user"),
                        message("m2", "worker-1", sender="critic"), workers=1)
        _, acts = routed(snap, "worker-1", "Answer.", started_by=["m1"])
        assert_has(self, acts, ToUser, recipient=USER)
        assert_none(self, acts, Send, recipient="critic")
        _, acts = routed(snap, "worker-1", "Answer.", started_by=["m1", "m2"])
        assert_has(self, acts, Send, recipient="critic", key=TextKey.WORKER_FINAL)
        assert_none(self, acts, ToUser)
        _, acts = routed(snap, "worker-1", "Job done.", triggers=["job"])
        assert_has(self, acts, Send, recipient="critic")

    def test_LAB_6_send_to_critic_is_refused_and_nothing_goes_to_the_critic(self):
        reply = message("r1", "user", "worker-1", status="delivered", needs_answer=False, turn="t9", body="My answer.")
        reply.sender, reply.seq = "worker-1", 5
        snap = snapshot(reply)
        acts = decide(R, snap, event(SendToCritic, snap, "", message_id="r1"))
        assert_has(self, acts, CommandReply, ok=False)
        assert_none(self, acts, Send)

    def test_WRK_4_late_reply_of_a_closed_worker_is_recorded_not_routed(self):
        snap = snapshot(agent("worker-1", lifecycle="closed"))
        _, acts = routed(snap, "worker-1", "Late result.")
        m = assert_has(self, acts, Send, recipient="critic")
        assert_has(self, acts, Drop, message_id=m.message.id, reason=DropReason.WORKER_CLOSED)
        self.assertFalse(sent_to(acts, "critic"))

    def test_handover_turns_and_monitor_are_not_routed(self):
        snap = snapshot(message("h1", "worker-1", key=TextKey.HANDOVER_WRITE, values={"agent": "worker-1"}))
        s, ev = turn_ended(snap, "worker-1", "Handover text.", started_by=["h1"])
        self.assertEqual(decide(R, s, ev), [])


class TestCriticReplies(unittest.TestCase):
    def test_LAB_4_scenario_5_silent_still_sends_worker_messages(self):
        snap = snapshot()
        _, acts = routed(snap, "critic", 'Fine. <message to="worker-1">Go on.</message> <silent/>')
        assert_has(self, acts, Send, recipient="worker-1", body="Go on.", sender="critic")
        report = assert_has(self, acts, Send, recipient="manager", key=TextKey.CRITIC_REPORT)
        assert_has(self, acts, Drop, message_id=report.message.id, reason=DropReason.SILENT)
        self.assertFalse(sent_to(acts, "manager"))
        self.assertEqual([m.body for m in sent_to(acts, "worker-1")], ["Go on."])

    def test_critic_untagged_text_wakes_the_manager(self):
        _, acts = routed(snapshot(), "critic", "Blocked: need the dataset path.")
        self.assertEqual([m.body for m in sent_to(acts, "manager")], ["Blocked: need the dataset path."])

    def test_LAB_1_critic_has_no_manager_address(self):
        snap = snapshot()
        _, acts = routed(snap, "critic", '<message to="manager">Hi</message>')
        assert_has(self, acts, ReturnToSender, recipient="critic")
        self.assertFalse(sent_to(acts, "manager"))

    def test_LAB_5_returned_once_then_passed_on_as_broken(self):
        snap = snapshot()
        _, acts = routed(snap, "critic", 'Text <message to="worker-1">oops')
        ret = assert_has(self, acts, ReturnToSender, recipient="critic", key=TextKey.LABEL_RETURNED)
        self.assertEqual(ret.message.text.values["problem"], Problem.UNCLOSED)
        snap = snapshot(message("m1", "critic", key=TextKey.LABEL_RETURNED, values={"label": "x", "problem": "unclosed"}))
        _, acts = routed(snap, "critic", 'Text <message to="worker-1">oops', started_by=["m1"])
        assert_none(self, acts, ReturnToSender)
        m = assert_has(self, acts, Send, recipient="manager", key=TextKey.LABEL_BROKEN_PASSED).message
        self.assertEqual(m.text.values["sender"], "critic")

    def test_scenario_7_removed_label_broken_twice_and_closed_worker(self):
        snap = snapshot(agent("worker-2", lifecycle="closed"), workers=1)
        _, acts = routed(snap, "critic", '<verdict>PASS</verdict><message to="worker-2">hello</message>')
        ret = assert_has(self, acts, ReturnToSender, recipient="critic", key=TextKey.LABEL_RETURNED)
        self.assertEqual(ret.message.text.values["label"], "<verdict>")
        rej = assert_has(self, acts, Reject, recipient="critic", key=TextKey.UNKNOWN_WORKER)
        self.assertEqual(rej.message.text.values, {"to": "worker-2", "available": ["worker-1"]})
        assert_has(self, acts, Reject, rejected__recipient="worker-2")

    def test_MSG_8_no_workers_the_manager_asks_the_user(self):
        snap = snapshot(workers=0)
        _, acts = routed(snap, "critic", '<message to="worker-1">hello</message>')
        assert_has(self, acts, Reject, key=TextKey.UNKNOWN_WORKER)
        assert_has(self, acts, Send, recipient="manager", key=TextKey.NO_WORKERS)
        _, acts = routed(snap, "manager", '<message to="worker-1">hello</message>')
        assert_has(self, acts, Reject, recipient="manager", key=TextKey.NO_WORKERS)

    def test_MSG_8_names_resolve_by_display_name_and_role_word(self):
        snap = snapshot(agent("worker-1", display="Ada"))
        _, acts = routed(snap, "critic", '<message to="the Ada">a</message><message to="worker">b</message>')
        self.assertEqual([m.body for m in sent_to(acts, "worker-1")], ["a", "b"])


class TestManagerReplies(unittest.TestCase):
    TEXT = ('Thinking. <question>Which db?\nA) pg\nB) sqlite</question><pause>wait for it</pause>'
            '<message to="critic">Continue with tests.</message><message to="worker-1">Hi.</message>')

    def test_GATE_2_scenario_6_question_holds_every_message(self):
        snap = snapshot()
        _, acts = routed(snap, "manager", self.TEXT)
        self.assertFalse(sent_to(acts, "critic"))
        self.assertFalse(sent_to(acts, "worker-1"))
        held = {a.message_id for a in find(acts, Hold, reason=HoldReason.QUESTION)}
        self.assertEqual(len(held), 3)  # critic, worker, and the notice about the worker message
        assert_has(self, acts, ToUser, body="Thinking.")

    def test_GATE_2_no_hold_at_night_or_in_a_discussion(self):
        _, acts = routed(snapshot(night=True), "manager", self.TEXT)
        assert_none(self, acts, Hold)
        _, acts = routed(snapshot(Discussion("manager")), "manager", self.TEXT)
        assert_none(self, acts, Hold)

    def test_GATE_7_pause_without_question_sends_the_message(self):
        _, acts = routed(snapshot(), "manager", '<pause>rest</pause><message to="critic">Go.</message>')
        self.assertEqual([m.body for m in sent_to(acts, "critic")], ["Go."])

    def test_MSG_9_direct_message_to_a_worker_is_shown_to_the_critic(self):
        _, acts = routed(snapshot(), "manager", '<message to="worker-1">Use B.</message>')
        assert_has(self, acts, Send, recipient="worker-1", body="Use B.")
        n = assert_has(self, acts, Tell, recipient="critic", key=TextKey.DIRECT_MESSAGE_NOTICE, body="Use B.").message
        self.assertEqual(n.text.values, {"worker": "worker-1"})

    def test_LAB_5_manager_broken_label_goes_to_the_user_the_second_time(self):
        snap = snapshot(message("m1", "manager", key=TextKey.LABEL_RETURNED, values={"label": "x", "problem": "empty"}))
        _, acts = routed(snap, "manager", "<decision>MAYBE</decision>", started_by=["m1"])
        assert_has(self, acts, ToUser, key=TextKey.LABEL_BROKEN_PASSED)


class TestChat(unittest.TestCase):
    def test_MSG_9_user_chat_to_a_worker_stays_between_the_user_and_the_worker(self):
        snap = snapshot(gate("g1"))
        acts = decide(R, snap, event(Chat, snap, "worker-1", text="Stop that."))
        m = assert_has(self, acts, Send, recipient="worker-1", sender=USER, body="Stop that.").message
        self.assertEqual(m.trigger, Trigger.USER_CHAT)
        assert_none(self, acts, Tell)  # the critic is not told
        assert_has(self, acts, CommandReply, ok=True, command_id="c1")
        assert_none(self, acts, CloseGate)  # GATE-5 chat closes no gate

    def test_chat_refusals(self):
        snap = snapshot(agent("worker-2", lifecycle="closed"))
        for who, text, why in (("nobody", "x", "unknown"), ("manager", " ", "empty"), ("worker-2", "x", "closed")):
            acts = decide(R, snap, event(Chat, snap, who, text=text))
            r = assert_has(self, acts, CommandReply, ok=False)
            self.assertEqual(r.text.values["why"], why)


def rename(snap, critic="reviewer", manager="lead"):
    for old, new in (("critic", critic), ("manager", manager)):
        a = snap.agents.pop(old)
        a.id = new
        snap.agents[new] = a
    return snap


class TestReview2(unittest.TestCase):
    def test_LAB_1_a_removed_label_keeps_its_body(self):
        r = labels.parse("<question>Which dataset, A or B?</question>Go.", Role.CRITIC)
        self.assertEqual([(b.problem, b.body) for b in r.broken], [(Problem.REMOVED, "Which dataset, A or B?")])
        self.assertEqual(r.untagged, "Go.")
        r = labels.parse("<verdict>PASS <message to=\"worker-1\">x</message>", Role.CRITIC)  # opened again: no swallowing
        self.assertEqual(len(r.messages), 1)

    def test_LAB_5_code_is_masked_exactly(self):
        r = labels.parse("Use ``<silent/>`` here.", Role.CRITIC)
        self.assertFalse(r.silent)
        text = 'Example:\n````\n```\n<message to="worker-1">in</message>\n```\n````\nafter <silent/>'
        r = labels.parse(text, Role.CRITIC)
        self.assertEqual((r.messages, r.silent), ([], True))
        r = labels.parse('wrap it in ``` fences. <message to="worker-1">Do X.</message> <silent/>', Role.CRITIC)
        self.assertEqual(len(r.messages), 1)
        self.assertTrue(r.silent)

    def test_LAB_5_a_tag_with_unreadable_attributes_is_broken_not_text(self):
        r = labels.parse("<message worker-1>Do X.</message>", Role.CRITIC)
        self.assertEqual([(b.problem, b.body) for b in r.broken], [(Problem.NO_RECIPIENT, "Do X.")])
        self.assertEqual((r.messages, r.untagged), ([], ""))

    def test_REC_10_not_healthy_is_not_healthy(self):
        self.assertIsNone(labels.parse("Not healthy at all.", Role.MONITOR).health)
        self.assertEqual(labels.parse("Not healthy, stopped.", Role.MONITOR).health, Health.STOPPED)

    def test_GATE_1_only_real_option_markers_are_options(self):
        r = labels.parse("<question>Which?\nA) one\n2. two\n- three\nPS: soon\nNo. never</question>", Role.MANAGER)
        self.assertEqual(r.questions[0].options, ["one", "two", "three"])
        self.assertIn("PS: soon", r.questions[0].text)

    def test_other_agent_ids_routing_uses_the_critic_and_manager_by_role(self):
        snap = rename(snapshot(message("m1", "worker-1", sender="critic")))
        _, acts = routed(snap, "worker-1", "Done.", started_by=["m1"])
        assert_has(self, acts, Send, recipient="reviewer", key=TextKey.WORKER_FINAL)
        _, acts = routed(snap, "reviewer", "Blocked.")
        assert_has(self, acts, Send, recipient="lead", key=TextKey.CRITIC_REPORT)
        _, acts = routed(snap, "lead", '<message to="worker-1">Use B.</message>')
        assert_has(self, acts, Tell, recipient="reviewer", key=TextKey.DIRECT_MESSAGE_NOTICE)
        _, acts = routed(snap, "reviewer", '<message to="lead">hi</message>')
        assert_has(self, acts, ReturnToSender, recipient="reviewer")
        acts = decide(R, snap, event(Chat, snap, "worker-1", text="x"))
        assert_none(self, acts, Tell)  # a chat with a worker is not shown to the critic

    def test_DISC_1_a_draft_sends_no_notice_and_rejects_nothing(self):
        snap = snapshot(Discussion("manager"))
        _, acts = routed(snap, "manager", '<message to="worker-1">Use B.</message><message to="ghost">x</message>')
        assert_none(self, acts, Tell)
        assert_none(self, acts, Reject)
        assert_has(self, acts, Send, recipient="worker-1")
        snap = snapshot(Discussion("critic"), workers=0)
        _, acts = routed(snap, "critic", 'Thinking aloud. <message to="worker-9">x</message>')
        assert_none(self, acts, Reject)
        assert_none(self, acts, Send, recipient="manager")
        assert_has(self, acts, ToUser, body="Thinking aloud.")  # DISC-5

    def test_LAB_3_a_summary_with_no_gate_goes_to_the_user(self):
        _, acts = routed(snapshot(), "manager", "<summary>Changed X, checked Y.</summary>")
        assert_has(self, acts, ToUser, body="Changed X, checked Y.")
        for text in ("<summary>S</summary><question>Q?</question>", "<summary>S</summary><decision>ACCEPT</decision>"):
            _, acts = routed(snapshot(), "manager", text)
            assert_none(self, acts, ToUser)

    def test_UI_the_monitor_takes_no_user_chat(self):
        snap = snapshot(agent("monitor"))
        acts = decide(R, snap, event(Chat, snap, "monitor", text="hello"))
        self.assertEqual(assert_has(self, acts, CommandReply, ok=False).text.values["why"], Refusal.WRONG_ROLE)
