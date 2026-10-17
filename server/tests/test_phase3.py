"""Phase 3: heightmap model/.map I/O, interpolation, transform, library, probing."""

import math
import time

import pytest

from gcsender import controller as controller_mod
from gcsender.controller import Controller, ControllerError
from gcsender.events import EventBus
from gcsender.heightmap.model import HeightMap, MapError, MapLibrary, MapParams, _cubic
from gcsender.heightmap.service import HeightmapService
from gcsender.heightmap.transform import bounds, transform
from gcsender.probe import Prober
from gcsender.simulator import GrblSimulator, default_surface
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport

# A file as Candle writes it (CRLF, a NaN cell).
CANDLE_MAP = (
    "candle map v3\r\n"
    "0;0;40;30\r\n"
    "0;0\r\n"
    "5;4;-1;1;20\r\n"
    "0;41;31\r\n"
    "0;0.01;0.02;0.03;0.04\r\n"
    "0.005;0.015;0.025;0.035;0.045\r\n"
    "0.01;0.02;0.03;0.04;nan\r\n"
    "0.015;0.025;0.035;0.045;0.055\r\n"
)


def plane_map(fx=0.01, fy=0.02, **kw):
    p = MapParams(**{"x": 0, "y": 0, "w": 40, "h": 30, "nx": 5, "ny": 4, "interpNx": 9, "interpNy": 7, **kw})
    hm = HeightMap(p)
    for i in range(p.ny):
        for j in range(p.nx):
            x, y = p.point(i, j)
            hm.values[i][j] = fx * x + fy * y
    return hm


# ------------------------------------------------------------------ .map I/O


def test_reads_candle_map_file():
    hm = HeightMap.from_map_text(CANDLE_MAP)
    p = hm.params
    assert (p.x, p.y, p.w, p.h, p.nx, p.ny, p.zBottom, p.zTop, p.feed) == (0, 0, 40, 30, 5, 4, -1, 1, 20)
    assert (p.interpNx, p.interpNy) == (41, 31)
    assert hm.values[0] == [0, 0.01, 0.02, 0.03, 0.04]  # row 0 = minimum Y
    assert hm.values[2][4] is None
    assert hm.probed == 19 and not hm.complete


def test_writes_candle_layout_and_round_trips():
    hm = HeightMap.from_map_text(CANDLE_MAP)
    text = hm.to_map_text()
    lines = text.split("\r\n")
    assert lines[0] == "candle map v3"
    assert lines[1:5] == ["0;0;40;30", "0;0", "5;4;-1;1;20", "0;41;31"]
    assert lines[7].endswith(";nan")
    assert text.endswith("\r\n")
    again = HeightMap.from_map_text(text)
    assert again.values == hm.values and again.params == hm.params


@pytest.mark.parametrize(
    "text",
    ["candle map v2\r\n", CANDLE_MAP.replace("5;4;", "1;4;"), CANDLE_MAP.rsplit("\r\n", 2)[0]],
)
def test_rejects_bad_map_files(text):
    with pytest.raises(MapError):
        HeightMap.from_map_text(text)


def test_param_validation_and_reset_rules():
    hm = plane_map()
    assert hm.set_params({"feed": 50}) is False and hm.complete  # does not touch values
    assert hm.set_params({"nx": 6}) is True and hm.probed == 0
    for bad in ({"w": 0}, {"nx": 1}, {"zTop": -5}, {"interpType": 1}, {"bogus": 1}, {"x": "a"}):
        with pytest.raises(MapError):
            hm.set_params(bad)


# ------------------------------------------------------------- interpolation


def test_interpolation_hits_grid_points_and_is_candle_catmull_rom():
    hm = HeightMap.from_map_text(CANDLE_MAP)
    hm.values[2][4] = 0.05
    for i in range(4):
        for j in range(5):
            x, y = hm.params.point(i, j)
            assert hm.height(x, y) == pytest.approx(hm.values[i][j], abs=1e-12)
    # Interior point, computed independently with Candle's formula.
    v = hm.values
    x, y = 15.0, 15.0  # cell ix=1, iy=1; t = 0.5, 0.5
    rows = [[v[r][c] for c in (0, 1, 2, 3)] for r in (0, 1, 2, 3)]
    expected = _cubic([_cubic(r, 0.5) for r in rows], 0.5)
    assert hm.height(x, y) == pytest.approx(expected)


