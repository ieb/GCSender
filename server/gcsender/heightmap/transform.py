"""Heightmap transform (docs/PLAN.md §5.6).

Every G0/G1 move whose start and end are known is subdivided so no piece is
longer than the interpolation spacing, and each end point gets ``h(x, y)``
added to Z. Arcs are linearised (chord error <= ``arc_tolerance``) and emitted
as G1 pieces. Every emitted motion line carries an explicit G0/G1 so modal
state cannot be confused, and coordinates are written in the program's own
units and distance mode.

Lines that cannot or must not be compensated are copied unchanged: G53,
G28/G30, probing, G10/G92, and moves whose start position is not yet known
(e.g. a first ``G0 Z5`` before any XY). Such motion lines get the original
motion word prepended if they relied on a modal one.

``line_map[k]`` gives the source line index of output line k (for progress
display in phase 4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import gcode as gc
from ..gcode import AXES, State, Words, code_of
from .model import HeightMap


@dataclass
class Stats:
    lines_in: int = 0
    lines_out: int = 0
    moves: int = 0
    segments: int = 0
    outside: int = 0  # emitted points outside the map border (clamped)
    uncompensated: int = 0  # motion lines copied unchanged because the position was unknown
    warnings: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings and len(self.warnings) < 50:
            self.warnings.append(msg)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _fmt(v: float, mm: bool) -> str:
    s = f"{v:.{3 if mm else 4}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class _Walker:
    """Walks a program, calling ``move`` for compensable linear pieces."""

    def __init__(self, arc_tolerance: float, max_seg: float):
        self.st = State()
        self.arc_tolerance = arc_tolerance
        self.max_seg = max_seg
        self.stats = Stats()

    def walk(
        self,
        lines: list[str],
        raw: Callable[[int, str, Words | None], None],
        move: Callable[[int, Words, str, list[list[float]]], None],
    ) -> None:
        st = self.st
        for k, line in enumerate(lines):
            self.stats.lines_in += 1
            code = code_of(line)
            if not code or code.startswith("%"):
                raw(k, line, None)
                continue
            w = Words.parse(code)
            gs = set(w.gs)
            # modal groups that affect interpretation of this line
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
            if gs & {"54", "55", "56", "57", "58", "59"}:
                self.stats.warn("the program selects a work coordinate system; the heightmap assumes the one it was probed in")
            explicit = w.motion()
            if explicit:
                st.motion = explicit
            axes = {a: st.to_mm(w.vals[a]) for a in AXES if a in w.vals}

            if "92" in gs:
                for i, a in enumerate(AXES):
                    if a in axes:
                        if st.pos[i] is None:
                            self.stats.warn("G92 at an unknown position; later moves may be uncompensated")
                            st.pos[i] = None
                        else:
                            st.shift[i] = st.pos[i] - axes[a]  # type: ignore[operator]
                raw(k, line, None)
                continue
            if "92.1" in gs:
                st.shift = [0.0, 0.0, 0.0]
                raw(k, line, None)
                continue
            if gs & {"10", "4", "28.1", "30.1"} or not axes:
                raw(k, line, None)
                continue
            if gs & {"28", "30"}:
                st.pos = [None, None, None]  # goes to a stored machine position
                raw(k, line, None)
                continue
            if "53" in gs:
                for i, a in enumerate(AXES):
                    if a in axes:
                        st.pos[i] = None
                raw(k, line, None)
                continue

            motion = st.motion
            target: list[float | None] = list(st.pos)
            for i, a in enumerate(AXES):
                if a in axes:
                    if st.absolute:
                        target[i] = axes[a] + st.shift[i]
                    elif st.pos[i] is not None:
                        target[i] = st.pos[i] + axes[a]  # type: ignore[operator]
                    else:
                        target[i] = None

            if motion in ("0", "1", "2", "3") and st.known() and all(t is not None for t in target):
                start = [float(p) for p in st.pos]  # type: ignore[arg-type]
                end = [float(t) for t in target]  # type: ignore[arg-type]
                if motion in ("0", "1"):
                    pts = self._subdivide(start, end)
                else:
                    try:
                        arc = gc.arc_points(start, end, w, st, motion == "2", self.arc_tolerance, self.max_seg)
                    except ValueError as e:
                        self.stats.warn(f"line {k + 1}: {e}; copied unchanged")
                        st.pos = target
                        raw(k, line, w)
                        continue
                    pts = []
                    prev = start
                    for p in arc:
                        pts += self._subdivide(prev, p)
                        prev = p
                self.stats.moves += 1
                move(k, w, motion, pts)
                st.pos = target
                continue

            if motion in ("0", "1") and all(t is not None for t in target):
                # Start unknown (e.g. the first XY move of a program) but the end
                # is known: compensate the end point without subdividing.
                end = [float(t) for t in target]  # type: ignore[arg-type]
                self.stats.moves += 1
                move(k, w, motion, [end])
                st.pos = target
                continue
            if motion in ("0", "1", "2", "3"):
                self.stats.uncompensated += 1
            st.pos = target
            raw(k, line, w)

    def _subdivide(self, a: list[float], b: list[float]) -> list[list[float]]:
        n = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / self.max_seg - 1e-9))
        return [[a[i] + (b[i] - a[i]) * k / n for i in range(3)] for k in range(1, n + 1)]


def transform(text: str, hm: HeightMap, arc_tolerance: float = 0.01) -> dict[str, Any]:
    """Return ``{"gcode", "line_map", "stats"}`` for ``text`` compensated by ``hm``."""
    if not hm.complete:
        raise ValueError("the heightmap has unprobed points")
    step = min(hm.params.step)
    walker = _Walker(arc_tolerance, step)
    st = walker.st
    stats = walker.stats
    out: list[str] = []
    line_map: list[int] = []
    actual: list[float | None] = [None, None, None]  # where the emitted program leaves the machine (map frame)

    p = hm.params
    header = [
        f"(GCSender heightmap transform: map {hm.digest()}, {p.nx}x{p.ny} points)",
        f"(border X{_fmt(p.x, True)} Y{_fmt(p.y, True)} W{_fmt(p.w, True)} H{_fmt(p.h, True)}, segment <= {_fmt(step, True)} mm)",
    ]

    def emit(k: int, s: str) -> None:
        out.append(s)
        line_map.append(k)

    def raw(k: int, line: str, w: Words | None) -> None:
        if w is not None and any(a in w.vals for a in AXES):
            # A copied motion line: make its motion mode explicit, and track
            # where it really leaves the machine for incremental output.
            if w.motion() is None and st.motion in gc.MOTION:
                line = f"G{st.motion}{line.lstrip()}"
            for i in range(3):
                actual[i] = st.pos[i]
        emit(k, line.rstrip("\r\n"))

    def move(k: int, w: Words, motion: str, pts: list[list[float]]) -> None:
        prefix = "".join(
            f"{letter}{text}"
            for letter, text in w.order
            if letter not in "XYZIJKR" and not (letter == "G" and gc.gnum(text) in ("0", "1", "2", "3"))
        )
        word = "G0" if motion == "0" else "G1"
        for n, pt in enumerate(pts):
            if not hm.inside(pt[0], pt[1]):
                stats.outside += 1
            z = pt[2] + hm.height(pt[0], pt[1])
            comp = [pt[0], pt[1], z]
            if st.absolute:
                vals = [comp[i] - st.shift[i] for i in range(3)]
            else:
                base = actual if all(a is not None for a in actual) else None
                if base is None:
                    # Cannot happen once the start is known, but stay safe.
                    base = comp
                vals = [comp[i] - base[i] for i in range(3)]  # type: ignore[index]
            if not st.mm:
                vals = [v / 25.4 for v in vals]
            texts = [_fmt(v, st.mm) for v in vals]
            coords = "".join(f"{a}{t}" for a, t in zip(AXES, texts))
            emit(k, (prefix if n == 0 else "") + word + coords)
            if st.absolute:
                for i in range(3):
                    actual[i] = comp[i]
            else:
                # Track where the *rounded* increments really leave the machine,
                # so each next increment corrects the rounding: no drift.
                for i in range(3):
                    d = float(texts[i]) * (1.0 if st.mm else 25.4)
                    actual[i] = (actual[i] if actual[i] is not None else comp[i] - d) + d
            stats.segments += 1

    lines = text.splitlines()
    walker.walk(lines, raw, move)
    if stats.outside:
        stats.warn(f"{stats.outside} points lie outside the map border; the edge heights were used")
    if stats.uncompensated:
        stats.warn(
            f"{stats.uncompensated} move(s) before the first known XY position (usually the initial safe-Z move) "
            "were not compensated"
        )
    header.append(f"(warnings: {len(stats.warnings)})")
    gcode = "\n".join(header + out) + "\n"
    stats.lines_out = len(out) + len(header)
    return {"gcode": gcode, "line_map": [-1] * len(header) + line_map, "stats": stats.to_json()}


def bounds(text: str) -> dict[str, Any] | None:
    """XY(Z) extent of all known move end points (mm, map frame); None if no moves."""
    walker = _Walker(0.01, 1e9)
    lo = [math.inf] * 3
    hi = [-math.inf] * 3

    def take(pts: list[list[float]]) -> None:
        for p in pts:
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])

    def raw(_k: int, _line: str, w: Words | None) -> None:
        if w is not None and walker.st.known():
            take([[float(v) for v in walker.st.pos]])  # type: ignore[arg-type]

    def move(_k: int, w: Words, motion: str, pts: list[list[float]]) -> None:
        if motion != "0":  # rapids do not define the cutting area
            take(pts)

    walker.walk(text.splitlines(), raw, move)
    if not math.isfinite(lo[0]):
        return None
    return {"min": lo, "max": hi}
