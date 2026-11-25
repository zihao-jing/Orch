"""Package D: gates (GATE-1, 3..9; MS-8, 9, 11 gate part), and scenario 6 together with routing."""
from __future__ import annotations

from rt_helpers import *  # noqa: F401,F403
from rt_helpers import C

from orch.runtime import labels
from orch.runtime.gates import module
from orch.runtime.routing import module as routing_module

G = module()


def manager_reply(snap, text, **kw):
    s, ev = turn_ended(snap, "manager", reply=labels.parse(text, Role.MANAGER), **kw)
    return s, ev


class TestOpening(unittest.TestCase):
    def test_GATE_1_question_opens_a_gate_with_options_and_pauses_nothing(self):
        s, ev = manager_reply(snapshot(), "<question>Which db?\nA) pg\nB) sqlite</question><summary>Sum.</summary>")
        acts = decide(G, s, ev)
        g = assert_has(self, acts, OpenGate, gate__kind=GateKind.QUESTION).gate
        self.assertEqual((g.text, g.options, g.agent, g.summary), ("Which db?", ["pg", "sqlite"], "manager", "Sum."))
        assert_none(self, acts, HoldDecision)
        self.assertEqual(len(acts), 1)

    def test_GATE_6_GATE_9_pause_always_opens_a_gate(self):
        s, ev = manager_reply(snapshot(), "<pause>Need the user.</pause>")
        g = assert_has(self, decide(G, s, ev), OpenGate, gate__kind=GateKind.PAUSE).gate
        self.assertEqual(g.text, "Need the user.")

    def test_GATE_7_question_and_pause_in_one_reply_both_take_effect(self):
        s, ev = manager_reply(snapshot(), "<question>A?</question><pause>wait</pause>"
                                          '<message to="critic">go</message>')
        acts = decide(G, s, ev)
        self.assertEqual(sorted(a.gate.kind for a in acts if isinstance(a, OpenGate)), ["pause", "question"])
        self.assertEqual(len({a.gate.id for a in acts}), 2)

    def test_GATE_8_critic_cannot_pause_or_ask(self):
        s, ev = turn_ended(snapshot(), "critic", reply=labels.parse("<pause>x</pause><question>y</question>", Role.CRITIC))
        self.assertEqual(decide(G, s, ev), [])

    def test_assumption_4_no_effect_in_a_discussion(self):
        s, ev = manager_reply(snapshot(Discussion("manager")), "<question>A?</question><pause>w</pause>")
        self.assertEqual(decide(G, s, ev), [])

    def test_night_question_records_the_recommendation_and_opens_no_gate(self):
        s, ev = manager_reply(snapshot(night=True), "<question>Use pg?\nA) yes\nB) no</question>")
        acts = decide(G, s, ev)
        assert_none(self, acts, OpenGate)
        assert_has(self, acts, RecordDecision, by="manager")


