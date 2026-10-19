"""Bug reports: one zip with everything needed to diagnose a problem remotely.

Created from the SPA's Report button (or ``POST /api/bugreports``) and kept in
``<state>/bugreports``. A report contains:

* ``README.txt``       - summary: when, what the user wrote, versions, machine and job state
* ``report.json``      - the same, machine-readable
* ``client.json``      - what the browser saw: errors, failed API calls, visible messages, preferences
* ``server-state.json`` - controller/streamer internals, retained events, job/journal, settings, macros
* ``events.json``      - recent events (and status polls) with timestamps
* ``threads.txt``      - stack of every server thread (hangs, deadlocks)
* ``app-log.txt``      - the tail of the application log
* ``serial-tail.txt``  - the tail of the serial transcript (everything sent to / received from GRBL)
* ``files/``           - settings.json, macros.json, journal.json, current heightmap
* ``program.nc``       - the loaded program (optional; large files are trimmed)

Each section is captured independently: a failure is recorded in the report
instead of aborting it. Tokens are never included.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import secrets
import sys
import threading
import time
import traceback
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_REPORTS = 100
MAX_TOTAL_BYTES = 500 << 20
MAX_DESCRIPTION = 20_000
PROGRAM_FULL_LIMIT = 5 << 20
SERIAL_TAIL_LINES = 5000
APP_LOG_TAIL_LINES = 3000
SMALL_FILES = ("settings.json", "macros.json", "journal.json", "heightmap-current.json", "heightmap-current.map")


class ReportError(ValueError):
    pass


def _tail_lines(path: Path, n: int, max_bytes: int = 4 << 20) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, tuple, deque)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    return repr(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=_json_default)


class BugReports:
    def __init__(
        self,
        state_dir: Path | None,
        *,
        bus: Any,
        core: Callable[[], dict[str, Any]],
        transcript: Any = None,
        log_ring: Any = None,
        log_file: Path | None = None,
        info: dict[str, Any] | None = None,
    ):
        self.state_dir = state_dir
        self.root = state_dir / "bugreports" if state_dir is not None else None
        self._mem: dict[str, tuple[dict[str, Any], bytes]] = {}
        self.bus = bus
        self.core = core
        self.transcript = transcript
        self.log_ring = log_ring
        self.log_file = log_file
        self.info = info or {}
        self.started = time.time()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ capture

    def _server_info(self) -> dict[str, Any]:
        try:
            import serial  # type: ignore[import-untyped]

            pyserial = getattr(serial, "__version__", "?")
        except ImportError:
            pyserial = None
        return {
            **self.info,
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "pyserial": pyserial,
            "pid": os.getpid(),
            "startedAt": self.started,
            "uptime_s": round(time.time() - self.started, 1),
            "stateDir": str(self.state_dir) if self.state_dir else None,
            "threads": threading.active_count(),
        }

    def _server_state(self) -> dict[str, Any]:
        core = self.core()
        out: dict[str, Any] = {}

        def section(name: str, fn: Callable[[], Any]) -> None:
            try:
                out[name] = fn()
            except Exception as e:  # noqa: BLE001 - a report must always be produced
                out[name] = {"captureError": f"{type(e).__name__}: {e}"}

        ctl = core.get("controller")
        if ctl is not None:
            section("connection", lambda: ctl.connection)
            section("caps", lambda: ctl.caps)
            section("machineState", lambda: ctl.machine_state)
            section("busy", lambda: ctl.busy)
            section("streamer", lambda: ctl._streamer.describe() if ctl._streamer else None)
            section("status", lambda: ctl.model.status)
            section("modal", lambda: ctl.model.modal)
            section("offsets", lambda: ctl.model.offsets)
            section("build", lambda: ctl.model.build)
            section("grblSettings", lambda: {str(k): v.get("raw") for k, v in ctl.model.settings.items()})
            section("grblWarnings", lambda: ctl.model.warnings(ctl.caps))
            section("appSettings", lambda: ctl.settings.all())
            section("backlash", lambda: ctl.backlash.snapshot())
            section("internals", lambda: {
                k: getattr(ctl, k, None) for k in (
                    "_offsets_dirty", "_settings_dirty", "_refresh_pending", "_restore_pending", "_expect_reset",
                    "_last_alarm_at", "_last_status_at", "_last_rx", "_fast_poll_until",
                )
            } | {"monotonicNow": time.monotonic()})
        jobs = core.get("jobs")
        if jobs is not None:
            section("job", jobs.snapshot)
            section("journal", lambda: jobs.journal.read() if jobs.journal else None)
        macros = core.get("macros")
        if macros is not None:
            section("macros", lambda: {"list": macros.store.list(), "state": macros.state})
        section("retained", lambda: self.bus.snapshot()["retained"])
        return out

    @staticmethod
    def _threads() -> str:
        names = {t.ident: f"{t.name}{' (daemon)' if t.daemon else ''}" for t in threading.enumerate()}
        parts = []
        for ident, frame in sys._current_frames().items():
            parts.append(f"--- thread {names.get(ident, '?')} [{ident}]\n" + "".join(traceback.format_stack(frame)))
        return "\n".join(parts)

    def _app_log(self) -> str:
        if self.log_file is not None:
            lines: list[str] = []
            backup = Path(str(self.log_file) + ".1")
            if backup.exists():
                lines += _tail_lines(backup, APP_LOG_TAIL_LINES)
            lines += _tail_lines(self.log_file, APP_LOG_TAIL_LINES)
            if lines:
                return "\n".join(lines[-APP_LOG_TAIL_LINES:]) + "\n"
        recs = self.log_ring.records() if self.log_ring else []
        return "".join(
            f"{datetime.fromtimestamp(r['t']).isoformat(sep=' ', timespec='milliseconds')} {r['level']} {r['name']}: {r['msg']}\n"
            for r in recs
        )

    def _serial_tail(self) -> str:
        if self.transcript is None:
            return ""
        files = self.transcript.files()  # newest first
        lines: list[str] = []
        for f in files[:2]:
            p = self.transcript.path_for(f["date"])
            if p is not None:
                lines = _tail_lines(p, SERIAL_TAIL_LINES) + lines
            if len(lines) >= SERIAL_TAIL_LINES:
                break
        return "\n".join(lines[-SERIAL_TAIL_LINES:]) + ("\n" if lines else "")

    def _program(self) -> tuple[str | None, dict[str, Any] | None]:
        jobs = self.core().get("jobs")
        if jobs is None or jobs.variant is None:
            return None, None
        text = jobs.text()
        meta = {"name": (jobs.meta or {}).get("name"), "variant": jobs.variant.kind, "lines": len(jobs.variant.raw), "bytes": len(text)}
        if len(text.encode()) <= PROGRAM_FULL_LIMIT:
            return text, meta
        lines = text.splitlines()
        meta["trimmed"] = True
        keep = 20_000
        return "\n".join(lines[:keep] + [f"; ... {len(lines) - 2 * keep} lines omitted by the bug report ..."] + lines[-keep:]) + "\n", meta

    def _summary(self, state: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
        conn = state.get("connection") or {}
        job = state.get("job") or {}
        status = state.get("status") or {}
        return {
            "connection": {k: conn.get(k) for k in ("state", "port", "firmware", "session", "error")} if isinstance(conn, dict) else conn,
            "machineState": state.get("machineState"),
            "busy": state.get("busy"),
            "wpos": status.get("wpos") if isinstance(status, dict) else None,
            "mpos": status.get("mpos") if isinstance(status, dict) else None,
            "job": {k: job.get(k) for k in ("state", "message", "acked", "sent", "errorCount")} | {"name": (job.get("job") or {}).get("name")}
            if isinstance(job, dict) and "state" in job else None,
            "alarm": (state.get("retained") or {}).get("alarm") if isinstance(state.get("retained"), dict) else None,
            "clientErrors": len(client.get("errors") or []),
            "clientApiFailures": sum(
                1 for c in (client.get("apiCalls") or []) if isinstance(c, dict) and (c.get("status") == 0 or (c.get("status") or 0) >= 400)
            ),
        }

    # ----------------------------------------------------------- public API

    def create(self, description: str, client: Any = None, include_program: bool = True) -> dict[str, Any]:
        if not isinstance(description, str) or not description.strip():
            raise ReportError("please describe what happened")
        if len(description) > MAX_DESCRIPTION:
            raise ReportError("the description is too long")
        client = client if isinstance(client, dict) else {}
        now = time.time()
        rid = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        state = self._server_state()
        info = self._server_info()
        summary = self._summary(state, client)
        program, program_meta = (None, None)
        if include_program:
            try:
                program, program_meta = self._program()
            except Exception as e:  # noqa: BLE001
                program_meta = {"captureError": f"{type(e).__name__}: {e}"}
        first = description.strip().splitlines()[0][:120]
        meta = {
            "id": rid,
            "created": now,
            "title": first,
            "description": description,
            "server": info,
            "summary": summary,
            "program": program_meta,
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", self._readme(meta))
            z.writestr("report.json", _dumps(meta))
            z.writestr("client.json", _dumps(client))
            z.writestr("server-state.json", _dumps(state))
            for name, fn in (
                ("events.json", lambda: _dumps({**self.bus.history(), "console": self.bus.snapshot()["console"]})),
                ("threads.txt", self._threads),
                ("app-log.txt", self._app_log),
                ("serial-tail.txt", self._serial_tail),
            ):
                try:
                    z.writestr(name, fn())
                except Exception as e:  # noqa: BLE001
                    z.writestr(name + ".error.txt", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            if self.state_dir is not None:
                for fname in SMALL_FILES:
                    p = self.state_dir / fname
                    if p.is_file() and p.stat().st_size < (2 << 20):
                        z.write(p, f"files/{fname}")
            if program is not None:
                z.writestr("program.nc", program)
        data = buf.getvalue()
        entry = {"id": rid, "created": now, "title": first, "size": len(data), "summary": summary}
        with self._lock:
            if self.root is None:
                self._mem[rid] = (entry, data)
            else:
                self.root.mkdir(parents=True, exist_ok=True)
                (self.root / f"{rid}.zip").write_bytes(data)
                (self.root / f"{rid}.json").write_text(_dumps(entry), encoding="utf-8")
                self._prune()
        log.warning("bug report %s created: %s", rid, first)
        return entry

    @staticmethod
    def _readme(meta: dict[str, Any]) -> str:
        s = meta["summary"]
        srv = meta["server"]
        lines = [
            f"GCSender bug report {meta['id']}",
            f"Created: {datetime.fromtimestamp(meta['created']).isoformat(sep=' ', timespec='seconds')}",
            "",
            "What happened (from the user):",
            *("  " + ln for ln in meta["description"].splitlines()),
            "",
            f"GCSender {srv.get('version')} · Python {srv.get('python', '').split()[0]} · {srv.get('platform')}",
            f"Connection: {s.get('connection')}",
            f"Machine: {s.get('machineState')} · busy: {s.get('busy') or 'no'} · WPos {s.get('wpos')} · MPos {s.get('mpos')}",
            f"Job: {s.get('job')}",
            f"Alarm: {s.get('alarm')}",
            f"Browser errors: {s.get('clientErrors')} · failed API calls: {s.get('clientApiFailures')}",
            "",
            "Files: report.json, client.json, server-state.json, events.json, threads.txt,",
            "app-log.txt, serial-tail.txt, files/ (settings, macros, journal, heightmap), program.nc (optional).",
        ]
        return "\n".join(lines) + "\n"

    def _prune(self) -> None:
        assert self.root is not None
        zips = sorted(self.root.glob("*.zip"), key=lambda p: p.name, reverse=True)
        total = 0
        for i, p in enumerate(zips):
            total += p.stat().st_size
            if i >= MAX_REPORTS or total > MAX_TOTAL_BYTES:
                p.unlink(missing_ok=True)
                p.with_suffix(".json").unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        if self.root is None:
            items = [e for e, _ in self._mem.values()]
        else:
            items = []
            for p in self.root.glob("*.json") if self.root.exists() else []:
                try:
                    items.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda e: e.get("created", 0), reverse=True)

    def data(self, rid: str) -> bytes:
        if not ID_RE.match(rid):
            raise ReportError("invalid report id")
        if self.root is None:
            if rid not in self._mem:
                raise ReportError("no such report")
            return self._mem[rid][1]
        p = self.root / f"{rid}.zip"
        if not p.is_file():
            raise ReportError("no such report")
        return p.read_bytes()

    def archive(self) -> bytes:
        """All reports in one zip (each report is a zip inside it)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for e in self.list():
                try:
                    z.writestr(f"{e['id']}.zip", self.data(e["id"]))
                except ReportError:
                    continue
            z.writestr("index.json", _dumps(self.list()))
        return buf.getvalue()

    def delete(self, rid: str) -> None:
        if not ID_RE.match(rid):
            raise ReportError("invalid report id")
        if self.root is None:
            self._mem.pop(rid, None)
            return
        (self.root / f"{rid}.zip").unlink(missing_ok=True)
        (self.root / f"{rid}.json").unlink(missing_ok=True)
