import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from gcsender.api import build_router
from gcsender.controller import Controller
from gcsender.events import EventBus
from gcsender.logs import LogRing, Transcript
from gcsender.simulator import GrblSimulator
from gcsender.transport import SimTransport
from gcsender.webserver import AppServer

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
TOKEN = "test-token"


@pytest.fixture
def app(tmp_path):
    sim = GrblSimulator("0.9j", time_scale=5.0)
    bus = EventBus()
    transcript = Transcript(tmp_path / "logs")
    controller = Controller(bus, lambda port, baud: SimTransport(sim), poll_interval=0.05, transcript=transcript)
    router = build_router(bus, controller, simulator="0.9j", transcript=transcript, log_ring=LogRing())
    server = AppServer(("127.0.0.1", 0), bus=bus, router=router, web_root=WEB_ROOT, token=TOKEN)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield server, controller, bus
    server.shutting_down.set()
    bus.close()
    server.shutdown()
    controller.close()
    server.server_close()
    sim.close()


def call(server, method, path, body=None, headers=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = {"Host": f"127.0.0.1:{server.port}"}
    if token:
        h["X-GCS-Token"] = TOKEN
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = raw.decode()
    return resp.status, payload


def wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met")


def connect(server):
    status, body = call(server, "POST", "/api/connect", {"port": "sim"})
    assert status == 200, body
    wait_for(lambda: call(server, "GET", "/api/state")[1]["retained"]["connection"]["state"] == "connected")


# ------------------------------------------------------------------ security


def test_requires_token(app):
    server, *_ = app
    assert call(server, "GET", "/api/state", token=False)[0] == 401
    assert call(server, "GET", "/api/state", headers={"X-GCS-Token": "wrong"}, token=False)[0] == 401
    assert call(server, "GET", f"/api/state?t={TOKEN}", token=False)[0] == 200
    # query-string token is not accepted for mutating requests
    assert call(server, "POST", f"/api/disconnect?t={TOKEN}", {}, token=False)[0] == 401


def test_rejects_foreign_host_and_origin(app):
    server, *_ = app
    assert call(server, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 421
    assert call(server, "GET", "/", headers={"Host": f"rebind.example:{server.port}"})[0] == 421
    assert call(server, "GET", "/api/state", headers={"Host": f"127.0.0.1:{server.port + 1}"})[0] == 421
    assert call(server, "POST", "/api/disconnect", {}, headers={"Origin": "http://evil.example"})[0] == 403
    ok_origin = {"Origin": f"http://127.0.0.1:{server.port}"}
    assert call(server, "POST", "/api/disconnect", {}, headers=ok_origin)[0] == 200


def test_requires_json_for_mutations(app):
    server, *_ = app
    status, _ = call(server, "POST", "/api/command", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415


def test_routing_errors(app):
    server, *_ = app
    assert call(server, "GET", "/api/nope")[0] == 404
    assert call(server, "GET", "/api/connect")[0] == 405


def test_static_and_token_injection(app):
    server, *_ = app
    status, body = call(server, "GET", "/", token=False)
    assert status == 200
    assert f'<meta name="gcs-token" content="{TOKEN}">' in body
    assert call(server, "GET", "/js/app.js", token=False)[0] == 200
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("GET", "/../pyproject.toml", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{server.port}")
    conn.endheaders()
    assert conn.getresponse().status == 404


def test_lan_host_rules():
    bus = EventBus()
    server = AppServer(("127.0.0.1", 0), bus=bus, router=build_router(bus, None), web_root=WEB_ROOT, lan=True,
                       allowed_hosts=("cnc.lan",))
    try:
        p = server.port
        assert server.host_allowed(f"192.168.1.20:{p}")
        assert server.host_allowed(f"[::1]:{p}")
        assert server.host_allowed(f"cnc.lan:{p}")
        assert not server.host_allowed(f"attacker.example:{p}")
        assert not server.host_allowed(f"192.168.1.20:{p + 1}")
        assert not server.host_allowed(None)
    finally:
        server.server_close()


# ------------------------------------------------------------- controller


def test_connect_detects_firmware_and_runs_commands(app):
    server, *_ = app
    connect(server)
    state = call(server, "GET", "/api/state")[1]
    assert state["retained"]["caps"]["family"] == "0.9"
    assert state["retained"]["connection"]["firmware"] == "0.9j"
    status, body = call(server, "POST", "/api/command", {"line": "$G"})
    assert status == 200 and body["status"] == "ok"
    assert body["lines"][0].startswith("[G0 G54")
    status, body = call(server, "POST", "/api/command", {"line": "G1X"})
    assert body["status"] == "error" and body["response"] == "error: Bad number format"
    wait_for(lambda: call(server, "GET", "/api/state")[1]["retained"].get("status"))


def test_command_validation(app):
    server, *_ = app
    assert call(server, "POST", "/api/command", {"line": "$G"})[0] == 409  # not connected
    connect(server)
    assert call(server, "POST", "/api/command", {"line": "?"})[0] == 400
    assert call(server, "POST", "/api/command", {"line": "G0\x18"})[0] == 400
    assert call(server, "POST", "/api/realtime", {"op": "bogus"})[0] == 400
    assert call(server, "POST", "/api/realtime", {"op": "hold"})[0] == 200


def test_hold_then_reset_no_alarm(app):
    server, *_ = app
    connect(server)
    call(server, "POST", "/api/command", {"line": "G1X50F600"})
    time.sleep(0.2)
    call(server, "POST", "/api/realtime", {"op": "hold"})
    wait_for(lambda: (call(server, "GET", "/api/state")[1]["retained"]["status"] or {}).get("state") == "Hold")
    time.sleep(0.3)
    call(server, "POST", "/api/realtime", {"op": "reset"})
    st = wait_for(lambda: (s := call(server, "GET", "/api/state")[1]["retained"]["status"]) and s["state"] == "Idle" and s)
    assert st["mpos"][0] > 0
    assert call(server, "GET", "/api/state")[1]["retained"]["alarm"] is None


# ------------------------------------------------------------------- SSE


class SSE:
    def __init__(self, server, last=None):
        self.conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{server.port}"}
        if last:
            headers["Last-Event-ID"] = last
        self.conn.request("GET", f"/api/events?t={TOKEN}", headers=headers)
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200
        assert self.resp.getheader("Content-Type").startswith("text/event-stream")

    def next(self):
        ev_id, data = None, None
        while True:
            line = self.resp.fp.readline().decode().rstrip("\n")
            if line == "":
                if data is not None:
                    return ev_id, json.loads(data)
                continue
            if line.startswith("id: "):
                ev_id = line[4:]
            elif line.startswith("data: "):
                data = line[6:]

    def until(self, pred):
        while True:
            ev_id, msg = self.next()
            if pred(msg):
                return ev_id, msg

    def close(self):
        self.conn.close()


def test_sse_snapshot_then_replay(app):
    server, controller, bus = app
    connect(server)
    s1 = SSE(server)
    snap_id, snap = s1.next()
    assert snap["type"] == "snapshot" and snap_id == snap["data"]["id"]
    assert snap["data"]["retained"]["connection"]["state"] == "connected"
    call(server, "POST", "/api/command", {"line": "$G"})
    last_id, _ = s1.until(lambda m: m["type"] == "console" and m["data"]["line"] == "ok")
    s1.close()

    # Events published while "disconnected" are replayed on reconnect.
    call(server, "POST", "/api/command", {"line": "G0X1"})
    s2 = SSE(server, last=last_id)
    _, first = s2.next()
    assert first["type"] == "console" and first["data"]["line"] == "G0X1"
    s2.close()

    # An id from a previous server run yields a fresh snapshot.
    s3 = SSE(server, last="deadbeef:5")
    assert s3.next()[1]["type"] == "snapshot"
    s3.close()


def test_sse_streams_status(app):
    server, *_ = app
    connect(server)
    s = SSE(server)
    _, msg = s.until(lambda m: m["type"] == "status")
    assert msg["data"]["state"] in ("Idle", "Run")
    assert "mpos" in msg["data"] and "wpos" in msg["data"]
    s.close()


# --------------------------------------------------------------- phase 1 API


def test_grbl_settings_api(app):
    server, controller, bus = app
    connect(server)
    wait_for(lambda: len(call(server, "GET", "/api/grbl/settings")[1]["settings"]) == 31)
    status, body = call(server, "GET", "/api/grbl/settings")
    s110 = next(s for s in body["settings"] if s["id"] == 110)
    assert s110["value"] == 1500.0 and s110["unit"] == "mm/min" and s110["name"] == "X max rate"
    status, body = call(server, "PUT", "/api/grbl/settings", {"values": {"1": 30}})
    assert status == 200 and body["results"][0]["status"] == "ok"
    assert next(s for s in body["settings"] if s["id"] == 1)["value"] == 30
    assert call(server, "PUT", "/api/grbl/settings", {"values": {"999": 1}})[0] == 400
    assert call(server, "PUT", "/api/grbl/settings", {"values": {"1": -1}})[0] == 400
    assert call(server, "PUT", "/api/grbl/settings", {"values": {"1": "x"}})[0] == 400
    assert call(server, "PUT", "/api/grbl/settings", {"values": {}})[0] == 400
    assert call(server, "POST", "/api/grbl/settings/refresh", {})[0] == 200


def test_app_settings_api(app):
    server, *_ = app
    status, body = call(server, "GET", "/api/settings")
    assert status == 200 and body["statusInterval"] == 200
    status, body = call(server, "PUT", "/api/settings", {"statusInterval": 120})
    assert status == 200 and body["statusInterval"] == 120
    assert call(server, "PUT", "/api/settings", {"statusInterval": 1})[0] == 400
    assert call(server, "PUT", "/api/settings", {"nope": 1})[0] == 400


def test_control_api(app):
    server, *_ = app
    connect(server)
    assert call(server, "POST", "/api/control", {"op": "dance"})[0] == 400
    status, body = call(server, "POST", "/api/control", {"op": "home"})
    assert status == 409 and "homing is disabled" in body["error"]
    assert call(server, "POST", "/api/control", {"op": "unlock"})[0] == 200


def test_logs_api(app):
    server, *_ = app
    connect(server)
    call(server, "POST", "/api/command", {"line": "G0X1"})
    status, body = call(server, "GET", "/api/logs/serial/files")
    assert status == 200 and len(body["files"]) == 1
    date = body["files"][0]["date"]
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", f"/api/logs/serial?date={date}&t={TOKEN}", headers={"Host": f"127.0.0.1:{server.port}"})
    resp = conn.getresponse()
    text = resp.read().decode()
    assert resp.status == 200 and "attachment" in resp.getheader("Content-Disposition")
    assert "> G0X1" in text
    assert call(server, "GET", "/api/logs/serial?date=../../x")[0] == 404
    assert call(server, "GET", "/api/logs/app")[0] == 200
