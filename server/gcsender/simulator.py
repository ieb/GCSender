"""GRBL 0.9j / 1.1f serial-protocol simulator.

Good enough to develop and test every sender feature without a machine:

* 128-byte serial RX buffer (127 usable) with real overflow behaviour,
  80-character line limit, planner depth 18 (0.9j) / 16 (1.1f) blocks.
  Lines are only parsed when the planner has room, so character-counting
  streaming sees realistic back-pressure.
* Status reports in both formats (``$10`` aware), ``$$`` (0.9 with
  descriptions), ``$#``, ``$G``, ``$I``, ``$N``, ``$C``, ``$X``, ``$H``,
  ``$RST=``, ``$SLP`` (1.1), ``$J=`` jogging + ``0x85`` cancel (1.1),
  real-time overrides (1.1), text (0.9) vs numeric (1.1) errors and alarms.
* Motion with constant velocity per block (no acceleration), arcs split into
  planner segments, dwell, probing (G38.2-.5) against a configurable surface,
  feed hold with a short deceleration, and the reset semantics that matter:
  reset while moving -> ``Abort during cycle`` alarm; reset after a completed
  hold -> no alarm, position kept, G92/modal state cleared.
* The banner is emitted on reset and (optionally) when the port is "opened",
  emulating the DTR auto-reset of Arduino-style boards.

It is *not* a cycle-accurate model of GRBL's planner (no acceleration
profile, no junction deviation) and only approximates rarely-used edge cases.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

RX_BUFFER_SIZE = 128  # one slot always free in GRBL's ring buffer -> 127 usable
LINE_BUFFER_SIZE = 80
HOLD_DECEL_S = 0.15  # simulated deceleration time for feed hold / jog cancel

ERROR_TEXT_09 = {
    1: "Expected command letter",
    2: "Bad number format",
    3: "Invalid statement",
    4: "Value < 0",
    5: "Setting disabled",
    6: "Value < 3 usec",
    7: "EEPROM read fail. Using defaults",
    8: "Not idle",
    9: "Alarm lock",
    10: "Homing not enabled",
    11: "Line overflow",
    20: "Unsupported command",
    21: "Modal group violation",
    22: "Undefined feed rate",
}
ALARM_TEXT_09 = {1: "Hard limit", 2: "Soft limit", 3: "Abort during cycle", 4: "Probe fail", 5: "Homing fail"}
# Alarm codes by meaning -> (0.9 code, 1.1 code)
ALARM_ABORT = (3, 3)
ALARM_PROBE_FAIL = (4, 5)
ALARM_HOMING_FAIL = (5, 9)

SETTINGS_DESC_09 = {
    0: "step pulse, usec", 1: "step idle delay, msec", 2: "step port invert mask", 3: "dir port invert mask",
    4: "step enable invert, bool", 5: "limit pins invert, bool", 6: "probe pin invert, bool",
    10: "status report mask", 11: "junction deviation, mm", 12: "arc tolerance, mm", 13: "report inches, bool",
    20: "soft limits, bool", 21: "hard limits, bool", 22: "homing cycle, bool", 23: "homing dir invert mask",
    24: "homing feed, mm/min", 25: "homing seek, mm/min", 26: "homing debounce, msec", 27: "homing pull-off, mm",
    100: "x, step/mm", 101: "y, step/mm", 102: "z, step/mm",
    110: "x max rate, mm/min", 111: "y max rate, mm/min", 112: "z max rate, mm/min",
    120: "x accel, mm/sec^2", 121: "y accel, mm/sec^2", 122: "z accel, mm/sec^2",
    130: "x max travel, mm", 131: "y max travel, mm", 132: "z max travel, mm",
}
INT_SETTINGS = {0, 1, 2, 3, 4, 5, 6, 10, 13, 20, 21, 22, 23, 26, 32}
MASK_SETTINGS = {2, 3, 10, 23}


def default_settings(firmware: str) -> dict[int, float]:
    """Defaults copied from the user's C3018 (GRBL 0.9j.20180122, docs/responses.txt)."""
    s: dict[int, float] = {
        0: 10, 1: 25, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0,
        10: 3 if firmware.startswith("0.9") else 1,
        11: 0.010, 12: 0.002, 13: 0, 20: 0, 21: 0, 22: 0, 23: 0,
        24: 25.0, 25: 500.0, 26: 250, 27: 1.0,
        100: 400.0, 101: 400.0, 102: 400.0,
        110: 1500.0, 111: 1500.0, 112: 1500.0,
        120: 10.0, 121: 10.0, 122: 10.0,
        130: 200.0, 131: 200.0, 132: 200.0,
    }
    if firmware.startswith("1.1"):
        s.update({30: 1000.0, 31: 0.0, 32: 0})
    return s


