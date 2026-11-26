"""Package I, terminal (docs/redesign.md TERM-1 to 6). Scenario 14 of 4.4."""
import unittest

from rt_helpers import *
from orch.runtime.terminal import command, module

K = TextKey
NO_SHARE = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=True, receipts=True,
                stops_jobs=True)  # as Claude: the terminal cannot join the open session


def opened(snap, agent_id, **kw):
    return decide(module(), snap, event(TerminalOpen, snap, agent_id, **kw))


def why(actions):
    return actions[0].text.values["why"]


class TestCommand(unittest.TestCase):
    def test_TERM_1_one_short_command_names_project_and_agent(self):
        self.assertEqual(command("demo", "worker-1"), "orch terminal demo worker-1")
        self.assertEqual(command("my demo", "critic"), "orch terminal 'my demo' critic")


class TestOpen(unittest.TestCase):
    def test_scenario_14_TERM_4_messages_to_the_agent_wait_while_a_terminal_is_open(self):
        s = snapshot()
        actions = opened(s, "worker-1")
        assert_has(self, actions, HoldAgent, agent="worker-1", reason=HoldReason.TERMINAL)
        t = assert_has(self, actions, OpenTerminal).terminal
        self.assertEqual((t.agent, t.shared), ("worker-1", True))
        assert_has(self, actions, CommandReply, ok=True)
        late = Send(message("l1", "worker-1", "critic", status="waiting"))
        self.assertEqual(sent_to(actions + [late], "worker-1"), [])
        s2 = snapshot(agent("worker-1", holds=["terminal"]), Terminal("worker-1"))
        self.assertEqual(sent_to([late], "worker-1", s2), [])

    def test_TERM_4_where_the_terminal_joins_orch_need_not_close_its_copy(self):
        t = assert_has(self, opened(snapshot(), "worker-1"), OpenTerminal).terminal
        self.assertTrue(t.shared)
        s = snapshot(agent("worker-1", caps=NO_SHARE))
        t = assert_has(self, opened(s, "worker-1"), OpenTerminal).terminal
        self.assertFalse(t.shared)  # the engine closes Orch's CLI, then reopens it afterwards

    def test_TERM_3_a_terminal_on_a_worker_is_an_instant_conversation_the_agent_goes_on(self):
        s = snapshot(agent("worker-1", status="writing"), turn("worker-1", status="running"))
        actions = opened(s, "worker-1")  # shared: nothing ends, so no question
        assert_has(self, actions, OpenTerminal)
        assert_none(self, actions, StopJob)

    def test_TERM_5_ask_first_if_opening_would_end_a_job(self):
        s = snapshot(agent("worker-1", caps=NO_SHARE, status="waiting_job"), job("worker-1/j1"))
        actions = opened(s, "worker-1")
        self.assertEqual(len(actions), 1)
        reply = assert_has(self, actions, CommandReply, ok=False)
        self.assertEqual((reply.text.key, reply.text.values["jobs"]), (K.ASK_JOB_TERMINAL, ["train.py"]))
        self.assertEqual(reply.choices[0]["set"], {"stop_job": True})
        assert_none(self, actions, OpenTerminal)

    def test_TERM_5_a_writing_agent_that_would_stop_is_asked_too_with_no_jobs_named(self):
        s = snapshot(agent("manager", caps=NO_SHARE, status="writing"), turn("manager", status="running"))
        reply = assert_has(self, opened(s, "manager"), CommandReply, ok=False)
        self.assertEqual(reply.text.values["jobs"], [])

    def test_TERM_5_agreed_the_job_is_stopped_and_the_critic_told(self):
        s = snapshot(agent("worker-1", caps=NO_SHARE, status="waiting_job"), job("worker-1/j1"))
        actions = opened(s, "worker-1", stop_job=True)
        assert_has(self, actions, StopJob, job_id="worker-1/j1")
        assert_has(self, actions, Tell, recipient="critic", key=K.JOBS_STOPPED)
        assert_has(self, actions, OpenTerminal)

    def test_TERM_5_an_idle_agent_without_jobs_opens_without_a_question(self):
        s = snapshot(agent("worker-1", caps=NO_SHARE))
        assert_has(self, opened(s, "worker-1"), OpenTerminal)

    def test_a_terminal_is_refused_when_it_cannot_open(self):
        self.assertEqual(why(opened(snapshot(), "worker-9")), "unknown")
        s = snapshot(agent("monitor"))
        self.assertEqual(why(opened(s, "monitor")), "wrong_role")
        s = snapshot(agent("worker-1", lifecycle="closed"))
        self.assertEqual(why(opened(s, "worker-1")), "closed")
        s = snapshot(Terminal("worker-1"))
        self.assertEqual(why(opened(s, "worker-1")), "terminal_open")
        s = snapshot(agent("worker-1", sessions={}, session_key=""))
        self.assertEqual(why(opened(s, "worker-1")), "not_possible")


