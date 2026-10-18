"""Command-line entry point: ``uv run gcsender``."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import secrets
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from collections.abc import Mapping
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
from .auth import Auth, AuthError
from .bugreport import BugReports
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
    p.add_argument(
        "--token",
        help="API access token instead of a random one per start (at least 16 of A-Z a-z 0-9 - _ . ~); "
        f"also read from ${TOKEN_ENV}",
    )
    p.add_argument("--token-file", type=Path, help="read the API access token from this file (keeps it out of ps and shell history)")
    users = p.add_argument_group("user accounts (LAN login); these change the state directory and exit")
    users.add_argument("--add-user", metavar="NAME", help="create a user, or set a new password (asks for it; ends their sessions)")
    users.add_argument("--password-stdin", action="store_true", help="with --add-user: read the password from stdin")
    users.add_argument("--remove-user", metavar="NAME", help="delete a user (their sessions end at once)")
    users.add_argument("--list-users", action="store_true", help="list users and active sessions")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"gcsender {__version__}")
    return p.parse_args(argv)


def manage_users(args: argparse.Namespace) -> int | None:
    """--add-user / --remove-user / --list-users: change users.json and exit."""
    if not (args.add_user or args.remove_user or args.list_users):
        return None
    state_dir = args.state_dir.expanduser()
    auth = Auth(state_dir)
    try:
        if args.add_user:
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass(f"password for {args.add_user}: ")
                if getpass.getpass("again: ") != password:
                    print("error: the passwords differ", file=sys.stderr)
                    return 1
            created = auth.users.set_password(args.add_user, password)
            print(f"{'created' if created else 'changed the password of'} {args.add_user} in {state_dir / 'users.json'}")
        if args.remove_user:
            if not auth.users.remove(args.remove_user):
                print(f"error: no user {args.remove_user!r}", file=sys.stderr)
                return 1
            print(f"removed {args.remove_user}")
        if args.list_users:
            sessions = auth.sessions.active()
            for name in auth.users.names():
                mine = [x for x in sessions if x["user"] == name]
                print(f"{name}  ({len(mine)} active session{'s' if len(mine) != 1 else ''})")
            if not auth.users.names():
                print("no users")
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def setup_log_file(state_dir: Path) -> Path | None:
    """Keep the application log on disk (for bug reports): 2 MB x 3 files."""
    try:
        (state_dir / "logs").mkdir(parents=True, exist_ok=True)
        path = state_dir / "logs" / "gcsender.log"
        handler = RotatingFileHandler(path, maxBytes=2 << 20, backupCount=2, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"))
        logging.getLogger().addHandler(handler)
        return path
    except OSError as e:
        print(f"cannot write the log file: {e}", file=sys.stderr)
        return None


TOKEN_ENV = "GCSENDER_TOKEN"
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")


def resolve_token(args: argparse.Namespace, environ: Mapping[str, str] = os.environ) -> tuple[str | None, str]:
    """The API token and where it came from: --token, --token-file, $GCSENDER_TOKEN or random."""
    if args.token is not None and args.token_file is not None:
        raise ValueError("use either --token or --token-file, not both")
    if args.token is not None:
        token, source = args.token.strip(), "--token"
    elif args.token_file is not None:
        try:
            token, source = args.token_file.expanduser().read_text(encoding="utf-8").strip(), "--token-file"
        except OSError as e:
            raise ValueError(f"cannot read the token file: {e}") from None
    elif environ.get(TOKEN_ENV):
        token, source = environ[TOKEN_ENV].strip(), f"${TOKEN_ENV}"
    else:
        return None, "random"
    if not TOKEN_RE.match(token):
        raise ValueError(f"the token from {source} must be 16-256 characters of A-Z a-z 0-9 - _ . ~")
    return token, source


def load_reports_token(state_dir: Path) -> str:
    """A stable, read-only token for downloading bug reports remotely."""
    path = state_dir / "reports-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(18)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"cannot save the reports token: {e}", file=sys.stderr)
    return token


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    managed = manage_users(args)
    if managed is not None:
        return managed
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # start-up URLs reach logs and service managers at once
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
    log_file = setup_log_file(state_dir)
    reports_token = load_reports_token(state_dir)
    auth = Auth(state_dir)
    try:
        api_token, token_source = resolve_token(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if api_token is not None and api_token == reports_token:
        print("error: the API token must differ from the read-only reports token", file=sys.stderr)
        return 2
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
        core.update(controller=controller, jobs=jobs, macros=macros)
        core["router"] = build_router(
            bus, controller, simulator=args.simulate, transcript=transcript, log_ring=log_ring,
            maps=maps, prober=prober, sim=sim, jobs=jobs, macros=macros,
            sim_restart=simulated_restart if sim else None,
            profiles=ProfileStore(state_dir), bugreports=bugreports, access=access,
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

    access: dict[str, Any] = {}
    bugreports = BugReports(
        state_dir, bus=bus, core=lambda: core, transcript=transcript, log_ring=log_ring, log_file=log_file,
        info={"version": __version__, "simulate": args.simulate, "lan": bool(args.lan), "http": args.http},
    )
    build_core()
    router = core["router"]
    lan = args.lan or host not in ("127.0.0.1", "localhost", "::1")
    try:
        server = AppServer(
            (host, http_port),
            bus=bus,
            router=router,
            web_root=args.web_root,
            token=api_token,
            lan=lan,
            allowed_hosts=tuple(args.allow_host),
            reports_token=reports_token,
            auth=auth,
        )
    except OSError as e:
        print(f"cannot listen on {host}:{http_port}: {e}", file=sys.stderr)
        return 1
    server_ref.append(server)
    access.update(
        lan=lan, port=server.port, hostname=socket.gethostname(), reportsToken=reports_token,
        note=None if lan else "the server only listens on this computer: start it with --lan for remote access",
    )

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"gcsender {__version__} listening on http://{shown_host}:{server.port}/")
    if lan:
        names = auth.users.names()
        if names:
            print(f"LAN access: open http://<this-host>:{server.port}/ and log in ({len(names)} user{'s' if len(names) > 1 else ''}).")
        else:
            print("WARNING: LAN access is enabled but there are no user accounts, so other computers cannot log in.")
            print("         Create one with:  gcsender --add-user NAME")
    if sim:
        print(f"simulated GRBL {args.simulate} available as port '{SIM_PORT}'")

    print(f"API token: {'fixed, from ' + token_source if api_token else 'random for this run'}")
    print(f"state directory: {state_dir}")
    if lan:
        print(f"bug reports (read-only token): http://<this-host>:{server.port}/api/bugreports/archive?t={reports_token}")
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
