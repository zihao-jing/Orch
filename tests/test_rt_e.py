"""Package E: supervisor and workers (SIL-1..5, MSG-5, JOB-2, 5..8, REC-10, WRK-2, 5, 6), scenarios 2 to 4, 16 to 18."""
from __future__ import annotations

from dataclasses import replace

from rt_helpers import *  # noqa: F401,F403
from rt_helpers import C

from orch.runtime import labels
from orch.runtime.routing import module as routing_module
from orch.runtime.supervisor import module as supervisor_module
from orch.runtime.workers import module as workers_module

S = supervisor_module()
W = workers_module()


def run_tick(snap, prior=()):
    return decide(S, snap, tick(snap), prior)


class TestWorkers(unittest.TestCase):
    def test_WRK_2_add_uses_the_next_free_id_and_never_wakes_the_critic(self):
        snap = snapshot(agent("worker-3", lifecycle="deleted"), workers=2)
        acts = decide(W, snap, event(WorkerAdd, snap, ""))
        add = assert_has(self, acts, AddWorker, like="worker-1")
        self.assertEqual((add.agent.id, add.agent.lifecycle, add.agent.role), ("worker-4", Lifecycle.OPEN, Role.WORKER))
        assert_has(self, acts, CommandReply, ok=True)
        assert_none(self, acts, Send)  # WRK-6: shown in the next reminder, no message
        acts = decide(W, snap, event(WorkerAdd, snap, "", like="worker-9"))
        assert_has(self, acts, CommandReply, ok=False)

    def test_WRK_2_only_workers_are_managed(self):
        snap = snapshot()
        acts = decide(W, snap, event(WorkerClose, snap, "critic"))
        assert_has(self, acts, CommandReply, ok=False)
        assert_none(self, acts, SetLifecycle)

    def test_scenario_16_WRK_5_close_with_a_job_asks_then_finish_or_stop(self):
        snap = snapshot(job("worker-1/j1", state="running"), message("m1", "worker-1", sender="critic"))
        acts = decide(W, snap, event(WorkerClose, snap, "worker-1"))
        r = acts[0]
        self.assertFalse(r.ok)
        self.assertEqual(r.text.key, TextKey.ASK_JOBS_CLOSE)
        self.assertEqual([c["set"] for c in r.choices], [{"jobs": "finish"}, {"jobs": "stop"}])
        self.assertEqual(len(acts), 1)
        fin = decide(W, snap, event(WorkerClose, snap, "worker-1", jobs="finish"))
        assert_has(self, fin, SetLifecycle, agent="worker-1", lifecycle=Lifecycle.CLOSED)
        assert_none(self, fin, StopJob)
        assert_has(self, fin, Drop, message_id="m1", reason=DropReason.WORKER_CLOSED)
        stop = decide(W, snap, event(WorkerClose, snap, "worker-1", jobs="stop"))
        assert_has(self, stop, StopJob, job_id="worker-1/j1")
        assert_has(self, stop, Tell, recipient="critic", key=TextKey.JOBS_STOPPED)

    def test_scenario_16_late_result_of_a_closed_worker_is_recorded_then_reopened(self):
        snap = snapshot(agent("worker-1", lifecycle="closed"))
        s, ev = turn_ended(snap, "worker-1", "Job result.")
        acts = decide(routing_module(), s, ev)
        sent = assert_has(self, acts, Send, recipient="critic")
        assert_has(self, acts, Drop, message_id=sent.message.id, reason=DropReason.WORKER_CLOSED)
        self.assertFalse(sent_to(acts, "critic"))
        acts = decide(W, snap, event(WorkerReopen, snap, "worker-1"))
        assert_has(self, acts, SetLifecycle, lifecycle=Lifecycle.OPEN)
        assert_none(self, acts, Send)  # WRK-6
        again = decide(W, snapshot(), event(WorkerReopen, snapshot(), "worker-1"))
        assert_has(self, again, CommandReply, ok=False)

    def test_WRK_5_delete_asks_with_its_own_text(self):
        snap = snapshot(job("worker-1/j1", state="running"))
        acts = decide(W, snap, event(WorkerDelete, snap, "worker-1"))
        self.assertEqual(acts[0].text.key, TextKey.ASK_JOBS_DELETE)
        acts = decide(W, snap, event(WorkerDelete, snap, "worker-1", jobs="finish"))
        assert_has(self, acts, SetLifecycle, lifecycle=Lifecycle.DELETED)