def test_interpolation_reproduces_a_plane_inside_and_clamps_outside():
    hm = plane_map()
    # Exact everywhere, including edge cells (linear extrapolation of the phantom points).
    for x, y in [(12.3, 11.1), (20, 15), (27.7, 18.2), (12.3, 4.1), (1, 1), (39.5, 29.5)]:
        assert hm.height(x, y) == pytest.approx(0.01 * x + 0.02 * y, abs=1e-9)
    # A curved surface is reproduced well too (quadratic bowl, 5x4 grid).
    bowl = plane_map()
    for i in range(4):
        for j in range(5):
            x, y = bowl.params.point(i, j)
            bowl.values[i][j] = 0.0004 * ((x - 20) ** 2 + (y - 15) ** 2)
    for x, y in [(3, 2), (21, 16), (37, 28)]:
        # 10 mm grid spacing: a cubic follows a quadratic to ~1e-2 mm near corners
        assert bowl.height(x, y) == pytest.approx(0.0004 * ((x - 20) ** 2 + (y - 15) ** 2), abs=0.015)
    assert hm.height(-50, -50) == pytest.approx(hm.height(0, 0))
    assert hm.height(500, 15) == pytest.approx(hm.height(40, 15))


def test_incomplete_map_cannot_interpolate():
    hm = HeightMap.from_map_text(CANDLE_MAP)
    with pytest.raises(MapError):
        hm.height(39, 15)


# ------------------------------------------------------------------ transform


def run(text):
    """Independent interpreter: absolute machine path (mm) of a G0/G1-only program."""
    pos = [None, None, None]
    mm, absolute, shift = True, True, [0.0, 0.0, 0.0]
    pts = []
    for line in text.splitlines():
        code = line.split("(")[0].split(";")[0].upper().replace(" ", "")
        if not code:
            continue
        import re

        words = re.findall(r"([A-Z])([-+]?[\d.]+)", code)
        gs = [float(v) for k, v in words if k == "G"]
        if 20 in gs:
            mm = False
        if 21 in gs:
            mm = True
        if 90 in gs:
            absolute = True
        if 91 in gs:
            absolute = False
        ax = {k: float(v) * (1 if mm else 25.4) for k, v in words if k in "XYZ"}
        if 92 in gs:
            for i, a in enumerate("XYZ"):
                if a in ax:
                    shift[i] = pos[i] - ax[a]
            continue
        if not ax:
            continue
        for i, a in enumerate("XYZ"):
            if a in ax:
                pos[i] = ax[a] + shift[i] if absolute else pos[i] + ax[a]
        if None not in pos:
            pts.append(list(pos))
    return pts


def points_from(r, src_line):
    """Absolute output points produced by one source line (via line_map)."""
    out = r["gcode"].splitlines()
    pts = []
    for k in range(len(out)):
        p = run("\n".join(out[: k + 1]))
        if r["line_map"][k] == src_line and p:
            pts.append(p[-1])
    return pts


def test_linear_moves_subdivided_and_compensated():
    hm = plane_map()
    prog = "G21 G90\nG0 Z5\nG0 X0 Y0\nG1 Z-0.2 F100\nG1 X40 Y30 F300\nG0 Z5\n"
    r = transform(prog, hm)
    step = min(hm.params.step)
    cut = points_from(r, 4)
    prev = [0, 0]
    for p in cut:
        assert p[1] == pytest.approx(0.75 * p[0], abs=1e-3)  # on the original line
        assert p[2] == pytest.approx(-0.2 + hm.height(p[0], p[1]), abs=1e-3)
        assert math.hypot(p[0] - prev[0], p[1] - prev[1]) <= step + 1e-3
        prev = p
    assert cut[-1][:2] == pytest.approx([40, 30])
    assert r["stats"]["segments"] >= math.ceil(50 / step)
    assert len(r["line_map"]) == len(r["gcode"].splitlines())
    assert all("G0" in ln or "G1" in ln for ln in r["gcode"].splitlines() if ln.lstrip().startswith(("G1X", "G0X")))


