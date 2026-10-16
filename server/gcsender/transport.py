"""Byte transports to the controller: pyserial or the built-in simulator.

A transport delivers complete lines (without line endings) to ``on_line`` from
its own thread and reports unexpected loss of the link via ``on_close``.
Callbacks are never invoked while the transport holds an internal lock, so the
consumer may write from inside a callback.
"""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from typing import Callable

from .simulator import GrblSimulator


class TransportError(Exception):
    pass


class Transport(ABC):
    def __init__(self) -> None:
        self.on_line: Callable[[str], None] = lambda line: None
        self.on_close: Callable[[str | None], None] = lambda reason: None

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @property
    def description(self) -> str:
        return type(self).__name__


class LineSplitter:
    """Accumulates bytes and yields complete lines (CR/LF tolerant)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        self._buf.extend(data)
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx]).rstrip(b"\r")
            del self._buf[: idx + 1]
            lines.append(raw.decode("ascii", "replace"))
        if len(self._buf) > 4096:  # garbage without newlines (e.g. wrong baud)
            lines.append(bytes(self._buf).decode("ascii", "replace"))
            self._buf.clear()
        return lines


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._ser = None
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closing = False

    @property
    def description(self) -> str:
        return f"{self.port}@{self.baud}"

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        import serial  # imported lazily so the simulator works without a port

        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baud
        ser.timeout = 0.1
        ser.write_timeout = 2
        try:
            ser.exclusive = True
        except (AttributeError, ValueError):  # not supported on this platform
            pass
        # Attempt to avoid the Arduino DTR auto-reset. Most OS drivers still
        # pulse DTR on open, so this is best-effort (see docs/PLAN.md §8).
        ser.dtr = False
        ser.rts = False
        try:
            ser.open()
        except (serial.SerialException, OSError) as e:
            raise TransportError(str(e)) from e
        self._ser = ser
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, name=f"serial-{self.port}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        import serial

        splitter = LineSplitter()
        reason = None
        ser = self._ser
        try:
            while not self._closing and ser is not None:
                data = ser.read(ser.in_waiting or 1)
                if data:
                    for line in splitter.feed(data):
                        self.on_line(line)
        except (serial.SerialException, OSError) as e:
            reason = str(e)
        if not self._closing:
            self._close_port()
            self.on_close(reason or "serial port closed")

    def _close_port(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - closing a dead port
                pass

    def close(self) -> None:
        self._closing = True
        self._close_port()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)

    def write(self, data: bytes) -> None:
        import serial

        with self._write_lock:
            if self._ser is None:
                raise TransportError("not connected")
            try:
                self._ser.write(data)
            except (serial.SerialException, OSError) as e:
                raise TransportError(str(e)) from e


class SimTransport(Transport):
    """Connects to a long-lived :class:`GrblSimulator`.

    The simulator outlives connections (like a real machine), so a reconnect
    can find it mid-job. ``reset_on_open`` emulates the DTR auto-reset.
    """

    def __init__(self, sim: GrblSimulator, reset_on_open: bool = True) -> None:
        super().__init__()
        self.sim = sim
        self.reset_on_open = reset_on_open
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._open = False
        self._thread: threading.Thread | None = None

    @property
    def description(self) -> str:
        return f"simulator GRBL {self.sim.firmware}"

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._queue = queue.Queue()
        self._open = True
        self._thread = threading.Thread(target=self._deliver, args=(self._queue,), name="sim-rx", daemon=True)
        self._thread.start()
        q = self._queue
        self.sim.on_output = q.put  # called under the simulator lock: just enqueue
        self.sim.open_port(reset=self.reset_on_open)

    def _deliver(self, q: "queue.Queue[str | None]") -> None:
        while True:
            line = q.get()
            if line is None:
                return
            self.on_line(line)

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self.sim.on_output = lambda line: None
        self._queue.put(None)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError("not connected")
        self.sim.feed(data)


def list_serial_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for p in list_ports.comports():
        ports.append({"device": p.device, "description": p.description or "", "hwid": p.hwid or ""})
    return ports
