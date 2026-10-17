"""Phase 4: job library, analysis, estimator, runner (simulator)."""

import math
import time

import pytest

from gcsender import controller as controller_mod
from gcsender.controller import Controller, ControllerError
from gcsender.events import EventBus
from gcsender.heightmap.model import MapLibrary
from gcsender.heightmap.service import HeightmapService
from gcsender.jobs.analyze import clean_line, state_before
from gcsender.jobs.estimate import Machine, estimate
from gcsender.jobs.library import JobLibrary, Variant
from gcsender.jobs.runner import JobRunner
from gcsender.simulator import GrblSimulator
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport


def wait_for(fn, timeout=20.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met")


# --------------------------------------------------------------- analysis


def test_clean_line_strips_comments_and_spaces():
    assert clean_line("g1 x10 (move) y 2.5 ; done") == "G1X10Y2.5"
    assert clean_line("(only a comment)") == ""
    assert clean_line("%") == ""


def test_state_before_tracks_modal_state():
    lines = [clean_line(x) for x in [
        "G20 G90 G17 G55", "M3 S9000", "M8", "G0 X1 Y2 Z0.5", "G1 Z-0.01 F10", "G91 X0.5", "M9", "G2 X1 Y0 I0.5",
    ]]
    s = state_before(lines, 6)
    assert (s["units"], s["distance"], s["wcs"], s["motion"], s["feed"]) == ("G20", "G91", "G55", "G1", 10)
    assert (s["spindle"], s["speed"], s["coolant"]) == ("M3", 9000, ["M8"])
    assert s["pos"] == pytest.approx([1.5 * 25.4, 2 * 25.4, -0.01 * 25.4])
    assert state_before(lines, 7)["coolant"] == []


def test_variant_flags_long_lines_m6_and_bounds():
    m = Machine()
    text = "G90 G0 X0 Y0 Z1\nG1 X10 Y5 F100\nT2 M6\n" + "G1 X1." + "0" * 80 + "\n"
    v = Variant.build("original", text, m, 79)
    assert v.too_long == [3]
    assert v.m6_lines == [2]
    assert v.bounds["max"][:2] == pytest.approx([10, 5])


# -------------------------------------------------------------- estimator

M = Machine(rates=(1500, 1500, 1500), accels=(10, 10, 10), junction_deviation=0.01)


def est(*lines, m=M):
    return sum(estimate([clean_line(x) for x in ("G90 G0 X0 Y0 Z0",) + lines], m))


def test_estimate_trapezoid_and_triangle():
    assert est("G1 X100 F600") == pytest.approx(100 / 10 + 10 / 10)  # cruise + accel/decel
    assert est("G1 X1 F600") == pytest.approx(2 * math.sqrt(1 / 10))  # never reaches speed


def test_estimate_rapids_use_axis_max_rates_and_dwell_counts():
    # diagonal rapid: each axis at 1500 mm/min -> 25*sqrt(2) mm/s along the path
    t = est("G0 X300 Y300", m=Machine(rates=(1500, 1500, 1500), accels=(1e9, 1e9, 1e9)))
    assert t == pytest.approx(300 * math.sqrt(2) / (25 * math.sqrt(2)), rel=1e-6)
    assert est("G4 P2.5") == pytest.approx(2.5)


def test_estimate_corners_slow_down_straight_lines_do_not():
    straight = est("G1 X10 F1200", "G1 X20", "G1 X30")
    one = est("G1 X30 F1200")
    assert straight == pytest.approx(one, rel=1e-9)
    zigzag = est("G1 X10 F1200", "G1 X10 Y10", "G1 X20 Y10")  # two 90 degree corners
    assert zigzag > est("G1 X30 F1200")


def test_estimate_50k_lines_fast():
    lines = ["G21G90", "G0X0Y0Z1"] + [f"G1X{50+40*math.cos(i/200):.3f}Y{50+40*math.sin(i/200):.3f}F600" for i in range(50000)]
    t0 = time.monotonic()
    estimate(lines, M)
    assert time.monotonic() - t0 < 5


# -------------------------------------------------------------- library


def test_library_round_trip(tmp_path):
    lib = JobLibrary(tmp_path)
    meta = lib.add("../../evil name.nc", b"G0 X1\nG1 X2 F100\n")
    assert meta["name"] == "_evil name.nc" or "/" not in meta["name"]
    assert lib.get(meta["id"])[1] == "G0 X1\nG1 X2 F100\n"
    assert [m["id"] for m in lib.list()] == [meta["id"]]
    lib.delete(meta["id"])
    assert lib.list() == []
    with pytest.raises(ValueError):
        lib.get("../../etc")
    with pytest.raises(ValueError):
        lib.add("x", b"\x00\x01binary")


# ------------------------------------------------------------------ runner


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
    made = []

    def make(firmware="0.9j", time_scale=10.0):
        sim = GrblSimulator(firmware, time_scale=time_scale)
        bus = EventBus()
        ctl = Controller(bus, lambda p, b: SimTransport(sim), settings=SettingsStore(tmp_path / f"s{len(made)}.json"), poll_interval=0.03)
        maps = HeightmapService(bus, MapLibrary(tmp_path / f"m{len(made)}"))
        jobs = JobRunner(ctl, maps, JobLibrary(tmp_path / f"j{len(made)}"))
        made.append((sim, ctl))
        ctl.connect("sim")
        wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.modal and ctl.model.settings and ctl.model.build)
        wait_for(lambda: ctl.model.status["state"] == "Idle")
        return sim, ctl, maps, jobs

    yield make
    for sim, ctl in made:
        ctl.close()
        sim.close()


