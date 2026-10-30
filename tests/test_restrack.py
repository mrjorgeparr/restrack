import csv
import json
import os
import time
import urllib.request

import pytest

import restrack
from restrack.config import load_config, write_template
from restrack.storage import Alerts, Store, Tracker
from restrack.web.server import ResmonServer


def test_snapshot_shape():
    s = restrack.snapshot(0.2)
    for key in ("ts", "cpu", "memory", "disk", "network", "processes", "sensors", "gpu"):
        assert key in s
    assert 0 <= s["cpu"]["percent"] <= 100
    assert len(s["cpu"]["per_cpu"]) == restrack.system_info()["cpu_logical"]
    assert s["memory"]["total"] > 0
    assert s["processes"]["count"] > 0
    assert any(p["pid"] == os.getpid() for p in s["processes"]["list"])
    json.dumps(s)  # must be JSON serialisable


def test_watch_and_top_n():
    snaps = list(restrack.watch(interval=0.1, count=3, top_n=5))
    assert len(snaps) == 3
    assert all(len(s["processes"]["list"]) <= 5 for s in snaps)
    assert snaps[0]["seq"] < snaps[-1]["seq"]


def test_monitor_history_series_and_wait():
    got = []
    with restrack.Monitor(interval=0.1, history=5) as mon:
        unsub = mon.subscribe(got.append)
        item = mon.wait(0, timeout=3)
        assert item is not None
        time.sleep(0.8)
        unsub()
    assert len(mon.history()) == 5                      # bounded
    assert "list" not in mon.history()[0]["processes"]   # history is "light"
    assert "list" in mon.latest["processes"]             # latest is full
    series = mon.series("cpu.per_cpu.0")
    assert series and isinstance(series[0][1], float)
    assert got


def test_recorder_csv_and_jsonl(tmp_path):
    snaps = list(restrack.watch(interval=0.1, count=2))
    p_csv = tmp_path / "x.csv"
    with restrack.Recorder(str(p_csv)) as rec:
        for s in snaps:
            rec.write(s)
    rows = list(csv.DictReader(open(p_csv)))
    assert len(rows) == 2 and "cpu.percent" in rows[0] and "memory.percent" in rows[0]
    p_jl = tmp_path / "x.jsonl"
    with restrack.Recorder(str(p_jl), include_processes=False) as rec:
        rec.write(snaps[0])
    obj = json.loads(open(p_jl).readline())
    assert "list" not in obj["processes"]


def test_config_template_roundtrip(tmp_path):
    p = write_template(str(tmp_path / "c.toml"))
    cfg = load_config(str(p))
    assert cfg["monitor"]["interval"] == 1.0
    assert cfg["storage"]["path"].endswith("metrics.db")
    (tmp_path / "d.toml").write_text('[monitor]\ninterval = 2.5\n[storage]\npath = "m.db"\n')
    cfg = load_config(str(tmp_path / "d.toml"))
    assert cfg["monitor"]["interval"] == 2.5 and cfg["storage"]["path"] == "m.db"
    assert cfg["alerts"]["cpu_percent"] == 90  # defaults still merged in


def test_store_query_top_events(tmp_path):
    st = Store(str(tmp_path / "m.db"), top_processes=3)
    snaps = list(restrack.watch(interval=0.05, count=4))
    for s in snaps:
        st.write(s)
    st.event("WARNING", "cpu", "test alert", 99.0)
    t0, t1 = snaps[0]["ts"] - 1, snaps[-1]["ts"] + 1
    res = st.query(t0, t1, points=2)
    assert res["series"] and "cpu_max" in res["series"][0]
    assert sum(r["n"] for r in res["series"]) == 4
    assert st.top_processes_at(snaps[-1]["ts"], window=10)
    assert st.events(t0, time.time() + 1)[0]["message"] == "test alert"
    assert st.bounds()["rows"] == 4


def test_alerts_sustain_and_cooldown():
    a = Alerts({"enabled": True, "sustain": 2, "cooldown": 100, "cpu_percent": 50})
    base = restrack.snapshot(0.05)

    def snap(ts, cpu):
        s = dict(base, ts=ts)
        s["cpu"] = dict(base["cpu"], percent=cpu)
        s["disk"] = dict(base["disk"], partitions=[])
        return s

    assert a.check(snap(0, 80)) == []          # not sustained yet
    assert len(a.check(snap(3, 80))) == 1       # sustained -> fires
    assert a.check(snap(4, 80)) == []           # cooldown
    assert a.check(snap(5, 10)) == []           # resolves quietly


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read()


def test_web_endpoints(tmp_path):
    cfg = load_config()
    cfg["monitor"]["interval"] = 0.2
    cfg["storage"].update(path=str(tmp_path / "w.db"), every=1)
    cfg["logging"]["file"] = "-"
    tracker = Tracker(cfg).start()
    srv = ResmonServer(monitor=tracker.monitor, port=0, store=tracker.store, config=cfg).start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        time.sleep(1.0)
        assert _get(base + "/")[0] == 200
        assert b"app.js" in _get(base + "/")[1]
        info = json.loads(_get(base + "/api/info")[1])
        assert info["storage"] is True and info["allow_control"] is False
        assert json.loads(_get(base + "/api/snapshot")[1])["processes"]["list"]
        assert len(json.loads(_get(base + "/api/history?n=3")[1])) <= 3
        assert json.loads(_get(base + f"/api/process/{os.getpid()}")[1])["pid"] == os.getpid()
        assert json.loads(_get(base + "/api/range?points=10")[1])["series"]
        assert _get(base + "/api/export.csv")[1].startswith(b"time_iso")
        # DNS-rebinding guard
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + "/api/info", {"Host": "evil.example"})
        assert e.value.code == 403
        # process control disabled by default
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/process/99999/kill", method="POST",
                                                          headers={"X-Restrack-Control": "1"}), timeout=5)
        assert e.value.code == 403
        # path traversal
        with pytest.raises(urllib.error.HTTPError):
            _get(base + "/static/..%2f..%2fserver.py")
    finally:
        srv.close()
        tracker.stop()
