"""Profile a Python (pandas) program: where does time and memory go?

Mark sections of your code, or let restrack wrap whole modules, and get per-section
wall/CPU time, peak memory, disk vs OS-cache reads and page faults, plus the
functions and source lines responsible, with the machine's state alongside.

    from restrack import Profiler

    with Profiler("etl") as prof:
        with prof.section("load"):
            df = pd.read_csv("big.csv")

        @prof.track
        def clean(df): ...

        df = clean(df)
    prof.save_html("etl-report.html")

Annotations can stay in your code permanently: :func:`section`, :func:`track`,
:func:`count` and :func:`gauge` at module level use the active profiler and are
no-ops when nothing is profiling.
"""
from __future__ import annotations

import bisect
import cProfile
import functools
import inspect
import json
import linecache
import os
import platform
import pstats
import socket
import sys
import sysconfig
import threading
import time
import tracemalloc
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

import psutil

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - Windows
    resource = None

LINUX = psutil.LINUX
WINDOWS = psutil.WINDOWS
_ACTIVE: Optional["Profiler"] = None
_MAX_SPANS = 5000
_HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _lib_dirs() -> List[str]:
    dirs = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib", "scripts"):
        p = sysconfig.get_paths().get(key)
        if p:
            dirs.add(os.path.normcase(os.path.abspath(p)))
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            dirs.add(os.path.normcase(os.path.abspath(p)))
    for base in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        dirs.add(os.path.normcase(os.path.join(os.path.abspath(base), "lib")))
        dirs.add(os.path.normcase(os.path.join(os.path.abspath(base), "Lib")))
    return sorted(dirs)


_LIB_DIRS = _lib_dirs()
_USER_CACHE: Dict[str, bool] = {}


def is_user_file(path: str) -> bool:
    """True for your own code: not the stdlib, not installed packages, not restrack."""
    hit = _USER_CACHE.get(path)
    if hit is not None:
        return hit
    if not path or path.startswith("<frozen") or path in ("~", "<string>") or path.startswith("<built-in"):
        res = False
    elif path.startswith("<"):  # notebook cells such as <ipython-input-3-...>
        res = True
    else:
        p = os.path.normcase(os.path.abspath(path))
        res = not p.startswith(os.path.normcase(_HERE)) and not any(p.startswith(d + os.sep) or p == d
                                                                    for d in _LIB_DIRS)
    _USER_CACHE[path] = res
    return res


def _short(path: str) -> str:
    try:
        rel = os.path.relpath(path)
        return rel if not rel.startswith("..") else path
    except ValueError:  # different drive on Windows
        return path


def _rows(obj) -> Optional[int]:
    """Row count for pandas-like objects (DataFrame/Series), else None."""
    if obj is not None and hasattr(obj, "shape") and hasattr(obj, "memory_usage"):
        try:
            return int(obj.shape[0])
        except Exception:  # noqa: BLE001
            return None
    return None


class _Counters:
    """Process-level cumulative counters read at section boundaries."""

    __slots__ = ("cpu", "read_disk", "read_all", "write_disk", "minflt", "majflt", "rss")

    def __init__(self, proc: psutil.Process):
        try:
            ct = proc.cpu_times()
            self.cpu = ct.user + ct.system + getattr(ct, "children_user", 0.0) + getattr(ct, "children_system", 0.0)
        except psutil.Error:
            self.cpu = 0.0
        try:
            io = proc.io_counters()
            self.read_disk, self.write_disk = io.read_bytes, io.write_bytes
            self.read_all = getattr(io, "read_chars", None)  # Linux: includes reads served by the page cache
        except (psutil.Error, AttributeError, NotImplementedError):
            self.read_disk = self.write_disk = self.read_all = None
        try:
            mem = proc.memory_info()
            self.rss = mem.rss
        except psutil.Error:
            mem, self.rss = None, 0
        if resource is not None:
            ru = resource.getrusage(resource.RUSAGE_SELF)
            self.minflt, self.majflt = ru.ru_minflt, ru.ru_majflt
        else:  # Windows: one combined page-fault counter
            self.minflt, self.majflt = getattr(mem, "num_page_faults", None), None


def _diff(a, b):
    return None if a is None or b is None else max(0, b - a)


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
class _Frame:
    __slots__ = ("name", "path", "depth", "thread", "t0", "c0", "traced0", "peak", "child_wall",
                 "where", "snap0", "snap_peak", "peak_seen", "rows_in", "rows_out", "rss_hwm", "hw0")

    def __init__(self, name, path, depth, thread):
        self.name, self.path, self.depth, self.thread = name, path, depth, thread
        self.child_wall = 0.0
        self.peak = 0
        self.rss_hwm = 0
        self.rows_in = self.rows_out = None
        self.where = False
        self.snap0 = self.snap_peak = None
        self.peak_seen = 0
        self.hw0 = None


class SectionHandle:
    """Returned by ``with prof.section(...) as s``; set ``s.rows_in`` / ``s.rows_out`` for throughput."""

    def __init__(self, frame: Optional[_Frame]):
        self._f = frame

    @property
    def rows_in(self):
        return self._f.rows_in if self._f else None

    @rows_in.setter
    def rows_in(self, v):
        if self._f:
            self._f.rows_in = v

    @property
    def rows_out(self):
        return self._f.rows_out if self._f else None

    @rows_out.setter
    def rows_out(self, v):
        if self._f:
            self._f.rows_out = v


class _Section:
    def __init__(self, prof: "Profiler", name: str, where: Optional[bool]):
        self.prof, self.name, self.where = prof, name, where
        self.frame: Optional[_Frame] = None

    def __enter__(self) -> SectionHandle:
        self.frame = self.prof._enter(self.name, self.where)
        return SectionHandle(self.frame)

    def __exit__(self, *exc):
        self.prof._exit(self.frame)
        return False

    def __call__(self, func):  # usable as a decorator too
        @functools.wraps(func)
        def wrapper(*a, **kw):
            with _Section(self.prof, self.name, self.where):
                return func(*a, **kw)
        return wrapper


class _NullSection:
    def __enter__(self):
        return SectionHandle(None)

    def __exit__(self, *exc):
        return False

    def __call__(self, func):
        return func


