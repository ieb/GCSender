"""Controller: owns the transport, detects the firmware, polls status, keeps
the machine model current and routes controller output to the streamer, the
event bus and the serial transcript.

Phase 1 scope (docs/PLAN.md): connection lifecycle, firmware detection and
re-attach, automatic ``$G``/``$#``/``$$``/``$I`` queries, settings warnings,
console with source tagging and error help, control operations, and soft
reset with restoration of G92 / tool length offset / modal state.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any, Callable

from .backlash import BacklashTracker
from . import capabilities
from .errors import ControllerError
from .events import EventBus
from .grbl_messages import explain_alarm, explain_error
from .jog import Jogger
from .logs import Transcript
from .machine import MachineModel, parse_modal
from .protocol import classify
from .store import SettingsStore
from .streamer import Streamer, StreamerError
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

BANNER_WAIT_S = 2.5  # Arduino bootloader + GRBL init after a DTR reset
UNRESPONSIVE_S = 3.0  # no status report for this long -> "unresponsive"
EXPECTED_RESET_WINDOW_S = 5.0

REALTIME_COMMON = {"status": b"?", "hold": b"!", "resume": b"~", "reset": b"\x18"}
REALTIME_11 = {"jogCancel": b"\x85", "door": b"\x84"}
REALTIME_09 = {"door": b"@"}

OFFSET_CHANGING = re.compile(r"G(?:10|92(?:\.1)?|43\.1|49|28\.1|30\.1)(?![\d.])")
MODAL_WORDS = re.compile(r"[GMTFS]")
SETTING_WRITE = re.compile(r"^\$\d+=")
IDLE_LIKE = ("Idle", "Alarm")
MOVING = ("Run", "Jog", "Home", "Hold")
FAST_POLL_S = 0.05
G43_1 = re.compile(r"G43\.1(?![\d.])")
G49 = re.compile(r"G49(?![\d.])")
Z_WORD = re.compile(r"Z([-+]?(?:\d+\.?\d*|\.\d+))")


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Controller:
    def __init__(
        self,
        bus: EventBus,
        make_transport: Callable[[str, int], Transport],
        settings: SettingsStore | None = None,
        transcript: Transcript | None = None,
        poll_interval: float | None = None,
    ):
        self.bus = bus
        self.settings = settings or SettingsStore(None)
        state_dir = self.settings.path.parent if self.settings.path is not None else None
        self.backlash = BacklashTracker(state_dir / "backlash.json" if state_dir is not None else None)
        self.transcript = transcript or Transcript(None)
        self._make_transport = make_transport
        self._poll_override = poll_interval
        self._lock = threading.RLock()
        self._transport: Transport | None = None
        self._streamer: Streamer | None = None
        self._conn: dict[str, Any] = {"state": "disconnected"}
        self._caps: dict[str, Any] | None = None
        self.model = MachineModel()
        self._opened_at = 0.0
        self._last_rx = 0.0
        self._last_status_at = 0.0
        self._wco: list[float] | None = None
        self._machine_state: str | None = None
        self._settings_dirty = False
        self._offsets_dirty = False
        self._refresh_pending = False
        self._modal_refresh_queued = False
        self._offsets_refresh_queued = False
        self._expect_reset: dict[str, Any] | None = None
        self._restore_pending: dict[str, Any] | None = None
        self._reset_on_connect_sent = False
        self._last_alarm_at = 0.0
        self._status_cond = threading.Condition()
        self._status_seq = 0
        self._fast_poll_until = 0.0
        self.busy: str | None = None  # e.g. "probing": console/UI/jog commands are refused
        self.stop_hooks: list[Callable[[], None]] = []  # called on Stop and Reset (abort routines)
        self.jogger: Any = None  # set by Jogger(controller)
        self.job_runner: Any = None  # set by JobRunner(controller, ...)
        self._tasks: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._stop = threading.Event()
        self._publish_connection()
        self.bus.publish("app.settings", self.settings.all(), retain=True)
        Jogger(self)
        self._worker = threading.Thread(target=self._work_loop, name="controller-tasks", daemon=True)
        self._worker.start()
        self._poller = threading.Thread(target=self._poll_loop, name="status-poll", daemon=True)
        self._poller.start()

    # ------------------------------------------------------------ publishing

    def _publish_connection(self) -> None:
        self.bus.publish("connection", dict(self._conn), retain=True)

    def _console(self, direction: str, line: str, source: str | None = None, **extra: Any) -> None:
        self.bus.publish("console", {"dir": direction, "line": line, "src": source, "t": time.time(), **extra})

    def _publish_settings(self) -> None:
        self.bus.publish("grbl.settings", self.model.settings_list(), retain=True)
        self._publish_warnings()

    def _publish_warnings(self) -> None:
        self.bus.publish("warnings", self.model.warnings(self._caps), retain=True)

    def _set_caps(self, caps: dict[str, Any] | None) -> None:
        self._caps = caps
        self.bus.publish("caps", caps, retain=True)
        self._publish_warnings()

    @property
    def caps(self) -> dict[str, Any] | None:
        return self._caps

    @property
    def connection(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._conn)

    @property
    def poll_interval(self) -> float:
        base = self._poll_override if self._poll_override is not None else self.settings.get("statusInterval") / 1000.0
        if time.monotonic() < self._fast_poll_until:
            return min(base, FAST_POLL_S)
        return base

    def boost_polling(self, seconds: float) -> None:
        """Poll status faster for a while (jogging, stopping)."""
        self._fast_poll_until = max(self._fast_poll_until, time.monotonic() + seconds)

    @property
    def status_seq(self) -> int:
        """Increments with every status report (lets callers detect a fresh report)."""
        return self._status_seq

    @property
    def machine_state(self) -> str | None:
        return self._machine_state

    def wait_status(self, pred: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any] | None:
        """Wait for a *new* status report satisfying ``pred``; None on timeout."""
        deadline = time.monotonic() + timeout
        with self._status_cond:
            seq = self._status_seq
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._status_cond.wait(left)
                if self._status_seq != seq:
                    seq = self._status_seq
                    st = self.model.status
                    if st is not None and pred(st):
                        return st

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        values = self.settings.update(changes)
        self.bus.publish("app.settings", values, retain=True)
        if self.job_runner is not None and any(k.startswith("backlash") for k in changes):
            try:
                self.job_runner.settings_changed()  # rebuild the loaded program's compensation
            except ControllerError as e:
                log.warning("backlash: cannot rebuild the loaded program: %s", e)
        return values

    # ------------------------------------------------------------ connection

    def connect(self, port: str, baud: int = 115200) -> dict[str, Any]:
        with self._lock:
            if self._transport is not None:
                raise ControllerError("already connected")
            transport = self._make_transport(port, baud)
            transport.on_line = self._on_line
            transport.on_close = self._on_transport_closed
            self._streamer = Streamer(self._write_line)
            self._transport = transport
            self._caps = None
            self._wco = None
            self._machine_state = None
            self.model.reset_all()
            self._refresh_pending = False
            self._expect_reset = None
            self._restore_pending = None
            self._reset_on_connect_sent = False
            self._conn = {"state": "connecting", "port": port, "baud": baud, "transport": transport.description}
            self._opened_at = time.monotonic()
            self._last_rx = 0.0
            self._last_status_at = 0.0
            try:
                transport.open()
            except TransportError as e:
                self._transport = None
                self._streamer = None
                self._conn = {"state": "disconnected", "port": port, "baud": baud, "error": str(e)}
                self._publish_connection()
                log.warning("cannot open %s: %s", port, e)
                raise ControllerError(f"cannot open {port}: {e}", 502) from e
            self._publish_connection()
        for t in ("status", "modal", "offsets", "grbl.settings", "alarm", "caps", "warnings"):
            self.bus.publish(t, None if t != "warnings" else [], retain=True, volatile=(t == "status"))
        try:
            self.update_settings({"lastPort": port, "lastBaud": baud})
        except ValueError:
            pass
        self._console("sys", f"opened {transport.description}")
        self.transcript.write("#", f"opened {transport.description}")
        log.info("opened %s", transport.description)
        return self.connection

    def disconnect(self, reason: str | None = None) -> None:
        if self.jogger is not None:
            self.jogger.reset()
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
        self.bus.publish("warnings", [], retain=True)
        msg = f"disconnected{': ' + reason if reason else ''}"
        self._console("sys", msg)
        self.transcript.write("#", msg)
        (log.warning if reason else log.info)(msg)

    def _on_transport_closed(self, reason: str | None) -> None:
        # Unexpected loss of the link (USB unplugged, etc.).
        self.disconnect(reason or "connection lost")

    def close(self) -> None:
        self._stop.set()
        self._tasks.put(None)
        self.disconnect()

    # ------------------------------------------------------------- writing

    def _write_raw(self, data: bytes) -> None:
        transport = self._transport
        if transport is None:
            raise TransportError("not connected")
        transport.write(data)

    def _write_line(self, data: bytes) -> None:
        self._write_raw(data)
        self.transcript.write(">", data.decode("ascii", "replace").rstrip("\n"))

    def command(
        self, line: str, source: str = "console", timeout: float = 30.0, *, follow_up: bool = True
    ) -> dict[str, Any]:
        """Send one line and wait for its ok/error.

        ``follow_up`` schedules the automatic ``$G``/``$#``/``$$`` refreshes the
        command implies (e.g. after G92 or ``$n=``).
        """
        line = line.strip()
        if not line:
            raise ControllerError("empty command", 400)
        if any(ord(c) < 0x20 or ord(c) > 0x7E for c in line):
            raise ControllerError("command contains control or non-ASCII characters", 400)
        if line in ("?", "!", "~"):
            raise ControllerError("use the realtime endpoint for real-time commands", 400)
        streamer = self._streamer
        if streamer is None or self._conn.get("state") in ("disconnected", "connecting"):
            raise ControllerError("not connected")
        if self.busy and source in ("console", "ui", "jog"):
            raise ControllerError(f"busy: {self.busy}")
        upper = re.sub(r"\s+", "", line.upper())
        if upper == "$C" and self._machine_state == "Check":
            # Leaving check mode resets GRBL; restore the parser state afterwards.
            self._arm_reset(restore=None)
        if "G10" in upper or "G92" in upper:
            self.backlash.note_zero_command(upper, (self.model.modal or {}).get("wcs"))
        self._console("tx", line, source)
        try:
            pending = streamer.send(line, source, timeout=timeout)
        except (StreamerError, TransportError) as e:
            raise ControllerError(str(e), 503) from e
        if not pending.wait(timeout):
            raise ControllerError("timed out waiting for the controller response", 504)
        if pending.status == "ok" and follow_up:
            self._after_command(upper, source)
        return {"status": pending.status, "response": pending.response, "lines": pending.lines}

    def _after_command(self, upper: str, source: str) -> None:
        # GRBL applies G43.1/G49 at parse time, but 0.9 refuses $# while moving:
        # track the tool length offset from acknowledged commands.
        if G49.search(upper):
            self.model.offsets["TLO"] = 0.0
        elif G43_1.search(upper):
            m = Z_WORD.search(upper)
            if m:
                units = (self.model.modal or {}).get("units", "G21")
                if "G20" in upper or ("G21" not in upper and units == "G20"):
                    self.model.offsets["TLO"] = float(m.group(1)) * 25.4
                else:
                    self.model.offsets["TLO"] = float(m.group(1))
        if SETTING_WRITE.match(upper) or upper.startswith("$RST"):
            self._schedule(self._refresh_settings)
        if OFFSET_CHANGING.search(upper) or upper.startswith("$RST"):
            self._offsets_dirty = True
            if self._machine_state in IDLE_LIKE:
                self._schedule_offsets_refresh()
        if source != "system" and not upper.startswith("$") and MODAL_WORDS.search(upper):
            self._schedule_modal_refresh()

    def realtime(self, op: str, restore: bool | None = None) -> None:
        table = dict(REALTIME_COMMON)
        caps = self._caps or {}
        table.update(REALTIME_11 if caps.get("family") == "1.1" else REALTIME_09)
        data = table.get(op)
        if data is None:
            raise ControllerError(f"unsupported real-time command '{op}'", 400)
        if self._transport is None:
            raise ControllerError("not connected")
        if op == "reset":
            if self.jogger is not None:
                self.jogger.reset()
            self._run_stop_hooks()
            self._arm_reset(restore)
        try:
            self._write_raw(data)
        except TransportError as e:
            raise ControllerError(str(e), 503) from e
        if op != "status":
            self._console("rt", op, "ui")
            self.transcript.write("!", op)
        elif self.settings.get("transcriptStatus"):
            self.transcript.write("!", "?")
        if op == "reset" and self._streamer:
            self._streamer.reset("reset")

    def control(self, op: str) -> dict[str, Any]:
        """Named control operations with capability checks."""
        caps = self._caps or {}
        if op == "home":
            if self.model.setting(22) != 1:
                raise ControllerError("homing is disabled on this machine ($22=0)")
            return self.command("$H", source="ui", timeout=180)
        if op == "unlock":
            return self.command("$X", source="ui")
        if op == "check":
            return self.command("$C", source="ui")
        if op == "sleep":
            if not caps.get("sleep"):
                raise ControllerError("sleep is not supported by this firmware")
            return self.command("$SLP", source="ui")
        raise ControllerError(f"unknown control operation '{op}'", 400)

    # --------------------------------------------------- reset and restore

    def _arm_reset(self, restore: bool | None) -> None:
        if restore is None:
            restore = bool(self.settings.get("restoreAfterReset"))
        plan = self._capture_restore() if restore else None
        self._expect_reset = {"plan": plan, "at": time.monotonic()}

    def _capture_restore(self) -> dict[str, Any] | None:
        modal = self.model.modal
        if not modal:
            return None
        g92 = self.model.derived_g92()
        if g92 is None:
            v = self.model.offsets.get("G92")
            g92 = list(v) if isinstance(v, list) else [0.0, 0.0, 0.0]
        return {
            "wcs": modal.get("wcs", "G54"),
            "units": modal.get("units", "G21"),
            "distance": modal.get("distance", "G90"),
            "plane": modal.get("plane", "G17"),
            "feedMode": modal.get("feedMode", "G94"),
            "tool": modal.get("tool"),
            "feed": modal.get("feed"),
            "g92": g92,
            "tlo": self.model.tlo(),
            "coords": self.model.offsets.get(modal.get("wcs", "G54")),
        }

    def restore_commands(self, plan: dict[str, Any], mpos: list[float] | None) -> list[str]:
        """Commands that re-establish ``plan`` after a reset (position unchanged)."""
        cmds = ["G21", plan["wcs"]]
        g92 = plan.get("g92") or [0.0, 0.0, 0.0]
        coords = plan.get("coords")
        if any(abs(v) > 1e-6 for v in g92) and mpos is not None and isinstance(coords, list):
            # After reset TLO is 0, so G92 Xv sets g92 = mpos - coords - v.
            v = [m - c - g for m, c, g in zip(mpos, coords, g92)]
            cmds.append("G92" + "".join(f"{a}{_fmt(x)}" for a, x in zip("XYZ", v)))
        if plan.get("tlo"):
            cmds.append(f"G43.1Z{_fmt(plan['tlo'])}")
        if plan.get("feed"):
            # $G reports the feed in mm/min (GRBL stores it metric); G21 is active here.
            cmds.append(f"F{_fmt(plan['feed'])}")
        if plan.get("tool"):
            cmds.append(f"T{int(plan['tool'])}")
        cmds += [plan["plane"], plan["feedMode"], plan["distance"], plan["units"]]
        return cmds

    def _restore(self, plan: dict[str, Any]) -> None:
        mpos = (self.model.status or {}).get("mpos")
        cmds = self.restore_commands(plan, mpos)
        self._console("sys", "restoring offsets and parser state after reset")
        log.info("restoring after reset: %s", " ".join(cmds))
        for c in cmds:
            try:
                r = self.command(c, source="system", timeout=10, follow_up=False)
                if r["status"] != "ok":
                    log.warning("restore command %s failed: %s", c, r["response"])
            except ControllerError as e:
                log.warning("restore aborted: %s", e)
                return
        self._refresh_modal()
        self._refresh_offsets()

    def safe_stop(self) -> dict[str, Any]:
        """Stop all motion as quickly as possible without an alarm (Esc / Stop).

        0.9: feed hold, wait until the hold has fully decelerated (position
        stable across two reports), then soft reset - no alarm, position kept -
        and restore offsets/parser state. 1.1 during a jog step: jog cancel (0x85).
        """
        if self._transport is None:
            raise ControllerError("not connected")
        self._run_stop_hooks()
        caps = self._caps or {}
        state = self._machine_state
        if caps.get("family") == "1.1" and state == "Jog":
            self._write_raw(b"\x85")
            self._console("rt", "jogCancel", "ui")
            self.transcript.write("!", "jogCancel")
            return {"method": "jog-cancel"}
        if state not in MOVING:
            return {"method": "none", "state": state}
        self.boost_polling(10)
        self._write_raw(b"!")
        self._console("rt", "hold", "ui")
        self.transcript.write("!", "hold (stop)")
        if not self._wait_hold_settled(timeout=10):
            raise ControllerError("the machine did not come to rest after feed hold; it is still held", 504)
        self._arm_reset(None)
        if self.jogger is not None:
            self.jogger.reset()
        self._write_raw(b"\x18")
        self._console("rt", "reset", "ui")
        self.transcript.write("!", "reset (stop)")
        if self._streamer:
            self._streamer.reset("reset")
        return {"method": "hold-reset"}

    def _run_stop_hooks(self) -> None:
        for hook in self.stop_hooks:
            try:
                hook()
            except Exception:  # pragma: no cover - a hook must never block a stop
                log.exception("stop hook failed")

    def _wait_hold_settled(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        last = None
        stable = 0
        hold_sub = (self._caps or {}).get("holdSubstate")
        while time.monotonic() < deadline:
            st = self.wait_status(lambda s: True, timeout=1.0)
            if st is None:
                continue
            if st["state"] == "Hold":
                if hold_sub:
                    if st.get("substate") == 0:
                        return True
                    continue
                mpos = st.get("mpos")
                if last is not None and mpos == last:
                    stable += 1
                    if stable >= 2:
                        return True
                else:
                    stable = 0
                last = mpos
            elif st["state"] in ("Idle", "Alarm"):
                return st["state"] == "Idle"
        return False

    # ------------------------------------------------------------- reading

    def _on_line(self, raw: str) -> None:
        self._last_rx = time.monotonic()
        ln = classify(raw)
        if ln.kind == "blank":
            return
        if ln.kind == "status":
            if self.settings.get("transcriptStatus"):
                self.transcript.write("<", ln.raw)
            self._on_status(ln.data)
            return
        self.transcript.write("<", ln.raw)
        streamer = self._streamer
        if ln.kind in ("ok", "error"):
            p = streamer.on_response(ln.kind, ln.raw) if streamer else None
            src = p.source if p else None
            if ln.kind == "error":
                info = explain_error(ln.data["text"], ln.data["code"])
                if src == "job" and p is not None:
                    self._console("tx", p.line, "job")  # job lines are not echoed; show the failing one
                self._console("rx", ln.raw, src, help=info["help"], title=info["title"])
                if src != "system":
                    log.info("controller error for %r: %s", p.line if p else None, ln.raw)
            elif src != "job":  # a job's thousands of ok's would bury everything else
                self._console("rx", ln.raw, src)
            if self._settings_dirty:
                self._settings_dirty = False
                self._publish_settings()
            return
        src = streamer.head_source() if streamer else None
        if ln.kind == "banner":
            self._console("rx", ln.raw, None)
            self._on_banner(ln.data["version"])
            return
        if ln.kind == "alarm":
            info = explain_alarm(ln.data["text"], ln.data["code"])
            alarm = {**ln.data, **info}
            self._console("rx", ln.raw, src, help=info["help"], title=info["title"])
            self._last_alarm_at = time.monotonic()
            self.bus.publish("alarm", alarm, retain=True)
            log.warning("ALARM: %s", info["title"])
            return
        self._console("rx", ln.raw, src)
        if ln.kind == "setting":
            self.model.set_setting(ln.data["id"], ln.data["value"], ln.data["desc"])
            self._settings_dirty = True
        elif ln.kind == "feedback":
            self._on_feedback(ln.data)
        if streamer:
            streamer.on_info(ln.raw)

    def _on_banner(self, version: str) -> None:
        now = time.monotonic()
        with self._lock:
            was = self._conn.get("state")
            expected = self._expect_reset if (
                self._expect_reset and now - self._expect_reset["at"] < EXPECTED_RESET_WINDOW_S
            ) else None
            self._expect_reset = None
            caps = capabilities.for_version(version)
            self._wco = None
            # "fresh": banner right after opening the port (DTR reset / power-up).
            session = "reset" if was in ("connected", "unresponsive") else "fresh"
            self._conn = {**self._conn, "state": "connected", "firmware": version, "session": session}
            # Reset clears G92, TLO and the parser state; EEPROM data survives.
            self.model.modal = None
            for k in ("G92", "TLO"):
                self.model.offsets.pop(k, None)
            self._refresh_pending = True
            self._restore_pending = expected["plan"] if expected else None
            streamer = self._streamer
        if streamer:
            streamer.reset("controller reset")
        self._set_caps(caps)
        if now - self._last_alarm_at > 1.0:
            # An "Abort during cycle" alarm is printed just before the banner: keep it.
            self.bus.publish("alarm", None, retain=True)
        self.bus.publish("modal", None, retain=True)
        self.bus.publish("offsets", dict(self.model.offsets), retain=True)
        self._publish_connection()
        if session == "reset":
            self.bus.publish("controller.reset", {"version": version, "expected": bool(expected)})
            if not expected:
                log.warning("controller reset unexpectedly")
        self.transcript.write("#", f"controller reset ({session})")

    def _on_status(self, st: dict[str, Any]) -> None:
        self._last_status_at = time.monotonic()
        republish = False
        with self._lock:
            conn_state = self._conn.get("state")
            if conn_state in ("connecting", "unresponsive") and self._transport is not None:
                if self._caps is None:
                    # No banner: we attached to a live session (no DTR reset).
                    caps = capabilities.for_status_format(st.get("format"))
                    if caps:
                        self._conn = {**self._conn, "state": "connected", "firmware": None, "session": "attached"}
                        self._refresh_pending = True
                        republish = True
                        self._caps = caps
                else:
                    self._conn = {**self._conn, "state": "connected"}
                    republish = True
            if "wco" in st:
                self._wco = st["wco"]
            if self._wco is not None:
                if "mpos" in st and "wpos" not in st:
                    st["wpos"] = [m - o for m, o in zip(st["mpos"], self._wco)]
                elif "wpos" in st and "mpos" not in st:
                    st["mpos"] = [w + o for w, o in zip(st["wpos"], self._wco)]
                st.setdefault("wco", list(self._wco))
            elif "mpos" in st and "wpos" in st:
                st["wco"] = [m - w for m, w in zip(st["mpos"], st["wpos"])]
            prev = self._machine_state
            state = st["state"]
            self._machine_state = state
            self.model.status = st
            self.backlash.observe_mpos(st.get("mpos"))
            with self._status_cond:
                self._status_seq += 1
                self._status_cond.notify_all()
            restore = None
            if self._restore_pending is not None and state in IDLE_LIKE:
                restore, self._restore_pending = self._restore_pending, None
            refresh = self._refresh_pending and state in IDLE_LIKE
            if refresh:
                self._refresh_pending = False
        if republish:
            if self._caps is not None and self.bus.retained("caps") != self._caps:
                self._set_caps(self._caps)
            self._publish_connection()
        self.bus.publish("status", st, retain=True, volatile=True)
        if restore is not None:
            if state == "Idle":
                self._schedule(lambda: self._restore(restore))
            else:
                self._console("sys", "offsets not restored: controller is in alarm (position may be lost)")
                log.warning("not restoring offsets after reset: alarm")
        if refresh:
            self._schedule(self._refresh_all)
        elif self._refresh_pending and prev is None:
            self._schedule_modal_refresh()  # $G works in any state
        if state != prev:
            self.bus.publish("machine.state", {"state": state, "previous": prev})
            if state != "Alarm":
                self.bus.publish("alarm", None, retain=True)
            if state == "Idle" and prev is not None:
                self._schedule_modal_refresh()
                if self._offsets_dirty:
                    self._schedule_offsets_refresh()

    def _on_feedback(self, fb: dict[str, Any]) -> None:
        t = fb.get("type")
        if t == "modal":
            self.model.modal = parse_modal(fb["words"])
            self.bus.publish("modal", self.model.modal, retain=True)
        elif t in ("offset", "tlo", "probe"):
            key = fb.get("name") or {"tlo": "TLO", "probe": "PRB"}[t]
            with self._lock:
                old = self.model.offsets.get(key)
                self.model.offsets[key] = fb["value"] if t != "probe" else {"value": fb["value"], "ok": fb["ok"]}
                offsets = dict(self.model.offsets)
            if t == "offset":
                self.backlash.on_offsets(key, old, fb["value"])  # a zero changed: remember the side
            self.bus.publish("offsets", offsets, retain=True)
        elif t == "version":
            self.model.build = {"version": fb["version"], "info": fb.get("info", "")}
            self.bus.publish("build", self.model.build, retain=True)
            if self._caps is not None and self._caps.get("version") is None:
                caps = capabilities.for_version(fb["version"])
                if caps:
                    with self._lock:
                        self._conn = {**self._conn, "firmware": caps["version"]}
                    self._set_caps(caps)
                    self._publish_connection()

    # ------------------------------------------------------ background tasks

    def _schedule(self, fn: Callable[[], None]) -> None:
        self._tasks.put(fn)

    def _schedule_modal_refresh(self) -> None:
        if not self._modal_refresh_queued:
            self._modal_refresh_queued = True
            self._schedule(self._refresh_modal)

    def _schedule_offsets_refresh(self) -> None:
        if not self._offsets_refresh_queued:
            self._offsets_refresh_queued = True
            self._schedule(self._refresh_offsets)

    def _work_loop(self) -> None:
        while True:
            fn = self._tasks.get()
            if fn is None or self._stop.is_set():
                return
            try:
                fn()
            except ControllerError as e:
                log.debug("background task failed: %s", e)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("background task crashed")

    def _system(self, line: str, timeout: float = 10) -> dict[str, Any] | None:
        if self._transport is None:
            return None
        try:
            return self.command(line, source="system", timeout=timeout)
        except ControllerError as e:
            log.debug("%s failed: %s", line, e)
            return None

    def sync_modal(self) -> dict[str, Any] | None:
        """Query $G now (works in any state) so the caller sees the parser state
        after everything already sent; used before restoring modal state."""
        self._system("$G", timeout=5)
        return self.model.modal

    def _refresh_modal(self) -> None:
        self._modal_refresh_queued = False
        self._system("$G")

    def _refresh_offsets(self) -> None:
        self._offsets_refresh_queued = False
        if self._machine_state not in IDLE_LIKE:
            self._offsets_dirty = True
            return
        r = self._system("$#")
        if r and r["status"] == "ok":
            self._offsets_dirty = False

    def _refresh_settings(self) -> None:
        if self._machine_state not in IDLE_LIKE:
            return
        self._system("$$")

    def refresh_settings(self) -> None:
        if self._machine_state not in IDLE_LIKE:
            raise ControllerError("settings can only be read while Idle or in Alarm")
        r = self._system("$$")
        if not r:
            raise ControllerError("could not read settings", 503)

    def _refresh_all(self) -> None:
        self._refresh_modal()
        self._refresh_settings()
        self._refresh_offsets()
        if self.model.build is None:
            self._system("$I")

    def write_settings(self, values: dict[int, float]) -> list[dict[str, Any]]:
        """Write GRBL ``$n=value`` settings one by one (Idle/Alarm only)."""
        if self._machine_state not in IDLE_LIKE:
            raise ControllerError("settings can only be changed while Idle or in Alarm")
        results = []
        for sid, value in sorted(values.items()):
            line = f"${sid}={_fmt(value)}"
            r = self.command(line, source="ui", timeout=10, follow_up=False)
            results.append({"id": sid, "line": line, "status": r["status"], "response": r["response"]})
        self._system("$$")  # synchronous, so the caller sees the new values
        return results

    # --------------------------------------------------------------- polling

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            transport = self._transport
            if transport is None or not transport.is_open:
                continue
            state = self._conn.get("state")
            now = time.monotonic()
            age = now - self._opened_at
            try:
                if state == "connecting":
                    if age < BANNER_WAIT_S:
                        continue  # let the bootloader finish; bytes would be lost
                    if self.settings.get("resetOnConnect") and not self._reset_on_connect_sent:
                        self._reset_on_connect_sent = True
                        self._write_raw(b"\x18")
                        self.transcript.write("!", "reset (on connect)")
                        continue
                    if age > BANNER_WAIT_S + UNRESPONSIVE_S and self._last_rx == 0.0:
                        self._set_unresponsive()
                elif state == "connected" and self._last_status_at and now - self._last_status_at > max(
                    UNRESPONSIVE_S, 10 * self.poll_interval
                ):
                    self._set_unresponsive()
                transport.write(b"?")
            except TransportError:
                continue

    def _set_unresponsive(self) -> None:
        with self._lock:
            if self._conn.get("state") == "unresponsive":
                return
            self._conn = {**self._conn, "state": "unresponsive"}
        self._publish_connection()
        log.warning("controller is not responding to status queries")