class TestClose(unittest.TestCase):
    def test_scenario_14_TERM_6_take_back_closes_it_and_the_engine_reads_the_session(self):
        s = snapshot(agent("worker-1", holds=["terminal"]), Terminal("worker-1", opened=T0 - 3600))
        for kind in (TerminalTakeBack, TerminalClose):
            actions = decide(module(), s, event(kind, s, "worker-1"))
            assert_actions(self, actions, [(CloseTerminal, {"agent": "worker-1"}), (CommandReply, {"ok": True})])
        # the hold stays until the transcript has been read
        assert_none(self, actions, ReleaseAgent)

    def test_TERM_6_nothing_open_nothing_to_take_back(self):
        s = snapshot()
        self.assertEqual(why(decide(module(), s, event(TerminalTakeBack, s, "worker-1"))), "not_open")

    def test_TERM_6_the_dashboard_shows_the_terminal_and_the_waiting_messages(self):
        s = snapshot(agent("worker-1", holds=["terminal"]), Terminal("worker-1", opened=T0 - 60),
                     message("l1", "worker-1", "critic", status="waiting"))
        state = public_state(s)
        self.assertIn("worker-1", state["terminals"])
        self.assertEqual(state["agents"]["worker-1"]["waiting"], 1)


class TestRead(unittest.TestCase):
    def test_scenario_14_TERM_4_messages_go_out_after_the_session_is_back(self):
        s = snapshot(agent("worker-1", holds=["terminal"]))
        actions = decide(module(), s, event(TerminalRead, s, "worker-1"))
        assert_has(self, actions, ReleaseAgent, agent="worker-1", reason=HoldReason.TERMINAL)

    def test_TERM_2_replies_are_recorded_for_the_user(self):
        s = snapshot(agent("critic", holds=["terminal"]))
        said = (Said(USER, "What changed?", T0 - 50), Said("critic", "The tests.", T0 - 40))
        actions = decide(module(), s, event(TerminalRead, s, "critic", said=said))
        m = assert_has(self, actions, ToUser, sender="critic", body="The tests.").message
        self.assertEqual(m.recipient, USER)
        assert_none(self, actions, Tell)  # what the user said to the critic is a discussion: applied later

    def test_TERM_3_what_the_user_said_to_a_worker_is_shown_to_the_critic_as_its_own_users(self):
        s = snapshot(agent("worker-1", holds=["terminal"]))
        said = (Said(USER, "Use the other file.", T0 - 50), Said("worker-1", "Done.", T0 - 40))
        actions = decide(module(), s, event(TerminalRead, s, "worker-1", said=said))
        tell = assert_has(self, actions, Tell, recipient="critic", key=K.DIRECT_MESSAGE_NOTICE)
        self.assertEqual((tell.message.body, tell.message.text.values), ("Use the other file.", {"worker": "worker-1"}))
        assert_has(self, actions, ToUser, sender="worker-1", body="Done.")

    def test_TERM_3_a_shared_terminal_has_only_the_users_lines(self):
        s = snapshot(agent("worker-1", holds=["terminal"]))
        actions = decide(module(), s, event(TerminalRead, s, "worker-1", said=(Said(USER, "Hi.", T0),)))
        assert_none(self, actions, ToUser)
        assert_has(self, actions, Tell, recipient="critic")
        assert_has(self, actions, ReleaseAgent)

    def test_TERM_4_unknown_agent_and_empty_lines_do_nothing_harmful(self):
        s = snapshot()
        self.assertEqual(decide(module(), s, event(TerminalRead, s, "worker-9", said=(Said(USER, "x"),))), [])
        actions = decide(module(), s, event(TerminalRead, s, "worker-1", said=(Said(USER, "  "),)))
        self.assertEqual(actions, [])  # no hold left, nothing said

    def test_scenario_14_the_whole_story(self):
        s = snapshot()
        out = opened(s, "worker-1")                                                    # open
        self.assertTrue(find(out, HoldAgent))
        s = snapshot(agent("worker-1", holds=["terminal"]), Terminal("worker-1", T0))  # messages wait meanwhile
        late = Send(message("l1", "worker-1", "critic", status="waiting"))
        self.assertEqual(sent_to([late], "worker-1", s), [])
        back = decide(module(), s, event(TerminalTakeBack, s, "worker-1"))             # take back
        self.assertTrue(find(back, CloseTerminal))
        s2 = snapshot(agent("worker-1", holds=["terminal"]))                           # terminal record gone
        read = decide(module(), s2, event(TerminalRead, s2, "worker-1", said=(Said(USER, "Hello.", T0),)))
        self.assertTrue(find(read, ReleaseAgent))                                      # the waiting message may go
        s3 = snapshot(agent("worker-1"))
        self.assertEqual(sent_to([late], "worker-1", s3), [late.message])


if __name__ == "__main__":
    unittest.main()
