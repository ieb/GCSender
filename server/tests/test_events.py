from gcsender.events import EventBus


def drain(sub):
    out = []
    while True:
        ev = sub.get(timeout=0)
        if ev is None:
            return out
        out.append(ev)


def test_fresh_subscriber_gets_snapshot():
    bus = EventBus(boot="b1")
    bus.publish("connection", {"state": "connected"}, retain=True)
    bus.publish("console", {"line": "ok"})
    sub = bus.subscribe(None)
    (ev,) = drain(sub)
    assert ev.type == "snapshot"
    assert ev.id == "b1:2"
    assert ev.data["retained"]["connection"] == {"state": "connected"}
    assert ev.data["console"] == [{"line": "ok"}]


def test_replay_from_last_event_id():
    bus = EventBus(boot="b1")
    for i in range(5):
        bus.publish("console", {"n": i})
    bus.publish("status", {"state": "Run"}, retain=True, volatile=True)
    sub = bus.subscribe("b1:3")
    evs = drain(sub)
    assert [e.data for e in evs if e.type == "console"] == [{"n": 3}, {"n": 4}]
    # latest volatile status is refreshed after the replay
    assert evs[-1].type == "status" and evs[-1].id is None


def test_replay_up_to_date_client_gets_nothing_but_status():
    bus = EventBus(boot="b1")
    bus.publish("console", {"n": 0})
    sub = bus.subscribe("b1:1")
    assert drain(sub) == []


def test_snapshot_when_id_too_old_or_other_boot():
    bus = EventBus(ring_size=3, boot="b1")
    for i in range(10):
        bus.publish("console", {"n": i})
    assert drain(bus.subscribe("b1:2"))[0].type == "snapshot"  # fell out of the ring
    assert drain(bus.subscribe("b1:7"))[0].type == "console"  # 8,9,10 still in ring
    assert drain(bus.subscribe("old:9"))[0].type == "snapshot"  # previous server run
    assert drain(bus.subscribe("b1:999"))[0].type == "snapshot"  # from the future
    assert drain(bus.subscribe("garbage"))[0].type == "snapshot"


def test_volatile_events_have_no_id_and_are_not_replayed():
    bus = EventBus(boot="b1")
    ev = bus.publish("status", {"x": 1}, retain=True, volatile=True)
    assert ev.id is None
    assert bus.last_id == "b1:0"
    assert bus.snapshot()["retained"]["status"] == {"x": 1}


def test_live_delivery_and_close():
    bus = EventBus(boot="b1")
    sub = bus.subscribe("b1:0")
    bus.publish("console", {"n": 1})
    assert sub.get(timeout=1).data == {"n": 1}
    sub.close()
    assert bus.subscriber_count == 0


def test_slow_consumer_is_disconnected():
    bus = EventBus(boot="b1", queue_limit=3)
    sub = bus.subscribe("b1:0")
    for i in range(5):
        bus.publish("console", {"n": i})
    assert sub.closed and sub.overflowed
