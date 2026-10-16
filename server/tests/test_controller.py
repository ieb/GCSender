"""Controller behaviour against the simulator (threaded, accelerated time)."""

import time

import pytest

from gcsender import controller as controller_mod
from gcsender.controller import Controller, ControllerError
from gcsender.events import EventBus
from gcsender.logs import Transcript
from gcsender.simulator import GrblSimulator
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport, Transport


def wait_for(fn, timeout=5.0, step=0.02):
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
    monkeypatch.setattr(controller_mod, "UNRESPONSIVE_S", 0.4)
    made = []

    def make(firmware="0.9j", reset_on_open=True):
        sim = GrblSimulator(firmware, time_scale=5.0)
        bus = EventBus()
        ctl = Controller(
            bus,
            lambda port, baud: SimTransport(sim, reset_on_open=reset_on_open),
            settings=SettingsStore(tmp_path / f"settings-{len(made)}.json"),
            transcript=Transcript(tmp_path / "logs"),
            poll_interval=0.03,
        )
        made.append((sim, ctl))
        return sim, ctl, bus

    yield make
    for sim, ctl in made:
        ctl.close()
        sim.close()


def connected(ctl):
    wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.status)
    # initial refresh: $G, $$, $# and $I
    wait_for(lambda: ctl.model.modal and ctl.model.settings and "G54" in ctl.model.offsets and ctl.model.build)


def idle(ctl):
    wait_for(lambda: ctl.model.status and ctl.model.status["state"] == "Idle")


