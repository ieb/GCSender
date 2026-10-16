import pytest

from gcsender.simulator import GrblSimulator


class Harness:
    def __init__(self, firmware="0.9j", **kw):
        self.sim = GrblSimulator(firmware, threaded=False, **kw)
        self.out: list[str] = []
        self.sim.on_output = self.out.append
        self.sim.open_port(reset=True)
        self.out.clear()

    def send(self, line: str, dt: float = 0.0) -> list[str]:
        start = len(self.out)
        self.sim.feed((line + "\n").encode())
        self.sim.step(dt)
        return self.out[start:]

    def run(self, seconds: float, step: float = 0.01) -> None:
        t = 0.0
        while t < seconds:
            self.sim.step(step)
            t += step

    def status(self) -> str:
        start = len(self.out)
        self.sim.feed(b"?")
        return self.out[start]

    def rt(self, b: bytes) -> list[str]:
        start = len(self.out)
        self.sim.feed(b)
        self.sim.step(0)
        return self.out[start:]


@pytest.fixture(params=["0.9j", "1.1f"])
def h(request):
    return Harness(request.param)


def test_banner_on_open():
    sim = GrblSimulator("0.9j", threaded=False)
    out = []
    sim.on_output = out.append
    sim.open_port(reset=True)
    assert "Grbl 0.9j ['$' for help]" in out
    out.clear()
    sim.open_port(reset=False)  # board with auto-reset disabled
    assert out == []


def test_status_formats():
    assert Harness("0.9j").status() == "<Idle,MPos:0.000,0.000,0.000,WPos:0.000,0.000,0.000>"
    st11 = Harness("1.1f").status()
    assert st11.startswith("<Idle|MPos:0.000,0.000,0.000|FS:0,0")
    assert "WCO:0.000,0.000,0.000" in st11


def test_errors_text_vs_numeric():
    assert Harness("0.9j").send("G1X") == ["error: Bad number format"]
    assert Harness("1.1f").send("G1X") == ["error:2"]
    assert Harness("0.9j").send("G5X1") == ["error: Unsupported command"]
    assert Harness("0.9j").send("G1X1") == ["error: Undefined feed rate"]
    assert Harness("1.1f").send("$J=G91X1") == ["error:22"]


def test_settings_listing_formats():
    h9 = Harness("0.9j").send("$$")
    assert "$10=3 (status report mask:00000011)" in h9 and h9[-1] == "ok"
    h11 = Harness("1.1f").send("$$")
    assert "$10=1" in h11 and "$32=0" in h11


def test_modal_report_formats():
    assert Harness("0.9j").send("$G")[0] == "[G0 G54 G17 G21 G90 G94 M0 M5 M9 T0 F0. S0.]"
    assert Harness("1.1f").send("$G")[0] == "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]"


def test_motion_and_wco(h):
    assert h.send("G10L20P1X0Y0Z0") == ["ok"]
    assert h.send("G1X10F600") == ["ok"]
    h.run(1.2)
    assert h.sim.state == "Idle"
    assert h.sim.pos == pytest.approx([10.0, 0.0, 0.0])


def test_planner_backpressure_blocks_ok(h):
    # More moves than the planner holds: oks stop until blocks complete, and
    # the unparsed lines wait in the RX buffer (25 short lines fit in 127 B).
    oks = 0
    for i in range(25):
        oks += h.send(f"G1X{i + 1}F6000").count("ok")
    assert oks == h.sim.planner_size
    assert h.sim.rx_overflows == 0
    h.run(5)
    assert h.out.count("ok") == 25


def test_rx_buffer_overflow_drops_bytes():
    hs = Harness("0.9j")
    hs.sim.feed(b"G4P5\n")  # syncs and blocks the parser for 5 s
    hs.sim.step(0)
    hs.sim.feed(b"G0X1\n" * 40)  # 200 bytes into a 127-byte buffer
    assert hs.sim.rx_overflows > 0
    assert len(hs.sim.rx) == 127


def test_line_overflow_error():
    assert Harness("0.9j").send("G0X" + "1" * 90) == ["error: Line overflow"]