def load(jobs, text, name="t.nc"):
    meta = jobs.library.add(name, text.encode())
    jobs.select(meta["id"])
    return meta


def finished(jobs, timeout=30):
    wait_for(lambda: not jobs.active, timeout)
    return jobs.state


SQUARE = "G21 G90\nG0 X0 Y0 Z1\nG1 Z-0.1 F300\nG1 X10 F600\nG1 Y10\nG1 X0\nG1 Y0\nG0 Z1\n"


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_job_runs_to_completion_with_end_commands(rig, fw):
    sim, ctl, _maps, jobs = rig(fw)
    ctl.update_settings({"jobEndCommands": "M5\nG0 Z5"})
    load(jobs, "(header)\n\n" + SQUARE)
    jobs.start()
    assert finished(jobs) == "done"
    assert jobs.acked == len(jobs.variant.cmds) - 1 and jobs.error_count == 0
    assert sim.pos == pytest.approx([0, 0, 5])
    assert ctl.busy is None


def test_progress_is_in_the_retained_snapshot_for_a_reloading_spa(rig):
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    load(jobs, SQUARE.replace("F600", "F300"))
    jobs.start()
    snap = wait_for(lambda: (s := ctl.bus.snapshot()["retained"]["job"]) and s["state"] == "running" and s["acked"] >= 4 and s)
    assert snap["job"]["name"] == "t.nc" and snap["sent"] >= snap["acked"]
    assert snap["eta"] is not None and snap["elapsed"] is not None
    with pytest.raises(ControllerError, match="busy"):
        ctl.command("G0 X0", source="console")
    with pytest.raises(ControllerError, match="busy"):
        ctl.jogger.step([1, 0, 0], 100)
    jobs.stop()
    assert finished(jobs) == "stopped"


def test_pause_resume_and_stop_without_alarm(rig):
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\nG1 X30 F300\nG1 Y30\n")
    jobs.start()
    wait_for(lambda: jobs.state == "running" and sim.pos[0] > 1)
    jobs.pause()
    wait_for(lambda: ctl.model.status["state"] == "Hold")
    time.sleep(0.4)  # deceleration
    held = sim.pos[0]
    time.sleep(0.4)
    assert sim.pos[0] == pytest.approx(held, abs=1e-6)  # stays put while paused
    assert jobs.state == "paused"
    jobs.resume()
    wait_for(lambda: sim.pos[0] > held + 1)
    r = jobs.stop()
    assert r["method"] == "hold-reset"
    assert finished(jobs) == "stopped"
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    time.sleep(0.3)
    assert sim.state == "Idle"  # no alarm
    assert 0 < sim.pos[0] < 30


