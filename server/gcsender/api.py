"""REST API routes (phase 0 subset of docs/PLAN.md §6.2)."""

from __future__ import annotations

from typing import Any, Callable

from . import __version__
from .controller import Controller, ControllerError
from .events import EventBus
from .transport import list_serial_ports
from .webserver import ApiError, Request, Router

SIM_PORT = "sim"


def _wrap(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
    def inner(req: Request) -> Any:
        try:
            return fn(req)
        except ControllerError as e:
            raise ApiError(e.status, str(e)) from None

    return inner


def build_router(bus: EventBus, controller: Controller, *, simulator: str | None = None) -> Router:
    r = Router()

    def add(method: str, path: str, fn: Callable[[Request], Any]) -> None:
        r.add(method, path, _wrap(fn))

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
        if not isinstance(baud, int) or not 1200 <= baud <= 2_000_000:
            raise ApiError(400, "invalid baud rate")
        return controller.connect(port, baud)

    def disconnect(_req: Request) -> Any:
        controller.disconnect()
        return {"ok": True}

    def command(req: Request) -> Any:
        body = req.json()
        line = body.get("line")
        if not isinstance(line, str):
            raise ApiError(400, "line is required")
        timeout = body.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ApiError(400, "invalid timeout")
        return controller.command(line, source="console", timeout=float(timeout))

    def realtime(req: Request) -> Any:
        body = req.json()
        op = body.get("op")
        if not isinstance(op, str):
            raise ApiError(400, "op is required")
        controller.realtime(op)
        return {"ok": True}

    r.add("GET", "/api/state", state)
    add("GET", "/api/ports", ports)
    add("POST", "/api/connect", connect)
    add("POST", "/api/disconnect", disconnect)
    add("POST", "/api/command", command)
    add("POST", "/api/realtime", realtime)
    return r
