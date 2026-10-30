"""restrack — simple real-time machine resource tracking with psutil.

Quick use::

    import restrack

    snap = restrack.snapshot()                 # one-off reading
    print(snap["cpu"]["percent"], snap["memory"]["percent"])

    for s in restrack.watch(interval=1.0):     # live stream
        print(s["cpu"]["percent"])

    with restrack.Monitor(interval=0.5) as mon:  # background thread + history
        ...
        mon.series("network.recv_bps")

Long-term log + alerts driven by a config file::

    with restrack.Tracker(restrack.load_config("restrack.toml")) as t:
        ...                                     # writes SQLite rows, logs alerts

Profile your own (pandas) code — time, memory peaks, disk vs cache reads, hot lines::

    with restrack.Profiler("etl") as prof:
        with prof.section("load"):
            df = pd.read_csv("big.csv")
    prof.save_html("etl.html")         # or: restrack profile my_script.py

Web UI (Windows Resource Monitor look-alike, with a History tab)::

    restrack web --open        # or: python -m restrack web
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterator, Optional

from .collector import Collector, process_detail, system_info
from .monitor import Monitor, get_path, lighten
from .recorder import Recorder, flatten
from .config import load_config
from .storage import Alerts, Store, Tracker
from .profiler import Profiler, count, current, gauge, section, track

__version__ = "0.1.0"
__all__ = [
    "Collector", "Monitor", "Recorder", "snapshot", "watch", "system_info",
    "process_detail", "flatten", "lighten", "get_path", "load_config", "Store", "Tracker", "Alerts",
    "Profiler", "section", "track", "count", "gauge", "current", "__version__",
]


def snapshot(interval: float = 0.5, **collector_kwargs) -> Dict[str, Any]:
    """Return one snapshot, with rates measured over ``interval`` seconds."""
    c = Collector(**collector_kwargs)
    c.sample()
    time.sleep(interval)
    return c.sample()


def watch(interval: float = 1.0, count: Optional[int] = None, **collector_kwargs) -> Iterator[Dict[str, Any]]:
    """Yield a snapshot every ``interval`` seconds (forever, or ``count`` times)."""
    c = Collector(**collector_kwargs)
    c.sample()
    n = 0
    next_t = time.monotonic() + interval
    while count is None or n < count:
        time.sleep(max(0.0, next_t - time.monotonic()))
        next_t += interval
        yield c.sample()
        n += 1
