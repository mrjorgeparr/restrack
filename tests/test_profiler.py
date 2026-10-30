import json
import os
import sys
import textwrap
import time
from functools import lru_cache

import pytest

import restrack
from restrack import profiler as P
from restrack.hwcounters import PerfCounters, derive, status

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


def _busy(seconds):
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x += 1
    return x


# --------------------------------------------------------------------------- sections & timing
def test_sections_nest_time_and_order():
    with restrack.Profiler("t", functions=None) as prof:
        with prof.section("outer"):
            _busy(0.05)
            with prof.section("inner"):
                _busy(0.1)
            with prof.section("inner"):
                _busy(0.05)
    r = prof.report()
    by = {s["path"]: s for s in r["sections"]}
    assert [s["path"] for s in r["sections"]] == ["outer", "outer/inner"]  # tree order
    assert by["outer/inner"]["calls"] == 2
    assert 0.14 <= by["outer/inner"]["wall"] <= 0.5
    assert by["outer"]["wall"] >= by["outer/inner"]["wall"]
    assert by["outer"]["self"] == pytest.approx(by["outer"]["wall"] - by["outer/inner"]["wall"], abs=0.02)
    assert by["outer"]["cores"] > 0.5            # busy loop = CPU-bound
    assert r["wall"] >= by["outer"]["wall"]
    assert r["rss_peak"] >= r["rss_start"] > 0


def test_exception_inside_section_still_recorded():
    prof = restrack.Profiler(functions=None).start()
    with pytest.raises(ValueError):
        with prof.section("boom"):
            raise ValueError("x")
    with prof.section("after"):
        pass
    r = prof.stop()
    assert {s["path"] for s in r["sections"]} == {"boom", "after"}   # not nested under "boom"


def test_unclosed_sections_are_closed_on_stop():
    prof = restrack.Profiler(functions=None).start()
    prof._enter("left-open", None)
    r = prof.stop()
    assert r["sections"][0]["path"] == "left-open"


def test_module_level_annotations_are_noops_without_profiler():
    assert restrack.current() is None
    with restrack.section("free"):
        pass
    restrack.count("x")
    restrack.gauge("y", 1.0)

    @restrack.track
    def f(a):
        return a + 1

    assert f(1) == 2


def test_only_one_profiler_at_a_time():
    with restrack.Profiler(functions=None):
        with pytest.raises(RuntimeError):
            restrack.Profiler(functions=None).start()
    assert restrack.current() is None


# --------------------------------------------------------------------------- caches, counters, gauges
def test_caches_counters_gauges_and_hit_rates():
    @lru_cache(maxsize=8)
    def sq(x):
        return x * x

    with restrack.Profiler(functions=None) as prof:
        prof.watch_cache(sq, "sq")
        for i in [1, 2, 1, 1, 3, 2]:
            sq(i)
        for _ in range(3):
            restrack.count("lookup.hit")
        restrack.count("lookup.miss")
        restrack.count("disk.hits", 5)          # only hits recorded -> 100 %
        restrack.count("api.miss", 2)           # only misses recorded -> 0 %
        restrack.gauge("model.precision", 0.91)
        restrack.gauge("model.precision", 0.93)
    r = prof.report()
    assert r["caches"]["sq"] == {"hits": 3, "misses": 3, "size": 3, "maxsize": 8, "hit_rate": 50.0}
    assert r["hit_rates"]["lookup"]["hit_rate"] == 75.0
    assert r["hit_rates"]["disk"]["hit_rate"] == 100.0
    assert r["hit_rates"]["api"]["hit_rate"] == 0.0
    assert [v for _, v in r["gauges"]["model.precision"]] == [0.91, 0.93]
    assert "75.0% hit rate" in prof.summary()


