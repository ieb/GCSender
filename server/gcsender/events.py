"""Event bus with a replay ring buffer, retained state and per-client queues.

Design (see docs/PLAN.md §6.3):

* Every *durable* event gets a monotonically increasing id of the form
  ``"<boot>:<n>"`` and is stored in a bounded ring buffer so that a client
  reconnecting with ``Last-Event-ID`` can be replayed exactly what it missed.
* *Volatile* events (e.g. the 5-10 Hz machine ``status``) are delivered live
  but get no id and are not stored in the ring; only the latest value is kept.
* *Retained* events keep their latest payload per type. Together with the
  recent console history they form the ``snapshot`` that a fresh client (or one
  whose ``Last-Event-ID`` is too old / from a previous server run) receives.

The snapshot is built entirely from data owned by the bus, under the bus lock,
so it is always consistent with the event id it carries. Publishers never need
to hold any other lock while calling :meth:`EventBus.publish`, and the bus
never calls back into other components while holding its lock.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

DEFAULT_RING_SIZE = 5000
DEFAULT_CONSOLE_HISTORY = 500
DEFAULT_QUEUE_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: Any
    id: str | None = None  # None for volatile events
    ts: float = 0.0


class Subscription:
    """A single client's view of the bus. Thread-safe; consumed by one reader."""

    def __init__(self, bus: "EventBus", limit: int):
        self._bus = bus
        self._limit = limit
        self._queue: deque[Event] = deque()
        self._cond = threading.Condition()
        self.closed = False
        self.overflowed = False

    def _put(self, event: Event) -> None:
        with self._cond:
            if self.closed:
                return
            if len(self._queue) >= self._limit:
                # Slow consumer: drop the connection; the client reconnects with
                # Last-Event-ID and is replayed or re-snapshotted.
                self.overflowed = True
                self.closed = True
            else:
                self._queue.append(event)
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> Event | None:
        """Next event, or None on timeout / close."""
        with self._cond:
            if not self._queue and not self.closed:
                self._cond.wait(timeout)
            if self._queue:
                return self._queue.popleft()
            return None

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()
        self._bus._remove(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(
        self,
        ring_size: int = DEFAULT_RING_SIZE,
        console_history: int = DEFAULT_CONSOLE_HISTORY,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        boot: str | None = None,
    ):
        self.boot = boot or secrets.token_hex(4)
        self._lock = threading.Lock()
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._console: deque[Any] = deque(maxlen=console_history)
        self._retained: dict[str, Any] = {}
        self._subs: set[Subscription] = set()
        self._seq = 0
        self._queue_limit = queue_limit
        self._closed = False

    # -- publishing ---------------------------------------------------------

    def publish(self, type: str, data: Any = None, *, retain: bool = False, volatile: bool = False) -> Event:
        with self._lock:
            if retain:
                self._retained[type] = data
            if volatile:
                event = Event(type, data, None, time.time())
            else:
                self._seq += 1
                event = Event(type, data, f"{self.boot}:{self._seq}", time.time())
                self._ring.append(event)
                if type == "console":
                    self._console.append(data)
            subs = list(self._subs)
        for sub in subs:
            sub._put(event)
        return event

    # -- reading ------------------------------------------------------------

    @property
    def last_id(self) -> str:
        with self._lock:
            return f"{self.boot}:{self._seq}"

    def retained(self, type: str, default: Any = None) -> Any:
        with self._lock:
            return self._retained.get(type, default)

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "id": f"{self.boot}:{self._seq}",
            "boot": self.boot,
            "retained": dict(self._retained),
            "console": list(self._console),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _parse_id(self, last_event_id: str | None) -> int | None:
        """Sequence number for an id from this boot, else None."""
        if not last_event_id:
            return None
        boot, _, seq = last_event_id.partition(":")
        if boot != self.boot:
            return None
        try:
            n = int(seq)
        except ValueError:
            return None
        return n if 0 <= n <= self._seq else None

    def subscribe(self, last_event_id: str | None = None) -> Subscription:
        """Register a client.

        If ``last_event_id`` is from this boot and still covered by the ring,
        the missed events are queued (followed by the latest volatile status);
        otherwise a single ``snapshot`` event is queued first.
        """
        sub = Subscription(self, self._queue_limit)
        with self._lock:
            if self._closed:
                sub.closed = True
                return sub
            n = self._parse_id(last_event_id)
            oldest = self._ring[0] if self._ring else None
            oldest_n = int(oldest.id.split(":")[1]) if oldest else self._seq + 1
            if n is not None and n >= oldest_n - 1:
                for ev in self._ring:
                    if int(ev.id.split(":")[1]) > n:
                        sub._queue.append(ev)
                # Volatile retained state (e.g. status) is not in the ring:
                # resend its latest value so the client is current.
                for t in self._volatile_retained_types():
                    if t in self._retained:
                        sub._queue.append(Event(t, self._retained[t], None, time.time()))
            else:
                snap = self._snapshot_locked()
                sub._queue.append(Event("snapshot", snap, snap["id"], time.time()))
            self._subs.add(sub)
        return sub

    # Types that are published volatile+retained. Kept explicit so replay can
    # refresh them without guessing.
    VOLATILE_RETAINED = ("status",)

    def _volatile_retained_types(self) -> tuple[str, ...]:
        return self.VOLATILE_RETAINED

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self) -> None:
        """Wake and close every subscriber (server shutdown)."""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            sub.close()
