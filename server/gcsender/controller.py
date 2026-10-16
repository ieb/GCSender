"""Controller: owns the transport, detects the firmware, polls status, and
routes controller output to the streamer and the event bus.

Phase 0 scope: connect/disconnect, firmware detection, status polling, console
commands and real-time bytes. Jobs, jogging, probing etc. build on this.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import capabilities
from .events import EventBus
from .protocol import classify
from .streamer import Streamer, StreamerError
from .transport import Transport, TransportError

BANNER_WAIT_S = 2.5  # Arduino bootloader + GRBL init after a DTR reset
UNRESPONSIVE_S = 6.0

REALTIME_COMMON = {"status": b"?", "hold": b"!", "resume": b"~", "reset": b"\x18"}
REALTIME_11 = {"jogCancel": b"\x85", "door": b"\x84"}
REALTIME_09 = {"door": b"@"}


class ControllerError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Controller:
    def __init__(
        self,
        bus: EventBus,
        make_transport: Callable[[str, int], Transport],
        poll_interval: float = 0.2,
    ):
        self.bus = bus
        self._make_transport = make_transport
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._transport: Transport | None = None
        self._streamer: Streamer | None = None
        self._conn: dict[str, Any] = {"state": "disconnected"}
        self._caps: dict[str, Any] | None = None
        self._opened_at = 0.0
        self._last_rx = 0.0
        self._wco: list[float] | None = None
        self._machine_state: str | None = None
        self._offsets: dict[str, Any] = {}
        self._stop = threading.Event()
        self._publish_connection()
        self._poller = threading.Thread(target=self._poll_loop, name="status-poll", daemon=True)
        self._poller.start()

    # ------------------------------------------------------------ publishing

    def _publish_connection(self) -> None:
        self.bus.publish("connection", dict(self._conn), retain=True)

    def _console(self, direction: str, line: str, source: str | None = None) -> None:
        self.bus.publish("console", {"dir": direction, "line": line, "src": source, "t": time.time()})

    @property
    def caps(self) -> dict[str, Any] | None:
        return self._caps

    @property
    def connection(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._conn)

    # ------------------------------------------------------------ connection

    def connect(self, port: str, baud: int = 115200) -> dict[str, Any]:
        with self._lock:
            if self._transport is not None:
                raise ControllerError("already connected")
            transport = self._make_transport(port, baud)
            transport.on_line = self._on_line
            transport.on_close = self._on_transport_closed
            self._streamer = Streamer(self._write_raw)
            self._transport = transport
            self._caps = None
            self._wco = None
            self._machine_state = None
            self._offsets = {}
            self._conn = {"state": "connecting", "port": port, "baud": baud, "transport": transport.description}
            self._opened_at = time.monotonic()
            self._last_rx = 0.0
            try:
                transport.open()
            except TransportError as e:
                self._transport = None
                self._streamer = None
                self._conn = {"state": "disconnected", "port": port, "baud": baud, "error": str(e)}
                self._publish_connection()
                raise ControllerError(f"cannot open {port}: {e}", 502) from e
            self._publish_connection()
            self._console("sys", f"opened {transport.description}")
            return dict(self._conn)

    def disconnect(self, reason: str | None = None) -> None:
        with self._lock:
            transport, self._transport = self._transport, None
            streamer, self._streamer = self._streamer, None
            if transport is None:
                return
            prev = self._conn
            self._conn = {"state": "disconnected", "port": prev.get("port"), "baud": prev.get("baud")}
            if reason:
                self._conn["error"] = reason
            self._caps = None
        if streamer:
            streamer.reset("disconnected")
        transport.close()
        self._publish_connection()
        self.bus.publish("caps", None, retain=True)
        self._console("sys", f"disconnected{': ' + reason if reason else ''}")

    def _on_transport_closed(self, reason: str | None) -> None:
        # Unexpected loss of the link (USB unplugged, etc.).
        self.disconnect(reason or "connection lost")

    def close(self) -> None:
        self._stop.set()
        self.disconnect()

    # ------------------------------------------------------------- writing

    def _write_raw(self, data: bytes) -> None:
        transport = self._transport
        if transport is None:
            raise TransportError("not connected")
        transport.write(data)

    def command(self, line: str, source: str = "console", timeout: float = 30.0) -> dict[str, Any]:
        """Send one line and wait for its ok/error."""
        line = line.strip()
        if not line:
            raise ControllerError("empty command", 400)
        if any(ord(c) < 0x20 or ord(c) > 0x7E for c in line):
            raise ControllerError("command contains control or non-ASCII characters", 400)
        if line in ("?", "!", "~"):
            raise ControllerError("use the realtime endpoint for real-time commands", 400)
        streamer = self._streamer
        if streamer is None or self._conn.get("state") == "disconnected":
            raise ControllerError("not connected")
        self._console("tx", line, source)
        try:
            pending = streamer.send(line, source, timeout=timeout)
        except (StreamerError, TransportError) as e:
            raise ControllerError(str(e), 503) from e
        if not pending.wait(timeout):
            raise ControllerError("timed out waiting for the controller response", 504)
        return {"status": pending.status, "response": pending.response, "lines": pending.lines}

    def realtime(self, op: str) -> None:
        table = dict(REALTIME_COMMON)
        caps = self._caps or {}
        table.update(REALTIME_11 if caps.get("family") == "1.1" else REALTIME_09)
        data = table.get(op)
        if data is None:
            raise ControllerError(f"unsupported real-time command '{op}'", 400)
        if self._transport is None:
            raise ControllerError("not connected")
        try:
            self._write_raw(data)
        except TransportError as e:
            raise ControllerError(str(e), 503) from e
        if op != "status":
            self._console("rt", op, "ui")
        if op == "reset" and self._streamer:
            self._streamer.reset("reset")

    # ------------------------------------------------------------- reading

    def _on_line(self, raw: str) -> None:
        self._last_rx = time.monotonic()
        ln = classify(raw)
        if ln.kind == "blank":
            return
        if ln.kind == "status":
            self._on_status(ln.data)
            return
        self._console("rx", ln.raw)
        streamer = self._streamer
        if ln.kind in ("ok", "error"):
            if streamer:
                streamer.on_response(ln.kind, ln.raw)
        elif ln.kind == "banner":
            self._on_banner(ln.data["version"])
        elif ln.kind == "alarm":
            self.bus.publish("alarm", ln.data, retain=True)
        elif ln.kind == "feedback":
            self._on_feedback(ln.data)
            if streamer:
                streamer.on_info(ln.raw)
        elif streamer:
            streamer.on_info(ln.raw)

    def _on_banner(self, version: str) -> None:
        with self._lock:
            was_connected = self._conn.get("state") == "connected"
            self._caps = capabilities.for_version(version)
            self._wco = None
            self._conn = {**self._conn, "state": "connected", "firmware": version, "session": "reset"}
            streamer = self._streamer
        if streamer:
            streamer.reset("controller reset")
        self.bus.publish("caps", self._caps, retain=True)
        self.bus.publish("alarm", None, retain=True)
        self._publish_connection()
        if was_connected:
            self.bus.publish("controller.reset", {"version": version})

    def _on_status(self, st: dict[str, Any]) -> None:
        with self._lock:
            if self._conn.get("state") in ("connecting", "unresponsive") and self._transport is not None:
                # No banner: we attached to a live session (no DTR reset).
                caps = capabilities.for_status_format(st.get("format"))
                if caps:
                    self._caps = caps
                    self._conn = {**self._conn, "state": "connected", "firmware": None, "session": "attached"}
                    self.bus.publish("caps", caps, retain=True)
                    self._publish_connection()
            if "wco" in st:
                self._wco = st["wco"]
            if self._wco is not None:
                if "mpos" in st and "wpos" not in st:
                    st["wpos"] = [m - o for m, o in zip(st["mpos"], self._wco)]
                elif "wpos" in st and "mpos" not in st:
                    st["mpos"] = [w + o for w, o in zip(st["wpos"], self._wco)]
            elif "mpos" in st and "wpos" in st:
                st["wco"] = [m - w for m, w in zip(st["mpos"], st["wpos"])]
            changed = st["state"] != self._machine_state
            self._machine_state = st["state"]
        self.bus.publish("status", st, retain=True, volatile=True)
        if changed:
            self.bus.publish("machine.state", {"state": st["state"]})
            if st["state"] != "Alarm":
                self.bus.publish("alarm", None, retain=True)

    def _on_feedback(self, fb: dict[str, Any]) -> None:
        t = fb.get("type")
        if t == "modal":
            self.bus.publish("modal", fb["words"], retain=True)
        elif t in ("offset", "tlo", "probe"):
            key = fb.get("name") or t.upper()
            with self._lock:
                self._offsets[key] = fb["value"] if t != "probe" else {"value": fb["value"], "ok": fb["ok"]}
                offsets = dict(self._offsets)
            self.bus.publish("offsets", offsets, retain=True)
        elif t == "version" and self._caps is not None and self._caps.get("version") is None:
            caps = capabilities.for_version(fb["version"])
            if caps:
                with self._lock:
                    self._caps = caps
                    self._conn = {**self._conn, "firmware": caps["version"]}
                self.bus.publish("caps", caps, retain=True)
                self._publish_connection()

    # --------------------------------------------------------------- polling

    def _poll_loop(self) -> None:
        version_requested = False
        while not self._stop.wait(self.poll_interval):
            transport = self._transport
            if transport is None or not transport.is_open:
                version_requested = False
                continue
            state = self._conn.get("state")
            age = time.monotonic() - self._opened_at
            try:
                if state == "connecting":
                    if age < BANNER_WAIT_S:
                        continue  # let the bootloader finish; bytes would be lost
                    if age > UNRESPONSIVE_S and self._last_rx == 0.0:
                        with self._lock:
                            self._conn = {**self._conn, "state": "unresponsive"}
                        self._publish_connection()
                transport.write(b"?")
                if (
                    state == "connected"
                    and not version_requested
                    and self._caps is not None
                    and self._caps.get("version") is None
                    and self._machine_state in ("Idle", "Alarm")
                ):
                    version_requested = True
                    threading.Thread(target=self._request_version, daemon=True).start()
            except TransportError:
                continue

    def _request_version(self) -> None:
        try:
            self.command("$I", source="system", timeout=5)
        except ControllerError:
            pass