@dataclass
class SurfaceParams:
    """Virtual workpiece surface probed by the simulator (machine coordinates, mm).

    z(x, y) = base + tiltX*x + tiltY*y
              + ampX*sin(2*pi*x/periodX) + ampY*cos(2*pi*y/periodY)
              + bump * exp(-((x-bumpX)^2 + (y-bumpY)^2) / (2*bumpRadius^2))
    Tilts are in mm per mm (0.005 = 0.5 mm over 100 mm).
    """

    base: float = -30.0
    tiltX: float = 0.0008
    tiltY: float = 0.0
    ampX: float = 0.15
    periodX: float = 50 * math.pi  # ~157 mm
    ampY: float = 0.10
    periodY: float = 36 * math.pi  # ~113 mm
    bump: float = 0.0
    bumpX: float = 20.0
    bumpY: float = 15.0
    bumpRadius: float = 8.0

    LIMITS = {
        "base": (-500.0, 500.0), "tiltX": (-0.2, 0.2), "tiltY": (-0.2, 0.2),
        "ampX": (-10.0, 10.0), "periodX": (1.0, 10000.0), "ampY": (-10.0, 10.0), "periodY": (1.0, 10000.0),
        "bump": (-10.0, 10.0), "bumpX": (-1000.0, 1000.0), "bumpY": (-1000.0, 1000.0), "bumpRadius": (0.5, 1000.0),
    }

    def __call__(self, x: float, y: float) -> float:
        z = self.base + self.tiltX * x + self.tiltY * y
        z += self.ampX * math.sin(2 * math.pi * x / self.periodX) + self.ampY * math.cos(2 * math.pi * y / self.periodY)
        if self.bump:
            d2 = (x - self.bumpX) ** 2 + (y - self.bumpY) ** 2
            z += self.bump * math.exp(-d2 / (2 * self.bumpRadius ** 2))
        return z

    def to_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.LIMITS}

    def updated(self, changes: dict[str, Any]) -> "SurfaceParams":
        """New params with ``changes`` applied (a ``preset`` key is applied first)."""
        base = dict(self.to_dict())
        preset = changes.get("preset")
        if preset is not None:
            if preset not in SURFACE_PRESETS:
                raise ValueError(f"unknown surface preset {preset!r}; choose from {', '.join(SURFACE_PRESETS)}")
            base = {**SurfaceParams().to_dict(), **SURFACE_PRESETS[preset]}
        for k, v in changes.items():
            if k == "preset":
                continue
            if k not in self.LIMITS:
                raise ValueError(f"unknown surface parameter {k!r}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{k} must be a number")
            lo, hi = self.LIMITS[k]
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
            base[k] = float(v)
        return SurfaceParams(**base)


# Values not listed fall back to SurfaceParams defaults ("gentle" = the defaults).
SURFACE_PRESETS: dict[str, dict[str, float]] = {
    "gentle": {},
    "flat": {"tiltX": 0.0, "tiltY": 0.0, "ampX": 0.0, "ampY": 0.0},
    "tilted": {"tiltX": 0.005, "tiltY": -0.003, "ampX": 0.0, "ampY": 0.0},  # 0.5 / -0.3 mm per 100 mm
    "bent": {"tiltX": 0.0, "ampX": 0.6, "periodX": 120.0, "ampY": 0.4, "periodY": 90.0},
    "bump": {"tiltX": 0.0, "ampX": 0.0, "ampY": 0.0, "bump": 0.5, "bumpX": 20.0, "bumpY": 15.0, "bumpRadius": 8.0},
}


def parse_surface_spec(spec: str) -> SurfaceParams:
    """CLI form: ``preset=bent,base=-25,tiltX=0.002`` (any subset)."""
    changes: dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got {part!r}")
        key = key.strip()
        if key == "preset":
            changes[key] = value.strip()
        else:
            try:
                changes[key] = float(value)
            except ValueError:
                raise ValueError(f"{key} must be a number") from None
    return SurfaceParams().updated(changes)


def default_surface(x: float, y: float) -> float:
    """Machine-coordinate Z of the default simulated workpiece surface (gentle warp)."""
    return _DEFAULT_SURFACE(x, y)


_DEFAULT_SURFACE = SurfaceParams()


def _fmt(v: float, decimals: int = 3) -> str:
    s = f"{v:.{decimals}f}"
    return "0.000"[: decimals + 2] if s.startswith("-") and float(s) == 0 else s


