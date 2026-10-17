"""REST API routes (docs/PLAN.md §6.2) - phase 1 subset."""

from __future__ import annotations

from typing import Any, Callable

from . import __version__
from .controller import Controller
from .errors import ControllerError
from .events import EventBus
from . import operations as ops
from .heightmap.model import MapError
from .heightmap.service import HeightmapService
from .probe import Prober
from .simulator import SURFACE_PRESETS, SurfaceParams
from .logs import LogRing, Transcript
from .store import SettingsError
from .transport import list_serial_ports
from .webserver import ApiError, RawResponse, Request, Router

SIM_PORT = "sim"
CONTROL_OPS = ("home", "unlock", "check", "sleep")


def _wrap(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
    def inner(req: Request) -> Any:
        try:
            return fn(req)
        except ControllerError as e:
            raise ApiError(e.status, str(e)) from None
        except MapError as e:
            raise ApiError(409, str(e)) from None
        except SettingsError as e:
            raise ApiError(400, str(e)) from None

    return inner


def build_router(
    bus: EventBus,
    controller: Controller | None,
    *,
    simulator: str | None = None,
    transcript: Transcript | None = None,
    log_ring: LogRing | None = None,
    maps: HeightmapService | None = None,
    prober: Prober | None = None,
    sim: Any = None,
) -> Router:
    r = Router()

    def add(method: str, path: str, fn: Callable[[Request], Any]) -> None:
        r.add(method, path, _wrap(fn))

    def ctl() -> Controller:
        if controller is None:
            raise ApiError(503, "controller unavailable")
        return controller

    # -- state and connection -------------------------------------------------

    def state(_req: Request) -> Any:
        snap = bus.snapshot()
        snap["server"] = {"version": __version__, "simulator": simulator}
        return snap

    def ports(_req: Request) -> Any:
        items = list_serial_ports()
        if simulator:
            items.insert(0, {"device": SIM_PORT, "description": f"Simulated GRBL {simulator}", "hwid": ""})
        return {"ports": items}

    def connect(req: Request) -> Any:
        body = req.json()
        port = body.get("port")
        if not isinstance(port, str) or not port:
            raise ApiError(400, "port is required")
        baud = body.get("baud", 115200)
        if not isinstance(baud, int) or isinstance(baud, bool) or not 1200 <= baud <= 2_000_000:
            raise ApiError(400, "invalid baud rate")
        return ctl().connect(port, baud)

    def disconnect(_req: Request) -> Any:
        ctl().disconnect()
        return {"ok": True}

    # -- commands -------------------------------------------------------------

    def command(req: Request) -> Any:
        body = req.json()
        line = body.get("line")
        if not isinstance(line, str):
            raise ApiError(400, "line is required")
        timeout = body.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 600:
            raise ApiError(400, "invalid timeout")
        source = body.get("source", "console")
        if source not in ("console", "ui"):
            raise ApiError(400, "invalid source")
        return ctl().command(line, source=source, timeout=float(timeout))

    def realtime(req: Request) -> Any:
        body = req.json()
        op = body.get("op")
        if not isinstance(op, str):
            raise ApiError(400, "op is required")
        restore = body.get("restore")
        if restore is not None and not isinstance(restore, bool):
            raise ApiError(400, "restore must be a boolean")
        ctl().realtime(op, restore=restore)
        return {"ok": True}

    def control(req: Request) -> Any:
        op = req.json().get("op")
        if op not in CONTROL_OPS:
            raise ApiError(400, f"op must be one of {', '.join(CONTROL_OPS)}")
        return ctl().control(op)

    # -- step jogging and stop -----------------------------------------------------

    def _num(body: dict[str, Any], key: str, required: bool = True) -> float | None:
        v = body.get(key)
        if v is None and not required:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ApiError(400, f"{key} must be a number")
        return float(v)

    def jog_step(req: Request) -> Any:
        body = req.json()
        delta = [_num(body, a, required=False) or 0.0 for a in ("x", "y", "z")]
        return ctl().jogger.step(delta, _num(body, "feed"))

    def stop(_req: Request) -> Any:
        return ctl().safe_stop()

    # -- WCS, positioning, spindle, coolant -----------------------------------

    def wcs_select(req: Request) -> Any:
        return ops.select_wcs(ctl(), req.json().get("wcs", ""))

    def wcs_zero(req: Request) -> Any:
        body = req.json()
        axes = body.get("axes")
        if not isinstance(axes, dict):
            raise ApiError(400, "axes must be an object like {\"x\": 0}")
        persistent = body.get("persistent", True)
        if not isinstance(persistent, bool):
            raise ApiError(400, "persistent must be a boolean")
        return ops.set_zero(ctl(), axes, persistent)

    def wcs_clear_g92(_req: Request) -> Any:
        return ops.clear_g92(ctl())

    def goto(req: Request) -> Any:
        body = req.json()
        target = {a: body[a] for a in ("x", "y", "z") if a in body}
        use_safe = body.get("safeZ", True)
        if not isinstance(use_safe, bool):
            raise ApiError(400, "safeZ must be a boolean")
        safe = float(ctl().settings.get("safeZ")) if use_safe else None
        return ops.goto(ctl(), target, safe)

    def spindle(req: Request) -> Any:
        body = req.json()
        on = body.get("on")
        if not isinstance(on, bool):
            raise ApiError(400, "on must be a boolean")
        direction = body.get("direction", "cw")
        if direction not in ("cw", "ccw"):
            raise ApiError(400, "direction must be cw or ccw")
        return ops.spindle(ctl(), on, _num(body, "rpm", required=False), direction)

    def coolant(req: Request) -> Any:
        body = req.json()
        for k in ("flood", "mist"):
            if k in body and not isinstance(body[k], bool):
                raise ApiError(400, f"{k} must be a boolean")
        return ops.coolant(ctl(), body.get("flood"), body.get("mist"))

    # -- probing ---------------------------------------------------------------

    def _maps() -> HeightmapService:
        if maps is None:
            raise ApiError(503, "heightmaps unavailable")
        return maps

    def _prober() -> Prober:
        if prober is None:
            raise ApiError(503, "probing unavailable")
        return prober

    def _flag(body: dict[str, Any], key: str, default: bool) -> bool:
        v = body.get(key, default)
        if not isinstance(v, bool):
            raise ApiError(400, f"{key} must be a boolean")
        return v

    def probe_z(req: Request) -> Any:
        body = req.json()
        st = ctl().settings
        opts: dict[str, Any] = {}
        for key, setting in (("depth", "probeDepth"), ("fastFeed", "probeFastFeed"), ("slowFeed", "probeSlowFeed"),
                             ("backoff", "probeBackoff"), ("plate", "probePlate"), ("retract", "probeRetract")):
            v = _num(body, key, required=False)
            opts[key] = float(st.get(setting)) if v is None else v
            if opts[key] < 0 or (key in ("depth", "fastFeed", "slowFeed", "backoff") and opts[key] <= 0):
                raise ApiError(400, f"{key} out of range")
        opts["twoStage"] = _flag(body, "twoStage", bool(st.get("probeTwoStage")))
        opts["setZero"] = _flag(body, "setZero", bool(st.get("probeSetZero")))
        return _prober().probe_z(opts)

    def probe_test(req: Request) -> Any:
        return _prober().test(req.json().get("expect", ""))

    def probe_status(_req: Request) -> Any:
        return _prober().status

    # -- heightmap -------------------------------------------------------------

    MAX_GCODE = 32 << 20

    def _text(body: dict[str, Any], key: str) -> str:
        v = body.get(key)
        if not isinstance(v, str):
            raise ApiError(400, f"{key} must be a string")
        return v

    def hm_get(_req: Request) -> Any:
        return _maps().state()

    def hm_put(req: Request) -> Any:
        body = req.json()
        if not isinstance(body, dict):
            raise ApiError(400, "body must be an object of parameters")
        return {**_maps().set_params(body), "heightmap": _maps().state()}

    def hm_cells(req: Request) -> Any:
        cells = req.json().get("cells")
        if not isinstance(cells, list):
            raise ApiError(400, "cells must be a list")
        _maps().set_cells(cells)
        return _maps().state()

    def hm_clear(_req: Request) -> Any:
        _maps().clear_values()
        return _maps().state()

    def hm_auto(req: Request) -> Any:
        body = req.json(MAX_GCODE)
        margin = _num(body, "margin", required=False) or 0.0
        return _maps().auto_border(_text(body, "gcode"), margin)

    def hm_probe(_req: Request) -> Any:
        return _prober().probe_map()

    def hm_abort(_req: Request) -> Any:
        p = _prober()
        if not p.running:
            return {"running": False}
        # Abort = the normal stop: hold, settle, reset, restore.
        return ctl().safe_stop()

    def hm_interpolated(_req: Request) -> Any:
        return _maps().interpolated()

    def hm_export(_req: Request) -> Any:
        m = _maps()
        name = (m.name or "heightmap") + ".map"
        return RawResponse(m.export_text().encode(), "text/plain; charset=utf-8", name)

    def hm_import(req: Request) -> Any:
        _maps().import_text(_text(req.json(), "text"), None)
        return _maps().state()

    def hm_library(_req: Request) -> Any:
        return {"maps": _maps().library.names()}

    def hm_save(req: Request) -> Any:
        _maps().save(_text(req.json(), "name").strip())
        return _maps().state()

    def hm_load(req: Request) -> Any:
        _maps().load(_text(req.json(), "name"))
        return _maps().state()

    def hm_delete(req: Request) -> Any:
        _maps().delete(_text(req.json(), "name"))
        return {"maps": _maps().library.names()}

    def hm_origin(_req: Request) -> Any:
        p = _maps().hm.params
        return ops.goto(ctl(), {"x": p.originX, "y": p.originY}, float(ctl().settings.get("safeZ")))

    def hm_transform(req: Request) -> Any:
        body = req.json(MAX_GCODE)
        tol = float(ctl().settings.get("arcTolerance")) if controller else 0.01
        result = _maps().transform(_text(body, "gcode"), tol)
        if body.get("includeLineMap"):
            return result
        return {"gcode": result["gcode"], "stats": result["stats"]}

    # -- simulator (only with --simulate) ----------------------------------------

    def _sim() -> Any:
        if sim is None:
            raise ApiError(404, "simulator not enabled")
        return sim

    def sim_state() -> dict[str, Any]:
        s = _sim()
        surface = s.surface.to_dict() if isinstance(s.surface, SurfaceParams) else None
        return {
            "firmware": simulator,
            "surface": surface,
            "presets": sorted(SURFACE_PRESETS),
            "probeForced": s.probe_forced,
        }

    def publish_sim() -> dict[str, Any]:
        state = sim_state()
        bus.publish("sim", state, retain=True)
        return state

    def sim_get(_req: Request) -> Any:
        return sim_state()

    def sim_probe(req: Request) -> Any:
        forced = req.json().get("forced")
        if forced is not None and not isinstance(forced, bool):
            raise ApiError(400, "forced must be true, false or null")
        _sim().probe_forced = forced
        return publish_sim()

    def sim_surface(req: Request) -> Any:
        s = _sim()
        body = req.json()
        if not isinstance(body, dict):
            raise ApiError(400, "body must be an object")
        current = s.surface if isinstance(s.surface, SurfaceParams) else SurfaceParams()
        try:
            s.surface = current.updated(body)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return publish_sim()

    if sim is not None:
        publish_sim()

    # -- GRBL settings ($$) ---------------------------------------------------

    def grbl_settings(_req: Request) -> Any:
        c = ctl()
        return {"settings": c.model.settings_list(), "warnings": c.model.warnings(c.caps)}

    def grbl_settings_put(req: Request) -> Any:
        body = req.json()
        values = body.get("values")
        if not isinstance(values, dict) or not values:
            raise ApiError(400, "values must be a non-empty object of {id: number}")
        c = ctl()
        known = set(c.model.settings)
        clean: dict[int, float] = {}
        for k, v in values.items():
            try:
                sid = int(k)
            except (TypeError, ValueError):
                raise ApiError(400, f"invalid setting id {k!r}") from None
            if known and sid not in known:
                raise ApiError(400, f"setting ${sid} does not exist on this controller")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ApiError(400, f"invalid value for ${sid}")
            clean[sid] = v
        results = c.write_settings(clean)
        return {"results": results, "settings": c.model.settings_list(), "warnings": c.model.warnings(c.caps)}

    def grbl_settings_refresh(_req: Request) -> Any:
        c = ctl()
        c.refresh_settings()
        return {"settings": c.model.settings_list(), "warnings": c.model.warnings(c.caps)}

    # -- app settings ---------------------------------------------------------

    def app_settings(_req: Request) -> Any:
        return ctl().settings.all()

    def app_settings_put(req: Request) -> Any:
        body = req.json()
        if not isinstance(body, dict):
            raise ApiError(400, "expected an object")
        return ctl().update_settings(body)

    # -- logs -----------------------------------------------------------------

    def logs_app(_req: Request) -> Any:
        return {"records": log_ring.records() if log_ring else []}

    def logs_serial_list(_req: Request) -> Any:
        return {"files": transcript.files() if transcript else []}

    def logs_serial(req: Request) -> Any:
        date = (req.query.get("date") or [None])[0]
        path = transcript.path_for(date) if transcript else None
        if path is None:
            raise ApiError(404, "no transcript for that date")
        return RawResponse(path.read_bytes(), "text/plain; charset=utf-8", path.name)

    r.add("GET", "/api/state", state)
    add("GET", "/api/ports", ports)
    add("POST", "/api/connect", connect)
    add("POST", "/api/disconnect", disconnect)
    add("POST", "/api/command", command)
    add("POST", "/api/realtime", realtime)
    add("POST", "/api/control", control)
    add("POST", "/api/jog/step", jog_step)
    add("POST", "/api/stop", stop)
    add("POST", "/api/wcs/select", wcs_select)
    add("POST", "/api/wcs/zero", wcs_zero)
    add("POST", "/api/wcs/clear-g92", wcs_clear_g92)
    add("POST", "/api/goto", goto)
    add("POST", "/api/spindle", spindle)
    add("POST", "/api/coolant", coolant)
    add("POST", "/api/probe/z", probe_z)
    add("POST", "/api/probe/test", probe_test)
    add("GET", "/api/probe", probe_status)
    add("GET", "/api/heightmap", hm_get)
    add("PUT", "/api/heightmap", hm_put)
    add("PATCH", "/api/heightmap/cells", hm_cells)
    add("POST", "/api/heightmap/clear", hm_clear)
    add("POST", "/api/heightmap/auto", hm_auto)
    add("POST", "/api/heightmap/probe", hm_probe)
    add("POST", "/api/heightmap/abort", hm_abort)
    add("GET", "/api/heightmap/interpolated", hm_interpolated)
    add("GET", "/api/heightmap/export", hm_export)
    add("POST", "/api/heightmap/import", hm_import)
    add("GET", "/api/heightmap/library", hm_library)
    add("POST", "/api/heightmap/library/save", hm_save)
    add("POST", "/api/heightmap/library/load", hm_load)
    add("POST", "/api/heightmap/library/delete", hm_delete)
    add("POST", "/api/heightmap/origin", hm_origin)
    add("POST", "/api/heightmap/transform", hm_transform)
    add("GET", "/api/sim", sim_get)
    add("POST", "/api/sim/probe", sim_probe)
    add("PUT", "/api/sim/surface", sim_surface)
    add("GET", "/api/grbl/settings", grbl_settings)
    add("PUT", "/api/grbl/settings", grbl_settings_put)
    add("POST", "/api/grbl/settings/refresh", grbl_settings_refresh)
    add("GET", "/api/settings", app_settings)
    add("PUT", "/api/settings", app_settings_put)
    add("GET", "/api/logs/app", logs_app)
    add("GET", "/api/logs/serial/files", logs_serial_list)
    add("GET", "/api/logs/serial", logs_serial)
    return r
