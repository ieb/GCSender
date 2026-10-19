"""Only the computer running the server controls the machine; others watch and upload."""

import http.client
import io
import json
import re
import threading
import zipfile
from pathlib import Path

import pytest

from gcsender import webserver
from gcsender.api import build_router
from gcsender.webserver import AppServer

from test_phase4 import rig, wait_for  # noqa: F401  (rig is a fixture)

TOKEN = "view-only-test-token-1"
API_PY = Path(__file__).resolve().parents[1] / "gcsender" / "api.py"


@pytest.fixture
def srv(rig, monkeypatch):  # noqa: F811
    sim, ctl, _maps, jobs = rig()
    # Test-only: a request with X-Test-Remote: 1 comes "from another computer".
    monkeypatch.setattr(webserver.Handler, "_client_is_local", lambda self: self.headers.get("X-Test-Remote") != "1")
    server = AppServer(("127.0.0.1", 0), bus=ctl.bus, router=build_router(ctl.bus, ctl, jobs=jobs),
                       web_root=Path(__file__).resolve().parents[2] / "web", token=TOKEN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.status)
    yield server, sim, ctl, jobs
    server.shutting_down.set()
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None, raw=None, remote=False):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = {"Host": f"127.0.0.1:{server.port}", "X-GCS-Token": TOKEN}
    if remote:
        h["X-Test-Remote"] = "1"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif raw is not None:
        h["Content-Type"] = "text/plain"
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw_out = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw_out) if raw_out else None
    except ValueError:
        return resp.status, raw_out


def all_mutating_routes():
    src = API_PY.read_text()
    return re.findall(r'add\("(POST|PUT|DELETE)", "([^"]+)"', src)


def test_every_machine_changing_route_is_refused_to_a_viewer(srv):
    server, sim, ctl, _jobs = srv
    allowed = {("POST", "/api/jobs"), ("PUT", "/api/jobs/sets/{id}"), ("POST", "/api/bugreports")}
    routes = all_mutating_routes()
    assert len(routes) > 40  # the scan found the router table
    for method, path in routes:
        if (method, path) in allowed:
            continue
        concrete = path.replace("{id}", "0123456789ab").replace("{name}", "x")
        status, body = call(server, method, concrete, {}, remote=True)
        assert status == 403 and "view only" in body["error"], (method, path, status, body)
    # Even the API token does not give a viewer control, and the machine did not move.
    assert sim.pos == pytest.approx([0, 0, 0])
    status, body = call(server, "POST", "/api/jog/step", {"x": 1, "feed": 300}, remote=True)
    assert status == 403
    assert sim.pos == pytest.approx([0, 0, 0])


def test_a_viewer_watches_uploads_and_organises_sets(srv):
    server, _sim, _ctl, jobs = srv
    assert call(server, "GET", "/api/state", remote=True)[0] == 200
    assert call(server, "GET", "/api/session", remote=True)[1]["control"] is False
    assert call(server, "GET", "/api/session")[1]["control"] is True
    # A viewer's upload goes to the library but does not replace what is loaded.
    status, meta = call(server, "POST", "/api/jobs?name=v.nc", raw=b"G0 X1\n", remote=True)
    assert status == 200 and jobs.meta is None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("back.nc", "G0 X1\n")
        z.writestr("front.nc", "G0 X2\n")
    status, r = call(server, "POST", "/api/jobs?name=b.zip", raw=buf.getvalue(), remote=True)
    assert status == 200 and r["set"]["name"] == "b"
    ids = [f["id"] for f in r["set"]["files"]]
    status, st = call(server, "PUT", f"/api/jobs/sets/{r['set']['id']}", {"name": "Board", "order": ids[::-1]}, remote=True)
    assert status == 200 and st["name"] == "Board"
    # ...but loading, deleting and running are the operator's.
    assert call(server, "POST", "/api/job/select", {"jobId": meta["id"]}, remote=True)[0] == 403
    assert call(server, "DELETE", f"/api/jobs/{meta['id']}", remote=True)[0] == 403
    # The operator (this computer) can do all of it.
    assert call(server, "POST", "/api/job/select", {"jobId": meta["id"]})[0] == 200
    status, local_meta = call(server, "POST", "/api/jobs?name=l.nc", raw=b"G0 X3\n")
    assert status == 200 and jobs.meta["id"] == local_meta["id"]  # the operator's upload loads


def test_the_operator_keeps_control(srv):
    server, sim, _ctl, _jobs = srv
    status, _ = call(server, "POST", "/api/jog/step", {"x": 1, "feed": 600})
    assert status == 200
    wait_for(lambda: sim.pos[0] == pytest.approx(1))
