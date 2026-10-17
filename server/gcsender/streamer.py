"""Character-counting command streamer (docs/PLAN.md §5.2).

GRBL acknowledges every line with ``ok`` or ``error``. The host may keep
sending as long as the bytes of unacknowledged lines fit in the controller's
128-byte RX buffer (127 usable). Each ``ok``/``error`` pops the oldest
in-flight line. Real-time bytes bypass this queue entirely.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

RX_BUDGET = 127


class StreamerError(Exception):
    pass


@dataclass(eq=False)
class Pending:
    line: str
    source: str
    size: int
    event: threading.Event = field(default_factory=threading.Event)
    status: str | None = None  # ok | error | aborted
    response: str | None = None
    lines: list[str] = field(default_factory=list)  # informational lines before the response
    sent_at: float = 0.0

    def wait(self, timeout: float | None) -> bool:
        return self.event.wait(timeout)


class Streamer:
    def __init__(self, write: Callable[[bytes], None], budget: int = RX_BUDGET):
        self._write = write
        self.budget = budget
        self._inflight: deque[Pending] = deque()
        self._used = 0
        self._cond = threading.Condition()
        self._generation = 0  # bumped by reset() to release waiting senders
        self._reset_reason = "reset"

    @property
    def used(self) -> int:
        with self._cond:
            return self._used

    @property
    def inflight(self) -> int:
        with self._cond:
            return len(self._inflight)

    def send(self, line: str, source: str = "console", timeout: float = 30.0) -> Pending:
        """Queue a line once it fits in the RX budget. Does not wait for the reply."""
        size = len(line.encode("ascii", "replace")) + 1
        if size > self.budget:
            raise StreamerError(f"line too long ({size - 1} chars)")
        deadline = time.monotonic() + timeout
        with self._cond:
            gen = self._generation
            while self._used + size > self.budget:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise StreamerError("timed out waiting for controller buffer space")
                self._cond.wait(left)
                if gen != self._generation:
                    raise StreamerError(self._reset_reason)
            p = Pending(line, source, size, sent_at=time.monotonic())
            self._inflight.append(p)
            self._used += size
            try:
                self._write((line + "\n").encode("ascii", "replace"))
            except Exception:
                self._inflight.pop()
                self._used -= size
                raise
            return p

    def on_response(self, status: str, line: str) -> Pending | None:
        """Called for every ``ok`` / ``error`` line. Returns the completed entry."""
        with self._cond:
            if not self._inflight:
                return None  # unsolicited (e.g. after a reset we already flushed)
            p = self._inflight.popleft()
            self._used -= p.size
            self._cond.notify_all()
        p.status = status
        p.response = line
        p.event.set()
        return p

    def describe(self) -> dict[str, Any]:
        """What is in flight right now (for bug reports)."""
        now = time.monotonic()
        with self._cond:
            return {
                "budget": self.budget,
                "used": self._used,
                "inflight": [
                    {"line": p.line, "source": p.source, "bytes": p.size, "age_s": round(now - p.sent_at, 3)}
                    for p in self._inflight
                ],
            }

    def head_source(self) -> str | None:
        with self._cond:
            return self._inflight[0].source if self._inflight else None

    def on_info(self, line: str) -> Pending | None:
        """Attach an informational line to the oldest in-flight command."""
        with self._cond:
            p = self._inflight[0] if self._inflight else None
            if p is not None:
                p.lines.append(line)
            return p

    def reset(self, reason: str = "reset") -> list[Pending]:
        """Abort everything in flight (controller reset / disconnect)."""
        with self._cond:
            aborted = list(self._inflight)
            self._inflight.clear()
            self._used = 0
            self._generation += 1
            self._reset_reason = reason
            self._cond.notify_all()
        for p in aborted:
            p.status = "aborted"
            p.response = reason
            p.event.set()
        return aborted