class TestUnanswered(unittest.TestCase):
    def snap(self, **kw):
        m = message("m1", "critic", sender="manager", status="delivered", created=T0 - 400, sent_at=T0 - 400)
        return snapshot(agent("critic", idle_since=T0 - 400), m, **kw)

    def test_MSG_5_resend_once_then_the_agent_failed(self):
        snap = self.snap()
        assert_actions(self, run_tick(snap), [(Resend, {"message_id": "m1", "if_unknown": True})])
        snap.messages["m1"].checked_at = T0 - 301
        assert_actions(self, run_tick(snap), [(FailAgent, {"agent": "critic", "why": FailCause.NO_ANSWER})])
        snap.messages["m1"].checked_at = T0 - 100  # asked again recently: the 5 minutes run anew
        self.assertEqual(run_tick(snap), [])

    def test_MSG_5_counts_only_while_the_recipient_is_idle_and_the_message_was_sent(self):
        snap = self.snap()
        snap.agents["critic"].idle_since = T0 - 100
        self.assertEqual(run_tick(snap), [])  # idle for 100 s only
        snap = self.snap()
        snap.agents["critic"].status, snap.agents["critic"].idle_since = AgentStatus.WRITING, 0.0
        self.assertEqual(run_tick(snap), [])
        for hold in ("holds", "retry", "switch"):
            snap = self.snap()
            setattr(snap.agents["critic"], hold, [HoldReason.TERMINAL] if hold == "holds" else
                    (RetryState() if hold == "retry" else Switch("critic")))
            self.assertEqual(run_tick(snap), [], hold)
        snap = self.snap()
        snap.messages["m1"].sent_at = 0.0  # never really sent
        snap.messages["m1"].status = MessageStatus.WAITING
        self.assertEqual(run_tick(snap), [])

    def test_MSG_5_never_from_before_this_orch_began_and_notices_never_count(self):
        snap = self.snap()
        snap.orch_started = T0 - 100
        self.assertEqual([a for a in run_tick(snap) if isinstance(a, (Resend, FailAgent))], [])
        snap = self.snap()
        snap.messages["m1"].needs_answer = False  # MSG-4
        self.assertEqual([a for a in run_tick(snap) if isinstance(a, (Resend, FailAgent))], [])


