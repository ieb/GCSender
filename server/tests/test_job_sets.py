"""Sets: a zip of a PCB's G-code files uploaded at once, kept together in the library."""

import http.client
import io
import json
import threading
import zipfile
from pathlib import Path

import pytest

from gcsender.api import build_router
from gcsender.events import EventBus
from gcsender.jobs import library as lib_mod
from gcsender.jobs.library import JobError, JobLibrary, looks_like_gcode
from gcsender.webserver import AppServer

from test_phase4 import finished, rig, wait_for  # noqa: F401  (rig is a fixture)

GC = "G21 G90\nG0 X0 Y0 Z1\nG1 Z-0.1 F300\nG1 X2 F600\nG0 Z1\n"
EXCELLON = "M48\nMETRIC,TZ\nT1C0.800\n%\nT1\nX012500Y034000\nX015000Y034000\nM30\n"


def make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


PCB = {
    "myboard/drill.ngc": GC,
    "myboard/front.ngc": GC,
    "myboard/cutout.nc": GC,
    "myboard/back.ngc": GC,
    "myboard/alignment_holes.gcode": GC,
    "myboard/notes.txt": GC,  # .txt that is G-code: kept
    "myboard/README.md": "# my board\n",
    "myboard/board-F_Cu.gbr": "%FSLAX46Y46*%\n",
    "myboard/board.drl": EXCELLON,
    "myboard/excellon.txt": EXCELLON,  # Excellon with a .txt name: skipped
    "__MACOSX/myboard/._front.ngc": b"\x00\x05\x16\x07",
    "myboard/.DS_Store": b"\x00\x00\x00\x01Bud1",
}


def test_zip_becomes_an_ordered_set(tmp_path):
    lib = JobLibrary(tmp_path)
    r = lib.add_zip("myboard.zip", make_zip(PCB))
    st = r["set"]
    assert st["name"] == "myboard"
    # Default PCB order: back, align, front, drill, cut-out, then the rest by name.
    assert [f["path"] for f in st["files"]] == [
        "back.ngc", "alignment_holes.gcode", "front.ngc", "drill.ngc", "cutout.nc", "notes.txt",
    ]
    assert all(f["set"] == st["id"] and f["lines"] == 5 for f in st["files"])
    assert {s["path"]: s["reason"] for s in r["skipped"]} == {
        "myboard/README.md": "not G-code",
        "myboard/board-F_Cu.gbr": "not G-code",
        "myboard/board.drl": "not G-code",
        "myboard/excellon.txt": "not G-code",
    }
    # Persisted: a new library instance (server restart) sees the same set.
    again = JobLibrary(tmp_path).sets()
    assert [s["name"] for s in again] == ["myboard"] and len(again[0]["files"]) == 6
    # The same zip again: no clash, a second set.
    assert lib.add_zip("myboard.zip", make_zip(PCB))["set"]["name"] == "myboard (2)"


def test_rename_reorder_and_delete(tmp_path):
    lib = JobLibrary(tmp_path)
    st = lib.add_zip("b.zip", make_zip({"a.nc": GC, "b.nc": GC, "c.nc": GC}))["set"]
    ids = [f["id"] for f in st["files"]]
    st2 = lib.update_set(st["id"], name="Board v2", order=ids[::-1])
    assert st2["name"] == "Board v2" and [f["id"] for f in st2["files"]] == ids[::-1]
    with pytest.raises(JobError, match="exactly"):
        lib.update_set(st["id"], order=ids[:2])
    lib.delete(ids[0])  # one program: it leaves the set
    assert [f["id"] for f in lib.sets()[0]["files"]] == ids[:0:-1]
    deleted = lib.delete_set(st["id"])
    assert sorted(deleted) == sorted(ids[1:]) and lib.sets() == [] and lib.list() == []


