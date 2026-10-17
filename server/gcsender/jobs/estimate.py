"""Job time estimate (docs/PLAN.md §5.3).

A model of GRBL's planner run over the whole program:

* per block: nominal speed (feed, or the rapid rate), capped by every
  moving axis's ``$110-$112``; acceleration limited by ``$120-$122``;
* junction speeds from GRBL's junction-deviation formula (``$11``);
* a backward and forward pass so every block can stop within the remaining
  queue, then trapezoidal (or triangular) profiles per block;
* planner stops at dwells and at spindle/coolant/program-flow commands.

It uses full lookahead where GRBL has 16-17 blocks, so it is slightly
optimistic on long runs of tiny segments; the runner corrects the remaining
time with the measured ratio as the job runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .analyze import Scanner

MIN_SPEED = 0.0  # mm/s at a hard stop


@dataclass
class Machine:
    rates: tuple[float, float, float] = (500.0, 500.0, 500.0)  # mm/min ($110-$112)
    accels: tuple[float, float, float] = (10.0, 10.0, 10.0)  # mm/s^2 ($120-$122)
    junction_deviation: float = 0.01  # mm ($11)
    arc_tolerance: float = 0.002  # mm ($12)

    @classmethod
    def from_settings(cls, get: Callable[[int], float | None]) -> "Machine":
        def g(i: int, d: float) -> float:
            v = get(i)
            return float(v) if isinstance(v, (int, float)) and v > 0 else d

        return cls(
            rates=(g(110, 500), g(111, 500), g(112, 500)),
            accels=(g(120, 10), g(121, 10), g(122, 10)),
            junction_deviation=g(11, 0.01),
            arc_tolerance=g(12, 0.002),
        )


@dataclass
class _Block:
    line: int
    length: float
    unit: tuple[float, float, float]
    v_nom: float  # mm/s
    accel: float  # mm/s^2
    v_entry_max: float = 0.0
    v_entry: float = 0.0
    stop_after: bool = False


def _block(m: Machine, line: int, a: list[float], b: list[float], rapid: bool, feed_mm: float) -> _Block | None:
    d = [b[i] - a[i] for i in range(3)]
    length = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
    if length < 1e-9:
        return None
    u = (d[0] / length, d[1] / length, d[2] / length)
    v = math.inf if rapid else max(feed_mm, 1e-3) / 60.0
    acc = math.inf
    for i in range(3):
        if abs(u[i]) > 1e-12:
            v = min(v, m.rates[i] / 60.0 / abs(u[i]))
            acc = min(acc, m.accels[i] / abs(u[i]))
    return _Block(line, length, u, v, acc)


def _block_time(L: float, v0: float, v1: float, vmax: float, a: float) -> float:
    # Distance needed to reach vmax from v0 and to brake to v1.
    d_acc = max(0.0, (vmax * vmax - v0 * v0) / (2 * a))
    d_dec = max(0.0, (vmax * vmax - v1 * v1) / (2 * a))
    if d_acc + d_dec <= L:
        return (vmax - v0) / a + (L - d_acc - d_dec) / vmax + (vmax - v1) / a
    # Triangle: peak speed vp with (vp^2 - v0^2) + (vp^2 - v1^2) = 2 a L
    vp = math.sqrt(max(0.0, (2 * a * L + v0 * v0 + v1 * v1) / 2))
    return max(0.0, (vp - v0) / a) + max(0.0, (vp - v1) / a)


def estimate(lines: list[str], machine: Machine) -> list[float]:
    """Seconds attributed to each line (same length as ``lines``)."""
    per_line = [0.0] * len(lines)
    sc = Scanner(arc_tolerance=max(machine.arc_tolerance, 0.002))
    blocks: list[_Block] = []

    def flush() -> None:
        if not blocks:
            return
        _plan(blocks, machine)
        for k, b in enumerate(blocks):
            v1 = blocks[k + 1].v_entry if k + 1 < len(blocks) else 0.0
            per_line[b.line] += _block_time(b.length, b.v_entry, v1, b.v_nom, b.accel)
        blocks.clear()

    for i, code in enumerate(lines):
        for ev in sc.feed_line(i, code):
            if ev.kind == "move":
                prev = ev.start
                for p in ev.points:
                    blk = _block(machine, i, prev, p, ev.rapid, ev.feed_mm)  # type: ignore[arg-type]
                    if blk is not None:
                        blocks.append(blk)
                    prev = p
            else:
                flush()
                if ev.kind == "dwell":
                    per_line[i] += ev.seconds
    flush()
    return per_line


def _plan(blocks: list[_Block], m: Machine) -> None:
    # Junction limits (GRBL 0.9/1.1 junction deviation).
    prev: _Block | None = None
    for b in blocks:
        if prev is None:
            b.v_entry_max = MIN_SPEED
        else:
            cos_theta = -(prev.unit[0] * b.unit[0] + prev.unit[1] * b.unit[1] + prev.unit[2] * b.unit[2])
            if cos_theta > 0.999999:
                vj = MIN_SPEED  # reversal
            elif cos_theta < -0.999999:
                vj = math.inf  # straight on
            else:
                sin_half = math.sqrt(0.5 * (1.0 - cos_theta))
                a = min(prev.accel, b.accel)
                vj = math.sqrt(a * m.junction_deviation * sin_half / (1.0 - sin_half))
            b.v_entry_max = min(vj, b.v_nom, prev.v_nom)
        prev = b
    # Backward pass: be able to stop at the end of the queue.
    n = len(blocks)
    next_entry = 0.0
    for k in range(n - 1, -1, -1):
        b = blocks[k]
        b.v_entry = min(b.v_entry_max, math.sqrt(next_entry * next_entry + 2 * b.accel * b.length))
        next_entry = b.v_entry
    # Forward pass: acceleration limits.
    for k in range(n - 1):
        b = blocks[k]
        reachable = math.sqrt(b.v_entry * b.v_entry + 2 * b.accel * b.length)
        nxt = blocks[k + 1]
        if nxt.v_entry > reachable:
            nxt.v_entry = reachable