class TestSilence(unittest.TestCase):
    def test_SIL_2_scenario_4_three_checks_then_a_pause_gate(self):
        """SIL-3: the check carries the SILENCE_CHECK text that tells the manager to find the cause."""
        for tries in (0, 1, 2):
            snap = snapshot(silence=Silence(tries=tries, asked_at=T0 - 100))
            m = assert_has(self, run_tick(snap), AskManagerToCheck, recipient="manager", key=TextKey.SILENCE_CHECK)
            self.assertEqual(m.message.text.values, {"attempt": tries + 1, "attempts": 3})
        snap = snapshot(silence=Silence(tries=3, asked_at=T0 - 100))
        acts = run_tick(snap)
        g = assert_has(self, acts, OpenGate, gate__kind=GateKind.PAUSE, gate__cause=GateCause.SILENCE).gate
        self.assertEqual(g.words.key, TextKey.GATE_SILENCE)
        assert_none(self, acts, AskManagerToCheck)

    def test_SIL_2_scenario_4_the_count_restarts_when_work_restarts(self):
        snap = snapshot(agent("worker-1", status="writing"), silence=Silence(tries=2, asked_at=T0 - 100))
        assert_actions(self, run_tick(snap), [ResetSilence])
        snap = snapshot(job("worker-1/j1", state="running"), silence=Silence(tries=2))
        assert_actions(self, run_tick(snap), [ResetSilence])

    def test_scenario_5_a_silent_last_reply_does_not_hide_the_check(self):
        snap, _ = turn_ended(snapshot(), "critic", reply=labels.parse("<silent/>", Role.CRITIC))
        self.assertTrue(snap.turns["critic"].reply.silent)
        snap.now += 10
        assert_has(self, run_tick(snap), AskManagerToCheck)

    def test_SIL_1_anything_active_means_no_check(self):
        cases = {
            "writing": snapshot(agent("critic", status="writing")),
            "job": snapshot(job("worker-1/j1", state="running")),
            "open message": snapshot(message("m1", "worker-1", sender="critic", status="waiting")),
            "ended job not yet woke": snapshot(job("worker-1/j1", state="ended", ended=T0 - 5)),
            "discussion": snapshot(Discussion("manager")),
            "gate": snapshot(gate("g1", "operational", cause="silence")),
            "terminal": snapshot(Terminal("worker-1")),
            "retry": snapshot(agent("worker-1", retry=RetryState())),
            "stopped": snapshot(agent("worker-1", stopped_by_user=True)),
            "paused": snapshot(paused=True),
            "finished": snapshot(finished=True),
        }
        for name, snap in cases.items():
            self.assertFalse(find(run_tick(snap), AskManagerToCheck), name)

    def test_SIL_1_not_active_when_it_only_waits_for_nobody(self):
        snap = snapshot(agent("worker-1", lifecycle="closed"),
                        message("m1", "worker-1", sender="critic", status="delivered"),
                        message("m2", "user", sender="manager", needs_answer=False))
        assert_has(self, run_tick(snap), AskManagerToCheck)

    def test_SIL_2_waits_a_moment_after_the_last_activity(self):
        snap = snapshot(agent("critic", idle_since=T0 - 2, last_output=T0 - 2))
        self.assertEqual(run_tick(snap), [])


