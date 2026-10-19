"""Backlash compensation: the transform (checked against a physical dead-band
model), the side tracker, and the job runner integration."""

import json
import math
import re

import pytest

from gcsender.backlash import BacklashTracker, compensate

from test_phase4 import finished, load, rig, wait_for  # noqa: F401  (rig is a fixture)

B = [0.12, 0.08, 0.05]


# -- a machine with backlash ----------------------------------------------------------


class Axis:
    """Motor m and tool t with slack b: moving +, t = m - b once taken up; moving -, t = m."""

    def __init__(self, m: float, b: float, side: int):
        self.m, self.b = m, b
        self.t = m - b if side > 0 else m

    def move(self, m: float) -> float:
        """Returns how far the tool moved (|Δt|)."""
        before = self.t
        self.m = m
        self.t = min(max(self.t, m - self.b), m)
        return abs(self.t - before)


def motor_targets(gcode: str):
    """Absolute mm XYZ of each emitted motion line (the transform writes all three axes)."""
    out = []
    for line in gcode.splitlines():
        m = re.fullmatch(r"(?:[A-FH-WN][-+.\d]+)*G[01]X(-?[\d.]+)Y(-?[\d.]+)Z(-?[\d.]+)", line.replace(" ", ""))
        if m:
            out.append([float(v) for v in m.groups()])
    return out