def test_incremental_and_inch_programs():
    hm = plane_map()
    mm_prog = "G21 G90\nG0 X10 Y10 Z1\nG91\nG1 X10 F300\nG1 Y5\nG1 Z-1.5\nG1 X-10 Y-5\n"
    inch_prog = "G20 G90\nG0 X0.3937 Y0.3937 Z0.03937\nG91\nG1 X0.3937 F12\nG1 Y0.19685\nG1 Z-0.059055\nG1 X-0.3937 Y-0.19685\n"
    a = run(transform(mm_prog, hm)["gcode"])
    b = run(transform(inch_prog, hm)["gcode"])
    orig = run(mm_prog)
    assert a[-1][:2] == pytest.approx(orig[-1][:2], abs=6e-4)  # no accumulated rounding drift
    assert a[-1][2] == pytest.approx(orig[-1][2] + hm.height(*orig[-1][:2]), abs=2e-3)
    for p, q in zip(a, b):
        assert p == pytest.approx(q, abs=0.01)  # same path, inch output resolution
    assert "G20" in transform(inch_prog, hm)["gcode"]


def test_arcs_linearised_within_tolerance():
    hm = plane_map(0, 0)  # flat: isolates the arc geometry
    prog = "G21 G90 G17\nG0 X10 Y15 Z0\nG2 X30 Y15 I10 J0 F200\nG2 X10 Y15 R10\nG3 X30 Y15 I10 J0\n"
    r = transform(prog, hm, arc_tolerance=0.005)
    first, second, third = points_from(r, 2), points_from(r, 3), points_from(r, 4)
    for p in first + second + third:
        assert math.hypot(p[0] - 20, p[1] - 15) == pytest.approx(10, abs=1e-3)
    # Clockwise from the left point runs over the top, clockwise back runs under the bottom,
    # counter-clockwise from the left runs under the bottom.
    assert max(p[1] for p in first) == pytest.approx(25, abs=0.01) and min(p[1] for p in first) >= 15 - 1e-3
    assert min(p[1] for p in second) == pytest.approx(5, abs=0.01) and max(p[1] for p in second) <= 15 + 1e-3
    assert min(p[1] for p in third) == pytest.approx(5, abs=0.01)
    assert third[-1] == pytest.approx([30, 15, 0], abs=1e-3)
    # chord error: midpoints between consecutive points stay within tolerance of the circle
    for a, b in zip(first, first[1:]):
        mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
        assert 10 - math.hypot(mid[0] - 20, mid[1] - 15) <= 0.005 + 1e-3
    import re

    assert not re.search(r"G[23](?![\d.])", r["gcode"])


def test_program_g92_shift_tracked():
    hm = plane_map()
    prog = "G21 G90\nG0 X20 Y10 Z0\nG92 X0 Y0\nG1 X5 Y0 F100\n"
    pts = run(transform(prog, hm)["gcode"])
    assert pts[-1][:2] == pytest.approx([25, 10])
    assert pts[-1][2] == pytest.approx(hm.height(25, 10), abs=2e-3)


def test_uncompensable_lines_copied():
    hm = plane_map()
    prog = "G0 Z5\nG53 G0 Z-1\nG28\nG38.2 Z-5 F10\nM3 S1000\n(comment)\n"
    out = transform(prog, hm)["gcode"].splitlines()[3:]
    assert out[1:] == ["G53 G0 Z-1", "G28", "G38.2 Z-5 F10", "M3 S1000", "(comment)"]
    r = transform("G1 X5 Y5 Z0 F100\nX10\n", hm)  # modal G1 line after a known one
    assert r["gcode"].splitlines()[-1].startswith("G1X")


def test_outside_border_counted_and_incomplete_rejected():
    hm = plane_map()
    r = transform("G90 G0 X0 Y0 Z0\nG1 X60 Y0 F100\n", hm)
    assert r["stats"]["outside"] > 0 and any("outside" in w for w in r["stats"]["warnings"])
    hm.values[0][0] = None
    with pytest.raises(ValueError):
        transform("G0 X0", hm)


