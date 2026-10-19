"""Backlash compensation (GRBL 0.9 has none; the server rewrites what it streams).

Model, per axis with backlash ``b``: GRBL's position is the motor's; the tool
lags by the slack on the side the axis last moved. A position is exact when the
axis is on the same side as when that axis's work zero was set (probing Z moves
down, so Z is zeroed on the "down" side). On the other side every target must
be moved ``b`` further in the direction of travel::

    offset(side) = 0           if side == zero side
                   side * b    otherwise

``BacklashTracker`` (owned by the controller) knows which side each axis is on,
from the machine position reports and the jog steps as they are sent, and
remembers the side at which each work offset was last changed (zeroing,
probing, G10, G92), per coordinate system, in ``<state>/backlash.json``.

``compensate()`` rewrites a program: every emitted coordinate gets the offset
for the axis's side, and where an axis reverses direction inside the program a
take-up move on just the reversing axes is inserted first (``G0`` for rapids,
otherwise ``G1`` at the current feed), so the following cut is not skewed by
the slack. Arcs are linearised so a reversal inside an arc is caught. The first
move of each axis is not preceded by a take-up line (its target is still
offset correctly); this keeps the line numbers independent of where the
machine happens to be, so "start from line" and recovery stay stable.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import gcode as gc
from .gcode import AXES, Words
from .heightmap.transform import _fmt, _Walker

log = logging.getLogger(__name__)

EPS = 1e-6  # mm: smaller axis motion does not count as moving
MPOS_EPS = 0.0009  # mm: status reports have 3 decimals
WCS_KEYS = ("G54", "G55", "G56", "G57", "G58", "G59", "G92")


def _sgn(d: float, eps: float = EPS) -> int:
    return 1 if d > eps else -1 if d < -eps else 0


class BacklashTracker:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self.side: list[int | None] = [None, None, None]
        self._last_mpos: list[float] | None = None
        # key -> axis -> {"side": ±1 | None, "t": time}
        self.zero: dict[str, list[dict[str, Any] | None]] = {}
        # Zeroing commands just sent: the side *then* (the offsets are read back
        # later, e.g. after the probe has retracted): key -> axis -> (side, time)
        self._pending: dict[str, dict[int, tuple[int | None, float]]] = {}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.zero = {k: v for k, v in (data.get("zero") or {}).items() if k in WCS_KEYS}
            except (OSError, ValueError) as e:
                log.warning("cannot read %s: %s", path, e)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "zero": self.zero}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save %s: %s", self.path, e)

    # -- which side each axis is on ------------------------------------------------

    def observe_mpos(self, mpos: list[float] | None) -> None:
        if not mpos or len(mpos) < 3:
            return
        with self._lock:
            if self._last_mpos is not None:
                for i in range(3):
                    s = _sgn(mpos[i] - self._last_mpos[i], MPOS_EPS)
                    if s:
                        self.side[i] = s
            self._last_mpos = list(mpos[:3])

    def note_move(self, delta: list[float]) -> None:
        """A relative move about to be sent (a jog step): its direction is known now."""
        with self._lock:
            for i in range(3):
                s = _sgn(delta[i]) if i < len(delta) else 0
                if s:
                    self.side[i] = s

    def forget_sides(self) -> None:
        """After a reset/alarm or homing the side is unknown until the next move."""
        with self._lock:
            self.side = [None, None, None]
            self._last_mpos = None

    # -- the side at which each zero was set ------------------------------------------

    def note_zero_command(self, line: str, active_wcs: str | None) -> None:
        """Called as G10 L2/L20 or G92 is sent: remember each named axis's side now."""
        up = re.sub(r"\s+", "", line.upper())
        keys: list[str] = []
        if re.search(r"G92(?![.\d])", up):
            keys.append("G92")
        if re.search(r"G10(?![.\d])", up) and re.search(r"L20?(?!\d)", up):
            m = re.search(r"P(\d)", up)
            p = int(m.group(1)) if m else 0
            keys.append(f"G5{3 + p}" if 1 <= p <= 6 else (active_wcs or "G54"))
        axes = [i for i, a in enumerate(AXES) if re.search(a + r"[-+.\d]", up)]
        if not keys or not axes:
            return
        now = time.time()
        with self._lock:
            for k in keys:
                slot = self._pending.setdefault(k, {})
                for i in axes:
                    slot[i] = (self.side[i], now)

    def on_offsets(self, key: str, old: Any, new: Any) -> None:
        """A work offset was read: if an axis value changed, that axis was zeroed now."""
        if key not in WCS_KEYS or not isinstance(new, list):
            return
        with self._lock:
            entry = self.zero.setdefault(key, [None, None, None])
            changed = False
            for i in range(min(3, len(new))):
                if isinstance(old, list) and i < len(old) and abs(float(old[i]) - float(new[i])) < 1e-6:
                    continue
                if old is None and entry[i] is not None:
                    continue  # first read after start-up: keep what was saved
                side = self.side[i]
                pend = self._pending.get(key, {}).pop(i, None)
                if pend is not None and time.time() - pend[1] < 60:
                    side = pend[0]  # the side when the zeroing command was sent
                entry[i] = {"side": side, "t": time.time()}
                changed = True
            if changed:
                self._save()

    def zero_sides(self, wcs: str, g92: list[float] | None) -> list[int | None]:
        """Side at which each axis's current zero was set (the later of the WCS and G92)."""
        with self._lock:
            out: list[int | None] = []
            for i in range(3):
                best = (self.zero.get(wcs) or [None] * 3)[i]
                if g92 and i < len(g92) and abs(g92[i]) > 1e-9:
                    g = (self.zero.get("G92") or [None] * 3)[i]
                    if g is not None and (best is None or g.get("t", 0) > best.get("t", 0)):
                        best = g
                out.append(best.get("side") if best else None)
            return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"side": list(self.side), "zero": json.loads(json.dumps(self.zero))}


