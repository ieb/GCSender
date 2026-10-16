"""Serial transcript files and an in-memory ring of application log records."""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"^\d{8}$")


class Transcript:
    """Append-only, daily-rotated log of everything exchanged with the controller.

    Lines look like ``2026-10-16 16:02:11.123 > G0X10`` with ``>`` for host to
    controller, ``<`` for controller to host and ``!`` for real-time bytes.
    """

    def __init__(self, directory: Path | None):
        self.directory = directory
        self._lock = threading.Lock()
        self._file = None
        self._date = None

    def write(self, direction: str, text: str) -> None:
        if self.directory is None:
            return
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        date = now.strftime("%Y%m%d")
        with self._lock:
            if self._date != date or self._file is None:
                if self._file:
                    self._file.close()
                self.directory.mkdir(parents=True, exist_ok=True)
                self._file = open(self.directory / f"serial-{date}.log", "a", encoding="utf-8")  # noqa: SIM115
                self._date = date
            self._file.write(f"{stamp} {direction} {text}\n")
            self._file.flush()

    def files(self) -> list[dict[str, Any]]:
        if self.directory is None or not self.directory.exists():
            return []
        out = []
        for p in sorted(self.directory.glob("serial-*.log"), reverse=True):
            out.append({"date": p.stem.split("-", 1)[1], "size": p.stat().st_size})
        return out

    def path_for(self, date: str | None) -> Path | None:
        if self.directory is None:
            return None
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        if not DATE_RE.match(date):
            return None
        p = self.directory / f"serial-{date}.log"
        return p if p.is_file() else None

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


class LogRing(logging.Handler):
    """Keeps the most recent application log records for the UI log window."""

    def __init__(self, capacity: int = 500):
        super().__init__(level=logging.INFO)
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += f" ({record.exc_info[1]!r})"
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        with self._lock2:
            self._records.append({"t": record.created, "level": record.levelname, "name": record.name, "msg": msg})

    def records(self) -> list[dict[str, Any]]:
        with self._lock2:
            return list(self._records)
