"""What every adapter shares: the base class with the answers of a CLI that can do nothing special, and two helpers."""
from __future__ import annotations

import re
import time
from typing import Iterator, List, Optional

from ..contracts import (AgentEvent, AgentSpec, Caps, Clock, ErrorClass, Job, ModelInfo, ProcRef, Said, SessionRef)


class AdapterError(Exception):
    """A CLI that could not be started, or took no input. `error` says what kind, so recovery can decide."""

    def __init__(self, text: str, error: ErrorClass = ErrorClass.TRANSIENT):
        super().__init__(text)
        self.error = error


class SystemClock:
    def now(self) -> float:
        return time.time()


def job_id(agent: str, cli_id: object) -> str:
    """Job.id for a CLI's own job id."""
    return f"{agent}/{cli_id}"


_BILLING = re.compile(r"credit balance is too low|insufficient[_ ]quota|exceeded your current quota|billing|"
                      r"payment required|purchase (more )?credits|account (has been |is )?(suspended|disabled|"
                      r"deactivated)", re.I)
_MISSING = re.compile(r"executable not found|command not found|no such file or directory: '[^']*'$", re.I | re.M)
_TRANSIENT = re.compile(r"API Error|can't receive replies|portal\.anthropic\.com/verify|overloaded|rate[ _]limit|"
                        r"usage limit|too many requests|\b(429|5\d\d)\b|timed? ?out|connection|network", re.I)


def classify_error(text: str) -> ErrorClass:
    """The class of a CLI's error text (today's patterns). Pass only the CLI's or provider's own words, never the
    agent's cut-off reply. An adapter decides SESSION_LOST itself, from its CLI's own error."""
    if _BILLING.search(text or ""):
        return ErrorClass.BILLING
    if _MISSING.search(text or ""):
        return ErrorClass.MISSING_CLI
    return ErrorClass.TRANSIENT if _TRANSIENT.search(text or "") else ErrorClass.OTHER


class BaseAdapter:
    """Base of the adapters; a subclass overrides what its CLI can do and sets `caps` to match."""

    backend = ""
    caps = Caps()

    def __init__(self, clock: Clock):
        self.clock = clock
        self.spec: Optional[AgentSpec] = None
        self.proc: Optional[ProcRef] = None

    def start(self, spec: AgentSpec, session: Optional[SessionRef]) -> ProcRef:
        raise NotImplementedError

    def reconnect(self, proc: ProcRef) -> bool:
        return False

    def send(self, message_id: str, text: str) -> None:
        raise NotImplementedError

    def arrived(self, message_id: str) -> Optional[bool]:
        return None

    def events(self) -> Iterator[AgentEvent]:
        return iter(())

    def jobs(self) -> List[Job]:
        return []

    def stop_job(self, job_id: str) -> bool:
        return False

    def interrupt(self) -> bool:
        return False

    def detach(self) -> None:
        pass

    def stop(self) -> None:
        raise NotImplementedError

    def compact(self) -> bool:
        return False

    def terminal_command(self, spec: AgentSpec, session: SessionRef) -> List[str]:
        raise AdapterError("no terminal for this CLI", ErrorClass.OTHER)

    def transcript(self, session: SessionRef, since: float) -> List[Said]:
        return []

    def catalog(self) -> List[ModelInfo]:
        return []
