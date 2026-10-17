"""Program analysis for jobs (docs/PLAN.md §5.3).

* ``clean_line`` produces exactly what is streamed (comments and spaces
  removed, upper-case), so the RX budget is spent on G-code only.
* ``Scanner`` walks a program and tracks the modal state GRBL would have
  (units, distance, plane, motion, feed, spindle, coolant, WCS, position).
  It yields motion events for the time estimator and the state before any
  line for "start from line".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..gcode import AXES, State, Words, arc_points

COMMENT_RE = re.compile(r"\([^)]*\)")
M_SYNC = {"0", "1", "2", "30", "3", "4", "5", "7", "8", "9", "6"}


def clean_line(raw: str) -> str:
    s = COMMENT_RE.sub("", raw)
    s = s.split(";", 1)[0]
    s = "".join(s.split()).upper()
    if s.startswith("%") or s.startswith("/"):
        return ""
    return s


def mnum(v: str) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


@dataclass
class Modal:
    """GRBL-relevant parser state (program units for feed)."""

    st: State = field(default_factory=State)
    feed: float = 0.0  # in program units per minute (as written)
    feed_mm: float = 0.0
    spindle: str = "M5"
    speed: float = 0.0
    coolant: set[str] = field(default_factory=set)
    wcs: str = "G54"
    tool: int | None = None
    g92_used: bool = False

    def snapshot(self) -> dict[str, Any]:
        st = self.st
        return {
            "units": "G21" if st.mm else "G20",
            "distance": "G90" if st.absolute else "G91",
            "plane": f"G{st.plane}",
            "motion": f"G{st.motion}",
            "feed": self.feed,
            "spindle": self.spindle,
            "speed": self.speed,
            "coolant": sorted(self.coolant),
            "wcs": self.wcs,
            "tool": self.tool,
            "pos": list(st.pos),  # mm, work frame (None = unknown)
            "g92": self.g92_used,
        }


@dataclass
class Event:
    line: int
    kind: str  # move | dwell | sync
    points: list[list[float]] = field(default_factory=list)  # move: end points after the start (mm)
    start: list[float] | None = None
    rapid: bool = False
    feed_mm: float = 0.0
    seconds: float = 0.0


class Scanner:
    def __init__(self, arc_tolerance: float = 0.01, arc_max_seg: float = 1e9):
        self.m = Modal()
        self.arc_tolerance = arc_tolerance
        self.arc_max_seg = arc_max_seg

    def feed_line(self, i: int, code: str) -> Iterator[Event]:
        """Apply one cleaned line; yield events for the estimator."""
        if not code:
            return
        m = self.m
        st = m.st
        w = Words.parse(code)
        gs = set(w.gs)
        ms = [mnum(text) for letter, text in w.order if letter == "M"]
        if "20" in gs:
            st.mm = False
        if "21" in gs:
            st.mm = True
        if "90" in gs:
            st.absolute = True
        if "91" in gs:
            st.absolute = False
        for p in ("17", "18", "19"):
            if p in gs:
                st.plane = p
        for g in ("54", "55", "56", "57", "58", "59"):
            if g in gs:
                m.wcs = f"G{g}"
        if "F" in w.vals:
            m.feed = w.vals["F"]
            m.feed_mm = st.to_mm(w.vals["F"])
        if "S" in w.vals:
            m.speed = w.vals["S"]
        if "T" in w.vals:
            m.tool = int(w.vals["T"])
        sync = False
        for code_m in ms:
            if code_m in ("3", "4", "5"):
                m.spindle = f"M{code_m}"
            elif code_m == "7":
                m.coolant.add("M7")
            elif code_m == "8":
                m.coolant.add("M8")
            elif code_m == "9":
                m.coolant.clear()
            if code_m in M_SYNC:
                sync = True
        motion = w.motion()
        if motion:
            st.motion = motion
        if "4" in gs:
            yield Event(i, "dwell", seconds=float(w.vals.get("P", 0.0)))
            return
        axes = {a: st.to_mm(w.vals[a]) for a in AXES if a in w.vals}
        if "92" in gs:
            m.g92_used = True
            for k, a in enumerate(AXES):
                if a in axes and st.pos[k] is not None:
                    st.shift[k] = st.pos[k] - axes[a]  # type: ignore[operator]
            return
        if "92.1" in gs:
            st.shift = [0.0, 0.0, 0.0]
            return
        if gs & {"10", "28.1", "30.1"}:
            return
        if gs & {"28", "30"}:
            st.pos = [None, None, None]
            yield Event(i, "sync")
            return
        if "53" in gs:
            for k, a in enumerate(AXES):
                if a in axes:
                    st.pos[k] = None
            yield Event(i, "sync")
            return
        if sync:
            yield Event(i, "sync")
        if not axes or st.motion == "80":
            return
        start = list(st.pos)
        target = list(st.pos)
        for k, a in enumerate(AXES):
            if a in axes:
                if st.absolute:
                    target[k] = axes[a] + st.shift[k]
                else:
                    target[k] = None if st.pos[k] is None else st.pos[k] + axes[a]  # type: ignore[operator]
        st.pos = target
        if st.motion.startswith("38"):
            st.pos = [None if a in axes else p for a, p in zip(AXES, target)]
            yield Event(i, "sync")
            return
        if any(p is None for p in start) or any(p is None for p in target):
            return
        s = [float(v) for v in start]  # type: ignore[arg-type]
        t = [float(v) for v in target]  # type: ignore[arg-type]
        if st.motion in ("0", "1"):
            pts = [t]
        else:
            try:
                pts = arc_points(s, t, w, st, st.motion == "2", self.arc_tolerance, self.arc_max_seg)
            except ValueError:
                pts = [t]
        yield Event(i, "move", points=pts, start=s, rapid=st.motion == "0", feed_mm=m.feed_mm)


def state_before(lines: list[str], n: int) -> dict[str, Any]:
    """Modal state GRBL has just before cleaned line ``n`` (0-based)."""
    sc = Scanner()
    for i in range(min(n, len(lines))):
        for _ in sc.feed_line(i, lines[i]):
            pass
    return sc.m.snapshot()


def bounds_of(events: list[Event]) -> dict[str, Any] | None:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for e in events:
        if e.kind != "move":
            continue
        for p in [e.start, *e.points]:
            for k in range(3):
                lo[k] = min(lo[k], p[k])  # type: ignore[index]
                hi[k] = max(hi[k], p[k])  # type: ignore[index]
    if not math.isfinite(lo[0]):
        return None
    return {"min": [round(v, 4) for v in lo], "max": [round(v, 4) for v in hi]}