# --------------------------------------------------------------------------- #
class Profiler:
    """Measure a run of your program, section by section.

    The defaults are cheap (a few percent overhead): wall/CPU time, I/O, page faults and
    peak memory per section, a timeline of the process and the machine, and a
    statistical sampler that finds the hot lines of your code. The precise tools are
    opt-in because they can slow allocation-heavy pandas code several times over.

    Parameters
    ----------
    name:            label for the run (default: the script name).
    interval:        timeline sampling period in seconds (process + machine).
    functions:       ``"sample"`` (default): sample the call stack every
                     ``sample_interval`` seconds to find hot lines and the pandas calls
                     they make, with low overhead. ``"cprofile"``: exact call counts and
                     times via :mod:`cProfile` (slows Python-heavy code 2-3x).
                     ``None``/``False``: off.
    sample_interval: stack sampling period in seconds.
    memory:          trace Python/NumPy/pandas allocations with :mod:`tracemalloc` for
                     exact per-section *allocation* peaks (can slow code ~5x).
                     Without it, per-section peak *RSS* is still measured: exactly on
                     Linux (kernel high-water mark), from the timeline elsewhere.
    where:           find the source lines holding memory at each section's peak
                     (implies ``memory``; heaviest option; can be enabled per section).
    children:        include child processes (multiprocessing, joblib) in the timeline.
    hw:              CPU hardware counters per section: instructions per cycle, L1/LLC cache
                     miss rates, branch misses (Linux perf_event; see :mod:`restrack.hwcounters`).
                     ``"auto"`` (default) uses them when the machine exposes them; ``True``
                     also explains in the report why they're missing; ``False`` skips them.
                     They count the thread that starts the profiler and threads/processes it
                     starts afterwards.
    """

    def __init__(self, name: Optional[str] = None, *, interval: float = 0.1, functions="sample",
                 sample_interval: float = 0.005, memory: bool = False, where: bool = False,
                 children: bool = True, hw="auto") -> None:
        self.name = name or os.path.basename(sys.argv[0] or "python") or "run"
        self.interval = interval
        self.sample_interval = sample_interval
        self.memory = memory or where
        self.where = where
        if functions is True:
            functions = "sample"
        if functions not in (None, False, "sample", "cprofile"):
            raise ValueError("functions must be 'sample', 'cprofile' or None")
        self.functions = functions or None
        self.children = children
        self.hw = hw
        self._hw = None
        self._hw_run0 = None
        self.proc = psutil.Process()
        self._lock = threading.RLock()
        self._stacks: Dict[int, List[_Frame]] = {}
        self._hwm_ok = LINUX           # resettable kernel peak-RSS (VmHWM) available?
        self._run_hwm = 0
        self._line_time: Dict[tuple, float] = defaultdict(float)
        self._line_section: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._edge_time: Dict[tuple, float] = defaultdict(float)
        self._func_time: Dict[tuple, float] = defaultdict(float)
        self._stack_samples = 0
        self._stack_thread: Optional[threading.Thread] = None
        self._target_tid = threading.get_ident()
        self._open: List[_Frame] = []          # every open frame, all threads
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._spans: List[list] = []
        self._spans_dropped = 0
        self._samples: List[list] = []
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, List[list]] = defaultdict(list)
        self._caches: Dict[str, Any] = {}
        self._frames_df: List[Dict[str, Any]] = []
        self._notes: List[str] = []
        self._cprof: Optional[cProfile.Profile] = None
        self._started_tm = False
        self._stop_evt = threading.Event()
        self._sampler: Optional[threading.Thread] = None
        self._last_snap_t = 0.0
        self.t0 = self.t1 = None
        self.result: Optional[Dict[str, Any]] = None
        self._wrapped: List[tuple] = []
        self._where_calls: Dict[str, int] = {}
        self._snap_cost = 0.0
        self._run_peak = 0

    # lifecycle ------------------------------------------------------------ #
    def start(self) -> "Profiler":
        global _ACTIVE
        if self.t0 is not None:
            raise RuntimeError("a Profiler can only be started once")
        if _ACTIVE is not None:
            raise RuntimeError("another Profiler is already running")
        _ACTIVE = self
        try:
            return self._start()
        except BaseException:
            _ACTIVE = None
            if self._started_tm and tracemalloc.is_tracing():
                tracemalloc.stop()
            raise

    def _start(self) -> "Profiler":
        if self.memory and not tracemalloc.is_tracing():
            tracemalloc.start(10 if self.where else 1)
            self._started_tm = True
        elif self.memory and self.where and tracemalloc.get_traceback_limit() < 10:
            self._notes.append("tracemalloc was already running with a short traceback; "
                               "allocation sites may point into libraries")
        self.wall0 = time.time()
        self._target_tid = threading.get_ident()
        if self.hw:
            from .hwcounters import PerfCounters, status
            pc = PerfCounters()
            if pc.available:
                self._hw = pc
                self._hw_run0 = pc.read()
            else:
                pc.close()
                if self.hw is True:
                    self._notes.append("CPU cache counters unavailable: " + str(status().get("reason")))
        self._hwm_reset()
        self.t0 = time.perf_counter()
        self.c0 = _Counters(self.proc)
        self.sys0 = self._sys_counters()
        self._sys_cpu_prev = psutil.cpu_times()
        self._proc_cpu_prev = None
        self._io_prev = None
        # Since Python 3.12 cProfile observes *every* thread (sys.monitoring), so a background
        # sampler would corrupt its call stacks: in that combination the timeline is skipped.
        if not (self.functions == "cprofile" and sys.version_info >= (3, 12)):
            self._sampler = threading.Thread(target=self._sample_loop, name="restrack-profiler", daemon=True)
            self._sampler.start()
        else:
            self._notes.append("timeline not recorded: with functions='cprofile' on Python 3.12+ cProfile sees all "
                               "threads, so restrack's sampling thread would distort its numbers")
        if self.functions == "cprofile":
            self._cprof = cProfile.Profile()
            try:
                self._cprof.enable()
            except ValueError:  # another profiler (debugger, coverage) is active
                self._cprof = None
                self._notes.append("cProfile skipped: another profiler/tracer is active")
        elif self.functions == "sample":
            self._stack_thread = threading.Thread(target=self._stack_loop, name="restrack-stack-sampler",
                                                  daemon=True)
            self._stack_thread.start()
        return self

    def stop(self) -> Dict[str, Any]:
        global _ACTIVE
        if self.result is not None:
            return self.result
        if self._cprof is not None:
            self._cprof.disable()
        self.t1 = time.perf_counter()
        hw_run1 = self._hw.read() if self._hw is not None else None
        c1 = _Counters(self.proc)
        sys1 = self._sys_counters()
        while self._open:  # close sections left open (exceptions, early exit)
            self._exit(self._open[-1])
        self._stop_evt.set()
        if self._sampler:
            self._sampler.join(2)
        if self._stack_thread:
            self._stack_thread.join(2)
        with self._lock:
            self._bump_peak()
        peak_traced = (max(self._run_peak, tracemalloc.get_traced_memory()[1])
                       if (self.memory and tracemalloc.is_tracing()) else None)
        for func_holder, attr, original in reversed(self._wrapped):
            setattr(func_holder, attr, original)
        self._wrapped.clear()
        self.result = self._build(c1, sys1, peak_traced, hw_run1)
        if self._hw is not None:
            self._hw.close()
        if self._started_tm:
            tracemalloc.stop()
        _ACTIVE = None
        return self.result

    def __enter__(self) -> "Profiler":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # annotations ---------------------------------------------------------- #
    def section(self, name: str, *, where: Optional[bool] = None):
        """Context manager (or decorator) timing a named block. Sections nest."""
        return _Section(self, name, where)

    def track(self, func: Optional[Callable] = None, *, name: Optional[str] = None, where: Optional[bool] = None):
        """Decorator: profile every call of ``func`` as a section named after it.

        If the function returns a DataFrame/Series, its row count is recorded; if the
        first argument is one, that becomes ``rows_in`` (for rows/second).
        """
        def deco(f):
            label = name or getattr(f, "__qualname__", getattr(f, "__name__", "function"))

            @functools.wraps(f)
            def wrapper(*a, **kw):
                frame = self._enter(label, where)
                try:
                    for arg in a[:2]:  # first DataFrame among the first two args (skips `self`)
                        r = _rows(arg)
                        if r is not None:
                            frame.rows_in = r
                            break
                    out = f(*a, **kw)
                    r = _rows(out)
                    if r is not None:
                        frame.rows_out = r
                    return out
                finally:
                    self._exit(frame)
            wrapper.__restrack_wrapped__ = f
            return wrapper
        return deco(func) if func is not None else deco

    def track_module(self, module, include: Optional[List[str]] = None, exclude: Optional[List[str]] = None,
                     classes: bool = True) -> List[str]:
        """Wrap the functions (and class methods) *defined in* ``module`` as sections.

        Wrapping replaces the module attributes, so code that did ``from module import f``
        *before* this call keeps the unwrapped function: call it early, or use
        ``module.f(...)`` style calls. Everything is unwrapped again on :meth:`stop`.
        Returns the names that were wrapped.
        """
        if isinstance(module, str):
            import importlib
            module = importlib.import_module(module)
        modname = module.__name__
        done = []

        def wanted(n):
            return (include is None or n in include) and not (exclude and n in exclude) and not n.startswith("__")

        for attr, obj in list(vars(module).items()):
            if inspect.isfunction(obj) and obj.__module__ == modname and wanted(attr):
                self._wrapped.append((module, attr, obj))
                setattr(module, attr, self.track(obj, name=f"{modname.split('.')[-1]}.{obj.__name__}"))
                done.append(attr)
            elif classes and inspect.isclass(obj) and obj.__module__ == modname and wanted(attr):
                for mname, meth in list(vars(obj).items()):
                    if inspect.isfunction(meth) and not mname.startswith("__"):
                        self._wrapped.append((obj, mname, meth))
                        setattr(obj, mname, self.track(meth, name=f"{obj.__name__}.{mname}"))
                        done.append(f"{attr}.{mname}")
        return done

    def count(self, name: str, n: float = 1) -> None:
        """Increment a counter, e.g. ``prof.count("cache.hit")``."""
        with self._lock:
            self._counters[name] += n

    def gauge(self, name: str, value: float) -> None:
        """Record a value over time, e.g. ``prof.gauge("model.precision", 0.93)``."""
        with self._lock:
            self._gauges[name].append([round(time.perf_counter() - self.t0, 4), value])

    def watch_cache(self, obj, name: Optional[str] = None) -> None:
        """Report hits/misses of an ``functools.lru_cache``/``cache`` function (or anything with
        ``cache_info()``) at the end of the run."""
        self._caches[name or getattr(obj, "__qualname__", repr(obj))] = obj

    def inspect(self, df, name: str = "DataFrame") -> Dict[str, Any]:
        """Add a column-by-column memory report of a DataFrame to the run report."""
        from .dataframes import memory_report
        rep = memory_report(df, as_frame=False)
        entry = {"name": name, "t": round(time.perf_counter() - self.t0, 3) if self.t0 else 0.0, **rep}
        with self._lock:
            self._frames_df.append(entry)
        return rep

    # internals ------------------------------------------------------------ #
    def _stack(self) -> List[_Frame]:
        tid = threading.get_ident()
        st = self._stacks.get(tid)
        if st is None:
            st = self._stacks[tid] = []
        return st

    # kernel peak RSS (Linux): VmHWM can be reset by writing "5" to /proc/self/clear_refs
    def _hwm_read(self) -> Optional[int]:
        try:
            with open("/proc/self/status", "rb") as fh:
                for line in fh:
                    if line.startswith(b"VmHWM:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            pass
        return None

    def _hwm_reset(self) -> None:
        if not self._hwm_ok:
            return
        try:
            with open("/proc/self/clear_refs", "w") as fh:
                fh.write("5")
        except OSError:
            self._hwm_ok = False
            self._notes.append("exact peak RSS unavailable (cannot reset /proc/self/clear_refs); "
                               "section peaks come from the timeline")

    def _stack_loop(self) -> None:
        """Statistical profiler: attribute elapsed time to the innermost line of *your* code."""
        tid = self._target_tid
        last = time.perf_counter()
        while not self._stop_evt.wait(self.sample_interval):
            now = time.perf_counter()
            w, last = now - last, now
            frame = sys._current_frames().get(tid)
            if frame is None:
                continue
            try:
                self._attribute(frame, w)
            except Exception:  # noqa: BLE001
                pass
            del frame

    def _attribute(self, frame, w: float) -> None:
        lib = None            # the library function your line called (outermost library frame)
        line_key = None
        seen_funcs = set()
        f = frame
        while f is not None:
            code = f.f_code
            fn = code.co_filename
            if line_key is None and fn.startswith(_HERE):
                return  # inside restrack itself (section bookkeeping, snapshots): profiler overhead
            if fn != __file__ and is_user_file(fn):
                if line_key is None:
                    line_key = (fn, f.f_lineno, code.co_name)
                fkey = (fn, code.co_firstlineno, code.co_name)
                if fkey not in seen_funcs:
                    seen_funcs.add(fkey)
                    self._func_time[fkey] += w
            elif line_key is None and not fn.startswith(_HERE):
                lib = (fn, code.co_name)
            f = f.f_back
        if line_key is None:
            return
        self._stack_samples += 1
        self._line_time[line_key] += w
        st = self._stacks.get(self._target_tid)
        sec = st[-1].path if st else "(no section)"
        self._line_section[line_key][sec] += w
        if lib is not None:
            self._edge_time[(line_key, lib)] += w

    def _bump_peak(self) -> None:
        """Propagate the peaks seen since the last boundary to every open section, then reset them."""
        if self._hwm_ok:
            hwm = self._hwm_read()
            if hwm is not None:
                if hwm > self._run_hwm:
                    self._run_hwm = hwm
                for f in self._open:
                    if hwm > f.rss_hwm:
                        f.rss_hwm = hwm
                self._hwm_reset()
        if not self.memory or not tracemalloc.is_tracing():
            return
        _, peak = tracemalloc.get_traced_memory()
        if peak > self._run_peak:
            self._run_peak = peak
        for f in self._open:
            if peak > f.peak:
                f.peak = peak
        tracemalloc.reset_peak()

    def _enter(self, name: str, where: Optional[bool]) -> _Frame:
        stack = self._stack()
        parent = stack[-1] if stack else None
        path = f"{parent.path}/{name}" if parent else name
        th = threading.current_thread().name
        if th != "MainThread":
            path = f"[{th}] {path}" if not parent else path
        f = _Frame(name, path, len(stack), th)
        f.where = self.where if where is None else where
        if f.where and not tracemalloc.is_tracing():
            f.where = False
        if f.where:  # snapshots are expensive: only for the first calls of each section
            n = self._where_calls.get(path, 0)
            self._where_calls[path] = n + 1
            if n >= 3:
                f.where = False
        with self._lock:
            self._bump_peak()
            if f.where:
                f.snap0 = tracemalloc.take_snapshot()
            f.traced0 = tracemalloc.get_traced_memory()[0] if (self.memory and tracemalloc.is_tracing()) else None
            f.peak_seen = f.traced0 or 0
            f.c0 = _Counters(self.proc)
            if self._hw is not None and threading.get_ident() == self._target_tid:
                f.hw0 = self._hw.read()
            f.t0 = time.perf_counter()
            self._open.append(f)
        stack.append(f)
        return f

    def _exit(self, f: Optional[_Frame]) -> None:
        if f is None:
            return
        t1 = time.perf_counter()
        stack = self._stack()
        if f in stack:
            while stack[-1] is not f:  # unbalanced (exception inside nested) - close children first
                self._exit(stack[-1])
            stack.pop()
        hw1 = self._hw.read() if (f.hw0 is not None and self._hw is not None) else None
        with self._lock:
            c1 = _Counters(self.proc)
            self._bump_peak()
            if f in self._open:
                self._open.remove(f)
            wall = t1 - f.t0
            parent = next((o for o in reversed(self._open) if o.thread == f.thread and o.depth == f.depth - 1), None)
            if parent is not None:
                parent.child_wall += wall
            st = self._stats.get(f.path)
            if st is None:
                st = self._stats[f.path] = {
                    "path": f.path, "name": f.name, "depth": f.depth, "calls": 0, "wall": 0.0, "self": 0.0,
                    "cpu": 0.0, "first": f.t0 - self.t0, "peak_alloc": None, "rss_delta": 0, "rss_peak": None,
                    "read_disk": None, "read_cache": None, "write_disk": None, "minflt": None, "majflt": None,
                    "rows_in": None, "rows_out": None, "max_wall": 0.0, "sites": {}, "sites_kind": None,
                    "peak_growth": None, "hw": None,
                }
                self._order.append(f.path)
            st["calls"] += 1
            st["wall"] += wall
            st["max_wall"] = max(st["max_wall"], wall)
            st["self"] += max(0.0, wall - f.child_wall)
            st["cpu"] += max(0.0, c1.cpu - f.c0.cpu)
            st["rss_delta"] += c1.rss - f.c0.rss
            if f.rss_hwm:
                st["rss_peak"] = max(st["rss_peak"] or 0, f.rss_hwm)
                st["peak_growth"] = max(st["peak_growth"] or 0, f.rss_hwm - f.c0.rss)
            if f.traced0 is not None:
                pa = max(0, f.peak - f.traced0)
                st["peak_alloc"] = max(st["peak_alloc"] or 0, pa)
            for key, a, b in (("read_disk", f.c0.read_disk, c1.read_disk), ("write_disk", f.c0.write_disk, c1.write_disk),
                              ("minflt", f.c0.minflt, c1.minflt), ("majflt", f.c0.majflt, c1.majflt)):
                d = _diff(a, b)
                if d is not None:
                    st[key] = (st[key] or 0) + d
            if f.c0.read_all is not None and c1.read_all is not None and c1.read_disk is not None:
                total = max(0, c1.read_all - f.c0.read_all)
                disk = _diff(f.c0.read_disk, c1.read_disk) or 0
                st["read_cache"] = (st["read_cache"] or 0) + max(0, total - disk)
            if hw1 is not None:
                from .hwcounters import PerfCounters
                d = PerfCounters.delta(f.hw0, hw1)
                acc = st["hw"] or {}
                for k, v in d.items():
                    acc[k] = acc.get(k, 0.0) + v
                st["hw"] = acc
            for key in ("rows_in", "rows_out"):
                v = getattr(f, key)
                if v is not None:
                    st[key] = (st[key] or 0) + v
            if f.where and f.snap0 is not None and wall < 0.1 and f.snap_peak is None:
                f.snap0 = None  # too short to matter; skip the expensive diff
            if f.where and f.snap0 is not None:
                snap = f.snap_peak
                kind = "peak"
                if snap is None:
                    snap, kind = tracemalloc.take_snapshot(), "end"
                sites = _sites_diff(f.snap0, snap)
                # keep the call with the largest footprint
                if sum(s["size"] for s in sites) > sum(s["size"] for s in st["sites"].get("list", [])):
                    st["sites"] = {"list": sites}
                    st["sites_kind"] = kind
                f.snap0 = f.snap_peak = None
            if len(self._spans) < _MAX_SPANS:
                self._spans.append([f.path, f.depth, round(f.t0 - self.t0, 4), round(t1 - self.t0, 4), f.thread])
            else:
                self._spans_dropped += 1

    def _sys_counters(self) -> Dict[str, Any]:
        try:
            sw = psutil.swap_memory()
            return {"sin": sw.sin, "sout": sw.sout}
        except (psutil.Error, OSError, RuntimeError, AttributeError, NotImplementedError):
            return {"sin": None, "sout": None}

    def _sample_loop(self) -> None:
        nxt = time.perf_counter()
        while not self._stop_evt.is_set():
            try:
                self._sample()
            except Exception:  # noqa: BLE001 - never break the user's program
                pass
            nxt += self.interval
            self._stop_evt.wait(max(0.0, nxt - time.perf_counter()))

    def _sample(self) -> None:
        now = time.perf_counter()
        t = now - self.t0
        procs = [self.proc]
        if self.children:
            try:
                procs += self.proc.children(recursive=True)
            except psutil.Error:
                pass
        rss = 0
        cpu_s = 0.0
        for p in procs:
            try:
                with p.oneshot():
                    rss += p.memory_info().rss
                    ct = p.cpu_times()
                    cpu_s += ct.user + ct.system
            except psutil.Error:
                continue
        now_cpu = time.perf_counter()  # after reading: the read itself may wait for the GIL
        prev = self._proc_cpu_prev
        self._proc_cpu_prev = (now_cpu, cpu_s)
        ncpu = psutil.cpu_count() or 1
        cores = 0.0 if prev is None or now_cpu - prev[0] < 0.02 else \
            min(float(ncpu), max(0.0, (cpu_s - prev[1]) / (now_cpu - prev[0])))
        sct = psutil.cpu_times()
        a, b = self._sys_cpu_prev, sct
        tot = sum(b) - sum(a)
        idle = (b.idle - a.idle) + (getattr(b, "iowait", 0) - getattr(a, "iowait", 0))
        sys_cpu = 0.0 if tot <= 0 else max(0.0, min(100.0, (tot - idle) / tot * 100))
        self._sys_cpu_prev = sct
        vm = psutil.virtual_memory()
        rdisk = rcache = None
        try:
            io = self.proc.io_counters()
            now_io = time.perf_counter()
            cur = (now_io, io.read_bytes, getattr(io, "read_chars", None))
            if self._io_prev is not None and now_io - self._io_prev[0] >= 0.02:
                dt = now_io - self._io_prev[0]
                rdisk = max(0, io.read_bytes - self._io_prev[1]) / dt
                if cur[2] is not None and self._io_prev[2] is not None:
                    rcache = max(0.0, (cur[2] - self._io_prev[2]) / dt - rdisk)
            self._io_prev = cur
        except (psutil.Error, AttributeError, NotImplementedError):
            pass
        traced = None
        if self.memory and tracemalloc.is_tracing():
            traced = tracemalloc.get_traced_memory()[0]
            # snapshot allocation sites near the peak of sections asking "where"
            with self._lock:
                # only when memory grew a lot, and never more often than ~5x the cost of a snapshot
                targets = [f for f in self._open if f.where and traced > f.peak_seen * 1.25 + 8_000_000]
                if targets and now - self._last_snap_t > max(1.0, 5 * self._snap_cost):
                    t_snap = time.perf_counter()
                    snap = tracemalloc.take_snapshot()
                    self._last_snap_t = time.perf_counter()
                    self._snap_cost = self._last_snap_t - t_snap
                    for f in targets:
                        f.snap_peak, f.peak_seen = snap, traced
        self._samples.append([round(t, 3), rss, round(cores, 3), round(sys_cpu, 1), round(vm.percent, 1),
                              None if rdisk is None else round(rdisk), None if rcache is None else round(rcache),
                              traced, len(procs) - 1])

    # results -------------------------------------------------------------- #
    def _build(self, c1: _Counters, sys1, peak_traced, hw_run1=None) -> Dict[str, Any]:
        from .hwcounters import PerfCounters, derive
        wall = self.t1 - self.t0
        c0 = self.c0
        total_read = _diff(c0.read_all, c1.read_all)
        read_disk = _diff(c0.read_disk, c1.read_disk)
        # parents start before their children: ordering by first start gives tree order
        sections = sorted((self._stats[p] for p in self._order), key=lambda x: (x["first"], x["depth"]))
        times = [smp[0] for smp in self._samples]
        spans_by_path: Dict[str, list] = defaultdict(list)
        for sp in self._spans:
            spans_by_path[sp[0]].append(sp)
        for s in sections:
            for k in ("wall", "self", "cpu", "max_wall"):
                s[k] = round(s[k], 6)
            s["pct"] = round(s["wall"] / wall * 100, 2) if wall else 0.0
            s["cores"] = round(s["cpu"] / s["wall"], 2) if s["wall"] else None
            rows = s["rows_in"] or s["rows_out"]
            s["rows_per_s"] = round(rows / s["wall"]) if rows and s["wall"] else None
            s["sites"] = s["sites"].get("list", []) if isinstance(s["sites"], dict) else s["sites"]
            # rss peak from the timeline samples that fall inside this section's spans
            peak = None
            for sp in spans_by_path.get(s["path"], ()):
                lo, hi = bisect.bisect_left(times, sp[2]), bisect.bisect_right(times, sp[3])
                if hi > lo:
                    m = max(self._samples[i][1] for i in range(lo, hi))
                    peak = m if peak is None else max(peak, m)
            if s["rss_peak"] is None:
                s["rss_peak"] = peak
                s["rss_peak_exact"] = False
                if peak is not None and s["peak_growth"] is None:
                    s["peak_growth"] = None  # timeline peaks are too coarse to compare with the start
            else:
                s["rss_peak_exact"] = True
            if s.get("hw"):
                s["hw"] = {k: int(v) for k, v in s["hw"].items()}
                s["hw_ratios"] = derive(s["hw"])
            else:
                s["hw_ratios"] = None
            s["hints"] = _hints(s, self)

        caches = {}
        for name, obj in self._caches.items():
            try:
                ci = obj.cache_info()
                hits, misses = ci.hits, ci.misses
                caches[name] = {"hits": hits, "misses": misses, "size": getattr(ci, "currsize", None),
                                "maxsize": getattr(ci, "maxsize", None),
                                "hit_rate": round(hits / (hits + misses) * 100, 2) if hits + misses else None}
            except Exception as e:  # noqa: BLE001
                caches[name] = {"error": str(e)}
        # counters that look like hit/miss pairs get a hit rate
        rates = {}
        for k, v in self._counters.items():
            for hit_word, miss_word in ((".hit", ".miss"), (".hits", ".misses"), ("_hit", "_miss"), ("_hits", "_misses")):
                base = None
                if k.endswith(hit_word):
                    base, hit, miss = k[: -len(hit_word)], v, self._counters.get(k[: -len(hit_word)] + miss_word, 0)
                elif k.endswith(miss_word) and (k[: -len(miss_word)] + hit_word) not in self._counters:
                    base, hit, miss = k[: -len(miss_word)], 0, v   # only misses recorded
                if base is not None and hit + miss:
                    rates[base] = {"hits": hit, "misses": miss, "hit_rate": round(hit / (hit + miss) * 100, 2)}

        samples = self._samples
        if len(samples) > 6000:  # downsample, keeping the max RSS of each bucket
            step = len(samples) / 6000
            out, i = [], 0.0
            while int(i) < len(samples):
                chunk = samples[int(i): int(i + step) or int(i) + 1]
                out.append(max(chunk, key=lambda r: r[1]))
                i += step
            samples = out

        try:
            import pandas as _pd
            pandas_v = _pd.__version__
        except Exception:  # noqa: BLE001
            pandas_v = None
        rss_peak_run = max([s[1] for s in self._samples] + [self._run_hwm, c1.rss])
        res = {
            "name": self.name,
            "started": self.wall0,
            "wall": round(wall, 4),
            "cpu": round(c1.cpu - c0.cpu, 4),
            "cores": round((c1.cpu - c0.cpu) / wall, 2) if wall else None,
            "rss_start": c0.rss, "rss_end": c1.rss, "rss_peak": rss_peak_run,
            "peak_alloc": peak_traced,
            "read_disk": read_disk,
            "read_cache": (max(0, total_read - (read_disk or 0)) if total_read is not None else None),
            "write_disk": _diff(c0.write_disk, c1.write_disk),
            "minflt": _diff(c0.minflt, c1.minflt), "majflt": _diff(c0.majflt, c1.majflt),
            "hw": ({k: int(v) for k, v in PerfCounters.delta(self._hw_run0, hw_run1).items()}
                   if hw_run1 is not None and self._hw_run0 is not None else None),
            "swap_in": _diff(self.sys0["sin"], sys1["sin"]), "swap_out": _diff(self.sys0["sout"], sys1["sout"]),
            "sections": sections,
            "spans": self._spans, "spans_dropped": self._spans_dropped,
            "samples": samples,
            "sample_fields": ["t", "rss", "cores", "sys_cpu", "sys_mem", "read_disk_bps", "read_cache_bps",
                              "traced", "children"],
            "functions": self._functions(),
            "lines": self._hot_lines(wall),
            "stack_samples": self._stack_samples,
            "counters": dict(self._counters), "hit_rates": rates,
            "gauges": dict(self._gauges), "caches": caches,
            "dataframes": self._frames_df,
            "notes": self._notes,
            "env": {
                "host": socket.gethostname(), "os": platform.platform(), "python": sys.version.split()[0],
                "pandas": pandas_v, "cpu_logical": psutil.cpu_count(), "cpu_physical": psutil.cpu_count(False),
                "memory_total": psutil.virtual_memory().total, "pid": self.proc.pid,
                "argv": sys.argv, "memory_tracing": self.memory, "where": self.where, "functions": self.functions,
                "rss_peak_exact": self._hwm_ok, "sample_interval": self.sample_interval,
                "io_split": c0.read_all is not None,
                "faults_split": resource is not None,
                "hw_counters": self._hw is not None,
            },
        }
        res["hw_ratios"] = derive(res["hw"]) if res["hw"] else None
        return res

    def _functions(self) -> Optional[Dict[str, Any]]:
        if self.functions == "sample":
            return self._sampled_functions()
        if self._cprof is None:
            return None
        st = pstats.Stats(self._cprof)
        raw = st.stats  # {(file, line, func): (cc, nc, tt, ct, callers)}

        def label(key):
            file, line, func = key
            if file == "~":
                return func
            return f"{func} ({_short(file)}:{line})"

        def lib_label(key):
            file, line, func = key
            if file == "~":
                return func.strip("<>")
            mod = file.replace("\\", "/")
            for marker in ("site-packages/", "dist-packages/", "/lib/python"):
                if marker in mod:
                    mod = mod.split(marker, 1)[1]
                    break
            mod = mod.rsplit(".py", 1)[0].replace("/", ".").lstrip(".")
            return f"{mod}.{func}" if func not in mod else mod

        user, lib = [], []
        for key, (cc, nc, tt, ct, callers) in raw.items():
            entry = {"label": label(key), "calls": nc, "self": round(tt, 6), "cum": round(ct, 6)}
            (user if is_user_file(key[0]) else lib).append((key, entry, callers))
        top_user_cum = [e for _, e, _ in sorted(user, key=lambda x: -x[1]["cum"])][:30]
        top_user_self = [e for _, e, _ in sorted(user, key=lambda x: -x[1]["self"])][:30]
        # time spent inside library calls, attributed to the calling function in your code
        edges = []
        for key, e, callers in lib:
            for ckey, cstat in callers.items():
                if is_user_file(ckey[0]):
                    ct = cstat[3]
                    if ct > 0:
                        edges.append({"caller": label(ckey), "callee": lib_label(key), "calls": cstat[1],
                                      "cum": round(ct, 6)})
        edges.sort(key=lambda x: -x["cum"])
        top_lib_self = [dict(e, label=lib_label(k)) for k, e, _ in sorted(lib, key=lambda x: -x[1]["self"])][:25]
        return {"mode": "cprofile", "user_cum": top_user_cum, "user_self": top_user_self, "edges": edges[:40],
                "lib_self": top_lib_self}

    def _sampled_functions(self) -> Dict[str, Any]:
        funcs = [{"label": f"{fn_name} ({_short(fn)}:{line})", "calls": None, "self": None, "cum": round(t, 4)}
                 for (fn, line, fn_name), t in self._func_time.items()]
        funcs.sort(key=lambda x: -x["cum"])
        edges = defaultdict(float)
        for ((fn, line, func), lib), t in self._edge_time.items():
            edges[(f"{func} ({_short(fn)}:{line})", _lib_label(lib[0], lib[1]))] += t
        edge_list = [{"caller": c, "callee": l, "calls": None, "cum": round(t, 4)} for (c, l), t in edges.items()]
        edge_list.sort(key=lambda x: -x["cum"])
        return {"mode": "sample", "user_cum": funcs[:30], "user_self": [], "edges": edge_list[:40], "lib_self": []}

    def _hot_lines(self, wall: float, top: int = 40) -> List[Dict[str, Any]]:
        if not self._line_time:
            return []
        lib_by_line: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for (lk, lib), t in self._edge_time.items():
            lib_by_line[lk][_lib_label(lib[0], lib[1])] += t
        out = []
        for key, t in sorted(self._line_time.items(), key=lambda kv: -kv[1])[:top]:
            fn, line, func = key
            libs = sorted(lib_by_line.get(key, {}).items(), key=lambda kv: -kv[1])
            secs = sorted(self._line_section[key].items(), key=lambda kv: -kv[1])
            out.append({
                "file": _short(fn), "line": line, "func": func, "time": round(t, 4),
                "pct": round(t / wall * 100, 2) if wall else 0.0,
                "code": linecache.getline(fn, line).strip()[:200],
                "calls_into": [{"callee": n, "time": round(v, 4)} for n, v in libs[:3]],
                "section": secs[0][0] if secs else None,
            })
        return out

    # output --------------------------------------------------------------- #
    def report(self) -> Dict[str, Any]:
        """The run's results as a JSON-serialisable dict (stops the profiler if needed)."""
        return self.result if self.result is not None else self.stop()

    def summary(self, top: int = 15) -> str:
        """A plain-text summary for the console."""
        return format_summary(self.report(), top)

    def print_summary(self, top: int = 15) -> None:
        print(self.summary(top))

    def save_json(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.report(), fh, default=str)
        return path

    def to_html(self) -> str:
        from .report import render_html
        return render_html(self.report())

    def save_html(self, path: Optional[str] = None) -> str:
        path = path or f"restrack-profile-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.wall0))}.html"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_html())
        return path

    def _repr_html_(self) -> str:  # Jupyter
        if self.result is None:
            return "<i>restrack Profiler running…</i>"
        from .report import render_html
        html = render_html(self.result).replace('"', "&quot;")
        return f'<iframe srcdoc="{html}" style="width:100%;height:900px;border:1px solid #ccc"></iframe>'