def test_bounds_use_cutting_moves_only():
    b = bounds("G21 G90\nG0 X-50 Y-50 Z5\nG1 Z-0.1 F100\nG1 X10 Y5\nG1 X12 Y8\nG0 Z5\nG0 X0 Y0\n")
    assert b["min"][:2] == [-50, -50] or b["min"][:2] == pytest.approx([-50, -50])  # plunge at the start point
    b2 = bounds("G21 G90\nG0 X1 Y1 Z5\nG1 X1 Y1 Z-0.1 F100\nG1 X10 Y5\nG1 X12 Y8\nG0 Z5\nG0 X100 Y100\n")
    assert b2["min"][:2] == pytest.approx([1, 1]) and b2["max"][:2] == pytest.approx([12, 8])


# ---------------------------------------------------------- service / library


def test_library_and_current_map_persist(tmp_path):
    svc = HeightmapService(EventBus(), MapLibrary(tmp_path))
    svc.set_params({"w": 20, "h": 10, "nx": 3, "ny": 2})
    svc.set_cells([{"i": 0, "j": 0, "z": 0.1}])
    svc.save("board A")
    assert [m["name"] for m in svc.library.names()] == ["board A"]
    with pytest.raises(MapError):
        svc.save("../evil")
    again = HeightmapService(EventBus(), MapLibrary(tmp_path))  # server restart
    assert again.hm.values[0][0] == 0.1 and again.name == "board A" and not again.dirty
    again.clear_values()
    again.load("board A")
    assert again.hm.values[0][0] == 0.1
    again.delete("board A")
    assert again.library.names() == [] and again.name is None
    with pytest.raises(MapError):
        again.set_cells([{"i": 9, "j": 0, "z": 1}])


def test_transform_cache_and_auto_border(tmp_path):
    svc = HeightmapService(EventBus(), MapLibrary(tmp_path))
    r = svc.auto_border("G90 G0 X5 Y5 Z1\nG1 Z-0.1 F100\nG1 X25 Y15\n", margin=1)
    assert r["border"] == {"x": 4, "y": 4, "w": 22, "h": 12, "originX": 4, "originY": 4}
    with pytest.raises(MapError):
        svc.transform("G0 X1")
    svc.hm = plane_map()
    a = svc.transform("G0 X1 Y1 Z0\nG1 X20 F100\n")
    assert svc.transform("G0 X1 Y1 Z0\nG1 X20 F100\n") is a
    svc.set_cells([{"i": 0, "j": 0, "z": 0.5}])
    assert svc.transform("G0 X1 Y1 Z0\nG1 X20 F100\n") is not a


# ------------------------------------------------------------------- probing


def wait_for(fn, timeout=20.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
    made = []

    def make(firmware="0.9j", time_scale=25.0):
        sim = GrblSimulator(firmware, time_scale=time_scale)
        bus = EventBus()
        ctl = Controller(bus, lambda p, b: SimTransport(sim), settings=SettingsStore(tmp_path / f"s{len(made)}.json"), poll_interval=0.03)
        maps = HeightmapService(bus, MapLibrary(tmp_path / f"state{len(made)}"))
        prober = Prober(ctl, maps)
        made.append((sim, ctl))
        ctl.connect("sim")
        wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.modal and ctl.model.settings and ctl.model.build)
        wait_for(lambda: ctl.model.status["state"] == "Idle")
        return sim, ctl, maps, prober

    yield make
    for sim, ctl in made:
        ctl.close()
        sim.close()


def finished(prober):
    wait_for(lambda: not prober.running)
    return prober.status


Z_OPTS = {"depth": 10, "fastFeed": 200, "slowFeed": 20, "backoff": 1, "plate": 1.5, "retract": 2, "twoStage": True, "setZero": True}


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_z_probe_sets_work_z_at_contact(rig, fw):
    sim, ctl, _maps, prober = rig(fw)
    ctl.command("G21 G0 X10 Y10 Z-25")  # 5 mm above the simulated surface
    ctl.command("G20")  # the user works in inches: must be restored afterwards
    wait_for(lambda: ctl.model.status["state"] == "Idle" and not sim.planner)
    prober.probe_z(Z_OPTS)
    st = finished(prober)
    assert st["state"] == "done", st
    contact = st["result"]["contact"][2]
    assert contact == pytest.approx(default_surface(10, 10), abs=0.002)
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    s = ctl.wait_status(lambda s: True, 2)
    work_z = s["wpos"][2] if s.get("wpos") else s["mpos"][2] - s["wco"][2]
    assert work_z == pytest.approx(1.5 + 2, abs=0.01)  # plate + retract, in mm (WPos reports mm with $13=0)
    assert sim.modal.units == 20 and sim.modal.distance == 90  # user's parser state restored


