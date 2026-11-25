"""A scripted CLI on a fake clock: no process, no network, no real time.

`MockCLI` is the simulated process (its output log, sessions and jobs); it lives on when an adapter detaches. Any number
of `MockAdapter`s, one after the other, can join it, as after an Orch restart. Everything happens at times of the clock:

    clock = FakeClock()
    cli = MockCLI(clock, "worker-1")
    cli.script(Reply("Started training.", jobs=["train"]), Reply("Noted."))     # the answers to the next messages
    ad = MockAdapter(clock, cli)
    ad.start(AgentSpec("worker-1"), None)
    ad.send("m1", "Train it.")
    cli.job_end("train", exit_code=0, after=60, wake="Trained: 93%.")           # wake=None: it does not wake itself
    clock.advance(120)
    list(ad.events())     # SessionChanged, Received, TurnStarted, JobStarted, Text, TurnEnded, JobEnded, TurnStarted...

Also: Reply(error=...), Reply(ends=False) (frozen), Reply(receipt=False, starts=False) (swallowed), Reply(exit=1) (dies),
cli.exit(), cli.lose_session(), cli.other_session(), cli.refuse_sends(), cli.duplicate(), cli.emit() for any event.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple, Type, Union

from ..contracts import (AgentEvent, AgentSpec, Caps, Clock, ErrorClass, Job, JobEnded, JobStarted, JobState, ModelInfo,
                         ModelReported, ProcRef, ProcessExited, Received, Said, SessionChange, SessionChanged,
                         SessionRef, Text, ToolEnd, ToolStart, TurnEnded, TurnStarted, TurnStatus, USER)
from .base import AdapterError, BaseAdapter, job_id

ALL_CAPS = Caps(sends_while_writing=True, reconnects=True, job_record=True, wakes_itself=True, receipts=True,
                shares_terminal=True, compacts=True, stops_jobs=True)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.t = now

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


@dataclass
class Reply:
    """What the CLI does with one message: one turn."""
    text: str = "Done."
    after: float = 1.0  # seconds the turn takes
    start_after: float = 0.0
    receipt: bool = True  # False: never reports Received, and arrived() is False
    starts: bool = True  # False: the message is taken in but no turn starts
    ends: bool = True  # False: the turn never ends
    tools: List[str] = field(default_factory=list)  # tool calls started (and, if the turn ends, ended)
    jobs: List[str] = field(default_factory=list)  # ids of jobs it starts
    error: Optional[ErrorClass] = None
    detail: str = ""
    exit: Optional[int] = None  # the process dies instead of ending the turn
    model: str = ""  # reports this model


class MockCLI:
    """The simulated CLI of one agent."""

    def __init__(self, clock: Clock, agent: str = "agent", caps: Caps = ALL_CAPS,
                 default: Optional[Reply] = None):
        self.clock, self.agent, self.caps, self.default = clock, agent, caps, default or Reply()
        self.alive = False
        self.pid = 0
        self.starts: List[Tuple[AgentSpec, Optional[SessionRef]]] = []  # every start, for assertions
        self.sent: List[Tuple[str, str]] = []  # (message id, text) the CLI's input took
        self.received: List[str] = []  # ids in the session
        self.said: List[Said] = []
        self.session_id = ""
        self.sessions = 0
        self.jobs: Dict[str, Job] = {}
        self.log: List[AgentEvent] = []  # this process's output; pos = index + 1
        self.replies: Deque[Reply] = deque()
        self._pending: List[Tuple[float, int, Union[AgentEvent, Callable[[], None]]]] = []
        self._n = 0
        self._busy = False
        self._next_change: Optional[SessionChange] = None
        self._refuse: List[ErrorClass] = []
        self._again = 0
        self._due: Optional[float] = None  # while something scheduled happens: its time

    # -------------------------------------------------- scripting

    def script(self, *replies: Union[Reply, str]) -> "MockCLI":
        self.replies.extend(Reply(r) if isinstance(r, str) else r for r in replies)
        return self

    def emit(self, kind: Type[AgentEvent], after: float = 0.0, **kw: Any) -> None:
        """Any event, `after` seconds from now."""
        self._at(after, kind(agent=self.agent, **kw))

    def job_start(self, name: str, label: str = "", after: float = 0.0) -> str:
        jid = job_id(self.agent, name)
        self.emit(JobStarted, after, job=Job(jid, self.agent, label or name, started=self._now() + after))
        return jid

    def job_end(self, name: str, exit_code: Optional[int] = 0, after: float = 0.0, wake: Union[Reply, str, None] = None,
                lost: bool = False, report: bool = True, forget: bool = False) -> None:
        """The job ends; `wake`: the turn the CLI then starts by itself (None: none). report=False: jobs() shows it
        ended, no event says so. forget=True: it also disappears from jobs()."""
        jid = job_id(self.agent, name)

        def end() -> None:
            j = self.jobs.get(jid)
            if j:
                self.jobs[jid] = replace(j, state=JobState.LOST if lost else JobState.ENDED, exit_code=exit_code,
                                         ended=self._now())
            if forget:
                self.jobs.pop(jid, None)

        self._at(after, end)
        if report:
            self.emit(JobEnded, after, job_id=jid, exit_code=exit_code, lost=lost)
        if wake is not None:
            self._at(after, lambda: self._turn(Reply(wake) if isinstance(wake, str) else wake, ()))

    def exit(self, code: int = 1, after: float = 0.0, detail: str = "") -> None:
        """The process dies; its jobs are gone from jobs()."""
        self._at(after, lambda: self._die(code, detail, False))

    def lose_session(self) -> None:
        """The next start with a session reports that the CLI lost or rejected it."""
        self._next_change = SessionChange.LOST

    def other_session(self) -> None:
        """The next start with a session silently returns another one."""
        self._next_change = SessionChange.DIFFERENT

    def refuse_sends(self, times: int = 1, error: ErrorClass = ErrorClass.TRANSIENT) -> None:
        self._refuse += [error] * times

    def duplicate(self, n: int = 1) -> None:
        """The last n events already read are reported once more, with the same pos."""
        self._again = n

    # -------------------------------------------------- inside

    def _at(self, after: float, what: Union[AgentEvent, Callable[[], None]]) -> None:
        self._n += 1
        self._pending.append((self._now() + after, self._n, what))

    def _now(self) -> float:
        return self.clock.now() if self._due is None else self._due

    def flush(self) -> None:
        """Let everything that is due happen, in time order."""
        while True:
            due = sorted((p for p in self._pending if p[0] <= self.clock.now()), key=lambda p: p[:2])
            if not due:
                return
            self._pending.remove(due[0])
            at, _n, what = due[0]
            if not isinstance(what, AgentEvent):
                self._due = at
                try:
                    what()
                finally:
                    self._due = None
                continue
            if not self.alive and not isinstance(what, ProcessExited):
                continue
            if isinstance(what, JobStarted) and what.job:
                self.jobs[what.job.id] = what.job
            elif isinstance(what, Received):
                self.received.append(what.message_id)
            elif isinstance(what, TurnEnded):
                self._busy = False
                self.said.append(Said(self.agent, what.text, at))
            stream = what.stream or f"{self.agent}-{self.pid}"
            self.log.append(replace(what, at=what.at or at, stream=stream, pos=len(self.log) + 1))

    def _turn(self, r: Reply, ids: Tuple[str, ...]) -> None:
        self._busy = True
        t = r.start_after
        self.emit(TurnStarted, t, message_ids=ids)
        if r.model:
            self.emit(ModelReported, t, model=r.model)
        for i, tool in enumerate(r.tools):
            self.emit(ToolStart, t, tool_id=f"t{i}", label=tool)
        for name in r.jobs:
            self.job_start(name, after=t)
        if r.text and r.error is None:
            self.emit(Text, t, text=r.text)
        t += r.after
        if r.exit is not None:
            self.exit(r.exit, t)
        elif r.ends:
            for i, _tool in enumerate(r.tools):
                self.emit(ToolEnd, t, tool_id=f"t{i}")
            self.emit(TurnEnded, t, text="" if r.error else r.text, error=r.error, detail=r.detail,
                      status=TurnStatus.ERROR if r.error else TurnStatus.OK, session_id=self.session_id)

    def _die(self, code: int, detail: str, asked: bool) -> None:
        if not self.alive:
            return
        self.alive = self._busy = False
        self._pending.clear()
        self.jobs.clear()
        self.emit(ProcessExited, exit_code=code, detail=detail, asked=asked)

    def _start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        self.flush()
        if self.alive:
            raise AdapterError("a CLI is already open for this agent", ErrorClass.OTHER)  # REC-8
        self.starts.append((spec, session))
        self.alive, self._busy, self.log, self._pending = True, False, [], []
        self.pid += 1
        change = SessionChange.NEW if session is None else (self._next_change or SessionChange.RESUMED)
        if change in (SessionChange.NEW, SessionChange.DIFFERENT):
            self.sessions += 1
            self.session_id, self.received = f"{self.agent}-s{self.sessions}", []
        elif session is not None:
            self.session_id = session.session_id
        if session is not None:
            self._next_change = None
        self.emit(SessionChanged, session_id=self.session_id, change=change)
        return ProcRef(pid=self.pid, started=self.clock.now(), stream=f"{self.agent}-{self.pid}")

    def _send(self, message_id: str, text: str) -> None:
        self.flush()
        if not self.alive:
            raise AdapterError("the CLI is not running")
        if self._refuse:
            raise AdapterError("refused", self._refuse.pop(0))
        if self._busy and not self.caps.sends_while_writing:
            raise AdapterError("busy", ErrorClass.OTHER)
        self.sent.append((message_id, text))
        self.said.append(Said(USER, text, self.clock.now()))
        if self._busy:
            if self.caps.receipts:
                self.emit(Received, message_id=message_id)
            return
        r = self.replies.popleft() if self.replies else self.default
        if r.receipt and self.caps.receipts:
            self.emit(Received, r.start_after, message_id=message_id)
        elif r.receipt:
            self.received.append(message_id)
        if r.starts:
            self._turn(r, (message_id,))


class MockAdapter(BaseAdapter):
    backend = "mock"

    def __init__(self, clock: Clock, cli: Optional[MockCLI] = None):
        super().__init__(clock)
        self.cli = cli or MockCLI(clock)
        self.caps = self.cli.caps
        self.pos = 0  # events up to here were read
        self.joined = False

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        proc = self.cli._start(spec, session)
        self.spec, self.proc, self.pos, self.joined = spec, proc, 0, True
        return proc

    def reconnect(self, proc: ProcRef) -> bool:
        self.cli.flush()
        if not self.cli.alive or self.cli.pid != proc.pid:
            return False
        self.proc, self.pos, self.joined = proc, proc.read_pos, True
        return True

    def send(self, message_id: str, text: str) -> None:
        if not self.joined:
            raise AdapterError("not connected", ErrorClass.OTHER)
        self.cli._send(message_id, text)

    def arrived(self, message_id: str) -> Optional[bool]:
        self.cli.flush()
        return message_id in self.cli.received if self.cli.caps.receipts else None

    def events(self) -> Iterator[AgentEvent]:
        if not self.joined:
            return
        self.cli.flush()
        again, self.cli._again = self.cli._again, 0
        for ev in self.cli.log[max(0, self.pos - again):self.pos]:
            yield ev
        while self.pos < len(self.cli.log):
            self.pos += 1
            yield self.cli.log[self.pos - 1]

    def jobs(self) -> List[Job]:
        self.cli.flush()
        return [replace(j) for j in self.cli.jobs.values()]

    def stop_job(self, job_id: str) -> bool:
        self.cli.flush()
        if job_id not in self.cli.jobs:
            return False
        self.cli.job_end(job_id.split("/", 1)[1], exit_code=143)
        return True

    def interrupt(self) -> None:
        self.cli.flush()
        if self.cli._busy:
            self.cli._pending = [p for p in self.cli._pending if not isinstance(p[2], (TurnEnded, Text, ToolEnd))]
            self.cli.emit(TurnEnded, status=TurnStatus.STOPPED)

    def detach(self) -> None:
        self.joined = False

    def stop(self) -> None:
        self.cli.flush()
        self.cli._die(0, "", True)

    def compact(self) -> bool:
        if not self.caps.compacts or not self.cli.alive:
            return False
        self.cli.emit(SessionChanged, session_id=self.cli.session_id, change=SessionChange.COMPACTED)
        return True

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        return ["mock-cli", "--session", session.session_id]

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        return [s for s in self.cli.said if s.at > since]

    def catalog(self) -> List[ModelInfo]:
        return [ModelInfo("mock", backend="mock", model="mock", label="Mock")]
