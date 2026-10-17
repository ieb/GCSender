"""Job runner (docs/PLAN.md §5.3).

Streams the active variant's cleaned lines through the character-counting
streamer (pipelined: it never waits for an ``ok`` before sending the next
line while the 127-byte RX budget allows), tracks sent/acknowledged lines,
and publishes progress on the retained ``job`` event, so a reloaded SPA sees
exactly where the job is.

* error: feed hold at once and wait for Continue or Stop (or carry on if
  "ignore errors" is set); an ALARM always ends the job.
* M6: wait until the machine is Idle, run the tool-change commands, then
  pause in ``toolchange`` (jog/probe are allowed) until Continue. GRBL does
  not accept M6, so the M6 word is not sent.
* Pause/Resume = feed hold/cycle start; Stop = the safe stop (hold, settle,
  reset, restore offsets; no alarm).
* Start from line N: restore the parser state at N and enter safely:
  raise to safe Z, rapid to the XY before N, spindle (+ dwell) and coolant,
  feed down to the Z before N, then continue with line N.
* Feed override: 1.1 real-time bytes; 0.9 rewrites F on outgoing motion
  lines (takes effect after the lines already in GRBL's buffers).
"""

from __future__ import annotations

import logging
import re
from collections import deque
import threading
import time
from typing import Any

from ..errors import ControllerError
from ..streamer import StreamerError
from .analyze import Scanner, state_before
from .estimate import Machine
from .library import M6_RE, JobError, JobLibrary, Variant

log = logging.getLogger(__name__)

PUBLISH_INTERVAL = 0.1
F_WORD = re.compile(r"F[-+]?(?:\d+\.?\d*|\.\d+)")
ACTIVE = ("running", "paused", "error", "toolchange", "starting")


def _f(v: float, mm: bool = True) -> str:
    s = f"{v:.{3 if mm else 4}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Stopped(Exception):
    pass