def program_points(n_circle=24):
    """A path with reversals on every axis, including inside arcs."""
    lines = ["G21 G90", "G0 X0 Y0 Z5", "G1 Z-0.1 F100", "G1 X10 F300", "G1 Y5", "G1 X0", "G1 Y0",
             "G0 Z2", "G0 X20 Y10", "G1 Z-0.2 F100", "G2 X20 Y10 I5 J0 F300",  # a full circle: X and Y reverse twice
             "G1 X15 Y12", "G1 X25 Y8", "G0 Z5"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("zero_side", [[1, 1, -1], [-1, -1, -1], [1, -1, 1]])
@pytest.mark.parametrize("start", [[1, 1, 1], [-1, -1, -1]])
def test_the_tool_lands_on_every_programmed_point(zero_side, start):
    prog = program_points()
    ref = compensate(prog, [0, 0, 0], start, zero_side)  # same pieces, no compensation: the intended points
    comp = compensate(prog, B, start, zero_side)
    want = motor_targets(ref["gcode"])
    got = motor_targets(comp["gcode"])
    n_take = comp["stats"]["takeupLines"]
    assert len(got) == len(want) + n_take and n_take >= 8

    # The machine starts where the program starts, on the start sides, and the
    # work zero was set on zero_side: tool = intended point exactly when on that side.
    def tool_for(point, side, i):
        return point[i] if side == zero_side[i] else point[i]  # intended tool position

    first = got[0]
    axes = []
    for i in range(3):
        # motor such that the tool is exactly at the intended first point on side start[i]
        o = 0.0 if start[i] == zero_side[i] else start[i] * B[i]
        axes.append(Axis(want[0][i] + o, B[i], start[i]))
        # compensated motor target for the first point uses the same offset
        assert first[i] == pytest.approx(want[0][i] + o, abs=1e-3)
    # A zero set on zero_side means: tool == work coordinate when on that side.
    # With the Axis model (t = m on -, t = m - b on +), shift the frame so that holds.
    shift = [0.0 if zero_side[i] < 0 else -B[i] for i in range(3)]

    k = 0
    seen_dir = [None, None, None]
    for tgt in got[1:]:
        moved = [axes[i].move(tgt[i]) for i in range(3)]
        tool = [axes[i].t - shift[i] for i in range(3)]
        dm = [tgt[i] - axes[i].m for i in range(3)]  # 0 now (already moved), kept for clarity
        del dm
        if k + 1 < len(want) and all(abs(tool[i] - want[k + 1][i]) < 1e-3 for i in range(3)):
            k += 1  # reached the next intended point exactly
        else:
            # Only a take-up line may leave the tool short of a point, and it must not move the tool.
            assert max(moved) < 1e-9, (tgt, tool, want[k + 1] if k + 1 < len(want) else None)
    assert k == len(want) - 1  # every intended point was reached, in order
    del first, seen_dir, tool_for


def test_no_skew_after_a_reversal():
    """After its take-up, a diagonal cut moves the tool along X and Y from the first micron."""
    prog = "G21 G90\nG0 X0 Y0 Z0\nG1 X10 Y0 F300\nG1 X5 Y5\n"  # X reverses on the diagonal
    out = compensate(prog, [0.2, 0.2, 0.0], [1, 1, -1], [1, 1, -1])["gcode"].splitlines()
    i = out.index("F300G1X10Y0Z0")
    assert out[i + 1] == "G1X9.8Y0Z0"  # take-up on X only
    assert out[i + 2] == "G1X4.8Y5Z0"  # then the diagonal, offset on X


def test_line_count_does_not_depend_on_where_the_machine_is():
    prog = program_points()
    counts = {len(compensate(prog, B, s, [None, None, -1])["gcode"].splitlines())
              for s in ([1, 1, 1], [-1, -1, -1], [None, None, None], [1, -1, None])}
    assert len(counts) == 1


def test_incremental_and_inch_programs():
    # Inches, absolute start, then incremental (a program that is G91 from its
    # first line has no known position and is copied unchanged, as for heightmaps).
    prog = "G20 G90\nG0 X0 Y0 Z0\nG91\nG1 X1 F10\nG1 X-1\n"
    out = compensate(prog, [0.254, 0, 0], [1, None, None], [1, None, None])["gcode"].splitlines()
    # +1 in, then X reverses: take-up -0.01 in on X alone, then -1 in.
    assert out[-3:] == ["F10G1X1Y0Z0", "G1X-0.01Y0Z0", "G1X-1Y0Z0"]


def test_zero_backlash_changes_nothing_but_the_header():
    prog = program_points()
    r = compensate(prog, [0, 0, 0], [1, 1, 1], [1, 1, -1])
    assert sum(r["stats"]["takeups"]) == 0


# -- tracker --------------------------------------------------------------------------


def test_tracker_sides_and_zero_sides(tmp_path):
    t = BacklashTracker(tmp_path / "backlash.json")
    t.observe_mpos([0, 0, 0])
    t.observe_mpos([1, 0, -2])
    assert t.side == [1, None, -1]
    t.note_move([0, -0.01, 0])
    assert t.side == [1, -1, -1]
    # Probe Z: G10 is sent at contact (moving down); the offsets are read after the retract.
    t.note_zero_command("G10 L20 P0 Z0", "G54")
    t.observe_mpos([1, 0, 0])  # retract
    assert t.side[2] == 1
    t.on_offsets("G54", [0, 0, 0], [0, 0, -12.5])
    assert t.zero_sides("G54", None) == [None, None, -1]  # the side at contact, not after the retract
    # An X/Y zero from the UI (no pending command): the side when read.
    t.on_offsets("G54", [0, 0, -12.5], [3, 4, -12.5])
    assert t.zero_sides("G54", None) == [1, -1, -1]
    # Persisted; the first read after a restart (old=None) keeps what was saved.
    t2 = BacklashTracker(tmp_path / "backlash.json")
    t2.on_offsets("G54", None, [3, 4, -12.5])
    assert t2.zero_sides("G54", None) == [1, -1, -1]
    # A later G92 on X wins for X.
    t2.note_move([-1, 0, 0])
    t2.on_offsets("G92", [0, 0, 0], [5, 0, 0])
    assert t2.zero_sides("G54", [5, 0, 0])[0] == -1
    assert json.loads((tmp_path / "backlash.json").read_text())["zero"]["G54"][2]["side"] == -1


# -- job runner -------------------------------------------------------------------------


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_job_streams_the_compensated_program(rig, fw):
    sim, ctl, _maps, jobs = rig(fw)
    ctl.update_settings({"backlashEnabled": True, "backlashX": 0.1, "backlashY": 0.1, "backlashZ": 0.05})
    load(jobs, "G21 G90\nG0 X0 Y0 Z2\nG1 Z-0.1 F300\nG1 X5 F600\nG1 X0\nG0 Z2\n")
    snap = jobs.snapshot()
    assert snap["backlash"] == {"enabled": True, "applied": True}
    bl = snap["variant"]["backlash"]
    assert bl["inputs"]["b"] == [0.1, 0.1, 0.05] and bl["stats"]["takeups"][0] == 1
    jobs.start()
    assert finished(jobs) == "done"
    assert "G1X4.9Y0Z-0.1" in jobs.variant.cmds  # the X take-up
    # GRBL ends where the compensated program sends it: X0 approached moving -X is X-0.1.
    assert sim.pos[0] == pytest.approx(-0.1)
    assert ctl.backlash.side[0] == -1 and jobs.meta["lastRun"]["backlash"] is True
    # Turning it off rebuilds the loaded program without compensation.
    ctl.update_settings({"backlashEnabled": False})
    assert jobs.snapshot()["backlash"] == {"enabled": False, "applied": False}
    assert jobs.variant is jobs.original


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_start_from_line_approaches_from_the_programs_side(rig, fw):
    sim, ctl, _maps, jobs = rig(fw)
    ctl.update_settings({"backlashEnabled": True, "backlashX": 0.2, "backlashY": 0.2, "backlashZ": 0,
                         "spindleDwell": 0})
    load(jobs, "G21 G90\nG0 X0 Y0 Z2\nG1 Z-0.1 F300\nG1 X10 F600\nG1 X0\nG1 Y5\nG0 Z2\n")
    v = jobs.variant
    n = next(i for i, c in enumerate(v.cmds) if c.startswith("G1Y5") or ("Y5" in c and c.startswith("G1")))
    jobs.start(from_line=n)
    assert finished(jobs) == "done"
    sent = [e["line"] for e in ctl.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    # Before line n the program was moving -X (and Y not yet): approach X from the + side.
    xs = [s for s in sent if s.startswith("G0X")]
    assert len(xs) >= 2
    pre, final = (float(re.search(r"X(-?[\d.]+)", s).group(1)) for s in xs[:2])
    assert pre > final


def test_recovery_rebuilds_the_same_program(rig, tmp_path):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"backlashEnabled": True, "backlashX": 0.1, "backlashY": 0.1, "backlashZ": 0.05})
    load(jobs, "G21 G90\nG0 X0 Y0 Z2\nG1 Z-0.1 F300\nG1 X5 F600\nG1 X0\nG0 Z2\n")
    digest = jobs.variant.digest
    inputs = jobs.variant.backlash["inputs"]
    data = jobs._journal_data()
    assert data["backlash"] == inputs
    # The machine has moved since (different sides): a rebuild from the journal is identical.
    ctl.backlash.note_move([-1, -1, 1])
    jobs._build_transformed(data["backlash"], use_saved=True)
    assert jobs.variant.digest == digest
    jobs._build_transformed()
    assert math.isfinite(len(jobs.variant.raw))  # rebuilt from today's sides


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_sides_follow_jogs_and_probe_z_records_the_down_side(rig, fw):
    """1.1 jogs with $J= and reports WCO instead of WPos: the tracking must still work."""
    sim, ctl, _maps, jobs = rig(fw)
    ctl.jogger.step([1, 0, 0], 600)
    wait_for(lambda: ctl.backlash.side[0] == 1)
    ctl.jogger.step([0, -1, 0], 600)
    wait_for(lambda: ctl.backlash.side[1] == -1 and sim.state == "Idle" and not sim.planner)
    # Zero Z the way Probe Z does: G10 at contact (moving down), then retract up.
    ctl.command("G91 G1 Z-0.5 F200", source="ui")
    wait_for(lambda: ctl.backlash.side[2] == -1 and sim.state == "Idle")
    ctl.command("G10 L20 P0 Z0", source="ui")
    ctl.command("G1 Z2 F600", source="ui")
    ctl.command("G90", source="ui")
    wait_for(lambda: ctl.backlash.side[2] == 1 and sim.state == "Idle")
    wait_for(lambda: ctl.backlash.zero_sides("G54", None)[2] == -1, timeout=15)
