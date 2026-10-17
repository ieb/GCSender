"""Job library on disk (``<state>/jobs``) and loaded-job variants."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyze import Scanner, bounds_of, clean_line
from .estimate import Machine, estimate

MAX_JOB_BYTES = 50 << 20
ID_RE = re.compile(r"^[0-9a-f]{12}$")
M6_RE = re.compile(r"M0*6(?![0-9.])")


class JobError(ValueError):
    pass


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w .\-()]+", "_", name or "").strip(" .") or "program.nc"
    return name[:120]


@dataclass
class Variant:
    """One streamable text (original, or heightmap-transformed)."""

    kind: str  # original | transformed
    raw: list[str]  # display lines (exact text)
    cmds: list[str]  # what is streamed per line ("" = skipped)
    seconds: list[float] = field(default_factory=list)
    cum: list[float] = field(default_factory=list)  # cumulative seconds *before* line i
    bounds: dict[str, Any] | None = None
    too_long: list[int] = field(default_factory=list)
    m6_lines: list[int] = field(default_factory=list)
    g92: bool = False
    digest: str = ""
    map_digest: str | None = None  # transformed: heightmap it was built from
    stats: dict[str, Any] | None = None  # transformed: transform stats

    @classmethod
    def build(cls, kind: str, text: str, machine: Machine, max_line: int, **extra: Any) -> "Variant":
        raw = text.splitlines()
        cmds = [clean_line(r) for r in raw]
        v = cls(kind, raw, cmds, **extra)
        v.digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        v.too_long = [i for i, c in enumerate(cmds) if len(c) > max_line][:100]
        v.m6_lines = [i for i, c in enumerate(cmds) if M6_RE.search(c)]
        v.seconds = estimate(cmds, machine)
        total = 0.0
        v.cum = []
        for s in v.seconds:
            v.cum.append(total)
            total += s
        v.cum.append(total)
        events = []
        sc = Scanner()
        for i, c in enumerate(cmds):
            events.extend(ev for ev in sc.feed_line(i, c) if ev.kind == "move")
        v.bounds = bounds_of(events)
        v.g92 = sc.m.g92_used
        return v

    @property
    def total(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lines": len(self.raw),
            "digest": self.digest,
            "eta": round(self.total, 1),
            "bounds": self.bounds,
            "tooLong": self.too_long,
            "m6Lines": self.m6_lines[:100],
            "g92": self.g92,
            "mapDigest": self.map_digest,
            "stats": self.stats,
        }


class JobLibrary:
    def __init__(self, root: Path | None):
        self.root = root / "jobs" if root is not None else None
        self._mem: dict[str, tuple[dict[str, Any], str]] = {}  # used without a state dir (tests)
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, job_id: str) -> tuple[Path, Path]:
        if not ID_RE.match(job_id):
            raise JobError("invalid job id")
        assert self.root is not None
        return self.root / f"{job_id}.nc", self.root / f"{job_id}.json"

    def add(self, name: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_JOB_BYTES:
            raise JobError("program larger than 50 MB")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        if "\x00" in text:
            raise JobError("this does not look like a G-code text file")
        job_id = secrets.token_hex(6)
        meta = {
            "id": job_id,
            "name": safe_name(name),
            "size": len(data),
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            "uploaded": time.time(),
        }
        if self.root is None:
            self._mem[job_id] = (meta, text)
            return meta
        nc, js = self._paths(job_id)
        nc.write_text(text, encoding="utf-8", newline="")
        js.write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def list(self) -> list[dict[str, Any]]:
        if self.root is None:
            items = [m for m, _ in self._mem.values()]
        else:
            items = []
            for js in self.root.glob("*.json"):
                try:
                    items.append(json.loads(js.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda m: m.get("uploaded", 0), reverse=True)

    def get(self, job_id: str) -> tuple[dict[str, Any], str]:
        if self.root is None:
            if job_id not in self._mem:
                raise JobError("no such job")
            return self._mem[job_id]
        nc, js = self._paths(job_id)
        if not nc.exists() or not js.exists():
            raise JobError("no such job")
        return json.loads(js.read_text(encoding="utf-8")), nc.read_text(encoding="utf-8")

    def delete(self, job_id: str) -> None:
        if self.root is None:
            self._mem.pop(job_id, None)
            return
        for p in self._paths(job_id):
            p.unlink(missing_ok=True)
