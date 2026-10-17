"""Phase 6: door command, settings defaults/profiles/import/export, $$ backup."""

import json
import time

import pytest

from gcsender import controller as controller_mod
from gcsender.api import build_router
from gcsender.controller import Controller
from gcsender.events import EventBus
from gcsender.macros import MacroRunner, MacroStore
from gcsender.profiles import ProfileStore
from gcsender.simulator import GrblSimulator
from gcsender.store import SettingsStore
from gcsender.transport import SimTransport
from gcsender.webserver import ApiError


def wait_for(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met")


class Req:
    def __init__(self, body=None, params=None):
        self._b = body if body is not None else {}
        self.params = params or {}
        self.query = {}

    def json(self):
        return self._b


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
    made = []

    def make(firmware="0.9j"):
        sim = GrblSimulator(firmware, time_scale=5.0)
        bus = EventBus()
        ctl = Controller(bus, lambda p, b: SimTransport(sim), settings=SettingsStore(tmp_path / "settings.json"), poll_interval=0.03)
        macros = MacroRunner(ctl, MacroStore(tmp_path / "macros.json"))
        router = build_router(bus, ctl, macros=macros, profiles=ProfileStore(tmp_path))
        made.append((sim, ctl))
        ctl.connect("sim")
        wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.settings and ctl.model.status)

        def call(method, path, body=None):
            fn, params = router.route(method, path)
            return fn(Req(body, params))

        return sim, ctl, macros, call

    yield make
    for sim, ctl in made:
        ctl.close()
        sim.close()


@pytest.mark.parametrize("fw", ["0.9j", "1.1f"])
def test_door_holds_and_resume_continues(app, fw):
    sim, ctl, _m, call = app(fw)
    ctl.command("G1 X20 F600", source="ui")
    wait_for(lambda: sim.pos[0] > 1)
    call("POST", "/api/realtime", {"op": "door"})
    wait_for(lambda: ctl.model.status["state"].startswith("Door"))
    time.sleep(0.2)
    held = sim.pos[0]
    time.sleep(0.2)
    assert sim.pos[0] == pytest.approx(held) and held < 20
    call("POST", "/api/realtime", {"op": "resume"})
    wait_for(lambda: sim.pos[0] == pytest.approx(20))


def test_reset_to_defaults_keeps_the_session(app):
    _sim, ctl, _m, call = app()
    call("PUT", "/api/settings", {"safeZ": 12.5, "jogSteps": "0.1,1"})
    port = ctl.settings.get("lastPort")
    values = call("POST", "/api/settings/reset")
    assert values["safeZ"] == 5.0 and values["jogSteps"] != "0.1,1"
    assert values["lastPort"] == port == "sim"


def test_export_import_round_trip(app):
    _sim, ctl, macros, call = app()
    call("PUT", "/api/settings", {"safeZ": 7.0, "spindleDwell": 1.5})
    macros.save([{"name": "Mine", "text": "G0 Z{safez}"}])
    raw = call("GET", "/api/settings/export")
    doc = json.loads(raw.body)
    assert raw.filename == "gcsender-settings.json"
    assert doc["format"] == "gcsender-settings" and doc["settings"]["safeZ"] == 7.0
    assert "lastPort" not in doc["settings"] and doc["macros"] == [{"name": "Mine", "text": "G0 Z{safez}"}]
    assert doc["grbl"]["110"] in ("1500.000", "500.000") or "110" in doc["grbl"]
    call("POST", "/api/settings/reset")
    macros.save([])
    doc["settings"]["futureKey"] = 1
    r = call("POST", "/api/settings/import", {"doc": doc, "macros": True})
    assert r["ignored"] == ["futureKey"] and r["macros"]
    assert ctl.settings.get("safeZ") == 7.0 and [m["name"] for m in macros.store.list()] == ["Mine"]


def test_import_is_all_or_nothing(app):
    _sim, ctl, macros, call = app()
    before = ctl.settings.all()
    bad_setting = {"format": "gcsender-settings", "version": 1, "settings": {"safeZ": 6.0, "statusInterval": -5}}
    with pytest.raises(ApiError):
        call("POST", "/api/settings/import", {"doc": bad_setting})
    bad_macro = {"format": "gcsender-settings", "version": 1, "settings": {"safeZ": 6.0},
                 "macros": [{"name": "x", "text": "G0 Z{nope}"}]}
    with pytest.raises(ApiError):
        call("POST", "/api/settings/import", {"doc": bad_macro})
    assert ctl.settings.all() == before
    for junk in ({"format": "other"}, {"format": "gcsender-settings", "version": 99, "settings": {}}, "text"):
        with pytest.raises(ApiError):
            call("POST", "/api/settings/import", {"doc": junk})


def test_profiles(app):
    _sim, ctl, macros, call = app()
    call("PUT", "/api/settings", {"safeZ": 3.0})
    assert [p["name"] for p in call("POST", "/api/settings/profiles", {"name": "PCB"})["profiles"]] == ["PCB"]
    call("PUT", "/api/settings", {"safeZ": 20.0})
    macros.save([])
    call("POST", "/api/settings/profiles/load", {"name": "PCB", "macros": False})
    assert ctl.settings.get("safeZ") == 3.0 and macros.store.list() == []
    call("POST", "/api/settings/profiles/load", {"name": "PCB"})
    assert len(macros.store.list()) == 3  # the defaults were saved with the profile
    for bad in ("../x", "", "a/b", "x" * 41):
        with pytest.raises(ApiError):
            call("POST", "/api/settings/profiles", {"name": bad})
    with pytest.raises(ApiError) as e:
        call("POST", "/api/settings/profiles/load", {"name": "nope"})
    assert e.value.status == 404
    assert call("DELETE", "/api/settings/profiles/PCB")["profiles"] == []


def test_grbl_settings_backup(app):
    _sim, _ctl, _m, call = app()
    raw = call("GET", "/api/grbl/settings/export")
    text = raw.body.decode()
    assert raw.filename == "grbl-settings.txt"
    assert text.startswith("; GRBL 0.9j") and "\n$0=" in text and "\n$110=" in text
