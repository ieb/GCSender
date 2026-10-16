from gcsender.protocol import classify, parse_status


def test_status_09_full_mask():
    st = parse_status("<Run,MPos:1.000,-2.500,3.250,WPos:0.000,0.000,1.000,Buf:4,RX:12,Lim:010>")
    assert st["format"] == "0.9"
    assert st["state"] == "Run"
    assert st["mpos"] == [1.0, -2.5, 3.25]
    assert st["wpos"] == [0.0, 0.0, 1.0]
    assert st["buf"] == 4 and st["rx"] == 12 and st["lim"] == "010"


def test_status_11():
    st = parse_status("<Hold:0|MPos:1.000,2.000,3.000|Bf:15,128|FS:500,8000|Pn:P|WCO:0.500,0.000,-1.000|Ov:100,50,120>")
    assert st["format"] == "1.1"
    assert st["state"] == "Hold" and st["substate"] == 0
    assert st["bf"] == [15, 128]
    assert st["fs"] == [500.0, 8000.0]
    assert st["pins"] == "P"
    assert st["wco"] == [0.5, 0.0, -1.0]
    assert st["ov"] == [100, 50, 120]


def test_status_bare():
    st = parse_status("<Idle>")
    assert st["state"] == "Idle" and st["format"] is None


def test_classify_09_and_11():
    assert classify("ok").kind == "ok"
    e = classify("error: Expected command letter")
    assert e.kind == "error" and e.data["code"] is None and e.data["text"] == "Expected command letter"
    e = classify("error:20")
    assert e.kind == "error" and e.data["code"] == 20
    a = classify("ALARM: Probe fail")
    assert a.kind == "alarm" and a.data["text"] == "Probe fail"
    assert classify("ALARM:5").data["code"] == 5
    b = classify("Grbl 0.9j ['$' for help]")
    assert b.kind == "banner" and b.data["version"] == "0.9j"
    assert classify("Grbl 1.1f ['$' for help]").data["version"] == "1.1f"


def test_classify_feedback():
    m = classify("[G0 G54 G17 G21 G90 G94 M0 M5 M9 T0 F0. S0.]")
    assert m.data == {"type": "modal", "words": ["G0", "G54", "G17", "G21", "G90", "G94", "M0", "M5", "M9", "T0", "F0.", "S0."]}
    assert classify("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]").data["type"] == "modal"
    prb = classify("[PRB:1.000,2.000,-3.000:1]").data
    assert prb == {"type": "probe", "value": [1.0, 2.0, -3.0], "ok": True}
    assert classify("[G54:1.000,2.000,3.000]").data == {"type": "offset", "name": "G54", "value": [1.0, 2.0, 3.0]}
    assert classify("[TLO:0.500]").data == {"type": "tlo", "value": 0.5}
    assert classify("[0.9j.20160726:]").data["version"] == "0.9j.20160726"
    assert classify("[VER:1.1f.20170801:]").data["version"] == "1.1f.20170801"
    assert classify("[MSG:'$H'|'$X' to unlock]").data == {"type": "msg", "text": "'$H'|'$X' to unlock"}
    assert classify("['$H'|'$X' to unlock]").data == {"type": "msg", "text": "'$H'|'$X' to unlock"}


def test_classify_settings():
    s = classify("$110=1000.000 (x max rate, mm/min)")
    assert s.kind == "setting" and s.data["id"] == 110 and s.data["value"] == "1000.000"
    assert classify("$10=1").data["desc"] is None