# --------------------------------------------------------------------------- #
def _group_traces(snap) -> Dict[tuple, list]:
    """{traceback (most recent frame first): [size, count]} for a tracemalloc snapshot.

    Uses the snapshot's raw trace tuples when available: about 10x faster than
    ``Snapshot.compare_to`` on large heaps (pandas imports alone create ~500k traces).
    """
    out: Dict[tuple, list] = {}
    raw = getattr(getattr(snap, "traces", None), "_traces", None)
    if raw is not None:
        get = out.get
        for tr in raw:
            tb = tr[2]
            e = get(tb)
            if e is None:
                out[tb] = [tr[1], 1]
            else:
                e[0] += tr[1]
                e[1] += 1
        return out
    for st in snap.statistics("traceback"):  # public-API fallback
        tb = tuple((f.filename, f.lineno) for f in reversed(st.traceback))
        out[tb] = [st.size, st.count]
    return out


def _sites_diff(snap0, snap1, top: int = 12) -> List[Dict[str, Any]]:
    """Memory live in ``snap1`` but not in ``snap0``, attributed to the closest line of *your* code."""
    g0, g1 = _group_traces(snap0), _group_traces(snap1)
    sizes, counts, via = defaultdict(int), defaultdict(int), {}
    tm_file = tracemalloc.__file__
    for tb, (size, cnt) in g1.items():
        prev = g0.get(tb)
        d = size - (prev[0] if prev else 0)
        if d <= 0 or not tb:
            continue
        if tb[0][0].startswith(_HERE) or tb[0][0] == tm_file:
            continue  # restrack's own bookkeeping
        chosen = libframe = None
        for fn, ln in tb:  # most recent call first
            if is_user_file(fn):
                chosen = (fn, ln)
                break
            if libframe is None:
                libframe = (fn, ln)
        if chosen is None:
            chosen = ("<library>", 0)
        sizes[chosen] += d
        counts[chosen] += max(0, cnt - (prev[1] if prev else 0))
        if libframe is not None and chosen not in via:
            via[chosen] = libframe
    out = []
    for key, d in sizes.items():
        file, line = key
        src = linecache.getline(file, line).strip() if line else ""
        v = via.get(key)
        out.append({"file": _short(file), "line": line, "size": d, "count": counts[key], "code": src[:160],
                    "via": f"{_short(v[0]).split('site-packages')[-1].lstrip(os.sep)}:{v[1]}" if v else None})
    out.sort(key=lambda x: -x["size"])
    return out[:top]


