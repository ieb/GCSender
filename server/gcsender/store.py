"""Persistent application settings (state dir ``settings.json``)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

# key: (type, default, min, max)
SCHEMA: dict[str, tuple[type, Any, float | None, float | None]] = {
    "statusInterval": (int, 200, 50, 2000),  # ms between '?' queries
    "resetOnConnect": (bool, False, None, None),  # send Ctrl-X after opening the port
    "restoreAfterReset": (bool, True, None, None),  # restore G92/TLO/modal after a UI reset
    "autoConnect": (bool, False, None, None),  # connect to lastPort at server start
    "lastPort": (str, None, None, None),
    "lastBaud": (int, 115200, 1200, 2_000_000),
    "transcriptStatus": (bool, False, None, None),  # include '?' and status reports in the transcript
    # Jogging
    # Step jogging only: each click/key press moves exactly one step.
    "jogSteps": (str, "0.01,0.05,0.1,0.5,1,5,10", None, None),  # mm, comma separated
    "jogFeeds": (str, "50,100,300,600,1000", None, None),  # mm/min, comma separated
    "jogLimitsEnabled": (bool, False, None, None),
    # Machine-coordinate box [xmin, xmax, ymin, ymax, zmin, zmax]. Without homing,
    # machine coordinates are relative to where the machine was powered on.
    "jogLimits": (list, [0.0, 300.0, 0.0, 180.0, -45.0, 0.0], None, None),
    # Positioning and spindle
    "safeZ": (float, 5.0, -500.0, 500.0),  # work Z to raise to before XY moves
    "spindleMin": (float, 0.0, 0.0, 100000.0),
    "spindleMax": (float, 1000.0, 1.0, 100000.0),  # GRBL 0.9 default SPINDLE_MAX_RPM: S1000 = full PWM
    # Z probe
    "probePlate": (float, 0.0, 0.0, 100.0),  # plate thickness (0 = probing the work surface itself)
    "probeDepth": (float, 10.0, 0.1, 100.0),  # max travel down for the first touch
    "probeFastFeed": (float, 100.0, 1.0, 1000.0),
    "probeSlowFeed": (float, 20.0, 1.0, 500.0),
    "probeTwoStage": (bool, True, None, None),
    "probeBackoff": (float, 1.0, 0.1, 10.0),  # lift between the fast and slow touch
    "probeRetract": (float, 2.0, 0.0, 50.0),  # lift after probing
    "probeSetZero": (bool, True, None, None),
    # Heightmap transform
    "arcTolerance": (float, 0.01, 0.001, 0.5),  # max chord error when arcs are linearised (mm)
}

NUMBER_LISTS = {"jogSteps", "jogFeeds"}


class SettingsError(ValueError):
    pass


def validate(key: str, value: Any) -> Any:
    if key not in SCHEMA:
        raise SettingsError(f"unknown setting '{key}'")
    typ, _default, lo, hi = SCHEMA[key]
    if value is None and typ is str:
        return None
    if typ is bool:
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false")
        return value
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} must be an integer")
    elif typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be a number")
        value = float(value)
    elif typ is list:
        if (
            not isinstance(value, list)
            or len(value) != 6
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
        ):
            raise SettingsError(f"{key} must be a list of 6 numbers")
        value = [float(v) for v in value]
        if any(value[i] >= value[i + 1] for i in (0, 2, 4)):
            raise SettingsError(f"{key}: each minimum must be below its maximum")
        return value
    elif typ is str:
        if not isinstance(value, str) or len(value) > 256:
            raise SettingsError(f"{key} must be a string")
        if key in NUMBER_LISTS:
            try:
                nums = [float(x) for x in value.split(",") if x.strip()]
            except ValueError:
                raise SettingsError(f"{key} must be comma-separated numbers") from None
            if not nums or any(n <= 0 for n in nums):
                raise SettingsError(f"{key} must contain positive numbers")
    if lo is not None and value < lo or hi is not None and value > hi:
        raise SettingsError(f"{key} must be between {lo} and {hi}")
    return value


class SettingsStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._values = {k: v[1] for k, v in SCHEMA.items()}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            for k, v in (data.items() if isinstance(data, dict) else []):
                try:
                    self._values[k] = validate(k, v)
                except SettingsError:
                    pass  # ignore unknown/invalid persisted values

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean = {k: validate(k, v) for k, v in changes.items()}
        with self._lock:
            self._values.update(clean)
            values = dict(self._values)
            self._save(values)
        return values

    def _save(self, values: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(values, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
