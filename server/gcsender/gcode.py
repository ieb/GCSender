"""Minimal RS274 reader shared by the heightmap transform and bounds.

It tracks what is needed to know where each move goes: units, distance
mode, plane, motion mode, the program's own G92 shift and the current
position. Positions are kept in millimetres in the *active work coordinate
frame without any program G92*, which is the frame the heightmap was probed
in.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
COMMENT_RE = re.compile(r"\([^)]*\)")
MOTION = {"0", "1", "2", "3", "38.2", "38.3", "38.4", "38.5", "80"}
AXES = "XYZ"
PLANES = {"17": (0, 1, 2), "18": (2, 0, 1), "19": (1, 2, 0)}  # (axis_0, axis_1, linear axis) as in GRBL


def code_of(line: str) -> str:
    """Line without comments, whitespace-free and upper-case."""
    s = COMMENT_RE.sub("", line)
    s = s.split(";", 1)[0]
    return s.replace(" ", "").replace("\t", "").upper()


def gnum(v: str) -> str:
    """Normalise a G number: '01' -> '1', '1.0' -> '1', '92.1' stays."""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


@dataclass
class Words:
    gs: list[str] = field(default_factory=list)
    vals: dict[str, float] = field(default_factory=dict)
    order: list[tuple[str, str]] = field(default_factory=list)  # original (letter, text) pairs

    @classmethod
    def parse(cls, code: str) -> "Words":
        w = cls()
        for letter, text in WORD_RE.findall(code):
            letter = letter.upper()
            w.order.append((letter, text))
            if letter == "G":
                w.gs.append(gnum(text))
            else:
                w.vals[letter] = float(text)
        return w

    def motion(self) -> str | None:
        return next((g for g in self.gs if g in MOTION), None)


@dataclass
class State:
    mm: bool = True
    absolute: bool = True
    plane: str = "17"
    motion: str = "0"
    pos: list[float | None] = field(default_factory=lambda: [None, None, None])  # map frame, mm
    shift: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # program G92 (map = prog + shift)

    def to_mm(self, v: float) -> float:
        return v if self.mm else v * 25.4

    def known(self) -> bool:
        return all(p is not None for p in self.pos)


def arc_points(
    start: list[float],
    target: list[float],
    words: Words,
    st: State,
    clockwise: bool,
    tolerance: float,
    max_seg: float,
) -> list[list[float]]:
    """Linearise a G2/G3 arc (GRBL geometry) into points after ``start``, ending at ``target``."""
    a0, a1, lin = PLANES[st.plane]
    ijk = {0: "I", 1: "J", 2: "K"}
    if "R" in words.vals:
        r = st.to_mm(words.vals["R"])
        x = target[a0] - start[a0]
        y = target[a1] - start[a1]
        h2 = 4.0 * r * r - x * x - y * y
        if h2 < 0:
            raise ValueError("arc radius too small for the end point")
        h = -math.sqrt(h2) / math.hypot(x, y) if (x or y) else 0.0
        if not clockwise:
            h = -h
        if r < 0:
            h = -h
            r = -r
        off0 = 0.5 * (x - y * h)
        off1 = 0.5 * (y + x * h)
    else:
        off0 = st.to_mm(words.vals.get(ijk[a0], 0.0))
        off1 = st.to_mm(words.vals.get(ijk[a1], 0.0))
    c0 = start[a0] + off0
    c1 = start[a1] + off1
    r0, r1 = -off0, -off1  # centre -> start
    rt0, rt1 = target[a0] - c0, target[a1] - c1
    radius = math.hypot(r0, r1)
    sweep = math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1)
    if clockwise:
        if sweep >= -1e-9:
            sweep -= 2 * math.pi
    elif sweep <= 1e-9:
        sweep += 2 * math.pi
    if radius < 1e-9:
        return [list(target)]
    theta = 2 * math.acos(max(-1.0, 1 - tolerance / radius)) if tolerance < radius else math.pi / 2
    n = max(1, math.ceil(abs(sweep) / theta), math.ceil(abs(sweep) * radius / max_seg))
    a_start = math.atan2(r1, r0)
    pts = []
    for k in range(1, n):
        a = a_start + sweep * k / n
        p = [0.0, 0.0, 0.0]
        p[a0] = c0 + radius * math.cos(a)
        p[a1] = c1 + radius * math.sin(a)
        p[lin] = start[lin] + (target[lin] - start[lin]) * k / n
        pts.append(p)
    pts.append(list(target))
    return pts