def compensate(
    text: str,
    backlash: list[float],
    start_side: list[int | None],
    zero_side: list[int | None],
    arc_tolerance: float = 0.01,
) -> dict[str, Any]:
    """Return ``{"gcode", "sides", "stats"}``; ``sides[k]`` is the side of each
    axis assumed after output line ``k`` (``None`` = not moved yet)."""
    b = [max(0.0, float(v)) for v in backlash]
    zs: list[int | None] = list(zero_side)
    for i in range(3):
        if zs[i] is None:
            zs[i] = start_side[i]  # unknown zero side: take where the machine is now as exact
    side: list[int | None] = list(start_side)  # physical side, for the offsets
    prog: list[int | None] = [None, None, None]  # side set by this program, for take-up lines
    walker = _Walker(arc_tolerance, math.inf)
    st = walker.st
    out: list[str] = []
    sides: list[list[int | None]] = []
    takeups = [0, 0, 0]
    stats: dict[str, Any] = {"takeups": takeups, "takeupLines": 0, "warnings": []}
    actual: list[float | None] = [None, None, None]  # emitted (offset) position, for G91 output

    def warn(msg: str) -> None:
        if msg not in stats["warnings"] and len(stats["warnings"]) < 20:
            stats["warnings"].append(msg)

    def off(i: int) -> float:
        s = side[i]
        if b[i] <= 0 or s is None:
            return 0.0
        if zs[i] is None:
            zs[i] = s  # first move of an axis with no reference: exact from here
        return 0.0 if s == zs[i] else s * b[i]

    def emit(line: str) -> None:
        out.append(line)
        sides.append(list(prog))

    def write(prefix: str, word: str, pt: list[float]) -> None:
        comp = [pt[i] + off(i) for i in range(3)]
        if st.absolute:
            vals = [comp[i] - st.shift[i] for i in range(3)]
        else:
            base = actual if all(a is not None for a in actual) else comp
            vals = [comp[i] - base[i] for i in range(3)]  # type: ignore[index]
        if not st.mm:
            vals = [v / 25.4 for v in vals]
        texts = [_fmt(v, st.mm) for v in vals]
        emit(prefix + word + "".join(f"{a}{t}" for a, t in zip(AXES, texts)))
        for i in range(3):
            if st.absolute:
                actual[i] = comp[i]
            else:
                d = float(texts[i]) * (1.0 if st.mm else 25.4)
                actual[i] = (actual[i] if actual[i] is not None else comp[i] - d) + d

    def raw(_k: int, line: str, w: Words | None) -> None:
        if w is not None and any(a in w.vals for a in AXES):
            if w.motion() is None and st.motion in gc.MOTION:
                line = f"G{st.motion}{line.lstrip()}"
            gs = set(w.gs)
            if gs & {"92"}:
                warn("the program sets G92: positions after it may be off by up to the backlash")
            moved = [i for i, a in enumerate(AXES) if a in w.vals]
            for i in moved:  # copied unchanged (e.g. G53, G28, unknown start): side unknown now
                prog[i] = None
            for i in range(3):
                actual[i] = st.pos[i]
        emit(line.rstrip("\r\n"))

    def move(_k: int, w: Words, motion: str, pts: list[list[float]]) -> None:
        prefix = "".join(
            f"{letter}{text}"
            for letter, text in w.order
            if letter not in "XYZIJKR" and not (letter == "G" and gc.gnum(text) in ("0", "1", "2", "3"))
        )
        word = "G0" if motion == "0" else "G1"
        start = list(st.pos)
        for pt in pts:
            if any(p is None for p in start):
                # Start unknown (first move of the program): no take-up, just the offset target.
                for i in range(3):
                    if start[i] is not None:
                        s = _sgn(pt[i] - start[i])  # type: ignore[operator]
                        if s:
                            side[i] = prog[i] = s
                write(prefix, word, pt)
                prefix = ""
                start = list(pt)
                continue
            dirs = [_sgn(pt[i] - start[i]) for i in range(3)]  # type: ignore[operator]
            reversing = [i for i in range(3) if dirs[i] and b[i] > 0 and prog[i] is not None and dirs[i] != prog[i]]
            if reversing:
                for i in reversing:
                    side[i] = dirs[i]
                    takeups[i] += 1
                stats["takeupLines"] += 1
                write(prefix, word, [float(p) for p in start])  # type: ignore[arg-type]
                prefix = ""
            for i in range(3):
                if dirs[i]:
                    side[i] = prog[i] = dirs[i]
            write(prefix, word, pt)
            prefix = ""
            start = list(pt)

    header = f"(GCSender backlash compensation: X{_fmt(b[0], True)} Y{_fmt(b[1], True)} Z{_fmt(b[2], True)} mm)"
    emit(header)
    walker.walk(text.splitlines(), raw, move)
    stats["linesOut"] = len(out)
    return {"gcode": "\n".join(out) + "\n", "sides": sides, "stats": stats}