class TestFrozenAndRounds(unittest.TestCase):
    def writing(self, id="worker-1", **kw):
        return agent(id, status="writing", **{"last_output": T0 - 2500, **kw})

    def test_scenario_17_SIL_4_frozen_worker_is_restarted_once_then_failed(self):
        snap = snapshot(self.writing())
        assert_has(self, run_tick(snap), RestartSession, agent="worker-1", frozen=True)
        snap = snapshot(self.writing(frozen_restart=T0 - 100))
        self.assertEqual(run_tick(snap), [])  # the 40 minutes run anew from the restart
        snap = snapshot(self.writing(frozen_restart=T0 - 2450))
        assert_has(self, run_tick(snap), FailAgent, agent="worker-1", why=FailCause.FROZEN)

    def test_scenario_17_a_long_tool_call_or_job_is_not_frozen(self):
        self.assertFalse(find(run_tick(snapshot(self.writing(tools=["t1"]))), RestartSession))
        snap = snapshot(self.writing(), job("worker-1/j1", state="running"))
        self.assertFalse(find(run_tick(snap), RestartSession))
        snap = snapshot(agent("worker-1", status="writing", last_output=T0 - 600))
        self.assertEqual(run_tick(snap), [])

    def test_SIL_4_manager_and_critic_get_an_operational_gate_and_the_monitor_none(self):
        for who in ("manager", "critic"):
            snap = snapshot(self.writing(who, frozen_restart=T0 - 2450))
            g = assert_has(self, run_tick(snap), OpenGate, gate__cause=GateCause.FROZEN).gate
            self.assertEqual((g.kind, g.agent, g.id), (GateKind.OPERATIONAL, who, f"op-frozen-{who}"))
        snap = snapshot(self.writing("monitor"))
        self.assertEqual(run_tick(snap), [])

    def test_SIL_4_never_from_before_this_orch_began(self):
        snap = snapshot(self.writing())
        snap.orch_started = T0 - 100
        self.assertEqual(run_tick(snap), [])

    def test_SIL_5_each_worker_reply_to_the_critic_is_a_round(self):
        snap = snapshot(workers=2)
        s1, e1 = turn_ended(snap, "worker-1", "One.")
        s2, e2 = turn_ended(snap, "worker-2", "Two.")
        r = decide(routing_module(), s1, e1) + decide(routing_module(), s2, e2)
        acts = decide(S, s1, e1, prior=r[:1])
        self.assertEqual(len(find(acts, CountRound)), 1)
        assert_none(self, acts, OpenGate)

    def test_SIL_5_reaching_the_limit_opens_an_operational_gate(self):
        snap = snapshot(rounds=4999)
        s, ev = turn_ended(snap, "worker-1", "Last.")
        r = decide(routing_module(), s, ev)
        acts = decide(S, s, ev, prior=r)
        g = assert_has(self, acts, OpenGate, gate__cause=GateCause.ROUND_LIMIT).gate
        self.assertEqual((g.kind, g.words.values), (GateKind.OPERATIONAL, {"rounds": 5000}))

    def test_SIL_5_a_dropped_reply_of_a_closed_worker_is_no_round(self):
        snap = snapshot(agent("worker-1", lifecycle="closed"))
        s, ev = turn_ended(snap, "worker-1", "Late.")
        acts = decide(S, s, ev, prior=decide(routing_module(), s, ev))
        assert_none(self, acts, CountRound)


