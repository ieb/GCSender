"""Settings profiles, defaults and import/export (docs/PLAN.md §3.10, P6).

A profile or an export holds the *application* settings (server-side; not the
GRBL ``$$`` values, which live in the controller's EEPROM) and, optionally,
the user commands. Session keys such as the last port are never included.
An export also carries the ``$$`` values as a read-only reference.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .store import SCHEMA

FORMAT = "gcsender-settings"
VERSION = 1
SESSION_KEYS = {"lastPort", "lastBaud"}
NAME_RE = re.compile(r"^[\w .\-()]{1,40}$")


class ProfileError(ValueError):
    pass


def portable(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k in SCHEMA and k not in SESSION_KEYS}


def defaults() -> dict[str, Any]:
    return portable({k: v[1] for k, v in SCHEMA.items()})


def document(settings: dict[str, Any], macros: list[dict[str, Any]] | None, grbl: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {"format": FORMAT, "version": VERSION, "saved": time.time(), "settings": portable(settings)}
    if macros is not None:
        doc["macros"] = [{"name": m["name"], "text": m["text"]} for m in macros]
    if grbl:
        doc["grbl"] = grbl
    return doc


def parse(doc: Any) -> tuple[dict[str, Any], list[dict[str, Any]] | None, list[str]]:
    """Return (settings, macros or None, ignored keys) from an export or profile."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ProfileError("not a GCSender settings file")
    if not isinstance(doc.get("version"), int) or doc["version"] > VERSION:
        raise ProfileError("this settings file is from a newer GCSender")
    raw = doc.get("settings")
    if not isinstance(raw, dict):
        raise ProfileError("the file has no settings")
    settings = portable(raw)
    ignored = sorted(k for k in raw if k not in settings)
    macros = doc.get("macros")
    if macros is not None and not isinstance(macros, list):
        raise ProfileError("macros must be a list")
    return settings, macros, ignored


class ProfileStore:
    def __init__(self, root: Path | None):
        self.root = root / "profiles" if root is not None else None
        self._mem: dict[str, dict[str, Any]] = {}

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name) or name.strip(" .") != name:
            raise ProfileError("a profile name is 1-40 letters, digits, spaces or . - _ ( )")
        assert self.root is not None
        return self.root / f"{name}.json"

    def list(self) -> list[dict[str, Any]]:
        if self.root is None:
            return sorted(({"name": n, "saved": d.get("saved")} for n, d in self._mem.items()), key=lambda p: p["name"].lower())
        if not self.root.exists():
            return []
        out = []
        for p in self.root.glob("*.json"):
            try:
                saved = json.loads(p.read_text(encoding="utf-8")).get("saved")
            except (OSError, ValueError):
                continue
            out.append({"name": p.stem, "saved": saved})
        return sorted(out, key=lambda p: p["name"].lower())

    def save(self, name: str, doc: dict[str, Any]) -> None:
        name = name.strip()
        if self.root is None:
            if not NAME_RE.match(name):
                raise ProfileError("invalid profile name")
            self._mem[name] = doc
            return
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(path)

    def load(self, name: str) -> dict[str, Any]:
        if self.root is None:
            if name not in self._mem:
                raise ProfileError("no such profile")
            return self._mem[name]
        path = self._path(name)
        if not path.exists():
            raise ProfileError("no such profile")
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, name: str) -> None:
        if self.root is None:
            self._mem.pop(name, None)
            return
        self._path(name).unlink(missing_ok=True)
