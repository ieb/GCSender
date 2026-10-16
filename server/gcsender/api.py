"""REST API routes (docs/PLAN.md §6.2) - phase 1 subset."""

from __future__ import annotations

from typing import Any, Callable

from . import __version__
from .controller import Controller, ControllerError
from .events import EventBus
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
    add("GET", "/api/grbl/settings", grbl_settings)
    add("PUT", "/api/grbl/settings", grbl_settings_put)
    add("POST", "/api/grbl/settings/refresh", grbl_settings_refresh)
    add("GET", "/api/settings", app_settings)
    add("PUT", "/api/settings", app_settings_put)
    add("GET", "/api/logs/app", logs_app)
    add("GET", "/api/logs/serial/files", logs_serial_list)
    add("GET", "/api/logs/serial", logs_serial)
    return r