class TestJobs(unittest.TestCase):
    def ended(self, **kw):
        return job("worker-1/j1", **{"state": "ended", "exit_code": 0, "ended": T0 - 200, **kw})

    def test_scenario_2_JOB_2_a_running_job_keeps_the_system_active_and_the_worker_waiting(self):
        snap = snapshot(job("worker-1/j1", state="running"))
        self.assertEqual(snap.status_of("worker-1"), AgentStatus.WAITING_JOB)
        s, _ = turn_ended(snap, "worker-1", "Started.")
        self.assertEqual(s.turns["worker-1"].ending, Ending.INTERMEDIATE)
        self.assertEqual(run_tick(snap), [])
        # it ended and its worker woke: final ending, nothing left to do
        woke = snapshot(self.ended(woke=True))
        s, _ = turn_ended(woke, "worker-1", "Done.")
        self.assertEqual(s.turns["worker-1"].ending, Ending.FINAL)
        assert_none(self, run_tick(woke), WakeWorker)

    def test_scenario_3_JOB_5_the_wakeup_comes_after_a_minute_and_once(self):
        snap = snapshot(self.ended(), agent("worker-1", idle_since=T0 - 30), now=T0)
        self.assertEqual(find(run_tick(snap), WakeWorker), [])  # CLI wakes itself: give it a minute
        snap.agents["worker-1"].idle_since = T0 - 61
        w = assert_has(self, run_tick(snap), WakeWorker, recipient="worker-1", key=TextKey.JOB_WAKEUP)
        self.assertEqual(w.job_ids, ("worker-1/j1",))
        self.assertEqual(w.message.text.values, {"jobs": [{"job": "train.py", "exit_code": 0}]})
        snap.jobs["worker-1/j1"].fallback_sent = True
        self.assertEqual(find(run_tick(snap), WakeWorker), [])
        snap.jobs["worker-1/j1"].fallback_sent, snap.jobs["worker-1/j1"].woke = False, True
        self.assertEqual(find(run_tick(snap), WakeWorker), [])

    def test_JOB_5_never_while_it_writes_or_is_held_or_failing(self):
        for kw in ({"status": "writing"}, {"holds": [HoldReason.TERMINAL]}, {"failed_why": "x"},
                   {"retry": RetryState()}, {"stopped_by_user": True}):
            snap = snapshot(self.ended(), agent("worker-1", idle_since=T0 - 300, **kw))
            self.assertEqual(find(run_tick(snap), WakeWorker), [], kw)

    def test_JOB_4_a_cli_that_does_not_wake_itself_is_told_at_once_also_after_a_restart(self):
        caps = replace(ALL_CAPS, wakes_itself=False)
        snap = snapshot(self.ended(), agent("worker-1", caps=caps, idle_since=T0 - 1))
        snap.orch_started = T0 - 5  # the job ended before this Orch began
        assert_has(self, run_tick(snap), WakeWorker, key=TextKey.JOB_WAKEUP)
        snap.agents["worker-1"].status, snap.agents["worker-1"].idle_since = AgentStatus.WRITING, 0.0
        self.assertEqual(find(run_tick(snap), WakeWorker), [])  # only with no open turn

    def test_scenario_3_JOB_7_a_lost_job_is_marked_then_the_worker_checks(self):
        snap = snapshot(job("worker-1/j1", state="running"), job("worker-1/j2", state="running"))
        ev = JobsChecked("worker-1", snap.now, jobs=(snap.jobs["worker-1/j2"],))
        acts = decide(S, snap, ev)
        assert_actions(self, acts, [(MarkLost, {"job_id": "worker-1/j1"})])
        snap = snapshot(self.ended(state="lost", exit_code=None), agent("worker-1", idle_since=T0 - 61))
        w = assert_has(self, run_tick(snap), WakeWorker, key=TextKey.JOB_LOST)
        self.assertEqual(w.job_ids, ("worker-1/j1",))
        snap.jobs["worker-1/j1"].stopped_by_user = True  # the user pressed stop: no wake
        self.assertEqual(find(run_tick(snap), WakeWorker), [])

    def test_JOB_6_and_8_a_job_without_progress_is_reported_once(self):
        snap = snapshot(job("worker-1/j1", state="running", started=T0 - 4000, last_progress=T0 - 3700))
        snap.orch_started = T0 - 5000
        acts = run_tick(snap)
        assert_has(self, acts, ReportJob, job_id="worker-1/j1")
        t = assert_has(self, acts, Tell, recipient="critic", key=TextKey.JOB_NO_PROGRESS).message
        self.assertEqual(t.text.values["minutes"], 61)
        snap.jobs["worker-1/j1"].reported = True
        self.assertEqual(find(run_tick(snap), ReportJob), [])
        fresh = snapshot(job("worker-1/j1", state="running", started=T0 - 4000, last_progress=T0 - 100))
        fresh.orch_started = T0 - 5000
        self.assertEqual(find(run_tick(fresh), ReportJob), [])
        # a running server keeps the system active (the manager is shown it at the review, milestones)
        assert_none(self, run_tick(snap), AskManagerToCheck)


