"""Long-term storage (SQLite), threshold alerts and file logging.

``Store`` persists a compact row per stored sample (plus the top processes) and answers downsampled time-range queries
for the History graphs. ``Alerts`` turns sustained threshold breaches into
log lines and ``events`` rows. ``Tracker`` wires both onto a ``Monitor`` from a
config dict (see :mod:`restrack.config`).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .monitor import Monitor

log = logging.getLogger("restrack")

# metric column -> how to read it from a snapshot
COLUMNS = {
    "cpu": lambda s: s["cpu"]["percent"],
    "cpu_freq": lambda s: s["cpu"].get("freq_percent"),
    "load1": lambda s: (s["cpu"].get("load_avg") or [None])[0],
    "mem": lambda s: s["memory"]["percent"],
    "mem_used": lambda s: s["memory"]["used"],
    "swap": lambda s: s["memory"]["swap_percent"],
    "hard_faults": lambda s: s["memory"].get("hard_faults_s"),
    "disk_read": lambda s: s["disk"]["read_bps"],
    "disk_write": lambda s: s["disk"]["write_bps"],
    "disk_busy": lambda s: s["disk"]["busy_percent"],
    "cache_read": lambda s: s["disk"].get("cache_read_bps"),
    "cache_hit": lambda s: s["disk"].get("cache_hit_percent"),
    "dirty": lambda s: s["memory"].get("dirty"),
    "net_sent": lambda s: s["network"]["sent_bps"],
    "net_recv": lambda s: s["network"]["recv_bps"],
    "procs": lambda s: (s.get("processes") or {}).get("count"),
    "threads": lambda s: (s.get("processes") or {}).get("threads"),
    "temp_max": lambda s: max((t["current"] for t in s["sensors"]["temperatures"] if t["current"]), default=None),
    "gpu": lambda s: max((g["util_percent"] for g in s.get("gpu") or [] if g["util_percent"] is not None),
                         default=None),
}

# metrics where a spike matters: we keep the bucket max alongside the average
_MAX_COLS = ("cpu", "mem", "disk_busy", "disk_read", "disk_write", "net_sent", "net_recv", "temp_max", "gpu")


class Store:
    """SQLite metrics log. Thread-safe; one connection guarded by a lock."""

    def __init__(self, path: str, retention_days: float = 30, top_processes: int = 10) -> None:
        self.path = path
        self.retention_days = retention_days
        self.top_processes = top_processes
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._last_prune = 0.0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            cols = ", ".join(f"{c} REAL" for c in COLUMNS)
            self._db.execute(f"CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, {cols})")
            have = {r[1] for r in self._db.execute("PRAGMA table_info(samples)")}
            for c in COLUMNS:  # forward-compatible: add metrics introduced by newer versions
                if c not in have:
                    self._db.execute(f"ALTER TABLE samples ADD COLUMN {c} REAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS proc_samples (
                ts REAL, pid INTEGER, name TEXT, user TEXT, cpu REAL, rss INTEGER,
                read_bps REAL, write_bps REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS proc_ts ON proc_samples(ts)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS events (
                ts REAL, level TEXT, kind TEXT, message TEXT, value REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")

    # writing ------------------------------------------------------------ #
    def write(self, snap: Dict[str, Any]) -> None:
        vals = []
        for fn in COLUMNS.values():
            try:
                vals.append(fn(snap))
            except (KeyError, TypeError, IndexError):
                vals.append(None)
        procs = []
        if self.top_processes and snap.get("processes"):
            for p in snap["processes"]["list"][: self.top_processes]:  # list is sorted by CPU
                procs.append((snap["ts"], p["pid"], p["name"], p["user"], p["cpu"], p["rss"],
                              p["read_bps"], p["write_bps"]))
        names = ", ".join(["ts", *COLUMNS])
        ph = ",".join("?" * (len(COLUMNS) + 1))
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(f"INSERT OR REPLACE INTO samples ({names}) VALUES ({ph})", [snap["ts"], *vals])
                if procs:
                    self._db.executemany("INSERT INTO proc_samples VALUES (?,?,?,?,?,?,?,?)", procs)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        if time.time() - self._last_prune > 3600:
            self.prune()

    def event(self, level: str, kind: str, message: str, value: Optional[float] = None,
              ts: Optional[float] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                             (ts or time.time(), level, kind, message, value))

    def prune(self) -> int:
        self._last_prune = time.time()
        if not self.retention_days:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM proc_samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return n

    # reading ------------------------------------------------------------ #
    def bounds(self) -> Dict[str, Any]:
        with self._lock:
            first, last, n = self._db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM samples").fetchone()
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"first": first, "last": last, "rows": n, "path": self.path, "bytes": size,
                "retention_days": self.retention_days}

    def query(self, start: float, end: float, points: int = 600) -> Dict[str, Any]:
        """Downsampled series between ``start`` and ``end`` (~``points`` buckets).

        Returns ``{"bucket": seconds, "series": [{"ts":..., "cpu":..., "cpu_max":..., ...}]}``.
        """
        span = max(1.0, end - start)
        bucket = max(1.0, span / max(1, points))
        avg = ", ".join(f"AVG({c}) AS {c}" for c in COLUMNS)
        mx = ", ".join(f"MAX({c}) AS {c}_max" for c in _MAX_COLS)
        sql = (f"SELECT CAST((ts - ?) / ? AS INTEGER) AS b, MIN(ts) AS ts, {avg}, {mx}, COUNT(*) AS n "
               f"FROM samples WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b")
        with self._lock:
            cur = self._db.execute(sql, (start, bucket, start, end))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        series = []
        for r in rows:
            d = dict(zip(cols, r))
            d.pop("b")
            series.append({k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()})
        return {"start": start, "end": end, "bucket": bucket, "series": series}

    def top_processes_at(self, ts: float, window: float = 60.0, n: int = 10) -> List[Dict[str, Any]]:
        """Processes using most CPU around ``ts`` (averaged over ``window`` seconds)."""
        with self._lock:
            cur = self._db.execute(
                """SELECT name, pid, MAX(user), AVG(cpu), MAX(cpu), MAX(rss), AVG(read_bps), AVG(write_bps),
                          COUNT(*) FROM proc_samples WHERE ts BETWEEN ? AND ?
                   GROUP BY pid, name ORDER BY AVG(cpu) DESC LIMIT ?""",
                (ts - window / 2, ts + window / 2, n))
            rows = cur.fetchall()
        keys = ("name", "pid", "user", "cpu", "cpu_max", "rss", "read_bps", "write_bps", "samples")
        return [dict(zip(keys, r)) for r in rows]

    def events(self, start: float, end: float, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, level, kind, message, value FROM events "
                                    "WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?",
                                    (start, end, limit)).fetchall()
        return [dict(zip(("ts", "level", "kind", "message", "value"), r)) for r in rows]

    def export_csv(self, start: float, end: float, fh) -> int:
        import csv

        cols = ["ts", *COLUMNS]
        w = csv.writer(fh)
        w.writerow(["time_iso", *cols])
        n = 0
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(cols)} FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                                    (start, end)).fetchall()
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r[0])), *r])
            n += 1
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()


# --------------------------------------------------------------------------- #
class Alerts:
    """Fires when a threshold is exceeded for ``sustain`` seconds; logs + stores an event."""

    def __init__(self, cfg: Dict[str, Any], store: Optional[Store] = None) -> None:
        self.cfg = cfg
        self.store = store
        self._since: Dict[str, float] = {}
        self._fired: Dict[str, float] = {}
        self._active: Dict[str, bool] = {}

    def _conditions(self, s: Dict[str, Any]):
        c = self.cfg
        if c.get("cpu_percent"):
            yield "cpu", s["cpu"]["percent"] >= c["cpu_percent"], s["cpu"]["percent"], \
                f"CPU at {s['cpu']['percent']:.0f}% (>= {c['cpu_percent']}%)"
        if c.get("memory_percent"):
            v = s["memory"]["percent"]
            yield "memory", v >= c["memory_percent"], v, f"Memory at {v:.0f}% (>= {c['memory_percent']}%)"
        if c.get("swap_percent") and s["memory"]["swap_total"]:
            v = s["memory"]["swap_percent"]
            yield "swap", v >= c["swap_percent"], v, f"Swap at {v:.0f}% (>= {c['swap_percent']}%)"
        if c.get("disk_busy_percent"):
            v = s["disk"]["busy_percent"]
            yield "disk_busy", v >= c["disk_busy_percent"], v, f"Disk active {v:.0f}% (>= {c['disk_busy_percent']}%)"
        if c.get("disk_free_percent"):
            for p in s["disk"]["partitions"]:
                free = 100.0 - p["percent"]
                yield f"disk_free:{p['mountpoint']}", free <= c["disk_free_percent"], free, \
                    f"{p['mountpoint']} only {free:.1f}% free (<= {c['disk_free_percent']}%)"
        if c.get("temperature_c"):
            for t in s["sensors"]["temperatures"]:
                if t["current"]:
                    yield f"temp:{t['chip']}:{t['label']}", t["current"] >= c["temperature_c"], t["current"], \
                        f"{t['label']} at {t['current']:.0f}°C (>= {c['temperature_c']}°C)"
        if c.get("process_cpu_percent") and s.get("processes"):
            for p in s["processes"]["list"][:20]:
                yield f"proc:{p['pid']}", p["cpu"] >= c["process_cpu_percent"], p["cpu"], \
                    f"{p['name']} (pid {p['pid']}) at {p['cpu']:.0f}% CPU (>= {c['process_cpu_percent']}%)"

    def check(self, snap: Dict[str, Any]) -> List[str]:
        if not self.cfg.get("enabled", True):
            return []
        now = snap["ts"]
        fired = []
        seen = set()
        for key, breached, value, msg in self._conditions(snap):
            seen.add(key)
            if not breached:
                self._since.pop(key, None)
                if self._active.pop(key, False):
                    log.info("resolved: %s back below threshold", key)
                    if self.store:
                        self.store.event("INFO", key, f"resolved: {key} back below threshold", value, now)
                continue
            start = self._since.setdefault(key, now)
            if now - start < self.cfg.get("sustain", 30):
                continue
            if key in self._fired and now - self._fired[key] < self.cfg.get("cooldown", 300):
                continue
            self._fired[key] = now
            self._active[key] = True
            log.warning("ALERT %s", msg)
            if self.store:
                self.store.event("WARNING", key, msg, value, now)
            fired.append(msg)
        for key in list(self._since):
            if key not in seen:  # e.g. a process that exited
                self._since.pop(key, None)
                self._active.pop(key, None)
        return fired


def setup_logging(cfg: Dict[str, Any]) -> None:
    root = logging.getLogger("restrack")
    root.setLevel(getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    path = cfg.get("file")
    if path and path != "-":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=int(cfg.get("max_bytes", 5_000_000)),
                                                  backupCount=int(cfg.get("backups", 3)), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.propagate = False


class Tracker:
    """Monitor + Store + Alerts + logging, all driven by one config dict.

    >>> from restrack.config import load_config
    >>> with Tracker(load_config()) as t:
    ...     t.monitor.latest
    """

    def __init__(self, cfg: Dict[str, Any], setup_log: bool = True) -> None:
        self.cfg = cfg
        if setup_log:
            setup_logging(cfg["logging"])
        m = cfg["monitor"]
        self.monitor = Monitor(interval=m["interval"], history=m["history"], processes=m["processes"],
                               connections_every=m["connections_every"], sensors_every=m["sensors_every"],
                               gpu=m["gpu"])
        st = cfg["storage"]
        self.store: Optional[Store] = None
        if st["enabled"]:
            self.store = Store(st["path"], retention_days=st["retention_days"], top_processes=st["top_processes"])
        self.alerts = Alerts(cfg["alerts"], self.store)
        self._every = max(1, int(st.get("every", 1)))
        self._n = 0
        self.monitor.subscribe(self._on_sample)

    def _on_sample(self, snap: Dict[str, Any]) -> None:
        self.alerts.check(snap)
        if self.store and self._n % self._every == 0:
            self.store.write(snap)
        self._n += 1

    def start(self) -> "Tracker":
        self.monitor.start()
        log.info("restrack started: interval=%ss, storage=%s, config=%s", self.monitor.interval,
                 self.store.path if self.store else "off", self.cfg.get("_source") or "defaults")
        if self.store:
            self.store.event("INFO", "start", "restrack started")
        return self

    def stop(self) -> None:
        self.monitor.stop()
        if self.store:
            self.store.event("INFO", "stop", "restrack stopped")
            self.store.close()
            self.store = None
        log.info("restrack stopped")

    def __enter__(self) -> "Tracker":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
