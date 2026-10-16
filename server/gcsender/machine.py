"""Machine model: parser (modal) state, offsets, GRBL settings and warnings."""

from __future__ import annotations

from typing import Any

from .grbl_messages import setting_meta

MODAL_GROUPS: dict[str, set[str]] = {
    "motion": {"G0", "G1", "G2", "G3", "G38.2", "G38.3", "G38.4", "G38.5", "G80"},
    "wcs": {"G54", "G55", "G56", "G57", "G58", "G59"},
    "plane": {"G17", "G18", "G19"},
    "units": {"G20", "G21"},
    "distance": {"G90", "G91"},
    "arcDistance": {"G91.1"},
    "feedMode": {"G93", "G94"},
    "cutterComp": {"G40"},
    "toolLength": {"G43.1", "G49"},
    "program": {"M0", "M1", "M2", "M30"},
    "spindle": {"M3", "M4", "M5"},
}
WCS_NAMES = ("G54", "G55", "G56", "G57", "G58", "G59")


def _num(word: str) -> float:
    return float(word[1:].rstrip(".") or 0)


def parse_modal(words: list[str]) -> dict[str, Any]:
    """Turn ``$G`` words (either firmware) into a structured dict."""
    out: dict[str, Any] = {"words": list(words), "coolant": []}
    for w in words:
        letter = w[:1]
        if letter == "T":
            out["tool"] = int(_num(w))
        elif letter == "F":
            out["feed"] = _num(w)
        elif letter == "S":
            out["speed"] = _num(w)
        elif w in ("M7", "M8", "M9"):
            if w == "M9":
                out["coolant"] = []
            else:
                out["coolant"].append(w)
        else:
            for group, codes in MODAL_GROUPS.items():
                if w in codes:
                    out[group] = w
                    break
    return out


class MachineModel:
    def __init__(self) -> None:
        self.reset_all()

    def reset_all(self) -> None:
        self.status: dict[str, Any] | None = None
        self.modal: dict[str, Any] | None = None
        self.offsets: dict[str, Any] = {}
        self.settings: dict[int, dict[str, Any]] = {}
        self.build: dict[str, Any] | None = None

    # ---------------------------------------------------------- settings

    def set_setting(self, sid: int, raw: str, desc: str | None = None) -> None:
        try:
            value: float | int = float(raw)
            if value.is_integer() and "." not in raw:
                value = int(value)
        except ValueError:
            value = raw  # type: ignore[assignment]
        self.settings[sid] = {"id": sid, "value": value, "raw": raw, "desc": desc}

    def setting(self, sid: int, default: Any = None) -> Any:
        s = self.settings.get(sid)
        return s["value"] if s else default

    def settings_list(self) -> list[dict[str, Any]]:
        return [{**s, **setting_meta(sid)} for sid, s in sorted(self.settings.items())]

    # ------------------------------------------------------------ offsets

    def active_wcs(self) -> str:
        return (self.modal or {}).get("wcs", "G54")

    def tlo(self) -> float:
        v = self.offsets.get("TLO")
        return float(v) if isinstance(v, (int, float)) else 0.0

    def derived_g92(self) -> list[float] | None:
        """Current G92 offset from the live WCO (needs $# coords for the active WCS).

        WCO = coords[active WCS] + G92 + (0, 0, TLO). Using the status report
        keeps this correct even if G92 changed since the last ``$#``.
        """
        if not self.status or "wco" not in self.status:
            return None
        coords = self.offsets.get(self.active_wcs())
        if not isinstance(coords, list):
            return None
        wco = self.status["wco"]
        tlo = self.tlo()
        return [wco[0] - coords[0], wco[1] - coords[1], wco[2] - coords[2] - tlo]

    # ----------------------------------------------------------- warnings

    def warnings(self, caps: dict[str, Any] | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not caps or not self.settings:
            return out
        mask = self.setting(10)
        if isinstance(mask, (int, float)):
            mask = int(mask)
            if caps.get("family") == "0.9" and (mask & 3) != 3:
                out.append({
                    "id": "status-mask",
                    "level": "error",
                    "text": f"$10={mask}: GRBL 0.9 status reports must include both machine and work position "
                            "(bits 0 and 1). Positions will be incomplete until this is changed.",
                    "fix": "$10=3",
                })
            elif caps.get("family") == "1.1" and not (mask & 1):
                out.append({
                    "id": "status-mask",
                    "level": "warn",
                    "text": f"$10={mask}: reports work position only; machine position is derived from WCO. "
                            "$10=1 (or 3) is recommended.",
                    "fix": "$10=1",
                })
        if self.setting(13) == 1:
            out.append({
                "id": "report-inches",
                "level": "error",
                "text": "$13=1: GRBL reports positions in inches; GCSender works in millimetres.",
                "fix": "$13=0",
            })
        return out
