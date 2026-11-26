"""orch/runtime/store.py (package B): Orch dies between any two steps, starts again, and nothing is lost or sent twice."""
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from rt_helpers import *
from orch.runtime import store as S
from orch.runtime.store import Store


class Died(BaseException):
    """The process is gone here."""


class Steps:
    """Dies at the n-th step (0: never). A step is a write of the store or something an adapter does."""

    def __init__(self, at=0):
        self.at, self.n, self.names = at, 0, []

    def __call__(self, name):
        self.n += 1
        self.names.append(name)
        if self.n == self.at:
            raise Died(name)

    def around(self, name, fn):
        """The process may die before or after it was done."""
        def run(*a, **k):
            self(f"before {name}")
            out = fn(*a, **k)
            self(f"after {name}")
            return out
        return run


class MiniEngine:
    """The steps of contracts.py, with one module: a chat goes to its agent, a worker's reply to the critic, the critic's
    to the user, and each reply is noted."""

    def __init__(self, run_dir, clock, clis, steps, notes):
        self.store = Store(run_dir, clock)
        self.clock, self.steps, self.notes, self.ads = clock, steps, notes, {}
        for i, cli in clis.items():
            ad = self.ads[i] = MockAdapter(clock, cli)
            proc = self.store.snapshot().agents[i].proc
            if proc is None or not ad.reconnect(proc):
                self.store.set_proc(i, ad.start(AgentSpec(i), None))
            ad.send = steps.around("send", ad.send)

    def die(self):
        self.store._release()
        for ad in self.ads.values():
            ad.detach()

    def decide(self, ev):
        m = lambda n, frm, to, body: Message(new_id("routing", ev, n), frm, to, body)
        if isinstance(ev, Chat):
            return [Send(m(0, USER, ev.agent, ev.text)), CommandReply(ev.id)]
        if isinstance(ev, TurnEnded) and ev.agent == "worker-1":
            return [Send(m(0, "worker-1", "critic", ev.text)), CountRound(""), SaveNotes(ev.agent, ev.text)]
        if isinstance(ev, TurnEnded):
            return [ToUser(m(0, ev.agent, USER, ev.text))]
        return []

    def start(self):
        if self.store.pending:
            self.store.apply(self.store.pending, self.decide(self.store.pending))
        self.drain()
        for i in self.ads:
            for m in self.store.in_doubt(i):
                if self.ads[i].arrived(m.id):
                    self.event(Received(i, uid=f"{m.id}.{m.attempts}", message_id=m.id))
                else:
                    self.store.not_arrived(m.id)

    def event(self, ev):
        ev = self.store.begin(ev)
        if ev is not None:
            self.store.apply(ev, self.decide(ev))
            self.perform()

    def perform(self):
        for i in self.store.unfinished():
            if isinstance(i.action, SaveNotes):
                self.steps.around("note", lambda: self.notes.append(i.action.text))()
            self.store.intent_done(i.id)  # a Send is done by the loop below, from the messages themselves
        for m in self.store.to_send():
            if self.store.begin_send(m.id, f"{m.sender}: {m.body}"):
                self.ads[m.recipient].send(m.id, f"{m.sender}: {m.body}")

    def drain(self):
        for ad in self.ads.values():
            for ev in ad.events():
                self.event(ev)

    def work(self, commands, until=12):
        self.start()
        for c in commands:
            self.event(c)
        for _ in range(until):
            self.perform()
            self.clock.advance(1)
            self.event(Tick("", self.clock.now()))
            self.drain()