# --------------------------------------------------------------------------- pandas-aware track
def test_track_records_rows_and_track_module(tmp_path, monkeypatch):
    mod = tmp_path / "mymod_restrack_test.py"
    mod.write_text(textwrap.dedent("""
        import pandas as pd
        def make(n):
            return pd.DataFrame({"a": range(n)})
        def double(df):
            return pd.concat([df, df])
        class K:
            def m(self):
                return 1
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    import mymod_restrack_test as m
    original = m.make
    with restrack.Profiler(functions=None) as prof:
        wrapped = prof.track_module(m)
        assert set(wrapped) >= {"make", "double", "K.m"}
        m.double(m.make(1000))
        m.K().m()
    assert m.make is original                     # unwrapped again
    by = {s["name"]: s for s in prof.report()["sections"]}
    assert by["mymod_restrack_test.make"]["rows_out"] == 1000
    assert by["mymod_restrack_test.double"]["rows_in"] == 1000
    assert by["mymod_restrack_test.double"]["rows_out"] == 2000
    assert by["K.m"]["calls"] == 1


# --------------------------------------------------------------------------- memory
def test_memory_tracing_peak_and_allocation_sites():
    with restrack.Profiler(functions=None, where=True) as prof:
        with prof.section("alloc"):
            big = bytearray(40 * 2**20)  # noqa: F841  (this line must be reported)
            time.sleep(0.15)              # long enough for the where-diff
            del big
    s = prof.report()["sections"][0]
    assert s["peak_alloc"] >= 40 * 2**20
    if s["sites"]:  # snapshot taken at the end: the bytearray was already freed then
        assert all(site["size"] > 0 for site in s["sites"])
    assert prof.report()["peak_alloc"] >= 40 * 2**20


def test_where_reports_the_line_that_holds_memory():
    keep = []
    with restrack.Profiler(functions=None, where=True) as prof:
        with prof.section("hold"):
            keep.append(bytearray(30 * 2**20))  # held at the end of the section
            time.sleep(0.15)
    sites = prof.report()["sections"][0]["sites"]
    top = sites[0]
    assert top["file"].endswith("test_profiler.py") and "bytearray" in top["code"]
    assert top["size"] >= 30 * 2**20


# --------------------------------------------------------------------------- function profiling
def test_sampled_hot_lines_point_at_user_code():
    with restrack.Profiler(sample_interval=0.002) as prof:
        with prof.section("hot"):
            _busy(0.3)
    r = prof.report()
    assert r["stack_samples"] > 20
    top = r["lines"][0]
    assert top["file"].endswith("test_profiler.py") and top["func"] == "_busy"
    assert top["section"] == "hot"
    # restrack's own frames are never blamed on user lines
    assert not any("tracemalloc" in c["callee"] or "psutil" in c["callee"] for ln in r["lines"] for c in ln["calls_into"])


def test_cprofile_mode_matches_section_time():
    def work():
        return _busy(0.15)

    with restrack.Profiler(functions="cprofile") as prof:
        with prof.section("w"):
            work()
    r = prof.report()
    fn = r["functions"]
    assert fn["mode"] == "cprofile"
    entry = next(e for e in fn["user_cum"] if e["label"].startswith("work "))
    assert entry["calls"] == 1
    assert entry["cum"] == pytest.approx(r["sections"][0]["wall"], rel=0.25)


# --------------------------------------------------------------------------- I/O, faults, timeline
def test_io_and_faults_and_timeline(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(os.urandom(8 * 2**20))
    with restrack.Profiler(functions=None, interval=0.02) as prof:
        with prof.section("read"):
            for _ in range(3):
                path.read_bytes()
            time.sleep(0.1)
    r = prof.report()
    s = r["sections"][0]
    if sys.platform.startswith("linux"):
        # the file was just written, so it's in the page cache: reads come from cache, not disk
        assert s["read_cache"] >= 20 * 2**20
        assert r["env"]["io_split"] is True
    assert r["minflt"] is not None
    assert len(r["samples"]) >= 3 and r["sample_fields"][0] == "t"
    assert all(0 <= row[2] <= (os.cpu_count() or 1) for row in r["samples"])  # cores within bounds


# --------------------------------------------------------------------------- output
def test_html_and_json_reports(tmp_path):
    with restrack.Profiler("evil </script><b>name", functions=None) as prof:
        with prof.section("</script>"):
            prof.inspect(pd.DataFrame({"a": ["x"] * 100, "b": np.arange(100)}), "df")
    html = prof.to_html()
    assert html.count("</script>") == 2  # the two real script tags only: data is escaped
    assert "window.RESTRACK" in html and "{{" not in html
    jpath = prof.save_json(str(tmp_path / "r.json"))
    data = json.load(open(jpath))
    assert data["dataframes"][0]["name"] == "df"
    hpath = prof.save_html(str(tmp_path / "r.html"))
    assert os.path.getsize(hpath) > 10_000


def test_run_script_captures_errors_and_argv(tmp_path):
    script = tmp_path / "s.py"
    script.write_text("import sys, restrack\nwith restrack.section('x'):\n    pass\n"
                      "assert sys.argv[1:] == ['a', 'b']\nraise RuntimeError('late failure')\n")
    prof = P.run_script(str(script), ["a", "b"], functions=None)
    r = prof.report()
    assert isinstance(prof.error, RuntimeError)
    assert any("late failure" in n for n in r["notes"])
    assert r["sections"][0]["path"] == "x"
    assert restrack.current() is None


# --------------------------------------------------------------------------- CPU counters
def test_hwcounters_status_and_derive():
    st = status()
    assert "available" in st and st["reason"]
    d = derive({"cycles": 1000, "instructions": 2500, "l1d_loads": 400, "l1d_misses": 20,
                "llc_loads": 50, "llc_misses": 10, "branches": 100, "branch_misses": 3})
    assert d == {"ipc": 2.5, "cache_miss_pct": None, "l1d_miss_pct": 5.0, "llc_miss_pct": 20.0,
                 "branch_miss_pct": 3.0}
    assert PerfCounters.delta({"a": 5.0}, {"a": 7.0, "b": 1.0}) == {"a": 2.0}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="perf_event is Linux-only")
def test_software_perf_counter_counts_real_work():
    pc = PerfCounters(["task_clock_ns"])
    if "task_clock_ns" not in pc.fds:
        pytest.skip("perf_event_open blocked here")
    before = pc.read()
    _busy(0.1)
    d = PerfCounters.delta(before, pc.read())
    pc.close()
    assert 0.07e9 <= d["task_clock_ns"] <= 0.5e9


class _FakePerf:
    """Stands in for PerfCounters on machines whose CPU counters are hidden (VMs)."""

    def __init__(self, *a, **k):
        self.n = 0
        self.fds = {"cycles": 1}

    available = True
    delta = staticmethod(PerfCounters.delta)

    def read(self):
        self.n += 1
        return {"cycles": self.n * 1e9, "instructions": self.n * 1.5e9,
                "llc_loads": self.n * 1e7, "llc_misses": self.n * 4e6}

    def close(self):
        pass


def test_profiler_attaches_cpu_cache_ratios(monkeypatch):
    import restrack.hwcounters as hw
    monkeypatch.setattr(hw, "PerfCounters", _FakePerf)
    with restrack.Profiler(functions=None, hw=True) as prof:
        with prof.section("s"):
            _busy(0.6)
    r = prof.report()
    s = r["sections"][0]
    assert s["hw"]["cycles"] == 1e9 and s["hw_ratios"]["ipc"] == 1.5
    assert s["hw_ratios"]["llc_miss_pct"] == 40.0
    assert r["env"]["hw_counters"] is True and r["hw_ratios"]["ipc"] == 1.5
    assert "CPU caches: IPC 1.5" in prof.summary()
    assert any("last-level-cache" in h for h in s["hints"])


def test_hw_true_explains_when_unavailable(monkeypatch):
    import restrack.hwcounters as hw

    class Nope(_FakePerf):
        available = False

    monkeypatch.setattr(hw, "PerfCounters", Nope)
    monkeypatch.setattr(hw, "status", lambda: {"reason": "hidden by hypervisor"})
    with restrack.Profiler(functions=None, hw=True) as prof:
        pass
    assert any("hidden by hypervisor" in n for n in prof.report()["notes"])
    assert prof.report()["hw"] is None
