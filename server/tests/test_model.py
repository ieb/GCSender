import json

import pytest

from gcsender.grbl_messages import explain_alarm, explain_error, setting_meta
from gcsender.logs import LogRing, Transcript
from gcsender.machine import MachineModel, parse_modal
from gcsender.store import SettingsError, SettingsStore

CAPS_09 = {"family": "0.9"}
CAPS_11 = {"family": "1.1"}


# ----------------------------------------------------------------- messages


def test_explain_error_09_text_and_id():
    assert explain_error("Bad number format", None)["code"] == 2
    e = explain_error("Invalid gcode ID:33", None)
    assert e["code"] == 33 and "invalid" in e["help"].lower()
    assert explain_error("Undefined feed rate", None)["help"].startswith("The feed rate")
    assert explain_error("Something new", None) == {"code": None, "title": "Something new", "help": ""}


def test_explain_error_11_numeric():
    assert explain_error("22", 22)["title"] == "Undefined feed rate"
    assert explain_error("9", 9)["title"] == "Alarm lock"


def test_explain_alarm():
    assert explain_alarm("Probe fail", None)["code"] == 4
    assert explain_alarm("Abort during cycle", None)["title"] == "Abort during cycle"
    a = explain_alarm("5", 5)
    assert a["title"] == "Probe fail" and "contact" in a["help"]


def test_setting_meta():
    assert setting_meta(110)["unit"] == "mm/min"
    assert setting_meta(10)["kind"] == "mask"
    assert setting_meta(999)["name"] == "$999"


# -------------------------------------------------------------------- model


def test_parse_modal_09_and_11():
    m = parse_modal("G0 G54 G17 G21 G90 G94 M0 M5 M9 T0 F0. S0.".split())
    assert m["motion"] == "G0" and m["wcs"] == "G54" and m["units"] == "G21" and m["program"] == "M0"
    assert m["feed"] == 0.0 and m["speed"] == 0.0 and m["tool"] == 0 and m["coolant"] == []
    m = parse_modal("G1 G55 G18 G20 G91 G93 M3 M8 M7 T2 F150.5 S8000".split())
    assert (m["wcs"], m["plane"], m["units"], m["distance"], m["feedMode"]) == ("G55", "G18", "G20", "G91", "G93")
    assert m["spindle"] == "M3" and m["coolant"] == ["M8", "M7"] and m["feed"] == 150.5 and m["tool"] == 2


def test_derived_g92_uses_live_wco():
    mm = MachineModel()
    mm.modal = parse_modal(["G55"])
    mm.offsets = {"G55": [5.0, 5.0, 0.0], "TLO": 1.5}
    mm.status = {"wco": [25.0, 15.0, 3.5]}
    assert mm.derived_g92() == [20.0, 10.0, 2.0]
    mm.status = {}
    assert mm.derived_g92() is None


def test_warnings():
    mm = MachineModel()
    assert mm.warnings(CAPS_09) == []  # no settings yet
    mm.set_setting(10, "3")
    mm.set_setting(13, "0")
    assert mm.warnings(CAPS_09) == []
    mm.set_setting(10, "1")
    w = mm.warnings(CAPS_09)
    assert w[0]["id"] == "status-mask" and w[0]["fix"] == "$10=3" and w[0]["level"] == "error"
    assert mm.warnings(CAPS_11) == []  # MPos only is fine on 1.1
    mm.set_setting(10, "0")
    assert mm.warnings(CAPS_11)[0]["fix"] == "$10=1"
    mm.set_setting(13, "1")
    assert any(x["id"] == "report-inches" for x in mm.warnings(CAPS_11))


def test_set_setting_types():
    mm = MachineModel()
    mm.set_setting(10, "3")
    mm.set_setting(110, "1500.000")
    assert mm.setting(10) == 3 and isinstance(mm.setting(10), int)
    assert mm.setting(110) == 1500.0 and isinstance(mm.setting(110), float)
    assert mm.settings_list()[1]["name"] == "X max rate"


# -------------------------------------------------------------------- store


def test_settings_store_persists(tmp_path):
    path = tmp_path / "settings.json"
    s = SettingsStore(path)
    assert s.get("statusInterval") == 200
    s.update({"statusInterval": 100, "lastPort": "/dev/ttyUSB0"})
    again = SettingsStore(path)
    assert again.get("statusInterval") == 100 and again.get("lastPort") == "/dev/ttyUSB0"
    assert json.loads(path.read_text())["statusInterval"] == 100


@pytest.mark.parametrize(
    "changes",
    [{"statusInterval": 5}, {"statusInterval": "fast"}, {"statusInterval": True}, {"bogus": 1}, {"autoConnect": 1}],
)
def test_settings_store_validation(tmp_path, changes):
    s = SettingsStore(tmp_path / "s.json")
    with pytest.raises(SettingsError):
        s.update(changes)
    assert s.get("statusInterval") == 200


def test_settings_store_ignores_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    assert SettingsStore(p).get("statusInterval") == 200
    p.write_text(json.dumps({"statusInterval": 1, "unknown": 3}))
    assert SettingsStore(p).get("statusInterval") == 200


# --------------------------------------------------------------------- logs


def test_transcript(tmp_path):
    t = Transcript(tmp_path)
    t.write(">", "G0X1")
    t.write("<", "ok")
    files = t.files()
    assert len(files) == 1
    path = t.path_for(files[0]["date"])
    lines = path.read_text().splitlines()
    assert lines[0].endswith("> G0X1") and lines[1].endswith("< ok")
    assert t.path_for("../../etc") is None
    assert t.path_for("19990101") is None
    t.close()


def test_log_ring():
    import logging

    ring = LogRing(capacity=2)
    logger = logging.getLogger("test-ring")
    logger.addHandler(ring)
    logger.setLevel(logging.INFO)
    for i in range(3):
        logger.info("msg %d", i)
    logger.removeHandler(ring)
    assert [r["msg"] for r in ring.records()] == ["msg 1", "msg 2"]