def _lib_label(file: str, func: str) -> str:
    """'pandas.core.frame.apply' style label for a library frame."""
    mod = file.replace("\\", "/")
    for marker in ("site-packages/", "dist-packages/", "/lib/python"):
        if marker in mod:
            mod = mod.split(marker, 1)[1]
            if marker == "/lib/python":
                mod = mod.split("/", 1)[-1]
            break
    else:
        if mod.startswith("<"):
            return f"{mod.strip('<>')}.{func}"
    mod = mod.rsplit(".py", 1)[0].replace("/", ".").lstrip(".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return f"{mod}.{func}"


def _fmt_b(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _hints(s: Dict[str, Any], prof: Profiler) -> List[str]:
    """Plain-language observations for a section (heuristics, not verdicts)."""
    hints = []
    wall = s["wall"]
    if wall < 0.05:
        return hints
    cores = s["cores"] or 0
    ncpu = psutil.cpu_count() or 1
    if cores < 0.5:
        hints.append(f"Waiting more than computing (CPU only {cores:.2f} cores of wall time): I/O, network, "
                     "sleep, locks or child processes.")
    elif ncpu >= 4 and 0.8 <= cores <= 1.3 and wall > 1:
        hints.append("CPU-bound on a single core: vectorise Python-level loops/apply, or parallelise.")
    if s.get("majflt") and s["majflt"] > 1000:
        hints.append(f"{s['majflt']:,} hard page faults: data came from disk/swap rather than RAM.")
    rd, rc = s.get("read_disk") or 0, s.get("read_cache") or 0
    if rd > 50 * 2**20:
        share = rd / (rd + rc) * 100 if rd + rc else 100
        hints.append(f"Read {_fmt_b(rd)} from disk ({share:.0f}% of reads missed the OS cache): consider columnar "
                     "formats (Parquet), reading only needed columns (usecols/columns=), or caching.")
    elif rc > 50 * 2**20 and rd < rc * 0.05:
        hints.append(f"Read {_fmt_b(rc)} served by the OS file cache: fast now, but slower on a cold start.")
    hr = s.get("hw_ratios") or {}
    if hr.get("llc_miss_pct") is not None and hr["llc_miss_pct"] > 30 and (s.get("hw") or {}).get("llc_loads", 0) > 1e6:
        hints.append(f"{hr['llc_miss_pct']:.0f}% of last-level-cache loads missed (memory-bound): random access over "
                     "large data, e.g. joins/lookups on unsorted keys, object columns, or poor locality.")
    if hr.get("ipc") is not None and hr["ipc"] < 0.7 and wall > 0.5 and cores >= 0.5:
        hints.append(f"Low IPC ({hr['ipc']:.2f} instructions/cycle): the CPU is mostly stalled waiting on memory.")
    pa, ro = s.get("peak_alloc"), s.get("rows_out")
    pg = s.get("peak_growth")
    if not pa and pg and pg > 200 * 2**20 and s["rss_delta"] < pg * 0.3:
        hints.append(f"Memory rose {_fmt_b(pg)} above the start of the section but little of it was kept: "
                     "large temporaries. Rerun with where=True to see the lines responsible.")
    if pa and pa > 200 * 2**20 and s["rss_delta"] < pa * 0.3:
        hints.append(f"Peak allocation {_fmt_b(pa)} but little memory kept: large temporaries (copies, "
                     "chained operations, astype/concat in loops). Check the allocation sites.")
    if s.get("rows_per_s") and ro and s["rows_per_s"] < 50_000 and wall > 1 and not prof.memory:
        hints.append(f"Only {s['rows_per_s']:,} rows/s: look for row-wise apply/iterrows/Python loops.")
    return hints


def format_summary(res: Dict[str, Any], top: int = 15) -> str:
    lines = [f"restrack profile: {res['name']} — {res['wall']:.3f}s wall, {res['cpu']:.3f}s CPU "
             f"({res['cores'] or 0:.2f} cores), peak RSS {_fmt_b(res['rss_peak'])}"
             + (f", peak traced alloc {_fmt_b(res['peak_alloc'])}" if res.get('peak_alloc') else "")]
    if res["read_disk"] is not None:
        lines.append(f"I/O: read {_fmt_b(res['read_disk'])} from disk"
                     + (f", {_fmt_b(res['read_cache'])} from OS cache" if res['read_cache'] is not None else "")
                     + f", wrote {_fmt_b(res['write_disk'])}")
    if res.get("hw_ratios"):
        h = res["hw_ratios"]
        bits = [f"IPC {h['ipc']}" if h.get("ipc") is not None else None,
                f"L1d miss {h['l1d_miss_pct']}%" if h.get("l1d_miss_pct") is not None else None,
                f"LLC miss {h['llc_miss_pct'] if h.get('llc_miss_pct') is not None else h.get('cache_miss_pct')}%"
                if (h.get("llc_miss_pct") is not None or h.get("cache_miss_pct") is not None) else None,
                f"branch miss {h['branch_miss_pct']}%" if h.get("branch_miss_pct") is not None else None]
        lines.append("CPU caches: " + ", ".join(b for b in bits if b))
    if res.get("minflt") is not None:
        lines.append(f"Page faults: {res['minflt']:,} minor (served from RAM)"
                     + (f", {res['majflt']:,} major (needed disk)" if res.get("majflt") is not None else ""))
    if res["sections"]:
        lines.append("")
        mem_label = "peak alloc" if res["env"].get("memory_tracing") else "mem growth"
        lines.append(f"{'section':<40} {'calls':>6} {'wall s':>9} {'%':>6} {'self s':>9} {'cores':>6} "
                     f"{mem_label:>11} {'disk read':>10}")
        for s in sorted(res["sections"], key=lambda x: -x["wall"])[:top]:
            name = ("  " * s["depth"] + s["name"])[:40]
            lines.append(f"{name:<40} {s['calls']:>6} {s['wall']:>9.3f} {s['pct']:>6.1f} {s['self']:>9.3f} "
                         f"{(s['cores'] or 0):>6.2f} "
                         f"{_fmt_b(s['peak_alloc'] if res['env'].get('memory_tracing') else s.get('peak_growth')):>11} "
                         f"{_fmt_b(s['read_disk']):>10}")
            for h in s["hints"]:
                lines.append(f"{'':<4}! {h}")
    if res.get("lines"):
        lines.append("")
        lines.append("Hot lines in your code (sampled):")
        for ln in res["lines"][:10]:
            via = f"  -> {ln['calls_into'][0]['callee']}" if ln["calls_into"] else ""
            where = f"{ln['file']}:{ln['line']} ({ln['func']})"
            lines.append(f"  {ln['time']:>8.3f}s {ln['pct']:>5.1f}%  {where:<34} {ln['code'][:60]}{via}")
    fn = res.get("functions")
    if fn and fn.get("mode") == "cprofile" and fn["edges"]:
        lines.append("")
        lines.append("Most time inside library calls (your function -> library call):")
        for e in fn["edges"][:8]:
            lines.append(f"  {e['cum']:>8.3f}s  {e['caller']}  ->  {e['callee']}  ({e['calls']} calls)")
    for name, c in res.get("caches", {}).items():
        if "hits" in c:
            lines.append(f"cache {name}: {c['hits']} hits / {c['misses']} misses ({c['hit_rate']}% hit rate)")
    for name, r in res.get("hit_rates", {}).items():
        lines.append(f"counter {name}: {r['hits']:,.0f} hits / {r['misses']:,.0f} misses ({r['hit_rate']}% hit rate)")
    for note in res.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# module-level annotations (no-ops without an active profiler)
# --------------------------------------------------------------------------- #
def current() -> Optional[Profiler]:
    """The running Profiler, or None."""
    return _ACTIVE


def section(name: str, *, where: Optional[bool] = None):
    """``with restrack.section("load"):`` — profiled when a Profiler is running, free otherwise."""
    p = _ACTIVE
    return p.section(name, where=where) if p is not None else _NullSection()


def track(func: Optional[Callable] = None, *, name: Optional[str] = None, where: Optional[bool] = None):
    """``@restrack.track`` — profiles calls only while a Profiler is running."""
    def deco(f):
        label = name or getattr(f, "__qualname__", f.__name__)

        @functools.wraps(f)
        def wrapper(*a, **kw):
            p = _ACTIVE
            if p is None:
                return f(*a, **kw)
            return p.track(f, name=label, where=where)(*a, **kw)
        return wrapper
    return deco(func) if func is not None else deco


def count(name: str, n: float = 1) -> None:
    if _ACTIVE is not None:
        _ACTIVE.count(name, n)


def gauge(name: str, value: float) -> None:
    if _ACTIVE is not None:
        _ACTIVE.gauge(name, value)


def run_script(path: str, argv: List[str], track_modules: Optional[List[str]] = None,
               **profiler_kwargs) -> Profiler:
    """Run a Python script under a Profiler (what ``restrack profile`` does).

    ``track_modules``: names of your modules whose functions should each become a
    section (imported and wrapped before the script starts).
    """
    import runpy

    prof = Profiler(name=os.path.basename(path), **profiler_kwargs)
    old_argv, old_path = sys.argv, list(sys.path)
    sys.argv = [path, *argv]
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    prof.error = None
    prof.start()
    for mod in track_modules or []:
        try:
            wrapped = prof.track_module(mod)
            if not wrapped:
                prof._notes.append(f"--track-module {mod}: no functions found")
        except ImportError as e:
            prof._notes.append(f"--track-module {mod}: {e}")
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            prof._notes.append(f"script exited with code {e.code}")
    except Exception as e:  # noqa: BLE001 - still produce a report for the part that ran
        import traceback
        prof.error = e
        prof._notes.append("script raised " + "".join(traceback.format_exception_only(type(e), e)).strip())
    finally:
        prof.stop()
        sys.argv, sys.path[:] = old_argv, old_path
    return prof
