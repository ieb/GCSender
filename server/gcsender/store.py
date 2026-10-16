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
}


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
    elif typ is str:
        if not isinstance(value, str) or len(value) > 256:
            raise SettingsError(f"{key} must be a string")
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