class SimError(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


@dataclass
class Block:
    kind: str  # line | dwell | probe | home | jog
    start: list[float]
    end: list[float]
    duration: float  # seconds at 100 % override
    rate: float = 0.0  # mm/min nominal (for FS:)
    rapid: bool = False
    elapsed: float = 0.0
    probe_mode: str = ""  # "38.2" ...
    done: bool = False
    on_done: Callable[["Block"], None] | None = None
    line: str = ""

    def position(self) -> list[float]:
        if self.duration <= 0:
            return list(self.end)
        t = min(1.0, self.elapsed / self.duration)
        return [a + (b - a) * t for a, b in zip(self.start, self.end)]


@dataclass
class Modal:
    motion: str = "0"  # 0 1 2 3 38.2 38.3 38.4 38.5 80
    wcs: int = 0
    plane: int = 17
    units: int = 21
    distance: int = 90
    feedmode: int = 94
    program: str = "M0"
    spindle: str = "M5"
    coolant: str = "M9"
    tool: int = 0
    feed: float = 0.0
    speed: float = 0.0
    tlo: float = 0.0


@dataclass
class Persist:
    """EEPROM-backed data (survives reset)."""

    settings: dict[int, float]
    coords: list[list[float]] = field(default_factory=lambda: [[0.0, 0.0, 0.0] for _ in range(6)])
    g28: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    g30: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    startup: list[str] = field(default_factory=lambda: ["", ""])
    build_info: str = ""


WORD_RE = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")


class GrblSimulator:
    """Thread-safe simulator. Bytes in via :meth:`feed`; lines out via ``on_output``."""

    def __init__(
        self,
        firmware: str = "0.9j",
        *,
        time_scale: float = 1.0,
        surface: Callable[[float, float], float] | None = None,
        tick: float = 0.005,
        boot_delay: float = 0.0,
        threaded: bool = True,
    ):
        if firmware not in ("0.9j", "1.1f"):
            raise ValueError("firmware must be '0.9j' or '1.1f'")
        self.firmware = firmware
        self.v11 = firmware == "1.1f"
        self.planner_size = 15 if self.v11 else 17  # usable blocks
        self.time_scale = time_scale
        self.surface = surface if surface is not None else SurfaceParams()
        self.boot_delay = boot_delay
        self.on_output: Callable[[str], None] = lambda line: None
        self.rx_overflows = 0

        self._lock = threading.RLock()
        self._tick = tick
        self.persist = Persist(default_settings(firmware))
        self.pos = [0.0, 0.0, 0.0]  # machine position (mm)
        self._boot_pending: float | None = None
        self._init_runtime()
        self.state = "Alarm" if self.persist.settings[22] else "Idle"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if threaded:  # tests drive time manually via step()
            self._thread = threading.Thread(target=self._run, name="grbl-sim", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ setup

    def _init_runtime(self) -> None:
        self.modal = Modal()
        self.g92 = [0.0, 0.0, 0.0]
        self.prb = [0.0, 0.0, 0.0]
        self.prb_ok = 0
        self.parser_pos = list(self.pos)
        self.rx = bytearray()
        self.planner: deque[Block] = deque()
        self._pending: deque[Block] = deque()  # blocks of the current line not yet in the planner
        self._cont: Callable[[], bool] | None = None  # parser continuation
        self.hold = False
        self.hold_left = 0.0  # remaining decel time
        self.jog_cancel = False
        self.feed_ov = 100
        self.rapid_ov = 100
        self.spindle_ov = 100
        self.status_count = 0
        self._last_wco: list[float] | None = None
        self.check_mode = False
        self.sleeping = False

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    # -------------------------------------------------------------- output

    def _out(self, line: str) -> None:
        self.on_output(line)

    def _msg(self, text: str) -> None:
        self._out(f"[MSG:{text}]" if self.v11 else f"[{text}]")

    def _ok(self) -> None:
        self._out("ok")

    def _error(self, code: int) -> None:
        if self.v11:
            self._out(f"error:{code}")
        else:
            text = ERROR_TEXT_09.get(code)
            self._out(f"error: {text}" if text else f"error: Invalid gcode ID:{code}")

    def _alarm(self, codes: tuple[int, int]) -> None:
        code = codes[1] if self.v11 else codes[0]
        self._out(f"ALARM:{code}" if self.v11 else f"ALARM: {ALARM_TEXT_09.get(code, code)}")
        self.state = "Alarm"
        self.planner.clear()
        self._pending.clear()
        self._cont = None
        self.hold = False
        self.parser_pos = list(self.pos)

    # --------------------------------------------------------------- serial

    def open_port(self, reset: bool = True) -> None:
        """Called by the transport when the host opens the port (DTR)."""
        if reset:
            with self._lock:
                self._soft_reset(from_dtr=True)

    def feed(self, data: bytes) -> None:
        with self._lock:
            for b in data:
                if self._realtime(b):
                    continue
                if self._boot_pending is not None:
                    continue  # bootloader: bytes are lost
                if len(self.rx) >= RX_BUFFER_SIZE - 1:
                    self.rx_overflows += 1  # GRBL silently drops the byte
                    continue
                self.rx.append(b)

    def _realtime(self, b: int) -> bool:
        ch = chr(b)
        if ch == "?":
            if self._boot_pending is None:
                self._out(self._status())
            return True
        if ch == "!":
            self._feed_hold()
            return True
        if ch == "~":
            self._cycle_start()
            return True
        if b == 0x18:
            self._soft_reset()
            return True
        if not self.v11:
            if ch == "@":  # GRBL 0.9 safety door
                self._door()
                return True
            return False
        if b == 0x85:
            if self.state == "Jog":
                self.jog_cancel = True
                self.hold_left = HOLD_DECEL_S
            return True
        if b == 0x84:
            self._door()
            return True
        if 0x90 <= b <= 0xA1:
            self._override(b)
            return True
        return b >= 0x80  # other extended real-time bytes are ignored

    def _override(self, b: int) -> None:
        clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
        if b == 0x90:
            self.feed_ov = 100
        elif b in (0x91, 0x92, 0x93, 0x94):
            self.feed_ov = clamp(self.feed_ov + {0x91: 10, 0x92: -10, 0x93: 1, 0x94: -1}[b], 10, 200)
        elif b == 0x95:
            self.rapid_ov = 100
        elif b == 0x96:
            self.rapid_ov = 50
        elif b == 0x97:
            self.rapid_ov = 25
        elif b == 0x99:
            self.spindle_ov = 100
        elif b in (0x9A, 0x9B, 0x9C, 0x9D):
            self.spindle_ov = clamp(self.spindle_ov + {0x9A: 10, 0x9B: -10, 0x9C: 1, 0x9D: -1}[b], 10, 200)
        elif b == 0xA0:
            self.modal.coolant = "M9" if self.modal.coolant == "M8" else "M8"
        elif b == 0xA1:
            self.modal.coolant = "M9" if self.modal.coolant == "M7" else "M7"

    def _door(self) -> None:
        """Safety door: a feed hold reported as Door; cycle start resumes."""
        if self.state not in ("Alarm", "Sleep"):
            self.state = "Door"
            self.hold = True
            self.hold_left = HOLD_DECEL_S if self.planner else 0.0

    def _feed_hold(self) -> None:
        if self.state == "Jog":
            self.jog_cancel = True
            self.hold_left = HOLD_DECEL_S
            return
        if self.state in ("Run", "Home"):
            self.hold = True
            self.hold_left = HOLD_DECEL_S
            self.state = "Hold"
        elif self.state == "Idle":
            self.hold = True
            self.hold_left = 0.0
            self.state = "Hold"

    def _cycle_start(self) -> None:
        if self.state in ("Hold", "Door") and self.hold and self.hold_left <= 0:
            self.hold = False
            self.state = "Run" if self.planner else "Idle"

    def _soft_reset(self, from_dtr: bool = False) -> None:
        moving = (
            self.state in ("Run", "Home", "Jog")
            or (self.state in ("Hold", "Door") and self.hold_left > 0)
        )
        if moving and not from_dtr:
            code = ALARM_ABORT[1] if self.v11 else ALARM_ABORT[0]
            self._out(f"ALARM:{code}" if self.v11 else f"ALARM: {ALARM_TEXT_09[code]}")
        # Planned-but-unexecuted motion is discarded; the position is wherever the
        # steppers are now (lost if they were moving).
        alarm = moving and not from_dtr
        if from_dtr:
            # A DTR auto-reset is a hardware reboot: machine position restarts at 0.
            self.pos = [0.0, 0.0, 0.0]
            alarm = bool(self.persist.settings[22])
        self._init_runtime()
        self.state = "Alarm" if (alarm or self.persist.settings[22]) else "Idle"
        if self.boot_delay > 0:
            self._boot_pending = time.monotonic() + self.boot_delay
        else:
            self._boot()

    def _boot(self) -> None:
        self._boot_pending = None
        self._out("")
        self._out(f"Grbl {self.firmware} ['$' for help]")
        if self.state == "Alarm":
            self._msg("'$H'|'$X' to unlock")

    # ---------------------------------------------------------------- status

    def _wco(self) -> list[float]:
        c = self.persist.coords[self.modal.wcs]
        return [c[0] + self.g92[0], c[1] + self.g92[1], c[2] + self.g92[2] + self.modal.tlo]

    probe_forced: bool | None = None  # test hook: force the probe input closed/open

    def probe_pin(self) -> bool:
        if self.probe_forced is not None:
            return self.probe_forced
        return self.pos[2] <= self.surface(self.pos[0], self.pos[1])

    def _current_rate(self) -> float:
        if self.planner and self.state in ("Run", "Jog", "Home"):
            b = self.planner[0]
            return b.rate * (self.rapid_ov if b.rapid else self.feed_ov) / 100.0
        return 0.0

    def _status(self) -> str:
        s = self.persist.settings
        mask = int(s[10])
        mpos = self.pos
        wco = self._wco()
        wpos = [m - o for m, o in zip(mpos, wco)]
        state = self.state
        if self.v11:
            if state == "Hold":
                state = "Hold:1" if self.hold_left > 0 else "Hold:0"
            elif state == "Door":
                state = "Door:2" if self.hold_left > 0 else "Door:0"
            parts = [state]
            if mask & 1:
                parts.append("MPos:" + ",".join(_fmt(v) for v in mpos))
            else:
                parts.append("WPos:" + ",".join(_fmt(v) for v in wpos))
            if mask & 2:
                parts.append(f"Bf:{self.planner_size - len(self.planner)},{RX_BUFFER_SIZE - 1 - len(self.rx)}")
            parts.append(f"FS:{round(self._current_rate())},{round(self.modal.speed if self.modal.spindle != 'M5' else 0)}")
            pins = "P" if self.probe_pin() else ""
            if pins:
                parts.append("Pn:" + pins)
            self.status_count += 1
            if self._last_wco != wco or self.status_count % 10 == 0:
                parts.append("WCO:" + ",".join(_fmt(v) for v in wco))
                self._last_wco = list(wco)
            elif self.status_count % 10 == 5:
                parts.append(f"Ov:{self.feed_ov},{self.rapid_ov},{self.spindle_ov}")
            return "<" + "|".join(parts) + ">"
        parts = [state]
        if mask & 1:
            parts.append("MPos:" + ",".join(_fmt(v) for v in mpos))
        if mask & 2:
            parts.append("WPos:" + ",".join(_fmt(v) for v in wpos))
        if mask & 4:
            parts.append(f"Buf:{len(self.planner)}")
        if mask & 8:
            parts.append(f"RX:{len(self.rx)}")
        if mask & 16:
            parts.append("Lim:000")
        return "<" + ",".join(parts) + ">"

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(self._tick)
            now = time.monotonic()
            dt = (now - last) * self.time_scale
            last = now
            with self._lock:
                if self._boot_pending is not None:
                    if now >= self._boot_pending:
                        self._boot()
                    continue
                self._advance(dt)
                self._process_input()

    def step(self, dt: float) -> None:
        """Advance simulated time synchronously (for deterministic tests)."""
        with self._lock:
            self._advance(dt)
            self._process_input()

    def _advance(self, dt: float) -> None:
        if self.hold or self.jog_cancel:
            if self.hold_left > 0:
                # Decelerating: keep moving at (roughly) half speed until stopped.
                self._move(min(dt, self.hold_left) * 0.5)
                self.hold_left = max(0.0, self.hold_left - dt)
                if self.hold_left <= 0 and self.jog_cancel:
                    self.jog_cancel = False
                    # Flush the remaining jog motion; position is where we stopped.
                    self.planner.clear()
                    self._pending.clear()
                    self.parser_pos = list(self.pos)
                    self.state = "Idle"
                    self._cont = None
            return
        if self.state in ("Run", "Jog", "Home", "Check") or (self.state == "Idle" and self.planner):
            if self.state == "Idle" and self.planner:
                self.state = "Jog" if self.planner[0].kind == "jog" else "Run"
            self._move(dt)
            if not self.planner and self.state in ("Run", "Jog", "Home"):
                self.state = "Idle"

    def _move(self, dt: float) -> None:
        while dt > 0 and self.planner:
            b = self.planner[0]
            ov = (self.rapid_ov if b.rapid else self.feed_ov) / 100.0 if b.kind in ("line", "probe") else 1.0
            remaining = (b.duration - b.elapsed) / ov
            use = min(dt, remaining)
            before = b.elapsed
            b.elapsed += use * ov
            dt -= use
            if b.kind != "dwell":
                self.pos = b.position()
            if b.kind == "probe":
                toward = b.probe_mode in ("38.2", "38.3")
                if self.probe_pin() == toward:
                    # Locate the trigger point within this tick (GRBL latches
                    # the position at the step where the pin changes).
                    lo, hi = before, b.elapsed
                    for _ in range(30):
                        b.elapsed = (lo + hi) / 2
                        self.pos = b.position()
                        if self.probe_pin() == toward:
                            hi = b.elapsed
                        else:
                            lo = b.elapsed
                    b.elapsed = hi
                    self.pos = b.position()
                    self._finish_probe(b, True)
                    continue
            if b.elapsed >= b.duration - 1e-12:
                if b.kind != "dwell":
                    self.pos = list(b.end)
                self.planner.popleft()
                b.done = True
                if b.kind == "probe":
                    self._finish_probe(b, False)
                elif b.on_done:
                    b.on_done(b)
        if self.state == "Home" and not self.planner:
            self.state = "Idle"

    def _finish_probe(self, b: Block, contact: bool) -> None:
        if self.planner and self.planner[0] is b:
            self.planner.popleft()
        b.done = True
        self.prb = list(self.pos)
        self.prb_ok = 1 if contact else 0
        self.parser_pos = list(self.pos)
        self._out("[PRB:" + ",".join(_fmt(v) for v in self.prb) + f":{self.prb_ok}]")
        if not contact and b.probe_mode in ("38.2", "38.4"):
            self._alarm(ALARM_PROBE_FAIL)
            self._ok()
            return
        self.state = "Idle"
        self._ok()

    # ------------------------------------------------------------- parsing

    def _busy(self) -> bool:
        return bool(self.planner) or self.hold

    def _process_input(self) -> None:
        while True:
            if self._cont is not None:
                cont = self._cont
                if not cont():
                    return
                if self._cont is cont:  # it may have installed a successor
                    self._cont = None
                continue
            if self.state == "Sleep":
                return
            idx = -1
            for i, c in enumerate(self.rx):
                if c in (10, 13):
                    idx = i
                    break
            if idx < 0:
                if len(self.rx) >= RX_BUFFER_SIZE - 1:
                    # A line longer than the RX buffer can never complete.
                    self.rx.clear()
                    self._error(11)
                return
            raw = bytes(self.rx[:idx]).decode("ascii", "replace")
            del self.rx[: idx + 1]
            self._execute_line(raw)

    def _execute_line(self, raw: str) -> None:
        # GRBL strips whitespace and comments, upper-cases, and checks length.
        line, depth = [], 0
        semicolon = False
        for ch in raw:
            if semicolon:
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif depth == 0:
                if ch == ";":
                    semicolon = True
                elif ch not in " \t":
                    line.append(ch.upper())
        text = "".join(line)
        if not text:
            self._ok()
            return
        if len(text) >= LINE_BUFFER_SIZE:
            self._error(11)
            return
        try:
            if text.startswith("$"):
                self._system(text)
            else:
                if self.state == "Alarm":
                    raise SimError(9)
                if self.state == "Sleep":
                    raise SimError(8)
                self._gcode(text)
        except SimError as e:
            self._error(e.code)

    # ------------------------------------------------------------ $ commands

    def _system(self, text: str) -> None:
        cmd = text[1:]
        if cmd == "":
            self._out("$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x" if self.v11 else "$$ (view Grbl settings)")
            self._ok()
            return
        if cmd == "$":
            if self.state in ("Run", "Hold", "Jog"):
                raise SimError(8)
            self._report_settings()
            self._ok()
            return
        if cmd == "G":
            self._report_modes()
            self._ok()
            return
        if cmd == "C":
            if self.state == "Check":
                # GRBL flags the reset, reports, acknowledges the line, then resets.
                self._msg("Disabled")
                self._ok()
                self._soft_reset()
                return
            if self.state != "Idle":
                raise SimError(8)
            self.state = "Check"
            self._msg("Enabled")
            self._ok()
            return
        if cmd == "X":
            if self.state == "Alarm":
                self._msg("Caution: Unlocked")
                self.state = "Idle"
            self._ok()
            return
        if self.v11 and cmd.startswith("J="):
            self._jog(cmd[2:])
            return
        if self.v11 and cmd == "SLP":
            self.planner.clear()
            self._pending.clear()
            self.modal.spindle, self.modal.coolant = "M5", "M9"
            self.state = "Sleep"
            self._msg("Sleeping")
            self._ok()
            return
        if self.state not in ("Idle", "Alarm"):
            raise SimError(8)
        if cmd == "#":
            self._report_ngc()
            self._ok()
        elif cmd == "H":
            if not self.persist.settings[22]:
                raise SimError(5)
            self._home()
        elif cmd.startswith("I"):
            if cmd == "I":
                if self.v11:
                    self._out(f"[VER:1.1f.20170801:{self.persist.build_info}]")
                    self._out(f"[OPT:V,{self.planner_size},{RX_BUFFER_SIZE}]")
                else:
                    self._out(f"[0.9j.20180122:{self.persist.build_info}]")
            elif cmd.startswith("I="):
                self.persist.build_info = cmd[2:]
            else:
                raise SimError(3)
            self._ok()
        elif cmd.startswith("RST="):
            what = cmd[4:]
            if what not in ("$", "#", "*"):
                raise SimError(3)
            if what in ("$", "*"):
                self.persist.settings = default_settings(self.firmware)
            if what in ("#", "*"):
                self.persist.coords = [[0.0, 0.0, 0.0] for _ in range(6)]
                self.persist.g28 = [0.0, 0.0, 0.0]
                self.persist.g30 = [0.0, 0.0, 0.0]
            self._msg("Restoring defaults")
            self._ok()
            self._soft_reset()
        elif cmd.startswith("N"):
            if cmd == "N":
                for i, ln in enumerate(self.persist.startup):
                    self._out(f"$N{i}={ln}")
                self._ok()
            else:
                m = re.fullmatch(r"N([01])=(.*)", cmd)
                if not m:
                    raise SimError(3)
                self.persist.startup[int(m.group(1))] = m.group(2)
                self._ok()
        else:
            m = re.fullmatch(r"(\d+)=([-+]?(?:\d+\.?\d*|\.\d+))", cmd)
            if not m:
                if re.match(r"\d", cmd):
                    raise SimError(2 if "=" in cmd else 3)
                raise SimError(3)
            key, value = int(m.group(1)), float(m.group(2))
            if key not in self.persist.settings:
                raise SimError(3)
            if value < 0:
                raise SimError(4)
            if key == 0 and value < 3:
                raise SimError(6)
            self.persist.settings[key] = int(value) if key in INT_SETTINGS else value
            self._ok()

    def _report_settings(self) -> None:
        for key in sorted(self.persist.settings):
            v = self.persist.settings[key]
            val = str(int(v)) if key in INT_SETTINGS else f"{v:.3f}"
            if self.v11:
                self._out(f"${key}={val}")
            else:
                desc = SETTINGS_DESC_09.get(key, "")
                if key in MASK_SETTINGS:
                    desc = f"{desc}:{int(v):08b}"
                self._out(f"${key}={val} ({desc})")

    def _report_modes(self) -> None:
        m = self.modal
        motion = {"0": "G0", "1": "G1", "2": "G2", "3": "G3", "80": "G80"}.get(m.motion, "G" + m.motion)
        parts = [motion, f"G{54 + m.wcs}", f"G{m.plane}", f"G{m.units}", f"G{m.distance}", f"G{m.feedmode}"]
        if self.v11:
            if m.program != "M0":
                parts.append(m.program)
            parts += [m.spindle, m.coolant, f"T{m.tool}", f"F{m.feed:g}", f"S{m.speed:g}"]
            self._out("[GC:" + " ".join(parts) + "]")
        else:
            parts += [m.program, m.spindle, m.coolant, f"T{m.tool}", f"F{m.feed:.0f}.", f"S{m.speed:.0f}."]
            self._out("[" + " ".join(parts) + "]")

    def _report_ngc(self) -> None:
        for i, c in enumerate(self.persist.coords):
            self._out(f"[G{54 + i}:" + ",".join(_fmt(v) for v in c) + "]")
        self._out("[G28:" + ",".join(_fmt(v) for v in self.persist.g28) + "]")
        self._out("[G30:" + ",".join(_fmt(v) for v in self.persist.g30) + "]")
        self._out("[G92:" + ",".join(_fmt(v) for v in self.g92) + "]")
        self._out(f"[TLO:{_fmt(self.modal.tlo)}]")
        self._out("[PRB:" + ",".join(_fmt(v) for v in self.prb) + f":{self.prb_ok}]")

    def _home(self) -> None:
        s = self.persist.settings
        pull = float(s[27])
        target = [-pull, -pull, -pull]
        dist = math.dist(self.pos, target)
        dur = 1.0 + dist / max(1.0, float(s[25])) * 60.0
        self.state = "Home"

        def done(_b: Block) -> None:
            self.parser_pos = list(self.pos)
            self.state = "Idle"
            self._ok()

        b = Block("home", list(self.pos), target, dur, rate=float(s[25]), on_done=done)
        self.planner.append(b)
        self._cont = lambda: b.done

    # ---------------------------------------------------------------- G-code

    def _words(self, text: str) -> list[tuple[str, float, str]]:
        pos = 0
        words = []
        while pos < len(text):
            m = WORD_RE.match(text, pos)
            if not m:
                if not text[pos].isalpha():
                    raise SimError(1)
                raise SimError(2)
            words.append((m.group(1), float(m.group(2)), m.group(2)))
            pos = m.end()
        return words

    def _to_mm(self, v: float) -> float:
        return v * 25.4 if self.modal.units == 20 else v

    def _gcode(self, text: str, jog: bool = False) -> None:
        words = self._words(text)
        gs: list[str] = []
        ms: list[int] = []
        vals: dict[str, float] = {}
        for letter, value, raw in words:
            if letter == "G":
                code = raw.lstrip("+")
                code = code.rstrip("0").rstrip(".") if "." in code else code
                code = str(int(float(code))) if float(code).is_integer() else code
                gs.append(code)
            elif letter == "M":
                if not value.is_integer():
                    raise SimError(20)
                ms.append(int(value))
            elif letter == "N":
                continue
            else:
                if letter in vals:
                    raise SimError(25)
                vals[letter] = value
        supported_g = {
            "0", "1", "2", "3", "4", "10", "17", "18", "19", "20", "21", "28", "28.1", "30", "30.1",
            "38.2", "38.3", "38.4", "38.5", "40", "43.1", "49", "53", "54", "55", "56", "57", "58", "59",
            "61", "80", "90", "90.1", "91", "91.1", "92", "92.1", "93", "94",
        }
        for g in gs:
            if g not in supported_g:
                raise SimError(20)
        for mcode in ms:
            if mcode not in (0, 1, 2, 3, 4, 5, 7, 8, 9, 30):
                raise SimError(20)
        if jog and (ms or any(g not in ("20", "21", "90", "91", "53") for g in gs)):
            raise SimError(16 if self.v11 else 20)
        m = self.modal

        # Modal and non-motion state (order per NIST RS274/GRBL).
        if "F" in vals:
            if vals["F"] < 0:
                raise SimError(4)
            if not jog:
                # The block's own G20/G21 applies to its F word.
                units = 20 if "20" in gs else 21 if "21" in gs else m.units
                inverse = "93" in gs or (m.feedmode == 93 and "94" not in gs)
                m.feed = vals["F"] if inverse else vals["F"] * (25.4 if units == 20 else 1.0)
        if "S" in vals:
            if vals["S"] < 0:
                raise SimError(4)
            m.speed = vals["S"]
        if "T" in vals:
            m.tool = int(vals["T"])
        for g in gs:
            if g in ("17", "18", "19"):
                m.plane = int(g)
            elif g in ("20", "21") and not jog:
                m.units = int(g)
            elif g in ("90", "91") and not jog:
                m.distance = int(g)
            elif g in ("93", "94"):
                m.feedmode = int(g)
            elif g in ("54", "55", "56", "57", "58", "59"):
                m.wcs = int(g) - 54
            elif g == "49":
                m.tlo = 0.0
            elif g == "80":
                m.motion = "80"
        units = m.units
        distance = m.distance
        if jog:
            units = 20 if "20" in gs else 21
            distance = 91 if "91" in gs else 90
        scale = 25.4 if units == 20 else 1.0
        axes = {k: vals[k] * scale for k in "XYZ" if k in vals}

        sync_needed = any(c in (0, 3, 4, 5, 7, 8, 9) for c in ms) or any(
            g in ("4", "10", "28", "28.1", "30", "30.1", "38.2", "38.3", "38.4", "38.5", "43.1", "92", "92.1") for g in gs
        )

        def apply_rest() -> None:
            for mcode in ms:
                if mcode in (3, 4, 5):
                    m.spindle = f"M{mcode}"
                elif mcode in (7, 8):
                    m.coolant = f"M{mcode}"
                elif mcode == 9:
                    m.coolant = "M9"
                elif mcode in (2, 30):
                    m.program = "M2" if mcode == 2 else "M30"
                    # Program end resets some modal state (GRBL behaviour).
                    m.motion, m.wcs, m.plane, m.distance, m.feedmode = "1", 0, 17, 90, 94
                    m.spindle, m.coolant = "M5", "M9"
                elif mcode in (0, 1):
                    m.program = f"M{mcode}"
            self._gcode_motion(gs, vals, axes, distance, jog)
            if 0 in ms:
                # M0 program pause: GRBL holds once the planner has emptied (cycle start resumes).
                self._feed_hold()

        if sync_needed and self._busy():
            # GRBL synchronises the planner before these commands.
            def wait() -> bool:
                if self._busy():
                    return False
                try:
                    apply_rest()
                except SimError as e:
                    self._error(e.code)
                return True

            self._cont = wait
            return
        apply_rest()

    def _target(self, axes: dict[str, float], distance: int, g53: bool) -> list[float]:
        wco = self._wco()
        tgt = list(self.parser_pos)
        for i, k in enumerate("XYZ"):
            if k in axes:
                if g53:
                    tgt[i] = axes[k]
                elif distance == 91:
                    tgt[i] = self.parser_pos[i] + axes[k]
                else:
                    tgt[i] = axes[k] + wco[i]
        return tgt

    def _duration(self, start: list[float], end: list[float], rate: float, rapid: bool) -> tuple[float, float]:
        s = self.persist.settings
        maxr = [float(s[110]), float(s[111]), float(s[112])]
        d = [abs(b - a) for a, b in zip(start, end)]
        length = math.sqrt(sum(x * x for x in d))
        if length == 0:
            return 0.0, 0.0
        # Axis-limited time (rapids move at the axis-limited max).
        t_axes = max(di / mr for di, mr in zip(d, maxr)) * 60.0
        t = t_axes if rapid else max(length / rate * 60.0, t_axes)
        return t, length / t * 60.0

    def _gcode_motion(self, gs, vals, axes, distance, jog) -> None:
        m = self.modal
        g53 = "53" in gs
        # Non-modal commands that use axis words.
        if "10" in gs:
            l_val = int(vals.get("L", -1))
            p = int(vals.get("P", 0))
            if l_val not in (2, 20) or not 0 <= p <= 6:
                raise SimError(20)
            idx = (m.wcs if p == 0 else p - 1)
            c = self.persist.coords[idx]
            for i, k in enumerate("XYZ"):
                if k in axes:
                    if l_val == 2:
                        c[i] = axes[k]
                    else:
                        c[i] = self.parser_pos[i] - axes[k] - self.g92[i] - (m.tlo if i == 2 else 0.0)
            self._ok()
            return
        if "92" in gs:
            wcs = self.persist.coords[m.wcs]
            for i, k in enumerate("XYZ"):
                if k in axes:
                    self.g92[i] = self.parser_pos[i] - wcs[i] - axes[k] - (m.tlo if i == 2 else 0.0)
            self._ok()
            return
        if "92.1" in gs:
            self.g92 = [0.0, 0.0, 0.0]
            self._ok()
            return
        if "28.1" in gs:
            self.persist.g28 = list(self.parser_pos)
            self._ok()
            return
        if "30.1" in gs:
            self.persist.g30 = list(self.parser_pos)
            self._ok()
            return
        if "43.1" in gs:
            m.tlo = self._to_mm(vals.get("Z", 0.0))
            self._ok()
            return
        if "4" in gs:
            p = vals.get("P")
            if p is None:
                raise SimError(28)
            b = Block("dwell", list(self.parser_pos), list(self.parser_pos), p)
            self.planner.append(b)

            def dwell_done() -> bool:
                if not b.done:
                    return False
                self._ok()
                return True

            self._cont = dwell_done
            return
        if "28" in gs or "30" in gs:
            stored = self.persist.g28 if "28" in gs else self.persist.g30
            blocks = []
            if axes:
                mid = self._target(axes, distance, g53)
                blocks.append(self._line_block(self.parser_pos, mid, 0, True))
                self.parser_pos = mid
            blocks.append(self._line_block(self.parser_pos, list(stored), 0, True))
            self.parser_pos = list(stored)
            self._queue_blocks(blocks)
            return

        motion = next((g for g in gs if g in ("0", "1", "2", "3", "38.2", "38.3", "38.4", "38.5", "80")), None)
        if jog:
            motion = "1"
            if "F" not in vals:
                raise SimError(22)
        elif motion is not None:
            m.motion = motion
        else:
            motion = m.motion if axes else None
        if motion is None or not axes:
            if motion in ("2", "3") and not axes and ("I" in vals or "J" in vals or "R" in vals):
                raise SimError(26)
            self._ok()
            return
        if motion == "80":
            raise SimError(31)
        tgt = self._target(axes, distance, g53)
        if motion.startswith("38"):
            if m.feed <= 0:
                raise SimError(22)
            dur, rate = self._duration(self.parser_pos, tgt, m.feed, False)
            pin = self.probe_pin()
            toward = motion in ("38.2", "38.3")
            if (toward and pin) or (not toward and not pin):
                # Probe already in the target state: GRBL alarms (1.1: ALARM:4).
                self._alarm((4, 4))
                self._ok()
                return
            b = Block("probe", list(self.parser_pos), tgt, dur, rate=rate, probe_mode=motion)
            self.state = "Run"
            self.planner.append(b)
            self.parser_pos = tgt
            self._cont = lambda: b.done
            return
        feed = float(vals["F"]) * (25.4 if "20" in gs else 1.0) if jog else m.feed
        if motion in ("1", "2", "3") and feed <= 0 and m.feedmode == 94:
            raise SimError(22)
        if motion in ("0", "1"):
            blocks = [self._line_block(self.parser_pos, tgt, feed, motion == "0", jog)]
        else:
            blocks = self._arc_blocks(tgt, vals, feed, motion == "2")
        self.parser_pos = tgt
        self._queue_blocks(blocks)

    def _line_block(self, start, end, feed, rapid, jog=False) -> Block:
        dur, rate = self._duration(start, end, feed, rapid)
        return Block("jog" if jog else "line", list(start), list(end), dur, rate=rate, rapid=rapid)

    def _arc_blocks(self, tgt, vals, feed, cw) -> list[Block]:
        m = self.modal
        a0, a1, lin = {17: (0, 1, 2), 18: (2, 0, 1), 19: (1, 2, 0)}[m.plane]
        start = self.parser_pos
        scale = 25.4 if m.units == 20 else 1.0
        if "R" in vals:
            r = vals["R"] * scale
            dx, dy = tgt[a0] - start[a0], tgt[a1] - start[a1]
            h2 = 4 * r * r - dx * dx - dy * dy
            if h2 < 0:
                raise SimError(33)
            h = -math.sqrt(h2) / math.hypot(dx, dy)
            if not cw:
                h = -h
            if r < 0:
                h = -h
                r = -r
            off = (0.5 * (dx - dy * h), 0.5 * (dy + dx * h))
        else:
            ijk = {0: "I", 1: "J", 2: "K"}
            off = (vals.get(ijk[a0], 0.0) * scale, vals.get(ijk[a1], 0.0) * scale)
            r = math.hypot(*off)
        cx, cy = start[a0] + off[0], start[a1] + off[1]
        ang0 = math.atan2(start[a1] - cy, start[a0] - cx)
        ang1 = math.atan2(tgt[a1] - cy, tgt[a0] - cx)
        sweep = ang1 - ang0
        if cw and sweep >= -1e-9:
            sweep -= 2 * math.pi
        elif not cw and sweep <= 1e-9:
            sweep += 2 * math.pi
        tol = float(self.persist.settings[12])
        seg_len = 2 * math.sqrt(max(tol * (2 * r - tol), 1e-6)) if r > tol else r
        n = max(1, int(abs(sweep) * r / max(seg_len, 0.05)))
        blocks, prev = [], list(start)
        for i in range(1, n + 1):
            t = i / n
            p = list(prev)
            p[a0] = cx + r * math.cos(ang0 + sweep * t)
            p[a1] = cy + r * math.sin(ang0 + sweep * t)
            p[lin] = start[lin] + (tgt[lin] - start[lin]) * t
            if i == n:
                p = list(tgt)
            blocks.append(self._line_block(prev, p, feed, False))
            prev = p
        return blocks

    def _queue_blocks(self, blocks: list[Block]) -> None:
        if self.state == "Check":
            self._ok()
            return
        self._pending.extend(b for b in blocks if b.duration > 0)

        def push() -> bool:
            while self._pending and len(self.planner) < self.planner_size:
                self.planner.append(self._pending.popleft())
            if self._pending:
                return False
            self._ok()
            return True

        if not push():
            self._cont = push

    def _jog(self, text: str) -> None:
        if self.state not in ("Idle", "Jog"):
            raise SimError(8)
        self._gcode(text, jog=True)
