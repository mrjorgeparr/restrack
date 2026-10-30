# Python API

This page covers machine monitoring. For profiling your own code see
[Profiling your code](profiling.md); for DataFrame memory and dtypes see
[Data & precision](dataframes.md).

Everything below is importable from `restrack` unless noted. Snapshots are plain
`dict`s that can be serialised to JSON. Their fields are listed in the
[Metrics reference](metrics.md).

## One reading: `snapshot()`

```python
import restrack

s = restrack.snapshot(interval=0.5)          # rates are measured over `interval` seconds
s["cpu"]["percent"]                           # 23.5
s["memory"]["available"]                      # bytes
s["network"]["recv_bps"]                      # bytes/second, all non-loopback adapters
top = s["processes"]["list"][:5]              # sorted by CPU, highest first
```

Keyword arguments are passed to [`Collector`](#collector), for example
`restrack.snapshot(processes=False)` to skip the process table.

## A simple loop: `watch()`

```python
for s in restrack.watch(interval=1.0):        # forever
    print(s["cpu"]["percent"], s["memory"]["percent"])

snaps = list(restrack.watch(interval=0.2, count=10))   # exactly 10 samples
```

`watch()` runs in the calling thread and paces itself, so samples stay `interval`
apart even if your loop body takes some time.

## Background sampling: `Monitor`

```python
with restrack.Monitor(interval=0.5, history=600) as mon:
    ...                                       # your program keeps running
    mon.latest                                # newest full snapshot (or None before the first)
    mon.history(60)                           # last 60 "light" snapshots
    mon.series("cpu.percent")                 # [(ts, value), ...] over the kept history
    mon.series("cpu.per_cpu.3")               # dotted paths, list indexes allowed
```

`Monitor(interval=1.0, history=300, collector=None, **collector_kwargs)`

| Member | Description |
|---|---|
| `start()` / `stop()` | start or stop the background thread (also via `with`) |
| `latest` | newest snapshot, including process and connection tables |
| `history(n=None)` | kept snapshots, oldest first; these are **light** (no `processes.list`, `network.tcp`, `network.listening`) to bound memory |
| `series(path, n=None)` | `(timestamp, value)` pairs for a dotted path such as `"network.recv_bps"` |
| `subscribe(fn)` | call `fn(snapshot)` after every sample, on the monitor thread; returns an `unsubscribe()` function |
| `wait(after_seq, timeout=None)` | block until a sample newer than `after_seq` exists; returns `(seq, snapshot)` or `None` |
| `stream(timeout=None)` | generator yielding each new snapshot (starts the monitor if needed) |
| `seq`, `running`, `interval`, `collector` | state |

Exceptions raised inside a callback are logged and don't stop the monitor.

```python
def on_sample(s):
    if s["memory"]["percent"] > 95:
        print("memory pressure!")

mon = restrack.Monitor().start()
unsubscribe = mon.subscribe(on_sample)
```

## Collector

The low-level sampler, which `snapshot`, `watch` and `Monitor` use internally.
Call `sample()` repeatedly. The first call primes the counters, so rates read 0 until
the second call.

`Collector(*, processes=True, top_n=None, connections_every=3, sensors_every=5, avg_window=60.0, gpu=True)`

| Parameter | Meaning |
|---|---|
| `processes` | collect the per-process table (the most expensive part: on the order of 10 ms per 100 processes per sample) |
| `top_n` | keep only the top N processes by CPU |
| `connections_every` | refresh the TCP/UDP table every N samples (0 disables it) |
| `sensors_every` | refresh temperatures, fans and battery every N samples (0 disables it) |
| `avg_window` | window in seconds of each process's exponential "Average CPU" |
| `gpu` | try NVIDIA metrics when `nvidia-ml-py` is installed |

`collector.info` holds static facts: hostname, OS, CPU model, core counts, total memory
and boot time. The same dict is available from `restrack.system_info()`.

The collector keeps its own CPU counters and never calls `psutil.cpu_percent()`, so it
doesn't interfere with other code in your process that does.

## Recording to files: `Recorder`

```python
with restrack.Monitor() as mon, restrack.Recorder("usage.csv") as rec:
    mon.subscribe(rec.write)
    time.sleep(60)
```

`Recorder(path, fmt=None, fields=None, include_processes=True)`

- The format comes from the extension (`.csv`, otherwise JSON Lines) or from `fmt`.
- CSV files hold flattened scalar metrics. `fields` restricts and orders the columns.
- JSONL files hold whole snapshots. `include_processes=False` drops the bulky tables.
- A `Recorder` can be called directly, so `mon.subscribe(rec)` works too.

`restrack.flatten(snapshot)` is the function that produces the CSV columns, if you
want flat dicts yourself (for example for pandas: `pd.DataFrame(map(restrack.flatten, snaps))`).

## Long-term storage, alerts and config

```python
cfg = restrack.load_config("restrack.toml")      # or load_config() to search the default locations
with restrack.Tracker(cfg) as t:                 # Monitor + Store + Alerts + log file
    t.monitor.latest
    t.store.query(time.time() - 3600, time.time(), points=60)
```

| Class | What it does |
|---|---|
| `Tracker(cfg, setup_log=True)` | wires a `Monitor` to a `Store` and to `Alerts` from a config dict; `start()`, `stop()`, or use `with` |
| `Store(path, retention_days=30, top_processes=10)` | the SQLite log (see below) |
| `Alerts(cfg_alerts, store=None)` | `check(snapshot)` returns the alert messages fired by this sample |

`Store` methods:

| Method | Returns |
|---|---|
| `write(snapshot)` | stores one row plus the top processes |
| `query(start, end, points=600)` | `{"start", "end", "bucket", "series": [...]}`, where each element holds per-bucket averages of every column, `<col>_max` for spike-prone ones, and `n` samples |
| `top_processes_at(ts, window=60, n=10)` | busiest processes around `ts`: `name`, `pid`, `user`, `cpu` (avg), `cpu_max`, `rss`, `read_bps`, `write_bps`, `samples` |
| `events(start, end, limit=500)` | alert, start and stop events, newest first |
| `export_csv(start, end, file)` | writes raw rows and returns the count |
| `bounds()` | first and last timestamp, row count, file size |
| `prune()` | deletes rows older than `retention_days` (it also runs automatically every hour) |

The column names are listed in [Storage & alerts](storage-and-alerts.md#what-is-stored).
`restrack.config.write_template(path=None, force=False)` writes the commented config template.

## Embedding the web UI

```python
from restrack.web import ResmonServer

mon = restrack.Monitor(interval=1.0)
srv = ResmonServer(monitor=mon, port=0).start()   # port=0 picks a free port
print(srv.url)
...
srv.close()                                       # also stops the monitor
```

`ResmonServer(monitor=None, host="127.0.0.1", port=8765, allow_control=False, interval=1.0, store=None, config=None)`.
Pass `store=` to enable the History tab and its endpoints. `restrack.web.serve(config, **overrides)`
is what `restrack web` runs.

## Other helpers

| Function | Purpose |
|---|---|
| `process_detail(pid)` | on-demand details for one process: exe, cmdline, cwd, open files, sockets, USS, context switches; raises `psutil.NoSuchProcess` |
| `lighten(snapshot)` | copy without the process and connection tables |
| `get_path(snapshot, "a.b.0")` | resolve a dotted path |
| `system_info()` | static machine facts |
