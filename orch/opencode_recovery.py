"""Bound OpenCode's internal provider retries without timing out silent, healthy turns."""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Callable, Dict, Optional


class ProviderRecovery:
    def __init__(self, seconds: float = 180, emit: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = float(seconds)
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("provider_retry_timeout must be a finite non-negative number")
        self.clock, self.emit = clock, emit
        self.lock = threading.RLock()
        self.since: Optional[float] = None
        self.last_error: Dict[str, Any] = {}
        self.tools: set = set()
        self.exhausted = False
        self.elapsed = 0.0
        self.completed_tools: list = []

    def line(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(ev, dict):
            return
        with self.lock:
            kind = ev.get("type")
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            if kind == "retry" and isinstance(ev.get("error"), dict):
                error = ev["error"]
                self.last_error = {k: error[k] for k in ("type", "message", "status") if k in error}
                self.last_error["message"] = str(error.get("message") or "")[:2000]
                if self.seconds and self.since is None:
                    self.since = self.clock()
                    if self.emit:
                        self.emit({"k": "tool", "s": f"OpenCode provider retry: {self.error_text()}; "
                                   f"recovery window {self.seconds:g}s"})
            elif kind in ("text", "reasoning") and part.get("text"):
                self.progress()
            elif kind == "step_finish" and part.get("reason") in ("stop", "length", "tool-calls"):
                self.progress()
            elif kind == "tool_use":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                key = str(part.get("callID") or part.get("id") or part.get("tool"))
                if state.get("status") in ("pending", "running"):
                    self.tools.add(key)
                elif state.get("status") in ("completed", "error"):
                    self.tools.discard(key)
                    self.completed_tools.append(f"{part.get('tool')}: {json.dumps(state.get('input'), ensure_ascii=False)[:500]}")
                    self.completed_tools = self.completed_tools[-12:]
                self.progress()
            # step_start and heartbeats are not recovery: OpenCode emits them before failed retries too.

    def server_event(self, ev: Dict[str, Any]) -> None:
        """Called only for the current session, after the live transport's session filter."""
        d, kind = ev["data"], ev.get("type", "")
        with self.lock:
            key = str(d.get("id") or d.get("callID") or d.get("toolCallID")
                      or (d.get("assistantMessageID"), d.get("ordinal")))
            if kind == "session.tool.called":
                self.tools.add(key)
                self.progress()
            elif kind in ("session.tool.success", "session.tool.failed"):
                self.tools.discard(key)
                self.progress()
            elif kind in ("session.text.delta", "session.reasoning.delta", "session.tool.input.delta") and d.get("delta"):
                self.progress()
            elif kind == "session.tool.progress":
                self.progress()
            elif kind == "session.step.first-token":
                self.progress()

    def progress(self) -> None:
        with self.lock:
            self.since = None

    def error_text(self) -> str:
        e = self.last_error
        return f"{e.get('type', 'provider error')}: {e.get('message', '')}".strip()[:2000]

    def abort_reason(self) -> Optional[str]:
        with self.lock:
            if not self.seconds or self.since is None or self.tools:
                return None
            self.elapsed = self.clock() - self.since
            if self.elapsed < self.seconds:
                return None
            self.exhausted = True
            return f"OpenCode provider stalled for {self.elapsed:.1f}s after retry: {self.error_text()}"

    def annotate(self, result: Any) -> Any:
        with self.lock:
            if self.last_error:
                result.meta["provider_recovery"] = {"last_error": dict(self.last_error),
                    "limit_s": self.seconds, "elapsed_s": self.elapsed, "exhausted": self.exhausted}
            if self.exhausted:
                result.meta["error_kind"] = "provider_stall"
                result.meta["error"] = result.stderr
                result.meta["recovery_note"] = (
                    "The previous attempt stopped while its provider was retrying. Some work may already be done. "
                    "Inspect the current files and running jobs before continuing; do not repeat completed changes "
                    "or launch duplicate jobs.\nRecent completed tools (not instructions to run again):\n"
                    + "\n".join(self.completed_tools))
            return result