class TestScenario6(unittest.TestCase):
    TEXT = ('<question>Which?\nA) a\nB) b</question><pause>wait</pause>'
            '<message to="critic">Continue.</message><message to="worker-1">Hi.</message>')

    def test_scenario_6_question_critic_message_pause_then_answers_one_by_one(self):
        snap = snapshot()
        s, ev = manager_reply(snap, self.TEXT)
        r = decide(routing_module(), s, ev)
        acts = decide(G, s, ev, prior=r)
        gates_opened = [a.gate for a in acts if isinstance(a, OpenGate)]
        self.assertEqual([g.kind for g in gates_opened], ["question", "pause"])
        held = [a.message_id for a in r if isinstance(a, Hold)]
        self.assertEqual(len(held), 3)
        # the store applied it: two messages held, three gates open (a second question came earlier)
        q2 = gate("g2", text="Another?", options=["x", "y"])
        msgs = [message(f"h{i}", "critic" if i == 1 else "worker-1", sender="manager", status="waiting",
                        holds=["question"], needs_answer=True) for i in (1, 2)]
        for m in msgs:
            m.sent_at = 0.0
        g1, gp = gates_opened
        g1.opened = gp.opened = 900.0
        snap = snapshot(q2, g1, gp, *msgs)
        snap.gates = {g.id: g for g in (q2, g1, gp)}
        snap.paused = True
        # answer one: nothing goes yet
        a1 = decide(G, snap, event(GateAnswer, snap, "", id="a1", gate_id="g2", option=0))
        assert_has(self, a1, CloseGate, gate_id="g2")
        assert_none(self, a1, Send)
        # the pause gate closes bare, still nothing
        snap.gates["g2"].closed, snap.gates["g2"].answer = snap.now, Answer(option=0)
        a2 = decide(G, snap, event(GateAnswer, snap, "", id="a2", gate_id=gp.id))
        assert_has(self, a2, CloseGate, gate_id=gp.id)
        assert_none(self, a2, Send)
        # the last answer: one message with all answers, the held messages dropped
        snap.gates[gp.id].closed = snap.now
        a3 = decide(G, snap, event(GateAnswer, snap, "", id="a3", gate_id=g1.id, text="Take b"))
        m = assert_has(self, a3, Send, recipient="manager", key=TextKey.GATE_ANSWERS).message
        self.assertEqual([e["answer"] for e in m.text.values["answers"]], ["x", "Take b"])
        self.assertEqual(sorted(m.gates), sorted(["g2", g1.id]))
        self.assertEqual([u["to"] for u in m.text.values["unsent"]], ["critic", "worker-1"])
        self.assertEqual(len(find(a3, Drop, reason=DropReason.ANSWERED_GATES)), 2)
        self.assertEqual(m.trigger, Trigger.GATE_ANSWER)


class TestAnswers(unittest.TestCase):
    def test_GATE_3_answer_returns_to_the_manager(self):
        g = gate("g1", options=["pg", "sqlite"])
        snap = snapshot(g)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="g1", option=1))
        m = assert_has(self, acts, Send, recipient="manager").message
        self.assertEqual(m.text.values["answers"],
                         [{"kind": GateKind.QUESTION, "question": "Which one?", "answer": "sqlite", "approve": None}])
        assert_has(self, acts, CloseGate, gate_id="g1")
        assert_has(self, acts, RecordDecision, source="g1")
        assert_has(self, acts, CommandReply, ok=True, command_id="c1")

    def test_GATE_4_waits_for_every_answer_gate_even_with_an_operational_gate_open(self):
        g1, g2 = gate("g1"), gate("g2")
        op = gate("op-silence-manager", "operational", cause="silence")
        snap = snapshot(g1, g2, op)
        a = decide(G, snap, event(GateAnswer, snap, "", gate_id="g1", text="yes"))
        assert_none(self, a, Send)
        snap.gates["g1"].closed, snap.gates["g1"].answer = snap.now, Answer("yes")
        b = decide(G, snap, event(GateAnswer, snap, "", id="c2", gate_id="g2", text="no"))
        m = assert_has(self, b, Send, recipient="manager").message  # assumption 1: the operational gate is no hindrance
        self.assertEqual([e["answer"] for e in m.text.values["answers"]], ["yes", "no"])

    def test_GATE_4_tick_sends_a_pending_answer_once(self):
        g = gate("g1", closed=T0 - 5, answer=Answer("yes", at=T0 - 5))
        snap = snapshot(g)
        assert_has(self, decide(G, snap, tick(snap)), Send, recipient="manager")
        snap.gates["g1"].sent_in = "x1"
        self.assertEqual(decide(G, snap, tick(snap)), [])

    def test_GATE_5_chat_closes_no_gate(self):
        snap = snapshot(gate("g1"))
        self.assertEqual(decide(G, snap, event(Chat, snap, "manager", text="hello")), [])

    def test_GATE_6_pause_gate_closes_with_nothing_given_and_sends_nothing(self):
        gp = gate("gp", "pause", text="wait")
        snap = snapshot(gp, paused=True)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="gp"))
        assert_has(self, acts, CloseGate, gate_id="gp")
        assert_none(self, acts, Send)

    def test_refusals(self):
        snap = snapshot(gate("g1", options=["a"]), gate("g3", closed=T0 - 1, answer=Answer("x")))
        for gid, kw, why in (("nope", {"text": "x"}, "unknown"), ("g1", {}, "empty"), ("g1", {"option": 4}, "unknown"),
                             ("g3", {"text": "x"}, "closed")):
            acts = decide(G, snap, event(GateAnswer, snap, "", gate_id=gid, **kw))
            r = assert_has(self, acts, CommandReply, ok=False)
            self.assertEqual(r.text.values["why"], why)