class TestCrash(unittest.TestCase):
    COMMANDS = [Chat("worker-1", T0, "c1", text="Build it."), Chat("critic", T0, "c2", text="How is it?")]

    def scenario(self, at, second=0, tear=False):
        """Run it, dying at step `at` (and at step `second` after the restart); returns (store, clis, notes, steps)."""
        tmp = Path(tempfile.mkdtemp(prefix="rtbc"))
        self.addCleanup(shutil.rmtree, tmp, True)
        run, clock, notes = tmp / "p" / "r", FakeClock(T0), []
        snap = snapshot(agent("worker-1", proc=None, sessions={}, session_key=""),
                        agent("critic", proc=None, sessions={}, session_key=""))
        Store.create(run, clock, snap)._release()
        clis = {i: MockCLI(clock, i, default=CliReply(f"Reply of {i}", after=2)) for i in ("worker-1", "critic")}
        clis["worker-1"].script(CliReply("Built.", after=2, tools=["bash"]))
        died, first = [], None
        for n in (at, second, 0):
            steps = Steps(n)
            first = first or steps
            with mock.patch.object(os, "replace", steps.around("replace", os.replace)), \
                    mock.patch.object(os, "fsync", steps.around("fsync", os.fsync)), \
                    mock.patch.object(os, "link", steps.around("link", os.link)):
                eng = None
                try:
                    with mock.patch.object(Store, "_release", Store._release):
                        eng = MiniEngine(run, clock, clis, steps, notes)
                        eng.work(self.COMMANDS)
                    break
                except Died as e:
                    died.append(str(e))
                    if eng:
                        eng.die()
                    else:  # it died while starting
                        for c in clis.values():
                            c.owner = None
            for f in run.glob("locks/*"):
                f.unlink()
            if tear and n == at:  # the file of the last write is damaged too
                p = run / "state.json"
                torn = p.with_name("torn")  # a new file: state.prev.json may be a link to this one
                torn.write_bytes(p.read_bytes()[:-50])
                os.replace(torn, p)
        eng.die()
        return eng.store, clis, notes, first, died

    def check(self, at, **kw):
        store, clis, notes, steps, died = self.scenario(at, **kw)
        s = store.snapshot()
        with self.subTest(died=died, at=at, **kw):
            for cli in clis.values():
                ids = [i for i, _ in cli.sent]
                self.assertEqual(len(ids), len(set(ids)), f"sent twice: {cli.sent}")
            self.assertEqual([t for _, t in clis["worker-1"].sent], ["user: Build it."])
            self.assertEqual(sorted(t for _, t in clis["critic"].sent), ["user: How is it?", "worker-1: Built."])
            self.assertFalse([m for m in s.messages.values() if m.open], "not answered")
            self.assertEqual(len([m for m in s.messages.values() if m.recipient != USER]), 3)
            self.assertEqual({m.body for m in s.messages.values() if m.recipient == USER}, {"Reply of critic"})
            self.assertEqual(s.rounds, 1)
            self.assertEqual(set(notes), {"Built."})  # at least once
            self.assertEqual(([], None), (store.unfinished(), store.pending))
            self.assertEqual({a.status for a in s.agents.values()}, {AgentStatus.IDLE})
            seqs = [m.seq for m in s.messages.values()]
            self.assertEqual(seqs, sorted(set(seqs)))
            files = sorted(p.name for p in (store.run_dir / "rounds").glob("r*/t*.json"))
            self.assertEqual(len(files), len(set(files)))
        return steps

    def test_MSG_6_REC_7_dying_at_every_step(self):
        steps = self.check(0)
        self.assertGreater(steps.n, 100)
        self.assertIn("before send", steps.names)
        for at in range(1, steps.n + 1):
            self.check(at)

    def test_MSG_6_dying_twice(self):
        n = self.check(0).n
        for at in range(5, n, 23):
            for second in range(3, 60, 13):
                self.check(at, second=second)

    def test_MSG_6_dying_with_the_last_write_damaged(self):
        n = self.check(0).n
        for at in range(40, n, 11):
            self.check(at, tear=True)

    def test_REC_7_steps_of_one_event(self):
        """Between begin and apply the event stays pending with its facts; between apply and the send the message waits."""
        tmp = Path(tempfile.mkdtemp(prefix="rtbc"))
        self.addCleanup(shutil.rmtree, tmp, True)
        clock = FakeClock(T0)
        st = Store.create(tmp / "r", clock, snapshot())
        ev = TurnEnded("worker-1", T0, "worker-1-1", "5", text="Done.")
        st.begin(ev)
        st._release()
        st = Store(tmp / "r", clock)
        self.assertEqual((st.pending, st.snapshot().turns["worker-1"].text), (ev, "Done."))
        self.assertEqual(st.snapshot().agents["worker-1"].proc.read_pos, "5")
        send = Send(Message(new_id("routing", ev), "worker-1", "critic", "Done."))
        st.apply(st.pending, [send])
        st._release()
        st = Store(tmp / "r", clock)
        self.assertIsNone(st.begin(ev))
        self.assertEqual([i.action.message.id for i in st.unfinished()], [send.message.id])
        self.assertTrue(st.begin_send(send.message.id, "x"))
        st._release()
        st = Store(tmp / "r", clock)
        self.assertEqual((st.to_send(), [m.rendered for m in st.in_doubt("critic")]), ([], ["x"]))
        st._release()


if __name__ == "__main__":
    unittest.main()