def test_error_holds_and_continue_or_ignore(rig):
    sim, ctl, _maps, jobs = rig()
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\nG1 X5 F600\nG99\nG1 X10\n")
    jobs.start()
    wait_for(lambda: jobs.state == "error")
    assert jobs.held_error["line"] == 3 and "G99" in jobs.held_error["command"]
    jobs.resume()
    assert finished(jobs) == "done"
    assert sim.pos[0] == pytest.approx(10) and jobs.error_count == 1
    ctl.update_settings({"ignoreErrors": True})
    jobs.start()
    assert finished(jobs) == "done" and jobs.error_count == 1


def test_stop_reports_the_executing_line(rig):
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    load(jobs, "G21 G90\nG0 X0 Y0 Z1\nG1 X20 F300\nG99 X5\nG1 X0\n")
    jobs.start()
    wait_for(lambda: jobs.state == "error")
    jobs.stop()
    assert finished(jobs) == "stopped"
    assert jobs.message == "stopped at line 3"  # the G1 X20 move was cut short


def test_m6_pauses_for_tool_change_and_allows_jogging(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"toolChangeCommands": "M5\nG0 Z20"})
    load(jobs, "G21 G90\nM3 S500\nG0 X0 Y0 Z1\nG1 X5 F600\nT2 M6\nG0 Z1\nG1 X10 F600\n")
    jobs.start()
    wait_for(lambda: jobs.state == "toolchange")
    assert jobs.toolchange == {"line": 4, "tool": 2}
    assert sim.pos[2] == pytest.approx(20) and sim.modal.spindle == "M5"
    ctl.jogger.step([0, 0, -1], 300)  # the user may jog during a tool change
    wait_for(lambda: not sim.planner and sim.state == "Idle")
    jobs.resume()
    assert finished(jobs) == "done"
    assert sim.pos[0] == pytest.approx(10)


def test_m6_ignored_when_configured(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"toolChangeMode": "ignore"})
    load(jobs, "G21 G90\nG0 X0 Y0 Z1\nM6 T3\nG1 X3 F600\n")
    jobs.start()
    assert finished(jobs) == "done" and jobs.error_count == 0


def test_start_from_line_enters_safely(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"spindleDwell": 0.5})
    text = "G21 G90\nM3 S800\nG0 X0 Y0 Z2\nG1 Z-0.5 F200\nG1 X10 F600\nG1 Y10\nG1 X0\nG0 Z5\n"
    load(jobs, text)
    jobs.start(from_line=5)  # G1 Y10, starting from (10, 0, -0.5)
    assert finished(jobs) == "done"
    out = [e["line"] for e in ctl.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    assert out[:2] == ["G21G90G17G54", "G0Z5"]
    # plunge at min(program feed 600, entryPlungeFeed 100)
    assert "G0X10Y0" in out and "M3S800" in out and "G1Z-0.5F100" in out and "G4P0.5" in out
    assert out.index("G0X10Y0") < out.index("M3S800") < out.index("G4P0.5") < out.index("G1Z-0.5F100")
    assert "G21G90G1F600" in out  # the program's modal state is restored before line 5
    assert sim.pos == pytest.approx([0, 10, 5])


def test_start_from_line_refused_when_position_unknown_or_g92(rig):
    sim, ctl, _maps, jobs = rig()
    load(jobs, "G21 G91\nG1 X1 F100\nG1 X1\n")
    with pytest.raises(ControllerError, match="not known"):
        jobs.start(from_line=2)
    load(jobs, "G21 G90\nG0 X1 Y1 Z1\nG92 X0 Y0\nG1 X1 F100\n")
    with pytest.raises(ControllerError, match="G92"):
        jobs.start(from_line=3)


def test_software_feed_override_rewrites_f_on_09(rig):
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\n" + "\n".join(f"G1 X{i} F600" if i == 1 else f"G1 X{i}" for i in range(1, 60)) + "\n")
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.sent > 5)
    jobs.set_override("feed", 50)
    wait_for(lambda: sim.modal.feed == pytest.approx(300), timeout=20)
    jobs.set_override("feed", 100)
    wait_for(lambda: sim.modal.feed == pytest.approx(600), timeout=20)
    jobs.stop()
    finished(jobs)
    with pytest.raises(ControllerError, match="1.1"):
        jobs.set_override("rapid", 50)