class JobRunner:
    def __init__(self, controller: Any, maps: Any, library: JobLibrary):
        self.ctl = controller
        self.maps = maps
        self.library = library
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self.meta: dict[str, Any] | None = None
        self.original: Variant | None = None
        self.transformed: Variant | None = None
        self.use_heightmap = False
        self.state = "idle"  # idle | ready | starting | running | paused | error | toolchange | done | stopped | failed
        self.message: str | None = None
        self.start_line = 0
        self.sent = -1  # highest line index sent (or skipped)
        self.acked = -1  # highest line index completed, all before it complete too
        self.errors: list[dict[str, Any]] = []
        self.error_count = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.toolchange: dict[str, Any] | None = None
        self.held_error: dict[str, Any] | None = None
        self.override = {"feed": 100, "rapid": 100, "spindle": 100}
        self._stop = False
        self._continue = threading.Event()
        self._last_pub = 0.0
        self._ov_force_f = False
        self._scanner: Scanner | None = None
        # GRBL acks a line when it enters the planner, up to ~17 moves before it
        # runs. Replay the planner: each acked line starts when the previous one
        # ends and takes its estimated time; the clock stops during a hold.
        self._timeline: deque[tuple[int, float]] = deque()  # (line, estimated finish)
        self._tl_end = 0.0
        self._exec = -1
        self._frozen_at: float | None = None
        self._paused_s = 0.0
        self._hold_t: float | None = None
        self._stop_line: int | None = None
        controller.stop_hooks.append(self._on_stop_hook)
        controller.job_runner = self
        self._publish(force=True)

    # ---------------------------------------------------------------- state

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def variant(self) -> Variant | None:
        if self.use_heightmap and self.transformed is not None:
            return self.transformed
        return self.original

    def executing(self) -> int:
        """Best guess of the last line GRBL has finished executing."""
        if self.state not in ("running", "paused", "error", "starting"):
            return self.acked
        now = self._frozen_at if self._frozen_at is not None else time.monotonic()
        tl = self._timeline
        planner = int((self.ctl.caps or {}).get("plannerBlocks", 17)) + 1
        while tl and (tl[0][1] <= now or len(tl) > planner):
            self._exec = tl.popleft()[0]
        return max(self._exec, self.start_line - 1)

    def _freeze(self) -> None:
        if self._frozen_at is None:
            self._frozen_at = time.monotonic()

    def _thaw(self) -> None:
        if self._frozen_at is None:
            return
        shift = time.monotonic() - self._frozen_at
        self._timeline = deque((i, t + shift) for i, t in self._timeline)
        self._tl_end += shift
        self._frozen_at = None

    def _eta(self) -> dict[str, Any]:
        v = self.variant
        if v is None:
            return {"elapsed": None, "eta": None, "total": None}
        ex = self.executing()
        at = v.cum[max(0, min(ex + 1, len(v.cum) - 1))]
        total_remaining = v.total - at
        elapsed = None
        if self.started_at is not None:
            elapsed = (self.finished_at or time.time()) - self.started_at
        eta = total_remaining
        if elapsed and self.state in ("running", "paused", "error", "toolchange"):
            done_est = at - v.cum[self.start_line]
            worked = elapsed - self._paused_s
            if done_est > 5 and worked > 5:
                eta = total_remaining * min(3.0, max(0.33, worked / done_est))
        if self.state in ("done", "stopped", "failed", "idle", "ready"):
            eta = None if self.state != "ready" else v.total
        return {"elapsed": None if elapsed is None else round(elapsed, 1), "eta": None if eta is None else round(eta, 1), "total": round(v.total, 1)}

    def snapshot(self) -> dict[str, Any]:
        v = self.variant
        hm_digest = self.maps.hm.digest() if self.maps is not None else None
        return {
            "job": self.meta,
            "variant": v.summary() if v else None,
            "original": self.original.summary() if self.original else None,
            "heightmap": {
                "enabled": self.use_heightmap,
                "stale": bool(self.use_heightmap and self.transformed and self.transformed.map_digest != hm_digest),
            },
            "state": self.state,
            "message": self.message,
            "startLine": self.start_line,
            "sent": self.sent,
            "acked": self.acked,
            "executing": self.executing(),
            "errors": self.errors[-50:],
            "errorCount": self.error_count,
            "heldError": self.held_error,
            "toolchange": self.toolchange,
            "override": self.override,
            "overrideMode": (self.ctl.caps or {}).get("overrides", "software-feed"),
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            **self._eta(),
        }

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_pub < PUBLISH_INTERVAL:
            return
        self._last_pub = now
        self.ctl.bus.publish("job", self.snapshot(), retain=True)

    def _set(self, state: str, message: str | None = None) -> None:
        self.state = state
        self.message = message
        self._publish(force=True)

    # ------------------------------------------------------------- loading

    def _machine(self) -> Machine:
        return Machine.from_settings(lambda i: self.ctl.model.setting(i))

    def _max_line(self) -> int:
        return int((self.ctl.caps or {}).get("lineLength", 80)) - 1

    def select(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self.active:
                raise ControllerError("a job is running")
            meta, text = self.library.get(job_id)
            self.meta = meta
            self.original = Variant.build("original", text, self._machine(), self._max_line())
            self.transformed = None
            if self.use_heightmap:
                self._build_transformed()
            self._reset_progress()
            self._set("ready")
            return self.snapshot()

    def unload(self) -> None:
        with self._lock:
            if self.active:
                raise ControllerError("a job is running")
            self.meta = self.original = self.transformed = None
            self._reset_progress()
            self._set("idle")

    def _reset_progress(self) -> None:
        self.sent = self.acked = -1
        self.start_line = 0
        self.errors = []
        self.error_count = 0
        self.started_at = self.finished_at = None
        self.toolchange = None
        self.held_error = None
        self._timeline.clear()
        self._tl_end = 0.0
        self._exec = -1
        self._frozen_at = None
        self._paused_s = 0.0  # time waiting for the user (tool change, error prompt)

    def _build_transformed(self) -> None:
        assert self.original is not None
        text = "\n".join(self.original.raw) + "\n"
        try:
            result = self.maps.transform(text, float(self.ctl.settings.get("arcTolerance")))
        except ValueError as e:  # MapError
            raise ControllerError(f"heightmap: {e}") from None
        self.transformed = Variant.build(
            "transformed", result["gcode"], self._machine(), self._max_line(),
            map_digest=self.maps.hm.digest(), stats=result["stats"],
        )

    def set_heightmap(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            if self.active:
                raise ControllerError("a job is running")
            if enabled and self.original is not None:
                self._build_transformed()
            self.use_heightmap = enabled
            if not enabled:
                self.transformed = None
            self._reset_progress()
            self._set("ready" if self.original else "idle")
            return self.snapshot()

    def text(self) -> str:
        v = self.variant
        if v is None:
            raise JobError("no job loaded")
        return "\n".join(v.raw) + "\n"

    # ------------------------------------------------------------- control

    def start(self, from_line: int = 0) -> dict[str, Any]:
        with self._lock:
            if self.active:
                raise ControllerError("a job is already running")
            if self.original is None:
                raise ControllerError("no job loaded")
            if self.ctl.connection.get("state") != "connected":
                raise ControllerError("not connected")
            if self.ctl.machine_state != "Idle":
                raise ControllerError(f"a job needs Idle (machine is {self.ctl.machine_state})")
            if self.ctl.busy:
                raise ControllerError(f"busy: {self.ctl.busy}")
            if self.use_heightmap:
                if self.transformed is None or self.transformed.map_digest != self.maps.hm.digest():
                    self._build_transformed()  # the map changed since: recompute
            v = self.variant
            assert v is not None
            if v.too_long:
                raise ControllerError(f"line {v.too_long[0] + 1} is longer than GRBL accepts ({self._max_line()} characters)")
            if not 0 <= from_line < len(v.cmds):
                raise ControllerError("start line out of range", 400)
            entry: list[str] = []
            sc = Scanner()
            if from_line > 0:
                entry = self._entry_lines(v, from_line)
            self._scanner = sc
            for i in range(from_line):
                for _ in sc.feed_line(i, v.cmds[i]):
                    pass
            self._reset_progress()
            self.start_line = from_line
            self.sent = self.acked = from_line - 1
            self._stop = False
            self._stop_line = None
            self._continue.clear()
            self.started_at = time.time()
            self.ctl.busy = "job"
            self._set("starting")
            self._thread = threading.Thread(target=self._run, args=(v, from_line, entry), name="job", daemon=True)
            self._thread.start()
            return self.snapshot()

    def pause(self) -> None:
        if self.state == "running" and self.ctl.machine_state == "Hold":
            raise ControllerError("already holding (e.g. an M0 in the program): use Resume")
        if self.state != "running":
            raise ControllerError(f"cannot pause while {self.state}")
        self.ctl.realtime("hold")
        self._freeze()
        self._hold_t = time.time()
        self._set("paused")

    def resume(self) -> None:
        if self.state == "paused":
            self.ctl.realtime("resume")
            self._thaw()
            if self._hold_t is not None:
                self._paused_s += time.time() - self._hold_t
                self._hold_t = None
            self._set("running")
        elif self.state in ("error", "toolchange"):
            self._continue.set()
        elif self.state == "running" and self.ctl.machine_state in ("Hold", "Door"):
            self.ctl.realtime("resume")  # M0/M1 program pause, door, or a hold from elsewhere
        else:
            raise ControllerError(f"cannot resume while {self.state}")

    def stop(self) -> dict[str, Any]:
        if not self.active:
            raise ControllerError("no job is running")
        self._mark_stop()
        self._stop = True
        self._continue.set()
        if self.state == "toolchange":
            return {"method": "none"}  # nothing is moving
        return self.ctl.safe_stop()

    def _mark_stop(self) -> None:
        if self._stop_line is None:
            # 1-based number of the line that was (probably) executing
            self._stop_line = min(self.executing() + 2, len(self.variant.cmds) if self.variant else 0)

    def _on_stop_hook(self) -> None:
        # Stop / Reset from anywhere ends a running job.
        if self.active:
            self._mark_stop()
            self._stop = True
            self._continue.set()

    def set_override(self, kind: str, value: int) -> dict[str, Any]:
        caps = self.ctl.caps or {}
        realtime = caps.get("overrides") == "realtime"
        if kind not in ("feed", "rapid", "spindle"):
            raise ControllerError("override must be feed, rapid or spindle", 400)
        if not realtime and kind != "feed":
            raise ControllerError(f"{kind} override needs GRBL 1.1", 400)
        if kind == "rapid":
            if value not in (25, 50, 100):
                raise ControllerError("rapid override is 25, 50 or 100 %", 400)
            if realtime:
                self.ctl._write_raw({100: b"\x95", 50: b"\x96", 25: b"\x97"}[value])
        else:
            value = int(max(10, min(200, value)))
            if realtime:
                self._realtime_override(kind, value)
            else:
                self._ov_force_f = True
        self.override[kind] = value
        self._publish(force=True)
        return self.override

    def _realtime_override(self, kind: str, value: int) -> None:
        reset, up10, down10, up1, down1 = (
            (b"\x90", b"\x91", b"\x92", b"\x93", b"\x94") if kind == "feed" else (b"\x99", b"\x9a", b"\x9b", b"\x9c", b"\x9d")
        )
        seq = [reset]
        delta = value - 100
        tens, ones = divmod(abs(delta), 10)
        seq += [up10 if delta > 0 else down10] * tens + [up1 if delta > 0 else down1] * ones
        self.ctl._write_raw(b"".join(seq))

    # ------------------------------------------------------------ the run

    def _entry_lines(self, v: Variant, n: int) -> list[str]:
        s = state_before(v.cmds, n)
        if s["g92"]:
            raise ControllerError("the program uses G92 before this line; start from the beginning")
        if any(p is None for p in s["pos"]):
            raise ControllerError("the position before this line is not known (no absolute XYZ yet)")
        x, y, z = s["pos"]
        safe = float(self.ctl.settings.get("safeZ"))
        st = self.ctl.model.status or {}
        wz = (st.get("wpos") or [None, None, None])[2]
        same_wcs = (self.ctl.model.modal or {}).get("wcs", "G54") == s["wcs"]
        lines = [f"G21G90{s['plane']}{s['wcs']}"]
        if wz is None or wz < safe or not same_wcs:  # WPos is relative to the active WCS
            lines.append(f"G0Z{_f(safe)}")
        lines.append(f"G0X{_f(x)}Y{_f(y)}")
        if s["spindle"] in ("M3", "M4"):
            lines.append(f"{s['spindle']}S{_f(s['speed'])}")
            dwell = float(self.ctl.settings.get("spindleDwell"))
            if dwell > 0:
                lines.append(f"G4P{_f(dwell)}")
        lines += sorted(s["coolant"], reverse=True)
        feed_mm = s["feed"] * (25.4 if s["units"] == "G20" else 1.0)
        plunge_max = float(self.ctl.settings.get("entryPlungeFeed"))
        plunge = min(feed_mm, plunge_max) if feed_mm > 0 else plunge_max
        lines.append(f"G1Z{_f(z)}F{_f(plunge)}")
        mode = s["units"] + s["distance"]
        if s["motion"] in ("G0", "G1", "G2", "G3"):
            mode += s["motion"]
        if s["feed"]:
            mode += "F" + _f(s["feed"], s["units"] == "G21")
        lines.append(mode)
        return lines

    def _send_blocking(self, line: str) -> None:
        r = self.ctl.command(line, source="job", timeout=120, follow_up=False)
        if r["status"] != "ok":
            raise ControllerError(f"{line}: {r['response']}")

    def _commands_setting(self, key: str) -> list[str]:
        text = str(self.ctl.settings.get(key) or "")
        return [c.strip() for c in text.replace(";", "\n").splitlines() if c.strip()]

    def _run(self, v: Variant, start: int, entry: list[str]) -> None:
        streamer = self.ctl._streamer
        inflight: list[tuple[int, Any]] = []
        try:
            for c in self._commands_setting("jobStartCommands"):
                self._send_blocking(c)
            for c in entry:
                self._send_blocking(c)
            self._set("running")
            n = len(v.cmds)
            for i in range(start, n):
                self._check_stop()
                self._collect(inflight)
                if self.held_error is not None:
                    self._wait_continue(inflight)
                cmd = v.cmds[i]
                if not cmd:
                    inflight.append((i, None))
                    self.sent = i
                    continue
                if M6_RE.search(cmd):
                    self._tool_change(i, cmd, inflight)
                    continue
                cmd = self._apply_override(cmd)
                try:
                    p = streamer.send(cmd, "job", timeout=600)
                except StreamerError as e:
                    if self._stop:
                        raise Stopped() from None
                    raise ControllerError(str(e)) from None
                inflight.append((i, p))
                self.sent = i
                self._publish()
            # Drain: wait for every acknowledgement and for motion to finish.
            while inflight:
                self._check_stop()
                self._collect(inflight, wait=0.2)
                if self.held_error is not None:
                    self._wait_continue(inflight)
            self._wait_idle()
            end = self._commands_setting("jobEndCommands")
            for c in end:
                self._send_blocking(c)
            if end:
                self._wait_idle()
            self.finished_at = time.time()
            self._set("done", f"finished in {self.finished_at - (self.started_at or self.finished_at):.0f} s")
        except Stopped:
            self.finished_at = time.time()
            self._set("stopped", f"stopped at line {self._stop_line or self.acked + 2}")
        except (ControllerError, StreamerError) as e:
            self.finished_at = time.time()
            if self._stop:
                self._set("stopped", f"stopped at line {self._stop_line or self.acked + 2}")
            else:
                self._set("failed", str(e))
        except Exception as e:  # pragma: no cover - defensive
            log.exception("job runner crashed")
            self.finished_at = time.time()
            self._set("failed", f"internal error: {e}")
        finally:
            self.ctl.busy = None
            self.ctl._fast_poll_until = 0.0
            self.ctl._schedule_modal_refresh()
            self._publish(force=True)

    def _check_stop(self) -> None:
        if self._stop:
            raise Stopped()
        if self.ctl.machine_state == "Alarm":
            raise ControllerError("ALARM: the job was stopped")
        if self.ctl.connection.get("state") != "connected":
            raise ControllerError("connection lost")

    def _collect(self, inflight: list[tuple[int, Any]], wait: float = 0.0) -> None:
        """Pop completed lines from the front, in order."""
        if wait and inflight and inflight[0][1] is not None:
            inflight[0][1].wait(wait)
        while inflight:
            i, p = inflight[0]
            if p is not None:
                if not p.event.is_set():
                    break
                if p.status == "ok":
                    v = self.variant
                    now = time.monotonic()
                    self._tl_end = max(now, self._tl_end) + (v.seconds[i] if v else 0.0)
                    self._timeline.append((i, self._tl_end))
                if p.status == "aborted":
                    raise Stopped() if self._stop else ControllerError(p.response or "aborted")
                if p.status == "error":
                    self._on_error(i, p)
            inflight.pop(0)
            self.acked = i
        self._publish()

    def _on_error(self, i: int, p: Any) -> None:
        err = {"line": i, "command": p.line, "response": p.response}
        self.errors.append(err)
        self.errors = self.errors[-200:]
        self.error_count += 1
        if self.ctl.machine_state == "Alarm":
            raise ControllerError(f"ALARM after line {i + 1}: {p.response}")
        if self.ctl.settings.get("ignoreErrors"):
            return
        if self.held_error is None:
            self.ctl.realtime("hold")  # stop motion while the user decides
            self._freeze()
            self.held_error = err
            self._set("error", f"line {i + 1}: {p.response}")

    def _wait_continue(self, inflight: list[tuple[int, Any]]) -> None:
        self._continue.clear()
        t_wait = time.time()
        while not self._continue.wait(0.2):
            self._check_stop()
            self._collect(inflight)
        self._paused_s += time.time() - t_wait
        self._check_stop()
        self.held_error = None
        self.ctl.realtime("resume")
        self._thaw()
        self._set("running")

    def _wait_idle(self) -> None:
        while True:
            self._check_stop()
            self._publish()
            st = self.ctl.wait_status(lambda s: True, timeout=1.0)
            if st is not None and st["state"] == "Idle" and not self.ctl._streamer.inflight:
                self._timeline.clear()
                self._exec = self.acked
                return

    def _tool_change(self, i: int, cmd: str, inflight: list[tuple[int, Any]]) -> None:
        # Let everything before the M6 finish.
        while inflight:
            self._check_stop()
            self._collect(inflight, wait=0.2)
            if self.held_error is not None:
                self._wait_continue(inflight)
        self._wait_idle()
        rest = M6_RE.sub("", cmd)
        tool = None
        m = re.search(r"T(\d+)", rest)
        if m:
            tool = int(m.group(1))
        if rest and re.search(r"[A-SU-Z]", rest):  # anything besides a T word
            self._send_blocking(rest)
        mode = self.ctl.settings.get("toolChangeMode")
        if mode == "pause":
            for c in self._commands_setting("toolChangeCommands"):
                self._send_blocking(c)
            self._wait_idle()
            self.toolchange = {"line": i, "tool": tool}
            self.ctl.busy = None  # the user may jog, probe Z and zero now
            self._continue.clear()
            self._set("toolchange", f"change to tool {tool if tool is not None else '?'} and press Continue")
            t_wait = time.time()
            while not self._continue.wait(0.2):
                self._check_stop()
            self._paused_s += time.time() - t_wait
            self._check_stop()
            self.ctl.busy = "job"
            self.toolchange = None
            self._wait_idle()  # e.g. a jog step the user made is still finishing
            self._set("running")
        self.sent = self.acked = i
        self._publish()

    def _apply_override(self, cmd: str) -> str:
        """Track the program's modal state; on 0.9 rewrite F for the override."""
        sc = self._scanner
        if sc is None:
            return cmd
        before_feed = sc.m.feed
        for _ in sc.feed_line(-1, cmd):
            pass
        if (self.ctl.caps or {}).get("overrides") == "realtime":
            return cmd
        pct = self.override["feed"]
        motion = sc.m.st.motion
        is_feed_move = motion in ("1", "2", "3") and re.search(r"[XYZIJKR]", cmd)
        if pct == 100 and not self._ov_force_f:
            return cmd
        if not (is_feed_move or "F" in cmd):
            return cmd
        feed = sc.m.feed or before_feed
        if not feed:
            return cmd
        new_f = "F" + _f(feed * pct / 100.0, sc.m.st.mm)
        out = F_WORD.sub(new_f, cmd) if "F" in cmd else cmd + new_f
        if len(out) > self._max_line():
            return cmd
        if is_feed_move:
            self._ov_force_f = False
        return out
