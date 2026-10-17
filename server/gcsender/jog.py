"""Step jogging (docs/PLAN.md §5.4).

Jogging is deliberately *step only*: every click or key press moves exactly
the selected distance and nothing else. There is no hold-to-move mode, so
there is nothing that can overrun into the workpiece.

- 0.9: ``G91 G21 G1 X.. F..`` followed by one line that restores the previous
  units / distance mode / motion mode / feed.
- 1.1: ``$J=G91 G21 X.. F..`` (no modal side effects).

Deliberate presses may queue: a step sent while earlier steps are still
moving is appended and runs after them. Holding a key or the mouse button is
always exactly one step; that is enforced in the SPA, which ignores keyboard
auto-repeat. Steps are only accepted from Idle or behind other queued steps,
never behind a program or console motion.
"""

from __future__ import annotations

import math
import threading
from typing import Any

from .errors import ControllerError


def _f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def axis_words(delta: list[float]) -> str:
    return "".join(f"{a}{_f(d)}" for a, d in zip("XYZ", delta) if abs(round(d, 3)) >= 0.001)


class Jogger:
    def __init__(self, controller: Any):
        self.ctl = controller
        controller.jogger = self
        self._lock = threading.Lock()
        self._pending_seq: int | None = None  # status sequence when the last step was accepted
        self._target: list[float] | None = None  # machine position after all queued steps

    # ------------------------------------------------------------ helpers

    @property
    def family(self) -> str:
        return (self.ctl.caps or {}).get("family", "0.9")

    def _setting(self, sid: int, default: float) -> float:
        v = self.ctl.model.setting(sid)
        return float(v) if isinstance(v, (int, float)) and v > 0 else default

    def max_feed(self, unit: list[float], feed: float) -> float:
        """Requested feed limited so no axis exceeds its $11x max rate."""
        f = feed
        for i, u in enumerate(unit):
            if abs(u) > 1e-9:
                f = min(f, self._setting(110 + i, 500.0) / abs(u))
        return f

    @staticmethod
    def clamp(start: list[float], unit: list[float], dist: float, limits: list[float] | None) -> float:
        """Largest t <= dist keeping start + unit*t inside the limits box."""
        if limits is None:
            return dist
        t = dist
        for i in range(3):
            lo, hi = limits[2 * i], limits[2 * i + 1]
            u = unit[i]
            if u > 1e-12:
                t = min(t, (hi - start[i]) / u)
            elif u < -1e-12:
                t = min(t, (lo - start[i]) / u)
        return max(0.0, t)

    def restore_line(self, modal: dict[str, Any] | None) -> str:
        """Line that re-establishes units/distance/motion/feed after a 0.9 step."""
        if not modal:
            return "G90"
        units = modal.get("units", "G21")
        words = [units, modal.get("distance", "G90")]
        if modal.get("motion") in ("G0", "G1"):
            words.append(modal["motion"])
        feed = modal.get("feed")
        if feed is not None:
            words.append("F" + _f(feed / 25.4 if units == "G20" else feed))
        return "".join(words)

    def moving(self) -> bool:
        """True while the last accepted step may still be in motion."""
        with self._lock:
            pending = self._pending_seq
        if pending is None:
            return False
        st = self.ctl.model.status or {}
        if self.ctl.status_seq > pending and st.get("state") == "Idle":
            with self._lock:
                self._pending_seq = None
                self._target = None
            return False
        return True

    def reset(self) -> None:
        with self._lock:
            self._pending_seq = None
            self._target = None

    # ---------------------------------------------------------------- step

    def step(self, delta: list[float], feed: float) -> dict[str, Any]:
        ctl = self.ctl
        if ctl.connection.get("state") != "connected":
            raise ControllerError("not connected")
        if ctl.busy:
            raise ControllerError(f"busy: {ctl.busy}")
        queued = self.moving()
        state = ctl.machine_state
        if not (state == "Idle" or (queued and state in ("Run", "Jog", "Idle"))):
            raise ControllerError(f"cannot jog while {state}")
        length = math.sqrt(sum(d * d for d in delta))
        if length < 0.001:
            raise ControllerError("zero-length jog", 400)
        if feed <= 0:
            raise ControllerError("feed must be positive", 400)
        unit = [d / length for d in delta]
        st = ctl.model.status or {}
        with self._lock:
            start = list(self._target) if queued and self._target else (list(st["mpos"]) if st.get("mpos") else None)
        if ctl.settings.get("jogLimitsEnabled"):
            if start is None:
                raise ControllerError("position unknown")
            # Checked against where the machine will be after the queued steps.
            allowed = self.clamp(start, unit, length, list(ctl.settings.get("jogLimits")))
            if allowed < length - 1e-6:
                # Never move a different distance than the one asked for.
                raise ControllerError(f"step would leave the jog box (room: {allowed:.3f} mm)")
        words = axis_words(delta)
        if not words:
            raise ControllerError("zero-length jog", 400)
        f = self.max_feed(unit, feed)
        ctl.boost_polling(2 + length / max(f / 60, 0.1))
        if self.family == "1.1":
            r = ctl.command(f"$J=G91G21{words}F{_f(f)}", source="jog", follow_up=False)
        else:
            modal = ctl.sync_modal()
            r = ctl.command(f"G91G21G1{words}F{_f(f)}", source="jog", follow_up=False)
            if r["status"] == "ok":
                ctl.command(self.restore_line(modal), source="jog", follow_up=False)
        if r["status"] != "ok":
            raise ControllerError(f"jog rejected: {r['response']}")
        with self._lock:
            self._pending_seq = ctl.status_seq
            if start is not None:
                self._target = [p + float(_f(d)) for p, d in zip(start, delta)]
        return {"distance": round(length, 3), "feed": f}
