"""Run the complete runtime against persistent scripted CLI processes."""

import tempfile
import time
import unittest
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path

from rt_helpers import C, CLAUDE, CODEX, T0, agent, milestone, snapshot
from orch.runtime.adapters.mock import ALL_CAPS, FakeClock, MockAdapter, MockCLI, Reply
from orch.runtime.engine import Engine

MANAGER, CRITIC, W1, W2 = "manager", "critic", "worker-1", "worker-2"
BACKUP = replace(CODEX, name="backup")
QUIET = "<silent/>"


def tell(recipient, text):
    return f'<message to="{recipient}">{text}</message>'


class MockBackend(MockAdapter):
    """Select the simulated process by agent; keep smoke sessions separate."""

    def __init__(self, clock, processes, run_dir, smoke, smoke_replies):
        super().__init__(clock)
        self.processes = processes
        self.run_dir = run_dir
        self.smoke = smoke
        self.smoke_replies = smoke_replies

    def start(self, spec, session):
        if Path(spec.run_dir) == self.run_dir:
            self.cli = self.processes[spec.agent]
        else:
            replies = self.smoke_replies[spec.choice.backend]
            reply = replies.popleft() if replies else Reply("OK", after=0)
            self.cli = MockCLI(self.clock, spec.agent, default=reply)
            self.smoke.append(self.cli)
        self.caps = self.cli.caps
        return super().start(spec, session)

    def reconnect(self, proc):
        self.cli = self.processes[proc.stream.rsplit("-", 1)[0]]
        self.caps = self.cli.caps
        return super().reconnect(proc)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="r-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.checklist = self.path / "checklist.md"
        self.write_checklist(False)
        self.clock = FakeClock(T0)
        self.banks = {}
        self.smoke = []
        self.smoke_replies = defaultdict(deque)
        self.factories = {}
        self.agents = {}
        self.command_count = 0
        self.settings = C.Settings(silence_after=100_000, check_every=100_000)
        for name in (MANAGER, CRITIC, W1, W2):
            self.add_agent(name)

    def add_agent(self, name, choice=CLAUDE, backups=(), lifecycle=C.Lifecycle.OPEN):
        self.agents[name] = agent(
            name, cwd=str(self.path), proc=None, choice=choice, choices=[choice, *backups],
            sessions={}, session_key="", lifecycle=lifecycle, fresh_session="never",
            state_file=str(self.path / (name + "-state.md")),
        )
        for option in (choice, *backups):
            bank = self.banks.setdefault(option.backend, {})
            default = Reply(QUIET if name == CRITIC else "" if name == MANAGER else "Done.")
            caps = replace(ALL_CAPS, shares_terminal=True) if option.backend == "codex" else ALL_CAPS
            cli = MockCLI(self.clock, name, caps=caps, default=default)
            # Independent backends must not reuse the same mock process stream or session ID.
            offset = list(self.banks).index(option.backend) * 100
            cli.pid = cli.sessions = offset
            bank[name] = cli
            self.factories[option.backend] = lambda bank=bank: MockBackend(
                self.clock, bank, self.path / "run", self.smoke, self.smoke_replies,
            )

    def cli(self, name, backend="claude"):
        return self.banks[backend][name]

    def write_checklist(self, done):
        self.checklist.write_text("## Verify\n\n- [" + ("x" if done else " ") + "] Confirm output\n")

    def start(self, *milestones, finished=False, resume=False):
        run_dir = self.path / "run"
        self.engine = Engine(run_dir, self.clock, self.factories, {"tick_s": 1})
        self.addCleanup(self.engine.close)
        if resume:
            self.engine.start(None)
        else:
            records = milestones or (milestone(checklist_path=str(self.checklist), rubric=["Output works"]),)
            info = C.RunInfo(id="r-e2e", project="e2e", workspace=str(self.path), run_dir=str(run_dir),
                             decisions_path=str(self.path / "decisions.md"), started=self.clock.now())
            initial = snapshot(*records, agents=self.agents.copy(), workers=0, run=info,
                               settings=self.settings, finished=finished)
            self.engine.start(initial)
        return self.engine

    def restart(self):
        self.engine.close()
        return self.start(resume=True)

    @property
    def state(self):
        return self.engine.store.snapshot()

    def command(self, kind, target="", **kwargs):
        self.command_count += 1
        return self.engine.submit(kind(target, self.clock.now(), f"command-{self.command_count}", **kwargs))

    def chat(self, target, text):
        return self.command(C.Chat, target, text=text)

    def assign(self, text="Begin the work."):
        self.chat(CRITIC, text)
        self.advance(2)

    def advance(self, seconds=1):
        self.clock.advance(seconds)
        self.drain()

    def drain(self):
        for _ in range(200):
            if not self.engine.step():
                return
        self.fail("engine did not drain after 200 steps")

    def wait_for(self, predicate, description, *, limit=30, real_io=False, interval=1):
        for _ in range(limit):
            self.drain()
            if predicate():
                return
            if real_io:
                time.sleep(0.025)
            self.clock.advance(interval)
        state = self.state
        worker = state.agents[W1]
        failures = [(m.body or m.key, m.error) for m in state.messages.values() if m.error]
        self.fail(f"did not observe {description}: worker={worker.status.value}, "
                  f"failure={worker.failed_why!r}, switch={worker.switch!r}, retry={worker.retry!r}, "
                  f"jobs={list(state.jobs)}, message errors={failures}, "
                  f"smoke inputs={[(cli.agent, cli.sent) for cli in self.smoke]}")

    def messages(self, target, *, body=None, key=None):
        return [m for m in self.state.messages.values()
                if m.recipient == target and (body is None or body in m.body)
                and (key is None or m.key == key)]

    def incoming(self, target, *, body=None, key=None, backend="claude"):
        cli = self.cli(target, backend)
        cli.flush()
        messages = self.messages(target, body=body, key=key)
        return [m for m in messages for received in cli.received if m.id == received]

    def assert_received_once(self, target, *, body=None, key=None, backend="claude", answered=True):
        got = self.incoming(target, body=body, key=key, backend=backend)
        self.assertEqual(len(got), 1, f"{target} receipt for {body or key}: {got}")
        message = got[0]
        self.assertEqual(sum(mid == message.id for mid, _ in self.cli(target, backend).sent), 1)
        expected = C.MessageStatus.ANSWERED if answered else C.MessageStatus.DELIVERED
        self.assertEqual(message.status, expected)
        return message