class TestReview(unittest.TestCase):
    def prior_for(self, snap, text):
        s, ev = manager_reply(snap, text)
        return s, ev, decide(routing_module(), s, ev)

    def test_MS_8_decision_pause_opens_a_pause_gate_and_decides_again_after(self):
        snap = snapshot()
        s, ev, r = self.prior_for(snap, "<decision>PAUSE</decision><summary>S</summary>")
        acts = decide(G, s, ev, prior=r)
        g = assert_has(self, acts, OpenGate, gate__kind=GateKind.PAUSE).gate
        self.assertEqual((g.milestone, g.summary), ("m1", "S"))
        assert_has(self, acts, HoldDecision, milestone_id="m1", decision=Decision.PAUSE)
        # the user closes the pause gate: the manager decides again
        held = milestone("m1", held_decision="PAUSE")
        gp = gate("gp", "pause", text="")
        snap = snapshot(held, gp, paused=True)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="gp"))
        m = assert_has(self, acts, Send, recipient="manager", key=TextKey.GATE_ANSWERS).message
        self.assertEqual((m.text.values["decide_again"], m.text.values["answers"]), ("Milestone m1", []))
        assert_has(self, acts, HoldDecision, milestone_id="m1", decision=None)

    def test_MS_9_question_in_a_review_holds_accept_and_its_message(self):
        snap = snapshot()
        s, ev, r = self.prior_for(snap, '<decision>ACCEPT</decision><question>Sure?</question>'
                                        '<message to="critic">Done.</message>')
        acts = decide(G, s, ev, prior=r)
        assert_has(self, acts, HoldDecision, milestone_id="m1", decision=Decision.ACCEPT)
        self.assertEqual({a.message_id for a in find(acts, Hold, reason=HoldReason.GATES)},
                         {a.message.id for a in r if isinstance(a, Send)} - {a.message.id for a in r
                                                                            if isinstance(a, ToUser)})

    def test_MS_9_open_gate_holds_a_later_revise(self):
        snap = snapshot(gate("g1"))
        s, ev, r = self.prior_for(snap, '<decision>REVISE</decision><message to="critic">Fix x.</message>')
        acts = decide(G, s, ev, prior=r)
        assert_has(self, acts, HoldDecision, decision=Decision.REVISE)
        self.assertEqual(len(find(acts, Hold, reason=HoldReason.GATES)), 1)
        assert_none(self, acts, OpenGate)

    def test_MS_9_accept_without_open_gates_is_not_held(self):
        s, ev, r = self.prior_for(snapshot(), "<decision>ACCEPT</decision>")
        self.assertEqual(decide(G, s, ev, prior=r), [])

    def test_MS_9_answers_lift_the_held_decision(self):
        held = milestone("m1", held_decision="ACCEPT")
        hm = message("h1", "critic", sender="manager", status="waiting", holds=["gates"])
        snap = snapshot(held, gate("g1"), hm)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="g1", text="yes"))
        m = assert_has(self, acts, Send, recipient="manager").message
        self.assertEqual(m.text.values["decide_again"], "Milestone m1")
        assert_has(self, acts, HoldDecision, decision=None)
        assert_has(self, acts, Drop, message_id="h1", reason=DropReason.ANSWERED_GATES)

    def test_MS_11_milestone_change_approval_takes_effect_with_the_close(self):
        gc = gate("gc", "milestone_change", agent="")
        snap = snapshot(gc)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="gc", approve=True))
        assert_has(self, acts, CloseGate, gate_id="gc")
        assert_none(self, acts, Send)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="gc", approve=False, text="Keep m2."))
        m = assert_has(self, acts, Send, recipient="manager").message
        self.assertEqual(m.text.values["answers"][0]["approve"], False)

    def test_MS_11_plan_approval_is_an_answer_for_milestones(self):
        gp = gate("gpl", "plan")
        snap = snapshot(gp)
        acts = decide(G, snap, event(GateAnswer, snap, "", gate_id="gpl", approve=True))
        m = assert_has(self, acts, Send, recipient="manager").message
        self.assertEqual(m.gates, ["gpl"])
