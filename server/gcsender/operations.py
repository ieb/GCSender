"""Machine operations behind the WCS, positioning, spindle and coolant panels.

All motion-free operations require Idle so they cannot interleave with
motion; GRBL applies offsets at parse time, which would otherwise depend on
where the parser is in the queue.
"""

from __future__ import annotations

from typing import Any

from .errors import ControllerError
from .machine import WCS_NAMES


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _require(ctl: Any, *states: str) -> str:
    if ctl.connection.get("state") != "connected":
        raise ControllerError("not connected")
    state = ctl.machine_state
    if state not in states:
        raise ControllerError(f"not possible while {state}; requires {' or '.join(states)}")
    return state


def _ok(ctl: Any, line: str) -> dict[str, Any]:
    r = ctl.command(line, source="ui")
    if r["status"] != "ok":
        raise ControllerError(f"{line}: {r['response']}")
    return r


def _axes(values: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in values.items():
        a = str(k).upper()
        if a not in ("X", "Y", "Z"):
            raise ControllerError(f"unknown axis {k!r}", 400)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ControllerError(f"invalid value for {a}", 400)
        out[a] = float(v)
    return out


# ------------------------------------------------------------------- WCS


def select_wcs(ctl: Any, name: str) -> dict[str, Any]:
    name = str(name).upper()
    if name not in WCS_NAMES:
        raise ControllerError("wcs must be one of G54-G59", 400)
    _require(ctl, "Idle")
    return _ok(ctl, name)


def set_zero(ctl: Any, values: dict[str, Any], persistent: bool = True) -> dict[str, Any]:
    """Make the current position read ``values`` in work coordinates.

    persistent: ``G10 L20 P<n>`` (active WCS, stored in EEPROM);
    otherwise ``G92`` (temporary, lost on reset/power cycle).
    """
    axes = _axes(values)
    if not axes:
        raise ControllerError("no axes given", 400)
    _require(ctl, "Idle")
    words = "".join(f"{a}{_f(v)}" for a, v in axes.items())
    units = (ctl.sync_modal() or {}).get("units", "G21")
    prefix = "G21" if units != "G21" else ""
    if persistent:
        p = WCS_NAMES.index(ctl.model.active_wcs()) + 1
        line = f"{prefix}G10L20P{p}{words}"
    else:
        line = f"{prefix}G92{words}"
    r = _ok(ctl, line)
    if prefix:
        _ok(ctl, units)  # values were given in mm; restore the user's units
    return r


def clear_g92(ctl: Any) -> dict[str, Any]:
    _require(ctl, "Idle")
    return _ok(ctl, "G92.1")


# ----------------------------------------------------------- positioning


def goto(ctl: Any, values: dict[str, Any], safe_z: float | None = None) -> dict[str, Any]:
    """Rapid to work coordinates. With ``safe_z``, first raise Z to it if the
    tool is below, and move XY before lowering to a requested Z."""
    axes = _axes(values)
    if not axes:
        raise ControllerError("no target given", 400)
    _require(ctl, "Idle")
    modal = dict(ctl.sync_modal() or {})
    status = ctl.model.status or {}
    wz = (status.get("wpos") or [0, 0, 0])[2]
    lines = []
    xy = {a: v for a, v in axes.items() if a in "XY"}
    if xy and safe_z is not None and wz < safe_z:
        lines.append(f"G21G90G0Z{_f(safe_z)}")
    if xy:
        lines.append("G21G90G0" + "".join(f"{a}{_f(v)}" for a, v in xy.items()))
    if "Z" in axes:
        lines.append(f"G21G90G0Z{_f(axes['Z'])}")
    for line in lines:
        _ok(ctl, line)
    restore = []
    if modal.get("units", "G21") != "G21":
        restore.append(modal["units"])
    if modal.get("distance", "G90") != "G90":
        restore.append(modal["distance"])
    if modal.get("motion") == "G1":
        restore.append("G1")
    if restore:
        _ok(ctl, "".join(restore))
    return {"lines": lines}


# --------------------------------------------------------- spindle/coolant


def spindle(ctl: Any, on: bool, rpm: float | None = None, direction: str = "cw") -> dict[str, Any]:
    caps = ctl.caps or {}
    state = ctl.machine_state
    if ctl.connection.get("state") != "connected":
        raise ControllerError("not connected")
    if state == "Hold" and caps.get("family") == "1.1":
        # Real-time spindle stop override toggles the spindle while held.
        modal_on = (ctl.model.modal or {}).get("spindle") in ("M3", "M4")
        ctl._write_raw(b"\x9e")
        ctl._console("rt", "spindleStop", "ui")
        return {"method": "realtime", "wasOn": modal_on}
    if state != "Idle":
        hint = "" if caps.get("family") == "1.1" else " (GRBL 0.9 waits for the planner to empty on spindle changes)"
        raise ControllerError(f"spindle can only be changed while Idle{hint}")
    if not on:
        return _ok(ctl, "M5")
    lo = float(ctl.settings.get("spindleMin"))
    hi = float(ctl.settings.get("spindleMax"))
    if rpm is None:
        rpm = float((ctl.model.modal or {}).get("speed") or hi)
    rpm = max(lo, min(hi, float(rpm)))
    code = "M4" if direction == "ccw" else "M3"
    return _ok(ctl, f"{code}S{_f(rpm)}")


def coolant(ctl: Any, flood: bool | None = None, mist: bool | None = None) -> dict[str, Any]:
    caps = ctl.caps or {}
    if ctl.connection.get("state") != "connected":
        raise ControllerError("not connected")
    current = set((ctl.model.modal or {}).get("coolant") or [])
    want = set(current)
    for code, flag in (("M8", flood), ("M7", mist)):
        if flag is True:
            want.add(code)
        elif flag is False:
            want.discard(code)
    if want == current:
        return {"lines": []}
    state = ctl.machine_state
    if state != "Idle":
        if caps.get("coolantToggle"):
            for code, byte in (("M8", b"\xa0"), ("M7", b"\xa1")):
                if (code in want) != (code in current):
                    ctl._write_raw(byte)
                    ctl._console("rt", f"toggle {code}", "ui")
            return {"method": "realtime"}
        raise ControllerError("coolant can only be changed while Idle on GRBL 0.9")
    lines = []
    if current - want:
        lines.append("M9")
        lines += sorted(want, reverse=True)
    else:
        lines += sorted(want - current, reverse=True)
    for line in lines:
        _ok(ctl, line)
    return {"lines": lines}
