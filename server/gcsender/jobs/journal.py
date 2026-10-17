"""Job journal (docs/PLAN.md §5.7, §8).

A small JSON file in the state directory describing the active job. It lets a
restarted server (or a reconnect after a lost link) find out what was
running and where, and offer to resume.

Writes are atomic (temp file + ``os.replace``, fsync'd). The runner writes it
about once a second while a job is active, on every state change and
*ahead* of streaming: before sending past line ``reserved`` it records a new
``reserved``. After a crash the last line actually sent therefore lies in
``[acked, reserved]``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1


class Journal:
    def __init__(self, path: Path | None):
        self.path = path
        self._mem: dict[str, Any] | None = None  # without a state dir (tests)

    def read(self) -> dict[str, Any] | None:
        if self.path is None:
            return dict(self._mem) if self._mem else None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable job journal %s: %s", self.path, e)
            return None
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return None
        return data

    def write(self, data: dict[str, Any]) -> None:
        data = {"version": VERSION, **data}
        if self.path is None:
            self._mem = data
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:  # never let journaling break a running job
            log.warning("cannot write the job journal: %s", e)

    def clear(self) -> None:
        if self.path is None:
            self._mem = None
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cannot remove the job journal: %s", e)