def test_reset_while_running_alarms(h):
    h.send("G1X50F600")
    h.run(0.5)
    out = h.rt(b"\x18")
    assert out[0] in ("ALARM: Abort during cycle", "ALARM:3")
    assert h.sim.state == "Alarm"
    assert h.send("G0X1")[0] in ("error: Alarm lock", "error:9")
    h.send("$X")
    assert h.sim.state == "Idle"


def test_reset_after_completed_hold_keeps_position_and_clears_g92(h):
    h.send("G92X0Y0Z0")
    h.send("G1X50F600")
    h.run(0.5)
    h.rt(b"!")
    h.run(0.5)  # deceleration finishes
    pos = list(h.sim.pos)
    assert h.sim.state == "Hold"
    out = h.rt(b"\x18")
    assert not any(line.startswith("ALARM") for line in out)
    assert h.sim.state == "Idle"
    assert h.sim.pos == pos
    assert h.sim.g92 == [0.0, 0.0, 0.0]
    assert pos[0] > 0  # G92 was cleared, so WPos == MPos again


def test_hold_resume(h):
    h.send("G1X20F1200")
    h.run(0.2)
    h.rt(b"!")
    h.run(0.5)
    held = h.sim.pos[0]
    h.run(0.5)
    assert h.sim.pos[0] == held
    h.rt(b"~")
    h.run(2)
    assert h.sim.pos[0] == pytest.approx(20.0)


def test_dtr_reset_zeroes_position():
    hs = Harness("0.9j")
    hs.send("G0X10")
    hs.run(2)
    hs.sim.open_port(reset=True)
    assert hs.sim.pos == [0.0, 0.0, 0.0]


def test_probe_success_and_fail(h):
    out = h.send("G38.2Z-40F300")
    assert out == []  # ok only after the probe completes
    h.run(10)
    prb = [line for line in h.out if line.startswith("[PRB:")]
    assert prb and prb[-1].endswith(":1]")
    assert h.out[-1] == "ok"
    z = float(prb[-1].split(",")[2].split(":")[0])
    assert z == pytest.approx(h.sim.surface(0, 0), abs=0.002)
    # Retract and probe a range that never reaches the surface -> alarm.
    h.send("G0Z-20")
    h.run(5)
    h.send("G38.2Z-25F300")
    h.run(5)
    assert any(line.startswith("ALARM") for line in h.out)
    assert h.sim.state == "Alarm"


def test_g38_3_no_contact_no_alarm(h):
    h.send("G38.3Z-5F300")
    h.run(3)
    assert h.out[-2].endswith(":0]") and h.out[-1] == "ok"
    assert h.sim.state == "Idle"


def test_system_commands_need_idle_09():
    hs = Harness("0.9j")
    hs.send("G1X50F600")
    hs.run(0.2)
    assert hs.send("$#") == ["error: Not idle"]
    assert hs.send("$$") == ["error: Not idle"]
    assert hs.send("$G")[-1] == "ok"  # allowed while running


def test_check_mode_toggle(h):
    out = h.send("$C")
    assert out[-1] == "ok" and h.sim.state == "Check"
    h.send("G1X10F100")
    assert h.sim.pos == [0.0, 0.0, 0.0]
    out = h.send("$C")
    assert any("Grbl" in line for line in out) and h.sim.state == "Idle"


def test_jog_and_cancel_11():
    hs = Harness("1.1f")
    assert hs.send("$J=G91X50F600") == ["ok"]
    hs.run(0.5)
    assert hs.sim.state == "Jog"
    hs.rt(b"\x85")
    hs.run(0.5)
    assert hs.sim.state == "Idle"
    assert 0 < hs.sim.pos[0] < 50


def test_jog_not_available_09():
    assert Harness("0.9j").send("$J=G91X1F100") == ["error: Invalid statement"]


def test_arc_ends_at_target(h):
    h.send("G2X20Y0I10J0F3000")
    h.run(3)
    assert h.sim.pos == pytest.approx([20.0, 0.0, 0.0], abs=1e-6)


def test_overrides_11():
    hs = Harness("1.1f")
    hs.rt(bytes([0x91, 0x91, 0x97, 0x9B]))
    assert (hs.sim.feed_ov, hs.sim.rapid_ov, hs.sim.spindle_ov) == (120, 25, 90)