def test_z_probe_single_stage_without_zero(rig):
    sim, ctl, _maps, prober = rig()
    ctl.command("G0 Z-25")
    prober.probe_z({**Z_OPTS, "twoStage": False, "setZero": False, "retract": 0})
    st = finished(prober)
    assert st["state"] == "done" and "wcs" not in st["result"]
    assert sim.persist.coords[0][2] == 0


def test_z_probe_failure_reports_alarm(rig):
    sim, ctl, _maps, prober = rig()
    prober.probe_z({**Z_OPTS, "depth": 5})  # surface is 30 mm below
    st = finished(prober)
    assert st["state"] == "failed" and "did not touch" in st["message"]
    wait_for(lambda: ctl.model.status["state"] == "Alarm")
    assert ctl.busy is None


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_probe_pin_test(rig, fw):
    sim, ctl, _maps, prober = rig(fw)
    before = list(sim.pos)
    sim.probe_forced = True
    assert prober.test("closed")["ok"]
    assert not prober.test("open")["ok"]
    sim.probe_forced = False
    assert prober.test("open")["ok"]
    r = prober.test("closed")
    assert not r["ok"] and "did NOT trigger" in r["message"]
    assert sim.pos == pytest.approx(before) and sim.state == "Idle"


def setup_map(ctl, maps, sim, **kw):
    ctl.command("G0 X0 Y0 Z-28")
    wait_for(lambda: not sim.planner and sim.state == "Idle")
    ctl.command("G10 L20 P1 X0 Y0 Z0")  # work Z0 ~2 mm above the surface
    ctl.wait_status(lambda s: s["state"] == "Idle", 2)
    maps.set_params({"x": 0, "y": 0, "w": 40, "h": 30, "nx": 5, "ny": 4, "originX": 0, "originY": 0, "zTop": 1, "zBottom": -4, "feed": 400, **kw})


def test_heightmap_probe_matches_surface_and_survives_reload(rig):
    sim, ctl, maps, prober = rig()
    setup_map(ctl, maps, sim)
    prober.probe_map()
    snap = wait_for(lambda: (s := ctl.bus.snapshot()["retained"]) and s["probe"]["state"] == "running" and s["probe"]["index"] >= 2 and s)
    # A reloading SPA gets the running state and partial map from the snapshot.
    assert snap["heightmap"]["probed"] >= 2
    with pytest.raises(ControllerError, match="busy"):
        ctl.command("G0 X0", source="console")
    with pytest.raises(ControllerError, match="busy"):
        ctl.jogger.step([1, 0, 0], 100)
    with pytest.raises(MapError):
        maps.set_params({"nx": 3})
    st = finished(prober)
    assert st["state"] == "done", st
    hm = maps.hm
    assert hm.complete
    ref = default_surface(0, 0)
    for i in range(4):
        for j in range(5):
            x, y = hm.params.point(i, j)
            assert hm.values[i][j] == pytest.approx(default_surface(x, y) - ref, abs=0.002)
    assert ctl.busy is None and maps.locked_by is None


def test_heightmap_probe_abort_via_stop(rig):
    sim, ctl, maps, prober = rig("0.9j", time_scale=5.0)
    setup_map(ctl, maps, sim, nx=4, ny=3)
    prober.probe_map()
    wait_for(lambda: prober.status.get("index", 0) >= 1)
    ctl.safe_stop()
    st = finished(prober)
    assert st["state"] == "aborted"
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    time.sleep(0.3)
    assert sim.state == "Idle"  # stopped without an alarm
    assert 1 <= maps.hm.probed < 12  # partial data kept
    assert ctl.busy is None and maps.locked_by is None


def test_heightmap_probe_refused_unless_idle(rig):
    sim, ctl, maps, prober = rig("0.9j", time_scale=1.0)
    ctl.command("G1 X30 F300", source="console")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    with pytest.raises(ControllerError, match="Idle"):
        prober.probe_map()
