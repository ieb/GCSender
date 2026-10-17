"""Phase 2: step jogging, safe stop, WCS/positioning, spindle and coolant (simulator)."""

import time

import pytest

from gcsender import controller as controller_mod
from gcsender import operations as ops
from gcsender.controller import Controller, ControllerError
from gcsender.events import EventBus
from gcsender.jog import Jogger
from gcsender.logs import Transcript
from gcsender.simulator import GrblSimulator
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport


def wait_for(fn, timeout=8.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Simulator rig; time_scale=1 where a move must still be running when the test acts."""
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
    made = []

    def make(firmware="0.9j", time_scale=1.0):
        sim = GrblSimulator(firmware, time_scale=time_scale)
        ctl = Controller(
            EventBus(),
            lambda port, baud: SimTransport(sim),
            settings=SettingsStore(tmp_path / f"settings-{len(made)}.json"),
            transcript=Transcript(tmp_path / "logs"),
            poll_interval=0.05,
        )
        made.append((sim, ctl))
        SIMS[id(ctl)] = sim
        ctl.connect("sim")
        wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.modal and ctl.model.settings and ctl.model.build)
        idle(ctl)
        return sim, ctl

    yield make
    for sim, ctl in made:
        ctl.close()
        sim.close()


SIMS = {}


def idle(ctl, timeout=10.0):
    """Wait until the jogger, the simulator planner and the reported state are all idle."""
    sim = SIMS[id(ctl)]
    time.sleep(0.12)
    wait_for(
        lambda: not ctl.jogger.moving()
        and sim.state == "Idle"
        and not sim.planner
        and not sim._pending
        and not sim.rx
        and ctl.model.status["state"] == "Idle",
        timeout,
    )


def modal_words(sim):
    m = sim.modal
    return m.units, m.distance, m.motion, m.feed


def console_out(ctl):
    hist = ctl.bus.snapshot()["console"]
    return [e["line"] for e in hist if e.get("dir") == "tx"]


# ------------------------------------------------------------------ step jog


def test_step_jog_09_moves_in_mm_and_restores_modal(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.command("G20 G90 G0 F10")  # inches, rapid mode, F10 in/min
    before = modal_words(sim)
    ctl.jogger.step([1.0, 0, 0], 300)
    idle(ctl)
    assert sim.pos[0] == pytest.approx(1.0)  # millimetres even though the user was in G20
    assert modal_words(sim) == before
    assert any(line.startswith("G91G21G1X1F300") for line in console_out(ctl))


def test_step_jog_11_uses_dollar_j(rig):
    sim, ctl = rig("1.1f", time_scale=5.0)
    ctl.command("G20")
    before = modal_words(sim)
    ctl.jogger.step([0, -2.5, 0], 500)
    idle(ctl)
    assert sim.pos[1] == pytest.approx(-2.5)
    assert modal_words(sim) == before
    assert "$J=G91G21Y-2.5F500" in console_out(ctl)


def test_step_feed_limited_by_max_rate(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    r = ctl.jogger.step([1.0, 0, 0], 99999)
    assert r["feed"] == 1500.0  # $110


def test_step_outside_jog_box_is_refused_not_shortened(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.update_settings({"jogLimitsEnabled": True, "jogLimits": [-5, 3, -5, 5, -5, 0]})
    with pytest.raises(ControllerError, match="jog box"):
        ctl.jogger.step([5, 0, 0], 600)
    assert sim.pos == [0, 0, 0]
    ctl.jogger.step([3, 0, 0], 600)  # exactly to the edge is fine
    idle(ctl)
    assert sim.pos[0] == pytest.approx(3)


def test_deliberate_presses_queue_and_sum_exactly(rig):
    sim, ctl = rig("0.9j")
    ctl.jogger.step([1, 0, 0], 300)  # 0.2 s of motion
    for _ in range(3):
        ctl.jogger.step([1, 0, 0], 300)  # queued behind the first
    assert ctl.jogger.moving()
    idle(ctl)
    assert sim.pos[0] == pytest.approx(4)
    assert sim.modal.distance == 90


def test_queued_steps_checked_against_jog_box_end_position(rig):
    sim, ctl = rig("0.9j")
    ctl.update_settings({"jogLimitsEnabled": True, "jogLimits": [-5, 2.5, -5, 5, -5, 0]})
    ctl.jogger.step([1, 0, 0], 300)
    ctl.jogger.step([1, 0, 0], 300)
    with pytest.raises(ControllerError, match="jog box"):
        ctl.jogger.step([1, 0, 0], 300)  # would end at 3 > 2.5 once the queue finishes
    idle(ctl)
    assert sim.pos[0] == pytest.approx(2)


def test_step_not_queued_behind_console_motion(rig):
    sim, ctl = rig("0.9j")
    ctl.command("G1 X20 F600", source="console")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    with pytest.raises(ControllerError, match="Run"):
        ctl.jogger.step([1, 0, 0], 300)


@pytest.mark.parametrize("step", [0.01, 0.05, 0.1, 0.5, 1, 5, 10])
def test_every_default_step_size_moves_exactly(rig, step):
    sim, ctl = rig("0.9j", time_scale=20.0)
    ctl.jogger.step([0, 0, -step], 1000)
    idle(ctl)
    assert sim.pos[2] == pytest.approx(-step, abs=1e-6)


def test_no_continuous_jog_api(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    for name in ("start", "heartbeat", "stop"):
        assert not hasattr(ctl.jogger, name)


# ------------------------------------------------------------------ safe stop


def test_safe_stop_during_long_step_09(rig):
    sim, ctl = rig("0.9j")
    ctl.jogger.step([0, 10, 0], 300)
    wait_for(lambda: ctl.model.status["state"] == "Run")
    assert ctl.safe_stop()["method"] == "hold-reset"
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    time.sleep(0.3)
    assert sim.state == "Idle" and 0 < sim.pos[1] < 10
    assert sim.modal.distance == 90  # not left in G91
    ctl.jogger.step([0, 1, 0], 300)  # jogging works again after the stop
    idle(ctl)


def test_safe_stop_during_step_11_uses_jog_cancel(rig):
    sim, ctl = rig("1.1f")
    ctl.jogger.step([10, 0, 0], 300)
    wait_for(lambda: ctl.model.status["state"] == "Jog")
    assert ctl.safe_stop()["method"] == "jog-cancel"
    idle(ctl)
    assert sim.state == "Idle" and sim.pos[0] < 10


def test_safe_stop_09_no_alarm_keeps_offsets_and_position(rig):
    sim, ctl = rig("0.9j")
    ctl.command("G10 L20 P1 X5 Y5 Z0")
    ctl.command("G92 Z1")
    ctl.command("G1 X40 F600", source="console")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    time.sleep(0.4)
    g54 = list(sim.persist.coords[0]) if hasattr(sim, "persist") else None
    r = ctl.safe_stop()
    assert r["method"] == "hold-reset"
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    stopped_at = list(sim.pos)
    assert 0 < stopped_at[0] < 40
    time.sleep(0.5)
    assert sim.state == "Idle"
    assert sim.pos == stopped_at  # reset after a completed hold keeps position
    # G92 is volatile in GRBL but restored by the sender after the reset
    wait_for(lambda: sim.g92[2] != 0)
    if g54 is not None:
        assert list(sim.persist.coords[0]) == g54


def test_safe_stop_when_idle_is_a_no_op(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    assert ctl.safe_stop()["method"] == "none"


# ------------------------------------------------------------- WCS / goto


def test_zero_persistent_uses_active_wcs(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.command("G0 X10 Y20")
    idle(ctl)
    ops.select_wcs(ctl, "G55")
    ops.set_zero(ctl, {"x": 0, "y": 0})
    assert "G10L20P2X0Y0" in console_out(ctl)
    wait_for(lambda: ctl.model.status["wpos"][:2] == [0.0, 0.0])


def test_zero_temporary_uses_g92_and_respects_inches(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.command("G20")
    ops.set_zero(ctl, {"z": 1.5}, persistent=False)
    assert "G21G92Z1.5" in console_out(ctl)
    assert sim.modal.units == 20
    ops.clear_g92(ctl)
    assert sim.g92 == [0, 0, 0]


def test_zero_rejects_bad_input(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    with pytest.raises(ControllerError):
        ops.set_zero(ctl, {"a": 0})
    with pytest.raises(ControllerError):
        ops.select_wcs(ctl, "G60")


def test_goto_raises_to_safe_z_first(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.command("G0 X10 Y10 Z-3")
    idle(ctl)
    ctl.command("G91 G1 F100")
    r = ops.goto(ctl, {"x": 0, "y": 0}, safe_z=5)
    assert r["lines"] == ["G21G90G0Z5", "G21G90G0X0Y0"]
    idle(ctl)
    assert sim.pos == [0, 0, 5]
    assert sim.modal.distance == 91 and sim.modal.motion == "1"  # restored


def test_goto_skips_safe_z_when_already_above(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ctl.command("G0 Z8")
    idle(ctl)
    assert ops.goto(ctl, {"x": 1}, safe_z=5)["lines"] == ["G21G90G0X1"]


# --------------------------------------------------------- spindle/coolant


def test_spindle_on_off_clamped_to_range(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    ops.spindle(ctl, True, 5000)
    assert "M3S1000" in console_out(ctl)  # spindleMax default 1000 (0.9 SPINDLE_MAX_RPM)
    ops.spindle(ctl, True, 300, "ccw")
    assert sim.modal.spindle == "M4"
    ops.spindle(ctl, False)
    assert sim.modal.spindle == "M5"


def test_spindle_refused_while_moving_on_09(rig):
    sim, ctl = rig("0.9j")
    ctl.command("G1 X30 F600", source="console")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    with pytest.raises(ControllerError, match="Idle"):
        ops.spindle(ctl, False)


def test_coolant_lines(rig):
    sim, ctl = rig("0.9j", time_scale=5.0)
    assert ops.coolant(ctl, flood=True)["lines"] == ["M8"]
    wait_for(lambda: "M8" in (ctl.model.modal.get("coolant") or []))
    assert ops.coolant(ctl, flood=True)["lines"] == []
    assert ops.coolant(ctl, flood=False)["lines"] == ["M9"]


def test_jogger_restore_line():
    j = Jogger.__new__(Jogger)
    assert j.restore_line({"units": "G20", "distance": "G90", "motion": "G1", "feed": 254.0}) == "G20G90G1F10"
    assert j.restore_line({"units": "G21", "distance": "G91", "motion": "G2", "feed": None}) == "G21G91"
    assert j.restore_line(None) == "G90"
