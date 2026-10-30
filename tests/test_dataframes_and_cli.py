import json
import os
import sys

import pytest

import restrack
from restrack import cli

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
from restrack.dataframes import float32_precision, memory_report, optimize  # noqa: E402


def _frame(n=20_000):
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "country": rng.choice(["ES", "FR", "DE"], n),                 # low cardinality text
        "small_int": rng.integers(0, 200, n).astype("int64"),          # fits uint8
        "amount": np.round(rng.gamma(2, 50, n), 2),                    # money: 2 decimals
        "noise": rng.random(n),                                         # full float64 precision
        "big_money": np.round(rng.uniform(1e8, 1e9, n), 2),            # float32 can't keep cents here
        "whole_with_nan": np.where(rng.random(n) < 0.1, np.nan, rng.integers(0, 1000, n)).astype("float64"),
        "num_text": rng.integers(0, 10_000, n).astype(str),             # numbers stored as text
        "uid": [f"user-{i}" for i in range(n)],                         # unique text
    })


def test_memory_report_suggestions():
    rep = memory_report(_frame()).set_index("column")
    assert rep.loc["country", "suggested"] == "category"
    assert rep.loc["small_int", "suggested"] == "uint8"
    assert rep.loc["amount", "suggested"] == "float32"
    assert pd.isna(rep.loc["noise", "suggested"]) and "keep float64" in rep.loc["noise", "note"]
    assert pd.isna(rep.loc["big_money", "suggested"])
    assert rep.loc["whole_with_nan", "suggested"] == "UInt16"
    assert rep.loc["num_text", "suggested"] == "int64" and "numbers stored as text" in rep.loc["num_text", "note"]
    assert rep.loc["uid", "suggested"] != "category"
    assert rep.attrs["saving"] > 0 and rep.attrs["est_total"] < rep.attrs["total"]


def test_float32_precision_is_decimal_aware():
    cents = np.round(np.random.default_rng(1).uniform(0, 10_000, 5000), 2)
    p = float32_precision(cents)
    assert p["safe"] and p["decimals"] == 2
    assert not float32_precision(np.array([123456789.12, 987654321.98]))["safe"]
    assert not float32_precision(np.array([1e300]))["safe"]          # overflows float32
    assert float32_precision(np.array([np.nan, np.inf]))["safe"]       # nothing finite to lose


def test_optimize_keeps_values_at_their_precision():
    df = _frame()
    small = optimize(df)
    assert small.memory_usage(deep=True).sum() < df.memory_usage(deep=True).sum() * 0.75
    assert str(small["amount"].dtype) == "float32"
    assert np.array_equal(np.round(small["amount"].astype("float64"), 2), df["amount"])
    assert str(small["noise"].dtype) == "float64"                    # unsafe: untouched
    assert str(small["big_money"].dtype) == "float64"
    assert (small["country"].astype(str) == df["country"]).all()
    assert small["whole_with_nan"].isna().sum() == df["whole_with_nan"].isna().sum()
    forced = optimize(df, floats=True)
    assert str(forced["noise"].dtype) == "float32"


def test_profiler_inspect_includes_report():
    with restrack.Profiler(functions=None) as prof:
        rep = prof.inspect(_frame(1000), "tx")
    assert rep["columns_count"] == 8
    assert prof.report()["dataframes"][0]["name"] == "tx"


# --------------------------------------------------------------------------- CLI
def test_cli_dtypes(tmp_path, capsys):
    path = tmp_path / "d.csv"
    _frame(2000).to_csv(path, index=False)
    assert cli.main(["dtypes", str(path)]) == 0
    out = capsys.readouterr().out
    assert "category" in out and "uint8" in out and "float32" in out and "smaller" in out


def test_cli_dtypes_refuses_pickle(tmp_path):
    path = tmp_path / "x.pkl"
    pd.DataFrame({"a": [1]}).to_pickle(path)
    with pytest.raises(SystemExit):
        cli.main(["dtypes", str(path)])


def test_cli_dtypes_write_parquet(tmp_path):
    pytest.importorskip("pyarrow")
    src = tmp_path / "d.csv"
    _frame(2000).to_csv(src, index=False)
    out = tmp_path / "d.parquet"
    assert cli.main(["dtypes", str(src), "--write", str(out)]) == 0
    back = pd.read_parquet(out)
    assert str(back["country"].dtype) == "category" and str(back["amount"].dtype) == "float32"


def test_cli_profile_writes_reports(tmp_path, capsys):
    script = tmp_path / "job.py"
    script.write_text("import restrack, sys\nwith restrack.section('step'):\n    sum(range(10**5))\n"
                      "print('args', sys.argv[1:])\n")
    html, js = tmp_path / "r.html", tmp_path / "r.json"
    rc = cli.main(["profile", "-o", str(html), "--json", str(js), str(script), "--flag", "x"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "args ['--flag', 'x']" in captured.out          # options after the script go to the script
    assert "restrack profile: job.py" in captured.err
    assert html.exists() and json.load(open(js))["sections"][0]["path"] == "step"


def test_cli_profile_failing_script_still_reports(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("raise KeyError('nope')\n")
    js = tmp_path / "r.json"
    assert cli.main(["profile", "--no-html", "--json", str(js), str(script)]) == 1
    assert any("nope" in n for n in json.load(open(js))["notes"])


def test_cli_counters(capsys):
    assert cli.main(["counters"]) == 0
    assert "CPU hardware counters" in capsys.readouterr().out


# --------------------------------------------------------------------------- machine-level page cache
def test_snapshot_page_cache_fields(tmp_path):
    s = restrack.snapshot(0.2)
    d, m = s["disk"], s["memory"]
    assert "cache_read_bps" in d and "cache_hit_percent" in d
    if sys.platform.startswith("linux"):
        assert m["dirty"] is not None and m["writeback"] is not None
        assert s["processes"]["read_logical_bps"] is not None
        if d["cache_hit_percent"] is not None:
            assert 0 <= d["cache_hit_percent"] <= 100
    else:
        assert d["cache_hit_percent"] is None


def test_store_has_cache_columns(tmp_path):
    st = restrack.Store(str(tmp_path / "m.db"))
    snaps = list(restrack.watch(interval=0.05, count=2))
    for s in snaps:
        st.write(s)
    row = st.query(snaps[0]["ts"] - 1, snaps[-1]["ts"] + 1, points=1)["series"][0]
    assert "cache_hit" in row and "cache_read" in row and "dirty" in row
