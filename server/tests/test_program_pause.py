"""M0/M1 program pauses (e.g. a CAM tool change written as (MSG, ...) + M0) and
progress that stays current while GRBL stops acknowledging (bug report
20261017-185144-b289: the UI stopped at line 1055 while the job sat at the M0)."""

import time

import pytest

from gcsender.jobs.library import find_pauses

from test_phase4 import finished, load, rig, wait_for  # noqa: F401  (rig is a fixture)

# Shaped like the report's program (FlatCAM-style tool change with M0).
TOOL_CHANGE = """G21 G90
G64 P0.01016
G00 Z25.00000 (Retract to tool change height)
T0
M5      (Spindle stop.)
G04 P0.2 (Wait for spindle to stop)
(MSG, Change tool bit to mill diameter 3.00000mm)
M0      (Temporary machine stop.)
M3 S1000 ( Spindle on clockwise. )
G00 Z2.00000
{cut}
G00 Z25.00000 (Retract to tool change height)
T1
M5      (Spindle stop.)
G04 P0.2
(MSG, Change tool bit to mill diameter 0.21000mm)
M0      (Temporary machine stop.)
M3 ( Spindle on clockwise. )
G00 Z2.00000
G01 F200 X5 Y5
M5
"""


def program(n=60):
    cut = "G01 F2000\n" + "\n".join(f"G01 X{(i % 10) * 0.5:.3f} Y{(i // 10) * 0.5:.3f}" for i in range(n))
    return TOOL_CHANGE.replace("{cut}", cut)


def test_pauses_and_messages_are_found():
    text = program(5)
    raw = text.splitlines()
    from gcsender.jobs.library import clean_line

    pauses = find_pauses(raw, [clean_line(r) for r in raw])
    assert [(p["code"], p["message"]) for p in pauses] == [
        ("M0", "Change tool bit to mill diameter 3.00000mm"),
        ("M0", "Change tool bit to mill diameter 0.21000mm"),
    ]
    raw2 = ["G0 X1", "M1 (MSG, check the clamp)", "M01", "M10", "M30", "M0.5"]
    p2 = find_pauses(raw2, [clean_line(r) for r in raw2])
    assert [(p["line"], p["code"], p["message"]) for p in p2] == [(1, "M1", "check the clamp"), (2, "M1", None)]


def test_m0_pauses_with_the_message_allows_jogging_and_continues(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"ignoreErrors": True})  # G64 is not GRBL
    load(jobs, program())
    assert len(jobs.variant.pauses) == 2
    jobs.start()
    wait_for(lambda: jobs.state == "toolchange", timeout=20)
    first = jobs.variant.pauses[0]["line"]
    assert jobs.toolchange == {"line": first, "tool": None, "kind": "M0", "message": "Change tool bit to mill diameter 3.00000mm"}
    assert "3.00000mm" in jobs.message
    # The M0 was not sent: GRBL is Idle (not Hold), so the user can jog.
    wait_for(lambda: sim.state == "Idle")
    ctl.jogger.step([0, 0, 1], 300)
    wait_for(lambda: not sim.planner and sim.state == "Idle")
    jobs.resume()
    wait_for(lambda: jobs.state == "toolchange" and (jobs.toolchange or {}).get("line") not in (None, first), timeout=30)
    second = jobs.variant.pauses[1]["line"]
    # Progress is current when the job pauses (the UI showed a stale line in the report).
    snap = ctl.bus.retained("job")
    assert snap["state"] == "toolchange" and snap["acked"] >= second - 1 and snap["toolchange"]["message"].endswith("0.21000mm")
    jobs.resume()
    assert finished(jobs) == "done"
    tx = [e["line"] for e in ctl.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    assert not any(t.startswith("M0") for t in tx)


def test_m1_is_skipped_when_optional_stop_is_off(rig):
    sim, ctl, _maps, jobs = rig()
    ctl.update_settings({"optionalStop": False})
    load(jobs, "G21 G90\nG0 X1\nM1\nG0 X2\n")
    jobs.start()
    assert finished(jobs) == "done" and sim.pos[0] == pytest.approx(2)
    ctl.update_settings({"optionalStop": True})
    load(jobs, "G21 G90\nG0 X1\nM1\nG0 X2\n")
    jobs.start()
    wait_for(lambda: jobs.state == "toolchange" and (jobs.toolchange or {}).get("kind") == "M1")
    jobs.resume()
    assert finished(jobs) == "done"


def test_progress_is_published_while_the_runner_is_blocked(rig):
    """A burst of acks inside the throttle window must still reach the UI."""
    sim, ctl, _maps, jobs = rig("0.9j", time_scale=1.0)
    # Long slow moves fill GRBL's buffer; the runner blocks in send().
    load(jobs, "G21 G90\nG0 X0 Y0\n" + "\n".join(f"G1 X{i % 2 * 3} Y{i * 0.1:.1f} F600" for i in range(40)) + "\n")
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 3, timeout=20)
    for _ in range(6):
        time.sleep(0.6)
        published = ctl.bus.retained("job")
        live = jobs.snapshot()
        assert published["acked"] >= live["acked"] - 1  # never more than one ack behind for long
    jobs.stop()
    finished(jobs)