def test_bad_zips(tmp_path, monkeypatch):
    lib = JobLibrary(tmp_path)
    with pytest.raises(JobError, match="not a valid zip"):
        lib.add_zip("x.zip", b"PK\x03\x04 nonsense")
    with pytest.raises(JobError, match="no G-code"):
        lib.add_zip("x.zip", make_zip({"README.md": "hi"}))
    monkeypatch.setattr(lib_mod, "MAX_ZIP_TOTAL", 1000)
    with pytest.raises(JobError, match="expands"):
        lib.add_zip("x.zip", make_zip({"a.nc": "G0 X0\n" * 500}))  # 3000 bytes uncompressed
    monkeypatch.setattr(lib_mod, "MAX_ZIP_TOTAL", 200 << 20)
    monkeypatch.setattr(lib_mod, "MAX_JOB_BYTES", 100)
    r = lib.add_zip("x.zip", make_zip({"big.nc": "G0 X0\n" * 50, "ok.nc": "G0 X1\n"}))
    assert [f["path"] for f in r["set"]["files"]] == ["ok.nc"]
    assert r["skipped"] == [{"path": "big.nc", "reason": "larger than 50 MB"}]


def test_gcode_sniffing():
    assert looks_like_gcode(GC)
    assert looks_like_gcode("(header)\n; comment\nN10 G0 X1\nN20 G1 Y2 F100\n")
    assert not looks_like_gcode(EXCELLON)
    assert not looks_like_gcode("Hello,\nthis is a readme.\n")


def test_the_last_run_is_remembered(rig):
    _sim, _ctl, _maps, jobs = rig()
    st = jobs.library.add_zip("p.zip", make_zip({"back.nc": GC, "front.nc": GC}))["set"]
    first = st["files"][0]
    jobs.select(first["id"])
    jobs.start()
    assert finished(jobs) == "done"
    files = jobs.library.sets()[0]["files"]
    assert files[0]["lastRun"]["state"] == "done" and "lastRun" not in files[1]
    assert jobs.snapshot()["job"]["lastRun"]["state"] == "done"


# -- HTTP ----------------------------------------------------------------------------

TOKEN = "jobs-sets-token-1234"


def test_http_upload_list_update_and_delete(rig):
    _sim, ctl, _maps, jobs = rig()
    bus = ctl.bus
    server = AppServer(("127.0.0.1", 0), bus=bus, router=build_router(bus, ctl, jobs=jobs),
                       web_root=Path(__file__).resolve().parents[2] / "web", token=TOKEN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def call(method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        h = {"Host": f"127.0.0.1:{server.port}", "X-GCS-Token": TOKEN}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif raw is not None:
            h["Content-Type"] = "text/plain"
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        out = resp.status, json.loads(resp.read() or b"null")
        conn.close()
        return out

    try:
        status, r = call("POST", "/api/jobs?name=board.zip", raw=make_zip({"front.nc": GC, "back.nc": GC}))
        assert status == 200 and r["set"]["name"] == "board" and [f["name"] for f in r["set"]["files"]] == ["back.nc", "front.nc"]
        assert jobs.meta is None  # a set does not auto-load: the user picks the file
        sid = r["set"]["id"]
        status, lst = call("GET", "/api/jobs")
        assert [s["id"] for s in lst["sets"]] == [sid] and len(lst["jobs"]) == 2
        ids = [f["id"] for f in r["set"]["files"]]
        status, st = call("PUT", f"/api/jobs/sets/{sid}", {"name": "Board A", "order": ids[::-1]})
        assert status == 200 and st["name"] == "Board A" and [f["name"] for f in st["files"]] == ["front.nc", "back.nc"]
        assert call("PUT", f"/api/jobs/sets/{sid}", {"order": ["nope"]})[0] == 400
        assert call("POST", "/api/jobs?name=bad.zip", raw=b"not a zip at all")[0] == 400
        # Loaded program of the set: deleting the set unloads it.
        jobs.select(ids[0])
        status, lst = call("DELETE", f"/api/jobs/sets/{sid}")
        assert status == 200 and lst["sets"] == [] and jobs.meta is None
        assert call("DELETE", f"/api/jobs/sets/{sid}")[0] in (400, 404)
        # A plain file still uploads and loads as before.
        status, meta = call("POST", "/api/jobs?name=single.nc", raw=GC.encode())
        assert status == 200 and meta["name"] == "single.nc"
        wait_for(lambda: jobs.meta and jobs.meta["id"] == meta["id"])
    finally:
        server.shutting_down.set()
        server.shutdown()
        server.server_close()
