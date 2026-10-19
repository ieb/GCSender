"""LAN login: accounts, HttpOnly session cookies, CSRF header, rate limiting."""

import http.client
import io
import json
import threading
import time
from pathlib import Path

import pytest

from gcsender import webserver
from gcsender.__main__ import main
from gcsender.api import build_router
from gcsender.auth import Auth, AuthError, LoginLimiter, SessionStore, UserStore
from gcsender.events import EventBus
from gcsender.webserver import AppServer

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
TOKEN = "full-control-token-123"
PASSWORD = "correct horse battery"


# -- stores ---------------------------------------------------------------------


def test_users_hash_verify_and_reload(tmp_path):
    users = UserStore(tmp_path / "users.json")
    assert users.set_password("ian", PASSWORD) is True
    assert users.verify("ian", PASSWORD) and not users.verify("ian", "wrong password")
    assert not users.verify("nobody", PASSWORD)
    raw = (tmp_path / "users.json").read_text()
    assert PASSWORD not in raw and "scrypt" not in raw
    assert (tmp_path / "users.json").stat().st_mode & 0o077 == 0  # private file
    # Another process (the CLI) changes the file: picked up without a restart.
    other = UserStore(tmp_path / "users.json")
    other.set_password("bob", "another password")
    time.sleep(0.01)
    assert users.names() == ["bob", "ian"]
    with pytest.raises(AuthError):
        users.set_password("bad name!", PASSWORD)
    with pytest.raises(AuthError):
        users.set_password("short", "1234")


def test_sessions_persist_expire_and_end_on_password_change(tmp_path):
    users = UserStore(tmp_path / "users.json")
    users.set_password("ian", PASSWORD)
    s1 = SessionStore(tmp_path / "sessions.json", users)
    sid = s1.create("ian", "10.0.0.2", "test")
    assert s1.lookup(sid) == "ian"
    assert sid not in (tmp_path / "sessions.json").read_text()  # only the hash is stored
    s2 = SessionStore(tmp_path / "sessions.json", users)  # a server restart
    assert s2.lookup(sid) == "ian"
    assert s2.lookup("forged") is None
    time.sleep(0.01)
    users.set_password("ian", "a brand new password")
    assert s2.lookup(sid) is None  # the password change ended it
    sid2 = s2.create("ian", "10.0.0.2", None)
    users.remove("ian")
    assert s2.lookup(sid2) is None  # the user was removed
    users.set_password("ian", PASSWORD)
    short = SessionStore(None, users, idle_s=0.05)
    sid3 = short.create("ian", "x", None)
    time.sleep(0.1)
    assert short.lookup(sid3) is None  # idle timeout


def test_login_limiter_backs_off():
    lim = LoginLimiter()
    for _ in range(LoginLimiter.FREE):
        assert lim.retry_after("a") == 0
        lim.failed("a")
    assert lim.retry_after("a") > 0 and lim.retry_after("b") == 0
    lim.succeeded("a")
    assert lim.retry_after("a") == 0


# -- HTTP ------------------------------------------------------------------------