def test_initial_queries_populate_model(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    assert ctl.connection["session"] == "fresh"
    assert ctl.caps["family"] == "0.9"
    assert ctl.model.setting(110) == 1500.0
    assert ctl.model.build["version"] == "0.9j.20180122"
    assert bus.retained("warnings") == []
    assert len(bus.retained("grbl.settings")) == 31


def test_attach_to_live_session_without_banner(rig):
    sim, ctl, bus = rig("1.1f", reset_on_open=False)
    ctl.connect("sim")
    wait_for(lambda: ctl.connection["state"] == "connected")
    assert ctl.connection["session"] == "attached"
    assert ctl.caps["family"] == "1.1"
    # version refined via $I once idle
    wait_for(lambda: ctl.connection.get("firmware") == "1.1f")


def test_console_events_are_source_tagged_with_help(rig):
    sim, ctl, bus = rig()
    sub = bus.subscribe(bus.last_id)
    ctl.connect("sim")
    connected(ctl)
    r = ctl.command("G1X")
    assert r["status"] == "error"
    events = []
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        ev = sub.get(timeout=0.1)
        if ev is not None and ev.type == "console":
            events.append(ev.data)
    sys_tx = [e for e in events if e["dir"] == "tx" and e["src"] == "system"]
    assert {e["line"] for e in sys_tx} >= {"$G", "$$", "$#", "$I"}
    sys_rx = [e for e in events if e["dir"] == "rx" and e["line"].startswith("$110=")]
    assert sys_rx and sys_rx[0]["src"] == "system"
    err = [e for e in events if e["dir"] == "rx" and e["line"].startswith("error")][-1]
    assert err["src"] == "console" and err["title"] == "Bad number format" and err["help"]


def test_status_mask_warning_and_fix(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("$10=1")
    wait_for(lambda: bus.retained("warnings"))
    assert bus.retained("warnings")[0]["fix"] == "$10=3"
    results = ctl.write_settings({10: 3})
    assert results == [{"id": 10, "line": "$10=3", "status": "ok", "response": "ok"}]
    assert ctl.model.setting(10) == 3
    assert bus.retained("warnings") == []


def test_write_settings_requires_idle(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G1X100F300")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    with pytest.raises(ControllerError):
        ctl.write_settings({1: 30})


def test_reset_after_hold_restores_offsets_and_modal(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G10L2P2X5Y5")
    ctl.command("G55G0X20Y10")
    idle(ctl)
    ctl.command("G92X0Y0")
    ctl.command("G43.1Z1.5")
    ctl.command("G91")
    ctl.command("G1X30F300")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    time.sleep(0.2)
    ctl.realtime("hold")
    wait_for(lambda: ctl.model.status["state"] == "Hold")
    time.sleep(0.3)  # deceleration complete
    wpos_before = list(ctl.model.status["wpos"])
    ctl.realtime("reset")
    wait_for(lambda: sim.g92 != [0.0, 0.0, 0.0] and sim.modal.tlo == 1.5 and sim.modal.distance == 91, timeout=5)
    assert sim.modal.wcs == 1  # G55
    # status reports have 3 decimals, so the restored G92 is within 0.0005 mm
    assert sim.g92 == pytest.approx([20.0, 10.0, 0.0], abs=1e-3)
    assert sim.modal.feed == 300
    assert sim.state == "Idle"
    wait_for(lambda: ctl.model.modal and ctl.model.modal.get("distance") == "G91")
    wait_for(lambda: ctl.model.status["wpos"] == pytest.approx(wpos_before, abs=1e-3))


def test_reset_without_restore_option(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G92X-10")
    ctl.realtime("reset", restore=False)
    wait_for(lambda: ctl.model.modal is not None and ctl.connection["session"] == "reset")
    time.sleep(0.3)
    assert sim.g92 == [0.0, 0.0, 0.0]


def test_reset_while_moving_alarms_and_skips_restore(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G92X5")
    ctl.command("G1X50F300")
    wait_for(lambda: ctl.model.status["state"] == "Run")
    ctl.realtime("reset")
    wait_for(lambda: ctl.model.status["state"] == "Alarm")
    alarm = wait_for(lambda: bus.retained("alarm"))
    assert alarm["title"] == "Abort during cycle" and alarm["help"]
    time.sleep(0.3)
    assert sim.g92 == [0.0, 0.0, 0.0]  # not restored: position may be lost
    ctl.control("unlock")
    idle(ctl)
    assert bus.retained("alarm") is None


def test_check_mode_exit_restores_state(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G56")
    wait_for(lambda: ctl.model.modal.get("wcs") == "G56")
    ctl.control("check")
    wait_for(lambda: ctl.model.status["state"] == "Check")
    ctl.control("check")  # leaving check mode resets GRBL
    wait_for(lambda: sim.modal.wcs == 2 and sim.state == "Idle")


def test_home_requires_homing_enabled(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    with pytest.raises(ControllerError, match="homing is disabled"):
        ctl.control("home")
    ctl.write_settings({22: 1})
    r = ctl.control("home")
    assert r["status"] == "ok"
    with pytest.raises(ControllerError):
        ctl.control("sleep")  # 0.9 has no sleep


def test_offsets_refresh_after_g92(rig):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G92X-3")
    wait_for(lambda: ctl.model.offsets.get("G92") == [3.0, 0.0, 0.0])


def test_transcript_written(rig, tmp_path):
    sim, ctl, bus = rig()
    ctl.connect("sim")
    connected(ctl)
    ctl.command("G0X1")
    files = ctl.transcript.files()
    text = ctl.transcript.path_for(files[0]["date"]).read_text()
    assert "> G0X1" in text and "< Grbl 0.9j" in text
    assert "<Idle" not in text  # status polls excluded by default


class SilentTransport(Transport):
    """A port that opens but never answers (wrong baud, crashed firmware...)."""

    def __init__(self):
        super().__init__()
        self._open = False

    @property
    def is_open(self):
        return self._open

    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def write(self, data):
        pass


def test_unresponsive_controller(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.05)
    monkeypatch.setattr(controller_mod, "UNRESPONSIVE_S", 0.1)
    bus = EventBus()
    ctl = Controller(bus, lambda p, b: SilentTransport(), settings=SettingsStore(None), poll_interval=0.02)
    try:
        ctl.connect("/dev/null-ish")
        wait_for(lambda: ctl.connection["state"] == "unresponsive")
        with pytest.raises(ControllerError):
            ctl.command("$G", timeout=0.2)
    finally:
        ctl.close()


def test_app_settings_update_publishes(rig):
    sim, ctl, bus = rig()
    values = ctl.update_settings({"statusInterval": 150})
    assert values["statusInterval"] == 150
    assert bus.retained("app.settings")["statusInterval"] == 150