def test_realtime_overrides_on_11(rig):
    sim, ctl, _maps, jobs = rig("1.1f")
    jobs.set_override("feed", 130)
    jobs.set_override("spindle", 80)
    jobs.set_override("rapid", 25)
    wait_for(lambda: (sim.feed_ov, sim.spindle_ov, sim.rapid_ov) == (130, 80, 25))
    jobs.set_override("feed", 100)
    wait_for(lambda: sim.feed_ov == 100)


def test_heightmap_variant_is_streamed(rig):
    sim, ctl, maps, jobs = rig()
    maps.set_params({"x": 0, "y": 0, "w": 10, "h": 10, "nx": 2, "ny": 2, "interpNx": 3, "interpNy": 3})
    maps.set_cells([{"i": i, "j": j, "z": 0.5 * j} for i in range(2) for j in range(2)])
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\nG1 X10 F600\n")
    jobs.set_heightmap(True)
    assert jobs.variant.kind == "transformed" and "heightmap transform" in jobs.text()
    jobs.start()
    assert finished(jobs) == "done"
    assert sim.pos == pytest.approx([10, 0, 0.5], abs=1e-3)
    maps.set_cells([{"i": 0, "j": 1, "z": 1.0}])  # the map changes: start re-transforms
    assert jobs.snapshot()["heightmap"]["stale"]
    jobs.start()
    assert finished(jobs) == "done"
    assert sim.pos[2] == pytest.approx(1.0, abs=1e-3)


def test_eta_matches_simulated_run_within_15_percent(rig):
    # The simulator moves at constant speed; give the estimator the same model.
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=4.0)
    for sid in (120, 121, 122):
        ctl.write_settings({sid: 100000.0})
    prog = "G21 G90\nG0 X0 Y0 Z1\nG1 Z0 F300\n" + "\n".join(
        f"G1 X{30 + 25 * math.cos(i / 40):.3f} Y{30 + 25 * math.sin(i / 40):.3f} F900" for i in range(260)
    ) + "\nG0 Z5\n"
    load(jobs, prog)
    predicted = jobs.variant.total
    t0 = time.monotonic()
    jobs.start()
    assert finished(jobs, timeout=60) == "done"
    actual = (time.monotonic() - t0) * 4.0
    assert actual == pytest.approx(predicted, rel=0.15)


def test_alarm_ends_the_job(rig):
    sim, ctl, _maps, jobs = rig()
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\nG38.2 Z-1 F100\nG1 X5 F600\n")  # probe misses: ALARM
    jobs.start()
    assert finished(jobs) == "failed"
    assert "ALARM" in jobs.message


def test_long_lines_refused(rig):
    sim, ctl, _maps, jobs = rig()
    load(jobs, "G1 X1" + "0" * 90 + "\n")
    with pytest.raises(ControllerError, match="longer"):
        jobs.start()


def test_executing_line_lags_acknowledged_lines(rig):
    # GRBL acks a line when it enters the planner; the ETA must not assume it has run.
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\n" + "\n".join(f"G1 X{i} Y{i % 2} F600" for i in range(1, 80)) + "\n")
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 30)
    snap = jobs.snapshot()
    assert snap["executing"] < snap["acked"] - 10
    jobs.stop()
    finished(jobs)
    assert jobs.executing() == jobs.acked


def test_m0_program_pause_can_be_resumed_from_the_job(rig):
    sim, ctl, _maps, jobs = rig()
    load(jobs, "G21 G90\nG0 X0 Y0 Z0\nG1 X2 F600\nM0\nG1 X4\n")
    jobs.start()
    wait_for(lambda: ctl.model.status["state"] == "Hold" and jobs.state == "running")
    with pytest.raises(ControllerError, match="Resume"):
        jobs.pause()
    jobs.resume()
    assert finished(jobs) == "done"
    assert sim.pos[0] == pytest.approx(4)


def test_start_from_line_raises_when_the_program_uses_another_wcs(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.command("G0 Z10", source="ui")  # already above safe Z in G54...
    wait_for(lambda: ctl.model.status["state"] == "Idle" and ctl.model.status["wpos"][2] == pytest.approx(10))
    load(jobs, "G21 G90 G55\nG0 X0 Y0 Z2\nG1 X5 F300\nG1 X6\n")
    jobs.start(from_line=3)
    assert finished(jobs) == "done"
    out = [e["line"] for e in ctl.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    assert out[:2] == ["G21G90G17G55", "G0Z5"]  # ...but the program is in G55: raise anyway
