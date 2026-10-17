"""Phase 5: journal and recovery after a server restart / lost link; macros."""

import json
import time

import pytest

from gcsender import controller as controller_mod
from gcsender.controller import Controller, ControllerError
from gcsender.events import EventBus
from gcsender.heightmap.model import MapLibrary
from gcsender.heightmap.service import HeightmapService
from gcsender.jobs.journal import Journal
from gcsender.jobs.library import JobLibrary
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


class Rig:
    """One 'server process' (controller + runner) over a long-lived simulator."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(controller_mod, "BANNER_WAIT_S", 0.2)
        self.tmp = tmp_path
        self.servers = []
        self.sims = []

    def sim(self, firmware="0.9j", time_scale=1.0):
        sim = GrblSimulator(firmware, time_scale=time_scale)
        received = []
        orig = sim.feed

        def feed(data):
            received.append(data)
            orig(data)

        sim.feed = feed
        sim.received = received
        self.sims.append(sim)
        return sim

    def boot(self, sim, reset_on_open=True, connect=True):
        bus = EventBus()
        ctl = Controller(
            bus, lambda p, b: SimTransport(sim, reset_on_open=reset_on_open),
            settings=SettingsStore(self.tmp / "settings.json"), poll_interval=0.03,
        )
        maps = HeightmapService(bus, MapLibrary(self.tmp))
        jobs = JobRunner(ctl, maps, JobLibrary(self.tmp), Journal(self.tmp / "journal.json"))
        self.servers.append(ctl)
        if connect:
            ctl.connect("sim")
            wait_for(lambda: ctl.connection["state"] == "connected" and ctl.model.status and ctl.model.modal is not None)
        return ctl, jobs

    @staticmethod
    def crash(ctl, jobs):
        jobs.journal = None  # the process dies: nothing more is written
        ctl.close()

    def close(self):
        for ctl in self.servers:
            ctl.close()
        for sim in self.sims:
            sim.close()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


def lines_received(sim):
    raw = b"".join(sim.received)
    # drop real-time bytes (status polls etc.) interleaved between lines
    text = bytes(b for b in raw if b < 0x80 and b not in b"?!~\x18").decode()
    return [ln for ln in text.split("\n") if ln]


PROGRAM = "G21 G90\nG0 X0 Y0 Z0\n" + "\n".join(f"G1 X{i} Y{(i % 2) * 2} F1200" for i in range(1, 61)) + "\nG0 Z3\n"
MOVES = [f"G1X{i}Y{(i % 2) * 2}F1200" for i in range(1, 61)]


def load(jobs, text=PROGRAM):
    meta = jobs.library.add("job.nc", text.encode())
    jobs.select(meta["id"])
    return meta


def journal_of(rig):
    return json.loads((rig.tmp / "journal.json").read_text())


def test_journal_is_written_ahead_of_streaming(rig):
    sim = rig.sim(time_scale=2.0)
    ctl, jobs = rig.boot(sim)
    load(jobs)
    jobs.start()
    samples = 0
    while jobs.active:
        j = journal_of(rig) if (rig.tmp / "journal.json").exists() else None
        sent = jobs.sent
        if j and j.get("active") and jobs.state == "running":
            # read after the journal: the runner may only have sent more since, never past reserved
            assert sent <= j["reserved"] or jobs.sent <= j["reserved"] + 1
            samples += 1
        time.sleep(0.01)
    assert samples > 5
    assert jobs.state == "done"
    assert not (rig.tmp / "journal.json").exists()  # a finished job leaves no journal


def test_restart_without_reset_resumes_seamlessly(rig):
    sim = rig.sim()
    ctl, jobs = rig.boot(sim, reset_on_open=False)
    load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 25)
    rig.crash(ctl, jobs)
    wait_for(lambda: sim.state == "Idle" and not sim.planner, timeout=30)  # GRBL finishes its buffer
    stopped_at = list(sim.pos)

    ctl2, jobs2 = rig.boot(sim, reset_on_open=False)
    assert ctl2.connection["session"] == "attached"
    rec = wait_for(lambda: jobs2.recovery and jobs2.recovery["phase"] == "ready" and jobs2.recovery)
    assert jobs2.state == "interrupted" and rec["mode"] == "attached"
    k = rec["matched"]
    assert rec["seamlessLine"] == k + 1
    assert MOVES[k - 2].startswith(f"G1X{stopped_at[0]:g}Y{stopped_at[1]:g}F")  # program lines 0-1 are the preamble
    with pytest.raises(ControllerError, match="interrupted"):
        jobs2.start()
    jobs2.recover("seamless")
    wait_for(lambda: not jobs2.active, timeout=30)
    assert jobs2.state == "done"
    sent = lines_received(sim)
    missing = [m for m in MOVES if m not in sent]
    assert not missing, (missing, rec, sent[-80:])  # nothing skipped
    tx = [e["line"] for e in ctl2.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    assert not any(t.startswith("G0") for t in tx[:2])  # seamless: no retract/reposition
    assert sim.pos == pytest.approx([60, 0, 3])


def test_restart_with_reset_needs_rezero_then_safe_entry(rig):
    sim = rig.sim()
    ctl, jobs = rig.boot(sim, reset_on_open=True)
    load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 25)
    time.sleep(1.1)  # let a periodic journal write record the progress
    rig.crash(ctl, jobs)
    time.sleep(0.3)

    ctl2, jobs2 = rig.boot(sim, reset_on_open=True)  # DTR reset: position lost
    assert ctl2.connection["session"] == "fresh"
    rec = wait_for(lambda: jobs2.recovery and jobs2.recovery["phase"] == "ready" and jobs2.recovery)
    assert rec["mode"] == "reset" and rec["seamlessLine"] is None
    assert rec["suggestedLine"] >= 5  # from the journal: conservative (<= 1 s stale)
    with pytest.raises(ControllerError, match="seamless"):
        jobs2.recover("seamless")
    with pytest.raises(ControllerError, match="work zero"):
        jobs2.recover("entry")
    # The user re-establishes work zero (here: says the tool is at work X0 Y0 Z5).
    ctl2.command("G10 L20 P1 X0 Y0 Z5", source="ui")
    wait_for(lambda: ctl2.model.status["wpos"] == pytest.approx([0, 0, 5]))
    line = rec["suggestedLine"]
    jobs2.recover("entry", rezeroed=True)
    wait_for(lambda: not jobs2.active, timeout=30)
    assert jobs2.state == "done"
    tx = [e["line"] for e in ctl2.bus.snapshot()["console"] if e.get("src") == "job" and e.get("dir") == "tx"]
    assert tx[0] == "G21G90G17G54" and "G1Z0F100" in tx  # safe entry, plunge to the line's Z
    sent = lines_received(sim)
    assert all(m in sent for m in MOVES[max(0, line - 2):])
    assert ctl2.model.status["wpos"] == pytest.approx([60, 0, 3])


def test_lost_link_in_the_same_process_interrupts_and_recovers(rig):
    sim = rig.sim()
    ctl, jobs = rig.boot(sim, reset_on_open=False)
    load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 20)
    ctl.disconnect("USB unplugged")
    wait_for(lambda: jobs.state == "interrupted")
    assert jobs.recovery["phase"].startswith("waiting for the controller")
    assert journal_of(rig)["active"] and journal_of(rig)["state"] == "interrupted"
    assert ctl.busy is None
    ctl.connect("sim")
    rec = wait_for(lambda: jobs.recovery and jobs.recovery["phase"] == "ready" and jobs.recovery, timeout=30)
    assert rec["mode"] == "attached" and rec["seamlessLine"] is not None
    jobs.recover("seamless")
    wait_for(lambda: not jobs.active, timeout=30)
    assert jobs.state == "done" and all(m in lines_received(sim) for m in MOVES)


def test_interrupted_job_whose_file_is_gone_can_only_be_discarded(rig):
    sim = rig.sim(time_scale=5.0)
    ctl, jobs = rig.boot(sim, reset_on_open=False)
    meta = load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 5)
    rig.crash(ctl, jobs)
    jobs.library.delete(meta["id"])
    ctl2, jobs2 = rig.boot(sim, reset_on_open=False)
    assert jobs2.state == "interrupted" and "cannot reload" in jobs2.recovery["error"]
    with pytest.raises(ControllerError, match="cannot reload"):
        jobs2.recover("entry")
    jobs2.recover("discard")
    assert jobs2.state == "idle" and not (rig.tmp / "journal.json").exists()


def test_discard_keeps_the_program_loaded(rig):
    sim = rig.sim(time_scale=5.0)
    ctl, jobs = rig.boot(sim, reset_on_open=False)
    load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 5)
    time.sleep(1.2)  # the journal is refreshed about once a second
    rig.crash(ctl, jobs)
    ctl2, jobs2 = rig.boot(sim, reset_on_open=False)
    assert jobs2.state == "interrupted" and jobs2.snapshot()["acked"] >= 5  # progress shown as it was
    jobs2.recover("discard")
    assert jobs2.state == "ready" and jobs2.meta["name"] == "job.nc"


# ------------------------------------------------------------------- macros

from gcsender.macros import MacroError, MacroRunner, MacroStore, evaluate, expand, validate  # noqa: E402


def test_expressions_are_whitelisted():
    v = {"wz": 2.5, "safez": 5.0, "wx": None}
    assert evaluate("max(safez, wz + 4) * 2", v) == 13
    assert expand("G0 Z{wz+1} X{-(1+2)/4}", v) == "G0 Z3.5 X-0.75"
    for bad in ("__import__('os')", "wz.real", "[1][0]", "open('x')", "1 if 1 else 2", "wz ** 2"):
        with pytest.raises(MacroError):
            evaluate(bad, v)
    with pytest.raises(MacroError, match="unknown variable"):
        evaluate("foo + 1", v)
    with pytest.raises(MacroError, match="not known yet"):
        evaluate("wx", v)


def test_validate_rejects_bad_macros():
    assert validate([{"name": "a", "text": "G0 Z{safez}"}])[0]["id"]
    for bad in ([{"name": "", "text": "x"}], [{"name": "a", "text": "G0 Z{nope}"}], [{"name": "a", "text": "!"}],
                [{"name": "a", "text": "G0 X1\x07"}], "notalist"):
        with pytest.raises(MacroError):
            validate(bad)


def test_store_defaults_and_persistence(tmp_path):
    st = MacroStore(tmp_path / "macros.json")
    assert [m["name"] for m in st.list()] == ["Z up 5 mm", "Park", "Spindle warm-up"]
    saved = st.save([{"name": "One", "text": "G0 X1"}])
    assert MacroStore(tmp_path / "macros.json").list() == saved


def test_macro_runs_with_position_variables_and_busy_gating(rig):
    sim = rig.sim(time_scale=5.0)
    ctl, _jobs = rig.boot(sim)
    mr = MacroRunner(ctl, MacroStore(None))
    m = mr.save([{"name": "rel", "text": "G0 X3 Y4\n; position is read after the move\nG0 Z{wz+2} X{wx*2}\nwait idle\nG0 Y{wy-1}"}])[0]
    mr.run(m["id"])
    with pytest.raises(ControllerError, match="busy"):
        ctl.command("G0 X0", source="console")
    with pytest.raises(ControllerError, match="busy"):
        mr.run(m["id"])
    wait_for(lambda: mr.state["running"] is None and ctl.busy is None)
    assert mr.state["error"] is None
    assert sim.pos == pytest.approx([6, 3, 2])


def test_macro_error_and_stop_end_it(rig):
    sim = rig.sim(time_scale=1.0)
    ctl, _jobs = rig.boot(sim)
    mr = MacroRunner(ctl, MacroStore(None))
    bad, slow = mr.save([{"name": "bad", "text": "G0 X1\nG99\nG0 X2"}, {"name": "slow", "text": "G1 X50 F300\nwait idle\nG0 X0"}])
    mr.run(bad["id"])
    wait_for(lambda: mr.state["running"] is None)
    assert "line 2" in mr.state["error"]
    wait_for(lambda: ctl.model.status["state"] == "Idle")
    mr.run(slow["id"])
    wait_for(lambda: sim.pos[0] > 1)
    ctl.safe_stop()
    wait_for(lambda: mr.state["running"] is None)
    assert mr.state["error"] == "stopped" and ctl.busy is None
    time.sleep(0.3)
    assert 1 < sim.pos[0] < 50  # the final G0 X0 never ran


def test_api_recover_and_macros(rig):
    from gcsender.api import build_router
    from gcsender.webserver import ApiError

    sim = rig.sim(time_scale=5.0)
    ctl, jobs = rig.boot(sim)
    mr = MacroRunner(ctl, MacroStore(None))
    router = build_router(ctl.bus, ctl, jobs=jobs, macros=mr)

    class Req:
        def __init__(self, body=None, params=None):
            self._b = body or {}
            self.params = params or {}
            self.query = {}

        def json(self):
            return self._b

    def call(method, path, body=None, params=None):
        fn, p = router.route(method, path)
        return fn(Req(body, {**p, **(params or {})}))

    ms = call("GET", "/api/macros")["macros"]
    assert len(ms) == 3
    with pytest.raises(ApiError) as e:
        call("PUT", "/api/macros", {"macros": [{"name": "x", "text": "G0 Z{bogus}"}]})
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        call("POST", "/api/macros/deadbeef/run")
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        call("POST", "/api/job/recover", {"action": "seamless"})
    assert e.value.status == 409 or e.value.status >= 400


def test_comments_are_stripped_outside_templates_only():
    from gcsender.macros import strip_comment

    assert strip_comment("G0 Z{max(safez, wz)} (raise) ; done") == "G0 Z{max(safez, wz)}"
    assert strip_comment("(only a comment)") == ""


def test_a_failed_resume_keeps_the_recovery_and_its_watcher(rig):
    sim = rig.sim()
    ctl, jobs = rig.boot(sim, reset_on_open=False)
    load(jobs)
    jobs.start()
    wait_for(lambda: jobs.state == "running" and jobs.acked > 20)
    ctl.disconnect("unplugged")
    wait_for(lambda: jobs.state == "interrupted")
    ctl.connect("sim")
    wait_for(lambda: jobs.recovery and jobs.recovery["phase"] == "ready")
    ctl.busy = "probing"  # something else holds the machine when Resume is pressed
    with pytest.raises(ControllerError, match="busy"):
        jobs.recover("seamless")
    ctl.busy = None
    assert jobs.state == "interrupted" and jobs.recovery is not None
    ctl.command("G91 G0 X1", source="ui")  # the machine moves away from the matched point
    ctl.command("G90", source="ui")
    rec = wait_for(lambda: jobs.recovery["phase"] == "ready" and jobs.recovery["seamlessLine"] is None and jobs.recovery)
    assert "matches no line end" in rec["note"]  # the watcher re-assessed the new position
    with pytest.raises(ControllerError, match="seamless"):
        jobs.recover("seamless")
