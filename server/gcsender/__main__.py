"""Command-line entry point: ``uv run gcsender``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .api import SIM_PORT, build_router
from .controller import Controller, ControllerError
from .events import EventBus
from .logs import LogRing, Transcript
from .heightmap.model import MapLibrary
from .heightmap.service import HeightmapService
from .jobs.journal import Journal
from .jobs.library import JobLibrary
from .macros import MacroRunner, MacroStore
from .jobs.runner import JobRunner
from .probe import Prober
from .profiles import ProfileStore
from .simulator import GrblSimulator, parse_surface_spec
from .store import SettingsStore
from .transport import SerialTransport, SimTransport, Transport
from .webserver import AppServer

log = logging.getLogger("gcsender")

DEFAULT_WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
DEFAULT_STATE_DIR = Path.home() / ".gcsender"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gcsender", description="G-code sender server for GRBL 0.9/1.1")
    p.add_argument("--port", help="serial port to connect to at start-up (or 'sim')")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--http", default="127.0.0.1:8080", help="listen address host:port (default 127.0.0.1:8080)")
    p.add_argument("--lan", action="store_true", help="allow access from other machines (binds 0.0.0.0 unless --http given)")
    p.add_argument("--allow-host", action="append", default=[], help="extra Host header name to accept (repeatable)")
    p.add_argument("--simulate", choices=["0.9j", "1.1f"], help="provide a simulated GRBL controller as port 'sim'")
    p.add_argument("--sim-no-reset", action="store_true", help="simulated board does not reset when the port opens")
    p.add_argument("--sim-speed", type=float, default=1.0, help="simulator time scale (default 1.0 = real time)")
    p.add_argument(
        "--sim-surface",
        default="",
        metavar="SPEC",
        help="virtual workpiece surface for probing, e.g. 'preset=bent' or 'base=-25,tiltX=0.005'. "
        "Presets: gentle (default), flat, tilted, bent, bump. Keys: base, tiltX, tiltY, ampX, periodX, "
        "ampY, periodY, bump, bumpX, bumpY, bumpRadius (machine coordinates, mm; tilts in mm/mm)",
    )
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                   help=f"settings, logs and (later) jobs/heightmaps (default {DEFAULT_STATE_DIR})")
    p.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT, help=argparse.SUPPRESS)
    p.add_argument("--token", help=argparse.SUPPRESS)  # fixed token for development/testing
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"gcsender {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log_ring = LogRing()
    logging.getLogger().addHandler(log_ring)

    host, _, port_s = args.http.rpartition(":")
    if not host:
        host = "127.0.0.1"
    if args.lan and args.http == "127.0.0.1:8080":
        host = "0.0.0.0"
    try:
        http_port = int(port_s)
    except ValueError:
        print(f"invalid --http address: {args.http}", file=sys.stderr)
        return 2

    sim = None
    if args.simulate:
        try:
            surface = parse_surface_spec(args.sim_surface)
        except ValueError as e:
            print(f"invalid --sim-surface: {e}", file=sys.stderr)
            return 2
        sim = GrblSimulator(args.simulate, time_scale=args.sim_speed, boot_delay=0.5, surface=surface)

    def make_transport(port: str, baud: int) -> Transport:
        if port == SIM_PORT:
            if sim is None:
                raise ControllerError("simulator not enabled (start with --simulate)", 400)
            return SimTransport(sim, reset_on_open=not args.sim_no_reset)
        return SerialTransport(port, baud)

    state_dir: Path = args.state_dir.expanduser()
    settings = SettingsStore(state_dir / "settings.json")
    transcript = Transcript(state_dir / "logs")
    bus = EventBus()
    core: dict[str, Any] = {}
    server_ref: list[AppServer] = []

    def build_core() -> None:
        """Everything a server process owns (rebuilt by the simulated restart)."""
        controller = Controller(bus, make_transport, settings=settings, transcript=transcript)
        maps = HeightmapService(bus, MapLibrary(state_dir))
        prober = Prober(controller, maps)
        jobs = JobRunner(controller, maps, JobLibrary(state_dir), Journal(state_dir / "journal.json"))
        macros = MacroRunner(controller, MacroStore(state_dir / "macros.json"))
        core.update(controller=controller, jobs=jobs)
        core["router"] = build_router(
            bus, controller, simulator=args.simulate, transcript=transcript, log_ring=log_ring,
            maps=maps, prober=prober, sim=sim, jobs=jobs, macros=macros,
            sim_restart=simulated_restart if sim else None,
            profiles=ProfileStore(state_dir),
        )

    def simulated_restart() -> None:
        # Development aid: emulate the server process dying and starting again
        # while the (simulated) machine keeps running its buffer.
        log.warning("simulated server restart")
        core["jobs"].abandon()
        core["controller"].close()
        time.sleep(1.0)
        build_core()
        server_ref[0].router = core["router"]
        try:
            core["controller"].connect(SIM_PORT, args.baud)
        except ControllerError as e:
            log.warning("reconnect after the simulated restart failed: %s", e)

    build_core()
    router = core["router"]
    lan = args.lan or host not in ("127.0.0.1", "localhost", "::1")
    try:
        server = AppServer(
            (host, http_port),
            bus=bus,
            router=router,
            web_root=args.web_root,
            token=args.token,
            lan=lan,
            allowed_hosts=tuple(args.allow_host),
        )
    except OSError as e:
        print(f"cannot listen on {host}:{http_port}: {e}", file=sys.stderr)
        return 1
    server_ref.append(server)

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"gcsender {__version__} listening on http://{shown_host}:{server.port}/")
    if lan:
        print("WARNING: LAN access enabled - anyone with the token can move the machine.")
        print(f"         remote URL: http://<this-host>:{server.port}/?t={server.token}")
    if sim:
        print(f"simulated GRBL {args.simulate} available as port '{SIM_PORT}'")

    print(f"state directory: {state_dir}")
    autoconnect = args.port or (SIM_PORT if sim else None)
    if not autoconnect and settings.get("autoConnect") and settings.get("lastPort"):
        autoconnect = settings.get("lastPort")
    if autoconnect:
        try:
            baud = args.baud if args.port or sim else settings.get("lastBaud")
            core["controller"].connect(autoconnect, baud)
        except ControllerError as e:
            print(f"auto-connect to {autoconnect} failed: {e}", file=sys.stderr)

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.shutting_down.set()
        bus.close()
        core["controller"].close()
        transcript.close()
        server.server_close()
        if sim:
            sim.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