@pytest.fixture
def srv(tmp_path):
    auth = Auth(tmp_path)
    auth.users.set_password("ian", PASSWORD)
    bus = EventBus()
    server = AppServer(("127.0.0.1", 0), bus=bus, router=build_router(bus, None), web_root=WEB_ROOT, token=TOKEN, auth=auth)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutting_down.set()
    bus.close()
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = {"Host": f"127.0.0.1:{server.port}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    out = (resp.status, json.loads(raw) if raw and "json" in (resp.getheader("Content-Type") or "") else raw, resp)
    conn.close()
    return out


def login(server, password=PASSWORD):
    status, body, resp = call(server, "POST", "/api/login", {"username": "ian", "password": password})
    return status, body, resp.getheader("Set-Cookie")


def test_login_sets_an_httponly_cookie_that_authorises_the_api(srv):
    status, body, _ = call(srv, "GET", "/api/session")
    assert body == {"authenticated": False, "control": True, "via": None, "user": None, "loginAvailable": True}
    assert call(srv, "GET", "/api/state")[0] == 401
    assert login(srv, "wrong password")[0] == 401
    status, body, cookie = login(srv)
    assert status == 200 and body["user"] == "ian"
    name = f"gcs_session_{srv.port}"
    assert cookie.startswith(f"{name}=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    sid = cookie.split(";")[0]
    assert sid.split("=", 1)[1] not in json.dumps(body)  # the page never sees the session id
    jar = {"Cookie": sid}
    assert call(srv, "GET", "/api/state", headers=jar)[0] == 200
    assert call(srv, "GET", "/api/session", headers=jar)[1] == {"authenticated": True, "control": True, "via": "session", "user": "ian", "loginAvailable": True}
    # Changes need the X-GCS-Request header with a cookie (CSRF).
    assert call(srv, "PUT", "/api/settings", {}, headers=jar)[0] == 403
    assert call(srv, "PUT", "/api/settings", {}, headers={**jar, "X-GCS-Request": "1"})[0] not in (401, 403)  # 503: no controller here
    assert call(srv, "PUT", "/api/settings", {}, headers={**jar, "X-GCS-Request": "1", "Origin": "http://evil.example"})[0] == 403
    # Event stream with the cookie.
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{srv.port}", **jar})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    conn.close()
    # Logout ends it on the server.
    status, _, resp = call(srv, "POST", "/api/logout", {}, headers={**jar, "X-GCS-Request": "1"})
    assert status == 200 and "Max-Age=0" in resp.getheader("Set-Cookie")
    assert call(srv, "GET", "/api/state", headers=jar)[0] == 401


def test_failed_logins_are_rate_limited(srv):
    for _ in range(LoginLimiter.FREE):
        assert login(srv, "nope nope nope")[0] == 401
    status, body, _ = login(srv)  # even the right password must wait now
    assert status == 429 and "try again" in body["error"]


def test_no_users_means_no_login(tmp_path):
    bus = EventBus()
    server = AppServer(("127.0.0.1", 0), bus=bus, router=build_router(bus, None), web_root=WEB_ROOT, token=TOKEN,
                       auth=Auth(tmp_path))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert call(server, "GET", "/api/session")[1]["loginAvailable"] is False
        status, body, _ = call(server, "POST", "/api/login", {"username": "ian", "password": PASSWORD})
        assert status == 403 and "--add-user" in body["error"]
    finally:
        server.shutting_down.set()
        bus.close()
        server.shutdown()
        server.server_close()


def test_lan_clients_get_no_token_and_cannot_use_it_in_urls(srv, monkeypatch):
    monkeypatch.setattr(webserver.Handler, "_client_is_local", lambda self: False)
    status, html, _ = call(srv, "GET", "/")
    assert status == 200 and TOKEN.encode() not in html
    assert call(srv, "GET", f"/api/state?t={TOKEN}")[0] == 401  # not in a LAN URL
    assert call(srv, "GET", "/api/state", headers={"X-GCS-Token": TOKEN})[0] == 200  # scripts: header
    sid = login(srv)[2].split(";")[0]
    assert call(srv, "GET", "/api/state", headers={"Cookie": sid})[0] == 200


def test_loopback_page_still_gets_the_token(srv):
    status, html, _ = call(srv, "GET", "/")
    assert TOKEN.encode() in html
    assert call(srv, "GET", f"/api/state?t={TOKEN}")[0] == 200


def test_user_management_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["--state-dir", str(tmp_path), "--add-user", "ian", "--password-stdin"]) == 0
    assert "created ian" in capsys.readouterr().out
    assert UserStore(tmp_path / "users.json").verify("ian", PASSWORD)
    assert main(["--state-dir", str(tmp_path), "--list-users"]) == 0
    assert "ian  (0 active sessions)" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["--state-dir", str(tmp_path), "--add-user", "ian", "--password-stdin"]) == 1
    assert main(["--state-dir", str(tmp_path), "--remove-user", "ian"]) == 0
    assert main(["--state-dir", str(tmp_path), "--remove-user", "ian"]) == 1
    assert UserStore(tmp_path / "users.json").names() == []