class TestMonitor(unittest.TestCase):
    def snap(self, now=T0 + 1300, **kw):
        w = agent("worker-1", status="writing", last_output=now - 100)
        return snapshot(agent("monitor"), w, now=now, **kw)

    def test_REC_10_one_check_at_a_time_of_a_worker_at_work(self):
        snap = self.snap()
        acts = run_tick(snap)
        c = assert_has(self, acts, Send, recipient="monitor", key=TextKey.MONITOR_CHECK)
        self.assertEqual(c.message.text.values["worker"], "worker-1")
        self.assertTrue(c.message.text.values["live_file"].endswith("/live/worker-1.jsonl"))
        w = assert_has(self, acts, SetWatch).watch
        self.assertEqual((w.worker, w.asked), ("worker-1", c.message.id))
        snap.watch["worker-1"] = w
        snap.messages[c.message.id] = replace(c.message, status=MessageStatus.DELIVERED, sent_at=snap.now)
        self.assertEqual(find(run_tick(snap), Send), [])
        idle = snapshot(agent("monitor"), now=T0 + 1300)
        self.assertEqual(find(run_tick(idle), Send, key=TextKey.MONITOR_CHECK), [])  # nothing at work

    def reply(self, text, watch):
        snap = self.snap()
        snap.watch["worker-1"] = watch
        snap.messages["c1"] = message("c1", "monitor", key=TextKey.MONITOR_CHECK, status="answered")
        s, ev = turn_ended(snap, "monitor", reply=labels.parse(text, Role.MONITOR), started_by=["c1"])
        return s, decide(S, s, ev)

    def test_REC_10_only_unhealthy_checks_reach_the_critic_once_per_change_then_hourly(self):
        _, acts = self.reply("Healthy.", Watch("worker-1", asked="c1"))
        assert_none(self, acts, Tell)
        self.assertEqual(assert_has(self, acts, SetWatch).watch.reported, Health.HEALTHY)
        s, acts = self.reply("Quiet for an hour.", Watch("worker-1", asked="c1", reported=Health.HEALTHY))
        t = assert_has(self, acts, Tell, recipient="critic", key=TextKey.MONITOR_REPORT).message
        self.assertEqual(t.text.values["health"], Health.QUIET)
        self.assertEqual(assert_has(self, acts, SetWatch).watch.reported, Health.QUIET)
        _, acts = self.reply("Quiet.", Watch("worker-1", asked="c1", reported=Health.QUIET, reported_at=T0 + 1300 - 600))
        assert_none(self, acts, Tell)
        _, acts = self.reply("Quiet.", Watch("worker-1", asked="c1", reported=Health.QUIET, reported_at=T0 + 1300 - 3601))
        assert_has(self, acts, Tell, key=TextKey.MONITOR_REPORT)
        _, acts = self.reply("Stopped.", Watch("worker-1", asked="c1", reported=Health.QUIET, reported_at=T0 + 1300 - 60))
        assert_has(self, acts, Tell, key=TextKey.MONITOR_REPORT)  # a change of status

    def test_REC_10_failure_is_shown_and_after_two_the_critic_is_told_nobody_watches(self):
        _, acts = self.reply("No idea what to say", Watch("worker-1", asked="c1"))
        assert_has(self, acts, ToUser, key=TextKey.NOTE_MONITOR_FAILED)
        assert_none(self, acts, Tell)
        self.assertEqual(assert_has(self, acts, SetWatch).watch.failures, 1)
        _, acts = self.reply("???", Watch("worker-1", asked="c1", failures=1))
        assert_has(self, acts, Tell, recipient="critic", key=TextKey.NOBODY_WATCHES)
        self.assertTrue(assert_has(self, acts, SetWatch).watch.told_unwatched)
        _, acts = self.reply("???", Watch("worker-1", asked="c1", failures=2, told_unwatched=True))
        assert_none(self, acts, Tell)


class TestNight(unittest.TestCase):
    def test_scenario_18_night_mode_question_holds_nothing_and_opens_no_gate(self):
        from orch.runtime.gates import module as gates_module
        snap = snapshot(night=True)
        s, ev = turn_ended(snap, "manager", reply=labels.parse(
            '<question>Use pg?\nA) yes\nB) no</question><message to="critic">Go on with pg.</message>', Role.MANAGER))
        r = decide(routing_module(), s, ev)
        g = decide(gates_module(), s, ev, prior=r)
        self.assertEqual([m.body for m in sent_to(r, "critic")], ["Go on with pg."])
        assert_none(self, r, Hold)
        assert_none(self, g, OpenGate)
        assert_has(self, g, RecordDecision, by="manager")
        # nothing open: the silence rule works as by day
        assert_has(self, run_tick(replace(snap)), AskManagerToCheck)
