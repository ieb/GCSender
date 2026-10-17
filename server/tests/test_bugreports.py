"""Bug reports: contents, storage, download and the read-only reports token."""

import http.client
import io
import json
import logging
import threading
import time
import zipfile
from pathlib import Path

import pytest

from gcsender import controller as controller_mod
from gcsender.api import build_router
from gcsender.bugreport import BugReports, ReportError
from gcsender.controller import Controller
from gcsender.events import EventBus
from gcsender.heightmap.model import MapLibrary
from gcsender.heightmap.service import HeightmapService
from gcsender.jobs.journal import Journal
from gcsender.jobs.library import JobLibrary
from gcsender.jobs.runner import JobRunner
from gcsender.logs import LogRing, Transcript
from gcsender.macros import MacroRunner, MacroStore
from gcsender.simulator import GrblSimulator
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport
from gcsender.webserver import AppServer

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
TOKEN = "full-control-token"
RTOKEN = "read-only-reports-token"


def wait_for(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
    sim = GrblSimulator("0.9j", time_scale=5.0)
    bus = EventBus()
    transcript = Transcript(tmp_path / "logs")
    ring = LogRing()
    logging.getLogger().addHandler(ring)
    ctl = Controller(bus, lambda p, b: SimTransport(sim), settings=SettingsStore(tmp_path / "settings.json"),
                     transcript=transcript, poll_interval=0.03)
    maps = HeightmapService(bus, MapLibrary(tmp_path))
    jobs = JobRunner(ctl, maps, JobLibrary(tmp_path), Journal(tmp_path / "journal.json"))
    macros = MacroRunner(ctl, MacroStore(tmp_path / "macros.json"))
    core = {"controller": ctl, "jobs": jobs, "macros": macros}
    reports = BugReports(tmp_path, bus=bus, core=lambda: core, transcript=transcript, log_ring=ring,
                         info={"version": "test"})
    access = {"lan": True, "port": 0, "reportsToken": RTOKEN}
    router = build_router(bus, ctl, jobs=jobs, macros=macros, bugreports=reports, access=access, log_ring=ring,
                          transcript=transcript)
    server = AppServer(("127.0.0.1", 0), bus=bus, router=router, web_root=WEB_ROOT, token=TOKEN, reports_token=RTOKEN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    ctl.connect("sim")
    wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.settings and ctl.model.status)
    yield server, ctl, jobs, reports, tmp_path
    server.shutting_down.set()
    bus.close()
    server.shutdown()
    ctl.close()
    server.server_close()
    sim.close()
    logging.getLogger().removeHandler(ring)


def call(server, method, path, body=None, token=TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = {"Host": f"127.0.0.1:{server.port}"}
    if token:
        h["X-GCS-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    ctype = resp.getheader("Content-Type") or ""
    disp = resp.getheader("Content-Disposition")
    conn.close()
    if "json" in ctype and raw:
        return resp.status, json.loads(raw), disp
    return resp.status, raw, disp


def test_report_contains_the_whole_picture(rig):
    server, ctl, jobs, _reports, _tmp = rig
    meta = jobs.library.add("part.nc", b"G21 G90\nG0 X1 Y1\nG1 X5 F300\n")
    jobs.select(meta["id"])
    ctl.command("G0 X2", source="console")
    client = {"userAgent": "TestBrowser/1.0", "errors": [{"msg": "TypeError: x is undefined"}], "apiCalls": [{"path": "/api/x", "status": 500}, {"path": "/api/y", "status": 200}]}
    status, entry, _ = call(server, "POST", "/api/bugreports", {"description": "Jog X did nothing\nthen it moved twice", "client": client})
    assert status == 200 and entry["title"] == "Jog X did nothing"
    status, raw, disp = call(server, "GET", f"/api/bugreports/{entry['id']}")
    assert status == 200 and f"gcsender-bugreport-{entry['id']}.zip" in disp
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = set(z.namelist())
    assert {"README.txt", "report.json", "client.json", "server-state.json", "events.json", "threads.txt",
            "app-log.txt", "serial-tail.txt", "program.nc", "files/settings.json"} <= names
    readme = z.read("README.txt").decode()
    assert "Jog X did nothing" in readme and "Browser errors: 1 · failed API calls: 1" in readme
    state = json.loads(z.read("server-state.json"))
    assert state["connection"]["state"] == "connected" and state["machineState"]
    assert state["grblSettings"]["110"] and state["streamer"]["budget"] == 127
    assert state["job"]["job"]["name"] == "part.nc"
    assert "G0X2" in z.read("serial-tail.txt").decode().replace(" ", "")
    events = json.loads(z.read("events.json"))
    assert any(e["type"] == "status" for e in events["volatile"]) and events["events"]
    assert "MainThread" in z.read("threads.txt").decode()
    assert z.read("program.nc").decode().startswith("G21 G90")
    assert json.loads(z.read("client.json"))["userAgent"] == "TestBrowser/1.0"
    everything = b"".join(z.read(n) for n in names)
    assert TOKEN.encode() not in everything and RTOKEN.encode() not in everything


def test_list_archive_delete_and_validation(rig):
    server, _ctl, _jobs, reports, _tmp = rig
    a = reports.create("first problem", {}, include_program=False)
    time.sleep(1.05)  # ids have one-second resolution
    b = reports.create("second problem", {}, include_program=False)
    status, body, _ = call(server, "GET", "/api/bugreports")
    assert [r["id"] for r in body["reports"]] == [b["id"], a["id"]]
    assert body["access"]["reportsToken"] == RTOKEN
    status, raw, disp = call(server, "GET", "/api/bugreports/archive")
    z = zipfile.ZipFile(io.BytesIO(raw))
    assert {f"{a['id']}.zip", f"{b['id']}.zip", "index.json"} <= set(z.namelist())
    assert "program.nc" not in zipfile.ZipFile(io.BytesIO(z.read(f"{a['id']}.zip"))).namelist()
    assert call(server, "DELETE", f"/api/bugreports/{a['id']}")[0] == 200
    assert [r["id"] for r in reports.list()] == [b["id"]]
    assert call(server, "GET", "/api/bugreports/../../etc")[0] in (400, 404)
    assert call(server, "GET", "/api/bugreports/20990101-000000-abcd")[0] == 404
    assert call(server, "POST", "/api/bugreports", {"description": "  "})[0] == 400
    with pytest.raises(ReportError):
        reports.data("nope")


def test_the_reports_token_only_reads_reports(rig):
    server, _ctl, _jobs, reports, _tmp = rig
    r = reports.create("visible remotely", {}, include_program=False)
    assert call(server, "GET", "/api/bugreports", token=RTOKEN)[0] == 200
    assert call(server, "GET", f"/api/bugreports/{r['id']}", token=RTOKEN)[0] == 200
    assert call(server, "GET", "/api/bugreports/archive", token=RTOKEN)[0] == 200
    # ...but nothing else
    assert call(server, "DELETE", f"/api/bugreports/{r['id']}", token=RTOKEN)[0] == 401
    assert call(server, "POST", "/api/bugreports", {"description": "x"}, token=RTOKEN)[0] == 401
    assert call(server, "GET", "/api/state", token=RTOKEN)[0] == 401
    assert call(server, "POST", "/api/command", {"line": "G0 X1"}, token=RTOKEN)[0] == 401
    assert call(server, "GET", "/api/bugreports", token="wrong")[0] == 401
    assert call(server, "GET", "/api/bugreports", token=None)[0] == 401


def test_a_report_is_made_even_when_disconnected_and_old_ones_are_pruned(rig, monkeypatch):
    _server, ctl, _jobs, reports, tmp = rig
    ctl.disconnect()
    import gcsender.bugreport as br

    monkeypatch.setattr(br, "MAX_REPORTS", 2)
    ids = []
    for i in range(3):
        ids.append(reports.create(f"offline {i}", None)["id"])
        time.sleep(1.05)
    left = [r["id"] for r in reports.list()]
    assert left == ids[:0:-1]  # the newest two
    z = zipfile.ZipFile(io.BytesIO(reports.data(left[0])))
    state = json.loads(z.read("server-state.json"))
    assert state["connection"]["state"] == "disconnected"
    assert len(list((tmp / "bugreports").glob("*.zip"))) == 2
