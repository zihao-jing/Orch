"""Observed turn timing, without pretending silent intervals are model reasoning."""
import threading
import time


class TurnTiming:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.first_activity = self.first_text = self.last_text = None
        self.tools = {}
        self.intervals = []
        self.unknown_tools = False

    def event(self, event):
        with self.lock:
            now = max(0, self.clock() - self.started)
            kind = event.get("k")
            if kind in ("text", "tool", "tool_start", "tool_end", "tool_timing", "reasoning"):
                if self.first_activity is None:
                    self.first_activity = now
            if kind == "text" and event.get("s"):
                if self.first_text is None:
                    self.first_text = now
                self.last_text = now
            if kind == "tool" and not event.get("timed"):
                self.unknown_tools = True
            elif kind == "tool_start":
                self.tools.setdefault(event.get("id"), now)
            elif kind == "tool_end":
                start = self.tools.pop(event.get("id"), None)
                if start is not None:
                    self.intervals.append((start, now))
                else:
                    self.unknown_tools = True
            elif kind == "tool_timing":
                duration = event.get("duration_s")
                if isinstance(duration, (int, float)) and duration >= 0:
                    self.intervals.append((max(0, now - duration), now))
                else:
                    self.unknown_tools = True

    def finish(self):
        with self.lock:
            total = max(0, self.clock() - self.started)
            intervals = self.intervals + [(start, total) for start in self.tools.values()]
            # Union, not a sum: tools can run concurrently.
            tool_time, end = 0.0, 0.0
            for a, b in sorted(intervals):
                tool_time += max(0, b - max(a, end))
                end = max(end, b)
            rounded = lambda n: None if n is None else round(n, 3)
            return {
                "first_activity_s": rounded(self.first_activity),
                "first_text_s": rounded(self.first_text),
                "text_window_s": rounded(None if self.first_text is None else self.last_text - self.first_text),
                "observed_tool_s": rounded(tool_time) if intervals else None,
                "tool_timing_complete": bool(intervals) and not self.tools and not self.unknown_tools,
                "unattributed_s": rounded(max(0, total - tool_time)),
                "note": "Observed events only. First activity includes startup/provider waiting; text window is "
                        "not active generation time. Unattributed time may include model activity, waiting, "
                        "retries, and uninstrumented tools.",
            }


def describe(timing):
    if not timing:
        return "Timing breakdown unavailable for this turn."
    def seconds(key):
        value = timing.get(key)
        return "unknown" if value is None else f"{value:g}s"
    return (f"First observed activity: {seconds('first_activity_s')}; first text: {seconds('first_text_s')}; "
            f"text response window: {seconds('text_window_s')}; observed tools: {seconds('observed_tool_s')} "
            f"({'complete' if timing.get('tool_timing_complete') else 'partial/unknown'}); "
            f"unattributed: {seconds('unattributed_s')}. " + timing.get("note", ""))
