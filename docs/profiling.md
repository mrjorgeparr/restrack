# Profiling your code

The rest of restrack watches the **machine**. The profiler watches **your program**. It
shows where time and memory go, how much was read from disk versus the OS cache, how
well your caches and the CPU's caches did, and which lines of your code (and which
pandas calls they make) are responsible.

```bash
restrack profile --open my_pipeline.py --input data.csv     # options go BEFORE the script
```

```python
import restrack

with restrack.Profiler("etl") as prof:
    with prof.section("load"):
        df = pd.read_csv("big.csv")
    with prof.section("transform"):
        df = transform(df)
prof.print_summary()
prof.save_html("etl-report.html")
```

The result is a console summary plus one self-contained HTML report. It works offline,
can be emailed, and opens in any browser.

## What you get

| Measurement | Per section | Whole run | Notes |
|---|---|---|---|
| Wall time, CPU time, cores used | ✅ | ✅ | `cores` = CPU ÷ wall: below 0.5 means waiting, about 1 means single-core bound |
| Peak memory (RSS) | ✅ | ✅ | exact on Linux (kernel high-water mark), sampled elsewhere |
| Memory kept (end − start) | ✅ | ✅ | |
| Peak Python allocations | ✅ | ✅ | with `memory=True` (tracemalloc) |
| **Which lines hold the memory** | ✅ | | with `where=True` |
| Disk reads vs **OS page-cache** reads | ✅ | ✅ | Linux |
| Disk writes | ✅ | ✅ | |
| Page faults: minor (RAM) / **major (disk)** | ✅ | ✅ | Windows reports one combined number |
| **CPU caches**: IPC, L1d / LLC miss %, branch miss % | ✅ | ✅ | Linux with exposed hardware counters, see [below](#cpu-caches) |
| **Your caches**: hit rates | | ✅ | `watch_cache()` and `count()` |
| Rows/second | ✅ | | for `@track`-ed functions taking or returning DataFrames |
| Hot lines + the library call each one spends its time in | | ✅ | statistical sampling, low overhead |
| Exact call counts and times | | ✅ | `functions="cprofile"` |
| Timeline: memory, cores, machine CPU/RAM, disk/cache reads | | ✅ | every 0.1 s |
| DataFrame memory and safe dtypes | | ✅ | `prof.inspect(df)`, see [Data & precision](dataframes.md) |

Each section also gets plain-language **observations**: waiting more than computing,
data coming from disk, large temporary copies, low rows/s, cache-unfriendly memory
access. These are heuristics to point you somewhere, not verdicts.

## Marking sections

```python
with prof.section("load"):          # a block
    ...

@prof.track                         # every call of a function
def clean(df): ...

prof.track_module(my_pipeline)      # every function and method defined in a module
```

Sections nest, and the report shows them as a tree with *self* time (excluding
children).

For annotations that stay in your code permanently, use the module-level versions.
They do nothing when no profiler is running:

```python
import restrack

@restrack.track
def clean(df): ...

with restrack.section("join"):
    ...

restrack.count("fx_cache.hit")      # counters
restrack.gauge("batch.size", 512)   # values over time
```

From the command line, `--track-module my_pipeline` wraps a module without editing it.

!!! note "Throughput"
    `@track` records `rows_in` (first DataFrame argument) and `rows_out` (returned
    DataFrame) to compute rows/s. In a `with` block you can set them yourself:
    `with prof.section("x") as s: s.rows_in = len(df)`.

## Caches

### Your Python caches

```python
@functools.lru_cache(maxsize=10_000)
def fx_rate(currency, day): ...

prof.watch_cache(fx_rate)                  # hits, misses, size and hit rate at the end of the run
```

For your own caches (dicts, Redis, disk), count hits and misses. Any pair named
`<name>.hit` / `<name>.miss` (also `.hits`/`.misses`, `_hit`/`_miss`) gets a hit rate:

```python
v = cache.get(key)
restrack.count("features.hit" if v is not None else "features.miss")
```

### The OS page cache

When your code reads a file, the data either comes from RAM (because it was read
recently) or from the disk. On Linux restrack shows both per section, so you can tell a
fast warm run from a slow cold one:

- **Disk read**: bytes that actually came from storage.
- **Cache read**: bytes read without touching the disk. This mostly means the page
  cache, but it also includes pipes and sockets.

High **major page faults** mean memory-mapped data or swapped-out memory had to come
from disk.

Machine-wide, the live UI shows the same split ("% of reads served from cache") and the
dirty/write-back memory waiting to be flushed. See [Metrics](metrics.md).

### CPU caches

With hardware counters available, each section shows:

| Ratio | Meaning | Rule of thumb |
|---|---|---|
| **IPC** (instructions per cycle) | how much work the CPU gets done per tick | > 2 good, < 1 stalled (usually waiting on memory) |
| **L1d miss %** | loads not found in the smallest, fastest cache | low single digits is typical |
| **LLC miss %** | loads that missed the last-level cache and went to RAM (~100× slower) | > 30 % on a heavy section suggests random access over large data |
| **branch miss %** | mispredicted branches | > 5 % suggests data-dependent branching |

In pandas code, high LLC miss rates usually come from joins or lookups on unsorted keys,
`object` (Python object) columns, or row-wise access patterns.

Check whether a machine supports them:

```bash
restrack counters
```

!!! warning "Linux only, and not in most VMs"
    CPU cache counters are read through Linux `perf_event`. **They aren't available on
    Windows or macOS**, which don't expose them to normal programs; use Intel VTune,
    AMD uProf or Apple Instruments there. On Linux they need a CPU whose performance
    counters are visible to the OS (most physical machines; many cloud VMs hide them)
    and `kernel.perf_event_paranoid ≤ 2`. When they're missing, the report simply
    omits these columns (or explains why, with `hw=True`).

    The counters measure the thread that starts the profiler, plus threads and child
    processes it starts *afterwards*. Thread pools created earlier (for example by NumPy
    or BLAS at import time) aren't included.

## Memory: `memory` and `where`

By default memory is measured as **RSS** (what the OS sees). That's cheap and catches
everything, including NumPy and pandas buffers.

| Option | Adds | Cost |
|---|---|---|
| `memory=True` / `--memory` | exact peak of Python-visible allocations per section (tracemalloc) | about 3–5× slower |
| `where=True` / `--where` | the lines of your code holding memory at each section's peak, and the library call that allocated it | about 10–30× slower |

Use `where` on a **sample** of your data. It's for finding *which line* creates the big
temporary copy, not for timing. Snapshots are only taken for the first three calls of
each section, and are skipped for sections shorter than 0.1 s.

## Function-level detail: `functions`

| Mode | How | Overhead | Gives |
|---|---|---|---|
| `"sample"` (default) | looks at the call stack every 5 ms | a few % | hot lines of your code, the library call each spends its time in, time per function |
| `"cprofile"` | Python's `cProfile` | 2–3× on Python-heavy code | exact call counts, self/total time, library internals |
| `None` | off | – | sections only |

On Python 3.12+, `cProfile` observes every thread. In `"cprofile"` mode the timeline
chart is therefore not recorded, so restrack's sampling thread doesn't distort the
numbers.

## The report

`restrack profile` writes `restrack-profile-<time>.html` (or `-o file.html`) and prints a
summary. In Python use `prof.save_html()`, `prof.save_json()`, `prof.report()` (a dict),
or just display `prof` in Jupyter.

Report sections:

1. **Cards**: wall/CPU time, peak memory, disk vs cache reads, writes, page faults, CPU
   caches.
2. **Timeline**: memory, cores, machine CPU and RAM, disk and cache reads, with a bar per
   section; hover for values.
3. **Sections**: the tree with all per-section measurements and observations. Click ▸ to
   see allocation sites.
4. **Hot lines** and **Functions**.
5. **Caches, counters & gauges**.
6. **DataFrames** inspected with `prof.inspect(df)`.

## Command line reference

```text
restrack profile [options] SCRIPT [script args...]
```

| Option | Default | |
|---|---|---|
| `-o, --html FILE` | `restrack-profile-<time>.html` | report path |
| `--json FILE` | – | also save raw results |
| `--no-html` | | console summary only |
| `--open` | | open the report when done |
| `--memory` / `--where` | off | see [Memory](#memory-memory-and-where) |
| `--functions {sample,cprofile,off}` | `sample` | |
| `--hw {auto,on,off}` | `auto` | CPU cache counters; `on` explains when unavailable |
| `--track-module MODULE` | – | repeatable |
| `-i, --interval S` | `0.1` | timeline period |
| `--sample-interval S` | `0.005` | stack sampling period |
| `--top N` | `15` | sections in the console summary |

The exit code is 1 if the script raised an exception. The report is still written for
the part that ran.

## API reference

`Profiler(name=None, *, interval=0.1, functions="sample", sample_interval=0.005, memory=False, where=False, children=True, hw="auto")`

| Method | |
|---|---|
| `start()`, `stop()`, `with` | one run per instance; only one profiler at a time |
| `section(name, where=None)` | context manager or decorator |
| `track(func=None, name=None, where=None)` | decorator |
| `track_module(module, include=None, exclude=None, classes=True)` | wraps functions defined in the module; undone on `stop()` |
| `count(name, n=1)`, `gauge(name, value)` | counters and values over time |
| `watch_cache(func, name=None)` | anything with `cache_info()` |
| `inspect(df, name)` | DataFrame memory and dtype report |
| `report()`, `summary(top)`, `print_summary()`, `save_html(path)`, `save_json(path)`, `to_html()` | output |

Module level: `restrack.section`, `restrack.track`, `restrack.count`, `restrack.gauge`,
`restrack.current()`, and `restrack.profiler.run_script(path, argv, track_modules=None, **kwargs)`.

Low level: `restrack.hwcounters.PerfCounters(events)`, `.read()`, `PerfCounters.delta(a, b)`,
`derive(counts)`, `status()`.
