import threading
import time

import pytest

from gcsender.streamer import Streamer, StreamerError


class Sink:
    def __init__(self):
        self.data = []

    def __call__(self, b):
        self.data.append(b)


def test_budget_blocks_until_ok():
    sink = Sink()
    s = Streamer(sink, budget=20)
    p1 = s.send("G1X100Y100", timeout=1)  # 11 bytes
    assert s.used == 11
    with pytest.raises(StreamerError):
        s.send("G1X200Y200", timeout=0.05)  # 11 more would exceed 20
    t = threading.Thread(target=lambda: s.on_response("ok", "ok"))
    t.start()
    p2 = s.send("G1X200Y200", timeout=1)
    t.join()
    assert p1.status == "ok" and p2.status is None
    assert sink.data == [b"G1X100Y100\n", b"G1X200Y200\n"]


def test_responses_pop_in_order_and_collect_info():
    s = Streamer(Sink())
    a = s.send("$G")
    b = s.send("G0X1")
    s.on_info("[G0 G54]")
    s.on_response("ok", "ok")
    s.on_response("error", "error: Bad number format")
    assert a.lines == ["[G0 G54]"] and a.status == "ok"
    assert b.status == "error" and b.response.startswith("error")
    assert s.used == 0


def test_reset_aborts_and_releases_waiters():
    s = Streamer(Sink(), budget=10)
    p = s.send("G0X12345", timeout=1)
    errors = []

    def blocked():
        try:
            s.send("G0X99999", timeout=5)
        except StreamerError as e:
            errors.append(str(e))

    t = threading.Thread(target=blocked)
    t.start()
    time.sleep(0.1)  # let the sender block on the budget
    s.reset("controller reset")
    t.join(2)
    assert p.status == "aborted"
    assert errors == ["controller reset"]


def test_line_too_long():
    with pytest.raises(StreamerError):
        Streamer(Sink()).send("G1" + "X1" * 80)
