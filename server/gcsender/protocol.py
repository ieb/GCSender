"""Classification and parsing of GRBL 0.9j / 1.1 output lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

BANNER_RE = re.compile(r"^Grbl\s+(\d+\.\d+[a-z]?)\b.*\[.*for help\]", re.IGNORECASE)
ERROR_RE = re.compile(r"^error:\s*(.*)$")
ALARM_RE = re.compile(r"^ALARM:\s*(.*)$")
SETTING_RE = re.compile(r"^\$(\d+)=([-+]?[\d.]+)(?:\s*\((.*)\))?$")
STARTUP_RE = re.compile(r"^\$N(\d)=(.*)$")


@dataclass
class Line:
    kind: str  # ok | error | alarm | status | banner | feedback | setting | startup | blank | other
    raw: str
    data: dict[str, Any] = field(default_factory=dict)


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.split(",")]


def parse_status(raw: str) -> dict[str, Any] | None:
    """Parse ``<...>`` in either the 0.9 (comma) or 1.1 (pipe) format.

    Returns a dict with ``format`` ('0.9' | '1.1'), ``state``, ``substate``,
    and whatever fields are present: ``mpos``, ``wpos``, ``wco``, ``buf``,
    ``rx``, ``bf``, ``fs``, ``ov``, ``pins``, ``lim``, ``ln``.
    """
    if not (raw.startswith("<") and raw.endswith(">")):
        return None
    body = raw[1:-1]
    out: dict[str, Any] = {}
    if "|" in body:
        parts = body.split("|")
        out["format"] = "1.1"
    elif "," in body:
        # 0.9: "Idle,MPos:1.000,2.000,3.000,WPos:..." - state then key:values
        out["format"] = "0.9"
        state, _, rest = body.partition(",")
        parts = [state]
        for m in re.finditer(r"([A-Za-z]+):([^:]*?)(?=,[A-Za-z]+:|$)", rest):
            parts.append(f"{m.group(1)}:{m.group(2)}")
    else:
        # Bare state, e.g. "<Idle>" from 0.9 with $10=0: format undeterminable.
        parts = [body]
        out["format"] = None
    state, _, sub = parts[0].partition(":")
    out["state"] = state
    out["substate"] = int(sub) if sub.isdigit() else None
    for part in parts[1:]:
        key, _, val = part.partition(":")
        try:
            if key == "MPos":
                out["mpos"] = _floats(val)
            elif key == "WPos":
                out["wpos"] = _floats(val)
            elif key == "WCO":
                out["wco"] = _floats(val)
            elif key == "Buf":
                out["buf"] = int(val)
            elif key == "RX":
                out["rx"] = int(val)
            elif key == "Bf":
                out["bf"] = [int(v) for v in val.split(",")]
            elif key in ("FS", "F"):
                out["fs"] = _floats(val)
            elif key == "Ov":
                out["ov"] = [int(v) for v in val.split(",")]
            elif key == "Pn":
                out["pins"] = val
            elif key == "Lim":
                out["lim"] = val
            elif key == "Ln":
                out["ln"] = int(val)
            elif key == "A":
                out["acc"] = val
        except ValueError:
            continue
    return out


def classify(raw: str) -> Line:
    line = raw.strip()
    if not line:
        return Line("blank", raw)
    if line == "ok":
        return Line("ok", line)
    if line.startswith("<") and line.endswith(">"):
        st = parse_status(line)
        if st is not None:
            return Line("status", line, st)
    m = ERROR_RE.match(line)
    if m:
        detail = m.group(1).strip()
        code = int(detail) if detail.isdigit() else None
        return Line("error", line, {"code": code, "text": detail})
    m = ALARM_RE.match(line)
    if m:
        detail = m.group(1).strip()
        code = int(detail) if detail.isdigit() else None
        return Line("alarm", line, {"code": code, "text": detail})
    m = BANNER_RE.match(line)
    if m:
        return Line("banner", line, {"version": m.group(1)})
    if line.startswith("[") and line.endswith("]"):
        return Line("feedback", line, parse_feedback(line[1:-1]))
    m = SETTING_RE.match(line)
    if m:
        return Line("setting", line, {"id": int(m.group(1)), "value": m.group(2), "desc": m.group(3)})
    m = STARTUP_RE.match(line)
    if m:
        return Line("startup", line, {"index": int(m.group(1)), "line": m.group(2)})
    return Line("other", line)


OFFSET_KEYS = ("G54", "G55", "G56", "G57", "G58", "G59", "G28", "G30", "G92")


def parse_feedback(body: str) -> dict[str, Any]:
    """Interpret a bracketed message body (without the brackets)."""
    if body.startswith("MSG:"):
        return {"type": "msg", "text": body[4:]}
    if body.startswith("GC:"):
        return {"type": "modal", "words": body[3:].split()}
    head, sep, rest = body.partition(":")
    if sep and head in OFFSET_KEYS:
        try:
            return {"type": "offset", "name": head, "value": _floats(rest)}
        except ValueError:
            pass
    if head == "TLO":
        try:
            return {"type": "tlo", "value": float(rest)}
        except ValueError:
            pass
    if head == "PRB":
        coords, _, ok = rest.rpartition(":")
        try:
            return {"type": "probe", "value": _floats(coords), "ok": ok == "1"}
        except ValueError:
            pass
    if head == "VER":
        version, _, info = rest.partition(":")
        return {"type": "version", "version": version, "info": info}
    if head == "OPT":
        return {"type": "options", "value": rest}
    if head == "echo":
        return {"type": "echo", "text": rest.strip()}
    # 0.9 build info "[0.9j.20160726:]"
    m = re.match(r"^(\d+\.\d+[a-z]?)\.(\d{8}):(.*)$", body)
    if m:
        return {"type": "version", "version": f"{m.group(1)}.{m.group(2)}", "info": m.group(3)}
    words = body.split()
    if words and all(re.match(r"^[GMTFS][\d.]+$", w) for w in words):
        # 0.9 $G output "[G0 G54 ... F0. S0.]"
        return {"type": "modal", "words": words}
    return {"type": "msg", "text": body}
