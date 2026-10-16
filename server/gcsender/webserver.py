"""HTTP server: static SPA, JSON API, SSE event stream and access control.

Security model (docs/PLAN.md §6.1) - this server can move a machine:

* Binds to 127.0.0.1 by default; ``--lan`` is an explicit opt-in.
* A random per-run token is required for every ``/api`` request
  (header ``X-GCS-Token``; GET requests may use ``?t=`` because
  ``EventSource`` cannot set headers). The SPA receives it injected into
  ``index.html`` - but only when the page is requested from loopback.
* ``Host`` must name this server (DNS-rebinding defence); ``Origin``, when
  present, must equal our own origin; mutating requests must be JSON (this
  also forces a CORS pre-flight for cross-site forms, which we never answer).
* No CORS headers are ever sent.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import mimetypes
import re
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .events import EventBus

log = logging.getLogger(__name__)

MAX_JSON_BODY = 1 << 20
SSE_KEEPALIVE_S = 15.0
TOKEN_META = '<meta name="gcs-token" content="">'


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Request:
    def __init__(self, handler: "Handler", path: str, query: dict[str, list[str]], params: dict[str, str]):
        self.handler = handler
        self.path = path
        self.query = query
        self.params = params
        self._body: bytes | None = None

    @property
    def method(self) -> str:
        return self.handler.command

    def header(self, name: str) -> str | None:
        return self.handler.headers.get(name)

    def body(self, limit: int = MAX_JSON_BODY) -> bytes:
        if self._body is None:
            try:
                length = int(self.header("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "bad Content-Length") from None
            if length > limit:
                raise ApiError(413, "request body too large")
            self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def json(self) -> Any:
        ctype = (self.header("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        raw = self.body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ApiError(400, f"invalid JSON: {e}") from None


Handler_t = Callable[[Request], Any]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler_t]] = []

    def add(self, method: str, pattern: str, fn: Handler_t) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method, regex, fn))

    def route(self, method: str, path: str) -> tuple[Handler_t, dict[str, str]]:
        allowed = False
        for m, regex, fn in self._routes:
            match = regex.match(path)
            if match:
                if m == method:
                    return fn, match.groupdict()
                allowed = True
        raise ApiError(405 if allowed else 404, "method not allowed" if allowed else "not found")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        bus: EventBus,
        router: Router,
        web_root: Path,
        token: str | None = None,
        lan: bool = False,
        allowed_hosts: tuple[str, ...] = (),
    ):
        self.bus = bus
        self.router = router
        self.web_root = web_root.resolve()
        self.token = token or secrets.token_urlsafe(24)
        self.lan = lan
        self.extra_hosts = {h.lower() for h in allowed_hosts}
        self.shutting_down = threading.Event()
        super().__init__(address, Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def host_allowed(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        host, port = _split_host(host_header)
        if port is not None and port != self.port:
            return False
        host = host.lower()
        if host in ("localhost", "127.0.0.1", "::1") or host in self.extra_hosts:
            return True
        if self.lan:
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return host == socket.gethostname().lower() or host == f"{socket.gethostname().lower()}.local"
        return False


def _split_host(value: str) -> tuple[str, int | None]:
    value = value.strip()
    if value.startswith("["):  # [::1]:8080
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else None
    else:
        host, sep, port = value.rpartition(":")
        if not sep:
            host, port = value, None
    try:
        return host, int(port) if port else None
    except ValueError:
        return host, -1


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"
    server_version = "gcsender"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the console quiet; errors are reported via responses

    # -- helpers ------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            if not self.server.host_allowed(self.headers.get("Host")):
                raise ApiError(421, "unrecognised Host header")
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{self.headers.get('Host')}":
                raise ApiError(403, "cross-origin request refused")
            url = urlsplit(self.path)
            path = url.path
            query = parse_qs(url.query)
            if path.startswith("/api/") or path == "/api":
                self._api(path, query)
            elif self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            if self.command not in ("GET", "HEAD"):
                # The request body may be unread; don't reuse this connection.
                self.close_connection = True
            self._error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - report, keep serving
            log.exception("unhandled error for %s %s", self.command, self.path)
            try:
                self._error(500, f"internal error: {type(e).__name__}")
            except OSError:
                pass

    def _check_token(self, query: dict[str, list[str]]) -> None:
        supplied = self.headers.get("X-GCS-Token")
        if supplied is None and self.command in ("GET", "HEAD"):
            supplied = (query.get("t") or [None])[0]
        if not supplied or not hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            raise ApiError(401, "missing or invalid token")

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        self._check_token(query)
        if path == "/api/events" and self.command == "GET":
            self._sse(query)
            return
        fn, params = self.server.router.route(self.command, path)
        req = Request(self, path, query, params)
        result = fn(req)
        if self.command in ("POST", "PUT", "PATCH", "DELETE") and req._body is None:
            req.body()  # drain an unread body so keep-alive stays in sync
        if isinstance(result, tuple):
            status, payload = result
        else:
            status, payload = 200, result
        self._json(status, payload if payload is not None else {"ok": True})

    # -- static -------------------------------------------------------------

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        root = self.server.web_root
        try:
            target = (root / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            raise ApiError(404, "not found") from None
        if root not in target.parents and target != root:
            raise ApiError(404, "not found")
        if not target.is_file():
            raise ApiError(404, "not found")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        if target.name == "index.html" and self._client_is_loopback():
            body = body.replace(
                TOKEN_META.encode(), f'<meta name="gcs-token" content="{self.server.token}">'.encode(), 1
            )
        extra = {
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                "connect-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'"
            ),
            "X-Frame-Options": "DENY",
        }
        self._send(200, body, ctype, extra)

    # -- server-sent events -------------------------------------------------

    def _sse(self, query: dict[str, list[str]]) -> None:
        last = self.headers.get("Last-Event-ID") or (query.get("last") or [None])[0]
        sub = self.server.bus.subscribe(last)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.server.shutting_down.is_set():
                ev = sub.get(timeout=SSE_KEEPALIVE_S)
                if ev is None:
                    if sub.closed:
                        break
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    chunk = ""
                    if ev.id is not None:
                        chunk += f"id: {ev.id}\n"
                    chunk += "data: " + json.dumps({"type": ev.type, "data": ev.data}, separators=(",", ":")) + "\n\n"
                    self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sub.close()
