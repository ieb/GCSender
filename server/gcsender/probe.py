"""Probe routines (docs/PLAN.md §5.5): Z probe, probe-input test, heightmap run.

All routines run on the server, so they keep going if the browser reloads or
closes; progress is published on the ``probe`` event (retained) and map points
through the ``heightmap`` event. While a routine runs the controller is
``busy``: console, UI and jog commands are refused. Real-time Hold/Resume/Stop
stay available; Stop (or a reset) aborts the routine.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .errors import ControllerError
from .heightmap.model import MapError
from .heightmap.service import HeightmapService
from .machine import WCS_NAMES
from .protocol import parse_feedback

log = logging.getLogger(__name__)


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class ProbeFailed(Exception):
    pass


class Aborted(Exception):
    pass


class Prober:
    def __init__(self, controller: Any, maps: HeightmapService):
        self.ctl = controller
        self.maps = maps
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._abort = False
        self.status: dict[str, Any] = {"state": "idle"}
        controller.stop_hooks.append(self.abort)
        self._publish()

    # ------------------------------------------------------------ plumbing

    def _publish(self, **changes: Any) -> None:
        self.status = {**self.status, **changes}
        self.ctl.bus.publish("probe", self.status, retain=True)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def abort(self) -> None:
        self._abort = True

    def _start(self, kind: str, target: Any, *args: Any) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise ControllerError("a probe routine is already running")
            if self.ctl.connection.get("state") != "connected":
                raise ControllerError("not connected")
            if self.ctl.machine_state != "Idle":
                raise ControllerError(f"probing needs Idle (machine is {self.ctl.machine_state})")
            if self.ctl.busy:
                raise ControllerError(f"busy: {self.ctl.busy}")
            self._abort = False
            self.ctl.busy = "probing"
            self._publish(state="running", kind=kind, started=time.time(), message=None, index=0, total=None, result=None)
            self._thread = threading.Thread(target=self._run, args=(kind, target, *args), name=f"probe-{kind}", daemon=True)
            self._thread.start()
        return {"started": kind}

    def _run(self, kind: str, target: Any, *args: Any) -> None:
        modal = None
        try:
            modal = self.ctl.sync_modal()
            result = target(*args)
            self._publish(state="done", result=result, message=None)
        except Aborted:
            self._publish(state="aborted", message="stopped by the user")
            modal = None  # the stop's reset restores the parser state
        except ProbeFailed as e:
            self._publish(state="failed", message=str(e))
        except (ControllerError, MapError) as e:
            self._publish(state="failed", message=str(e))
            if self._abort:
                modal = None
        except Exception as e:  # pragma: no cover - defensive
            log.exception("probe routine crashed")
            self._publish(state="failed", message=f"internal error: {e}")
        finally:
            if kind == "map":
                self.maps.end_probe()
            self.ctl.busy = None
            if modal is not None and self.ctl.machine_state == "Idle":
                try:
                    self._cmd(self.ctl.jogger.restore_line(modal), check_abort=False)
                    self.ctl._schedule_modal_refresh()
                except (ControllerError, ProbeFailed, Aborted):
                    pass
            self.ctl._schedule_offsets_refresh()

    def _cmd(self, line: str, timeout: float = 30, check_abort: bool = True) -> dict[str, Any]:
        if check_abort and self._abort:
            raise Aborted()
        try:
            r = self.ctl.command(line, source="probe", timeout=timeout, follow_up=False)
        except ControllerError:
            if self._abort:
                raise Aborted() from None
            raise
        if self._abort and check_abort:
            raise Aborted()
        if r["status"] != "ok":
            raise ProbeFailed(f"{line}: {r['response']}")
        return r

    def _probe(self, line: str, distance: float, feed: float) -> list[float]:
        """Run one G38.2 and return the machine-coordinate contact point."""
        timeout = abs(distance) / max(feed, 1e-3) * 60 * 1.5 + 15
        r = self._cmd(line, timeout=timeout)
        prb = None
        for raw in r.get("lines", []):
            if raw.startswith("[") and raw.endswith("]"):
                fb = parse_feedback(raw[1:-1])
                if fb.get("type") == "probe":
                    prb = fb
        if prb is None:
            raise ProbeFailed("no [PRB] report received")
        if not prb["ok"]:
            raise ProbeFailed(
                f"the probe did not touch within {distance:g} mm. GRBL is now in ALARM; "
                "check the probe wiring and position, then Unlock."
            )
        # Wait for the post-probe status so later moves and readings see Idle.
        self.ctl.wait_status(lambda s: s["state"] == "Idle", timeout=5)
        return prb["value"]

    def _mpos(self) -> list[float]:
        st = self.ctl.wait_status(lambda s: s["state"] == "Idle", timeout=3) or self.ctl.model.status
        if not st or not st.get("mpos"):
            raise ProbeFailed("machine position unknown")
        return list(st["mpos"])

    # ------------------------------------------------------------- Z probe

    def probe_z(self, opts: dict[str, Any]) -> dict[str, Any]:
        return self._start("z", self._probe_z, opts)

    def _probe_z(self, o: dict[str, Any]) -> dict[str, Any]:
        depth, fast, slow = o["depth"], o["fastFeed"], o["slowFeed"]
        backoff, plate, retract = o["backoff"], o["plate"], o["retract"]
        self.ctl.boost_polling(600)
        prb = self._probe(f"G91G21G38.2Z-{_f(depth)}F{_f(fast)}", depth, fast)
        if o["twoStage"]:
            self._cmd(f"G91G21G0Z{_f(backoff)}")
            prb = self._probe(f"G91G21G38.2Z-{_f(backoff * 2)}F{_f(slow)}", backoff * 2, slow)
        now = self._mpos()
        result: dict[str, Any] = {"contact": prb, "plate": plate}
        if o["setZero"]:
            # GRBL stops a little past the trigger point; make work Z at the
            # *contact* equal the plate thickness.
            value = plate + (now[2] - prb[2])
            p = WCS_NAMES.index(self.ctl.model.active_wcs()) + 1
            self._cmd(f"G21G10L20P{p}Z{_f(value)}")
            result["wcs"] = WCS_NAMES[p - 1]
        if retract > 0:
            self._cmd(f"G91G21G0Z{_f(retract)}")
        return result

    # ------------------------------------------------------------ pin test

    def test(self, expect: str) -> dict[str, Any]:
        """Check the probe input without relying on a pin report (GRBL 0.9).

        Sends a 0.1 mm upward G38.3. GRBL alarms at once - without moving -
        if the probe input is already triggered, so: alarm = circuit closed.
        The alarm is cleared with $X (no motion happened, position is valid).
        """
        if expect not in ("open", "closed"):
            raise ControllerError("expect must be 'open' or 'closed'", 400)
        with self._lock:
            if self.running or self.ctl.busy:
                raise ControllerError("busy")
            if self.ctl.machine_state != "Idle":
                raise ControllerError(f"probe test needs Idle (machine is {self.ctl.machine_state})")
            self.ctl.busy = "probing"
        try:
            modal = self.ctl.sync_modal()
            r = self.ctl.command("G91G21G38.3Z0.1F100", source="probe", timeout=15, follow_up=False)
            st = self.ctl.wait_status(lambda s: True, timeout=2) or self.ctl.model.status or {}
            triggered = st.get("state") == "Alarm"
            if triggered:
                self.ctl.command("$X", source="probe", timeout=5, follow_up=False)
                if self.ctl.wait_status(lambda s: s["state"] == "Idle", timeout=3) is None:
                    raise ControllerError("probe test: the alarm did not clear after $X")
            else:
                self.ctl.command("G91G21G0Z-0.1", source="probe", timeout=10, follow_up=False)
            self.ctl.command(self.ctl.jogger.restore_line(modal), source="probe", timeout=5, follow_up=False)
            self.ctl._schedule_modal_refresh()
        finally:
            self.ctl.busy = None
        ok = triggered == (expect == "closed")
        if expect == "open":
            msg = "Probe input reads open, as expected." if ok else "Probe input reads TRIGGERED with nothing touching: check wiring / the clip is not shorted."
        else:
            msg = "Probe input reads triggered, as expected: the probe works." if ok else "Probe input did NOT trigger while touching: check the clip, the plate and the A5 wiring."
        caps = self.ctl.caps or {}
        if caps.get("probePin"):
            msg += " (GRBL 1.1 also shows the pin live as Pn:P.)"
        return {"triggered": triggered, "ok": ok, "message": msg, "response": r.get("response")}

    # -------------------------------------------------------- heightmap run

    def probe_map(self) -> dict[str, Any]:
        if self.maps.locked_by:
            raise ControllerError("the heightmap is busy")
        return self._start("map", self._probe_map)

    def _probe_map(self) -> dict[str, Any]:
        p = self.maps.begin_probe("probing")
        total = p.nx * p.ny
        self._publish(total=total, index=0)
        self.ctl.boost_polling(3600 * 4)
        span = p.zTop - p.zBottom
        travel = f"G90G0Z{_f(p.zTop)}"
        self._cmd("G21G90")
        self._cmd(travel)
        self._cmd(f"G90G0X{_f(p.originX)}Y{_f(p.originY)}", timeout=120)
        first = self._probe(f"G90G38.2Z{_f(p.zBottom)}F{_f(p.feed)}", span, p.feed)[2]
        self._cmd(travel)
        n = 0
        for i in range(p.ny):
            for jj in range(p.nx):
                j = jj if i % 2 == 0 else p.nx - 1 - jj  # serpentine, as Candle
                x, y = p.point(i, j)
                self._cmd(f"G90G0X{_f(x)}Y{_f(y)}", timeout=120)
                z = self._probe(f"G90G38.2Z{_f(p.zBottom)}F{_f(p.feed)}", span, p.feed)[2]
                self.maps.record(i, j, z - first)
                self._cmd(travel)
                n += 1
                self._publish(index=n)
        return {"points": total, "reference": first}
