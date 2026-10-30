"""Metric collection built on psutil.

A :class:`Collector` turns psutil's cumulative counters into a JSON-friendly
snapshot dict with per-second rates. Call :meth:`Collector.sample` repeatedly;
the first call primes the counters, so rates are 0 until the second call.
"""
from __future__ import annotations

import math
import os
import platform
import socket
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import psutil

from . import gpu as _gpu

LINUX = psutil.LINUX
WINDOWS = psutil.WINDOWS

_EXPECTED = (psutil.Error, OSError, AttributeError, NotImplementedError, RuntimeError, ValueError)


def _call(fn: Callable, *args, default=None, **kwargs):
    """Call ``fn`` and swallow the errors psutil raises for unavailable data."""
    try:
        return fn(*args, **kwargs)
    except _EXPECTED:
        return default


class _Rates:
    """Turns monotonically increasing counters into per-second rates."""

    def __init__(self) -> None:
        self._prev: Dict[Any, tuple] = {}

    def __call__(self, key, value, now: float) -> float:
        prev = self._prev.get(key)
        self._prev[key] = (value, now)
        if prev is None or value is None:
            return 0.0
        v0, t0 = prev
        dt = now - t0
        if v0 is None or dt <= 0 or value < v0:  # counter reset / wrap
            return 0.0
        return (value - v0) / dt

    def prune(self, keep: Callable[[Any], bool]) -> None:
        self._prev = {k: v for k, v in self._prev.items() if keep(k)}


# --------------------------------------------------------------------------- #
# Static system information
# --------------------------------------------------------------------------- #
def _cpu_model() -> str:
    if LINUX:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def system_info() -> Dict[str, Any]:
    """Static facts about the machine (cheap; call once)."""
    freq = _call(psutil.cpu_freq)
    vm = psutil.virtual_memory()
    boot = _call(psutil.boot_time, default=0.0)
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "psutil": psutil.__version__,
        "cpu_model": _cpu_model(),
        "cpu_logical": psutil.cpu_count(logical=True) or 1,
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_freq_max_mhz": (freq.max or freq.current) if freq else None,
        "memory_total": vm.total,
        "swap_total": _call(lambda: psutil.swap_memory().total, default=0),
        "boot_time": boot,
        "pid": os.getpid(),
    }


# --------------------------------------------------------------------------- #
# CPU helpers (computed from cpu_times deltas so we never touch psutil's
# module-global cpu_percent() state, which other code may also be using)
# --------------------------------------------------------------------------- #
def _cpu_total(t) -> float:
    total = sum(t)
    if LINUX:  # guest time is already included in user/nice
        total -= getattr(t, "guest", 0.0) + getattr(t, "guest_nice", 0.0)
    return total


def _cpu_busy(t) -> float:
    return _cpu_total(t) - t.idle - getattr(t, "iowait", 0.0)


def _busy_pct(t0, t1) -> float:
    dtot = _cpu_total(t1) - _cpu_total(t0)
    if dtot <= 0:
        return 0.0
    return max(0.0, min(100.0, (_cpu_busy(t1) - _cpu_busy(t0)) / dtot * 100.0))


def _times_pct(t0, t1) -> Dict[str, float]:
    dtot = _cpu_total(t1) - _cpu_total(t0)
    if dtot <= 0:
        return {f: 0.0 for f in t1._fields}
    out = {}
    for f in t1._fields:
        if LINUX and f in ("guest", "guest_nice"):
            continue
        out[f] = round(max(0.0, (getattr(t1, f) - getattr(t0, f)) / dtot * 100.0), 2)
    return out


def _is_physical_disk(name: str) -> bool:
    if LINUX:
        # skip virtual/stacked devices so totals don't double-count I/O
        if name.startswith(("loop", "ram", "zram", "dm-", "md")):
            return False
        return os.path.exists("/sys/block/" + name.replace("/", "!"))
    return True


def _is_loopback(name: str, addrs) -> bool:
    if name == "lo" or name.lower().startswith("loopback"):
        return True
    for a in addrs or ():
        if a.family == socket.AF_INET and a.address.startswith("127."):
            return True
    return False


def _linux_major_faults(pid: int) -> Optional[int]:
    """Major ("hard") page faults of a process, read from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        return int(data[data.rindex(b")") + 2:].split()[9])
    except (OSError, ValueError, IndexError):
        return None


def _linux_system_major_faults() -> Optional[int]:
    try:
        with open("/proc/vmstat", "rb") as fh:
            for line in fh:
                if line.startswith(b"pgmajfault "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _linux_meminfo(keys=(b"Dirty", b"Writeback")) -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "rb") as fh:
            for line in fh:
                k, _, rest = line.partition(b":")
                if k in keys:
                    out[k.decode().lower()] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _r(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x, nd)


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
class Collector:
    """Samples machine-wide and per-process resource usage.

    Parameters
    ----------
    processes:
        Collect the per-process table (the most expensive part).
    top_n:
        If set, only keep the ``top_n`` processes by CPU in each snapshot.
    connections_every:
        Refresh the TCP/UDP connection table every N samples (0 disables).
    sensors_every:
        Refresh temperatures/fans/battery every N samples (0 disables).
    avg_window:
        Window (seconds) of the exponential "Average CPU" per process,
        like Resource Monitor's 60-second average.
    gpu:
        Try to collect NVIDIA GPU metrics (needs ``nvidia-ml-py``).
    """

    def __init__(
        self,
        *,
        processes: bool = True,
        top_n: Optional[int] = None,
        connections_every: int = 3,
        sensors_every: int = 5,
        avg_window: float = 60.0,
        gpu: bool = True,
    ) -> None:
        self.processes = processes
        self.top_n = top_n
        self.connections_every = connections_every
        self.sensors_every = sensors_every
        self.avg_window = avg_window
        self.ncpu = psutil.cpu_count() or 1
        self.info = system_info()

        self._rates = _Rates()
        self._procs: Dict[int, psutil.Process] = {}
        self._cpu_avg: Dict[int, float] = {}
        self._tick = 0
        self._last_mono: Optional[float] = None
        self._cpu_prev = psutil.cpu_times()
        self._percpu_prev = psutil.cpu_times(percpu=True)
        self._conn: Dict[str, Any] = {"available": None, "tcp": [], "listening": [], "by_pid": {}, "by_status": {}}
        self._sensors: Dict[str, Any] = {"temperatures": [], "fans": [], "battery": None}
        self._gpu = _gpu.NvidiaGPU() if gpu else None
        if self._gpu is not None and not self._gpu.available:
            self._gpu = None

    # ------------------------------------------------------------------ #
    def sample(self) -> Dict[str, Any]:
        """Take one snapshot. Rates are averaged since the previous call."""
        now = time.time()
        mono = time.monotonic()
        dt = (mono - self._last_mono) if self._last_mono else None
        self._last_mono = mono

        procs = self._processes(mono, dt) if self.processes else None
        if self.connections_every and self._tick % self.connections_every == 0:
            self._refresh_connections()
        if self.sensors_every and self._tick % self.sensors_every == 0:
            self._refresh_sensors()

        snap: Dict[str, Any] = {
            "ts": now,
            "seq": self._tick,
            "interval": _r(dt, 3),
            "uptime": _r(now - self.info["boot_time"], 0) if self.info["boot_time"] else None,
            "cpu": self._cpu(mono),
            "memory": self._memory(mono),
            "disk": self._disk(mono),
            "network": self._network(mono),
            "processes": procs,
            "sensors": self._sensors,
            "gpu": self._gpu.sample() if self._gpu else [],
        }
        # OS page cache: reads that processes asked for vs reads that actually hit the disk (Linux)
        d = snap["disk"]
        d["cache_read_bps"] = d["cache_hit_percent"] = None
        if procs and procs.get("read_logical_bps") is not None:
            logical, physical = procs["read_logical_bps"], procs["read_disk_bps"]
            d["cache_read_bps"] = max(0, logical - physical)
            d["cache_hit_percent"] = round(d["cache_read_bps"] / logical * 100, 1) if logical > 0 else None
        self._tick += 1
        return snap

    # ------------------------------------------------------------------ #
    def _cpu(self, mono: float) -> Dict[str, Any]:
        t1 = psutil.cpu_times()
        p1 = psutil.cpu_times(percpu=True)
        total = _busy_pct(self._cpu_prev, t1)
        per = [round(_busy_pct(a, b), 1) for a, b in zip(self._percpu_prev, p1)]
        breakdown = _times_pct(self._cpu_prev, t1)
        self._cpu_prev, self._percpu_prev = t1, p1

        freq = _call(psutil.cpu_freq)
        fmax = self.info["cpu_freq_max_mhz"]
        stats = _call(psutil.cpu_stats)
        load = _call(psutil.getloadavg)
        return {
            "percent": round(total, 1),
            "per_cpu": per,
            "times_percent": breakdown,
            "freq_mhz": _r(freq.current if freq else None, 0),
            "freq_max_mhz": _r(fmax, 0),
            "freq_percent": _r(min(100.0, freq.current / fmax * 100.0), 1) if (freq and fmax) else None,
            "load_avg": [round(x, 2) for x in load] if load else None,
            "ctx_switches_s": _r(self._rates("ctx", stats.ctx_switches, mono), 0) if stats else None,
            "interrupts_s": _r(self._rates("intr", stats.interrupts, mono), 0) if stats else None,
            "syscalls_s": _r(self._rates("sysc", stats.syscalls, mono), 0) if (stats and stats.syscalls) else None,
        }

    def _memory(self, mono: float) -> Dict[str, Any]:
        vm = psutil.virtual_memory()
        sw = _call(psutil.swap_memory)
        out = {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "free": vm.free,
            "percent": vm.percent,
            "cached": getattr(vm, "cached", None),
            "buffers": getattr(vm, "buffers", None),
            "shared": getattr(vm, "shared", None),
            "active": getattr(vm, "active", None),
            "inactive": getattr(vm, "inactive", None),
            "wired": getattr(vm, "wired", None),
            "swap_total": sw.total if sw else 0,
            "swap_used": sw.used if sw else 0,
            "swap_percent": sw.percent if sw else 0.0,
            "swap_in_bps": _r(self._rates("sin", sw.sin, mono), 0) if sw else 0.0,
            "swap_out_bps": _r(self._rates("sout", sw.sout, mono), 0) if sw else 0.0,
            "hard_faults_s": None,
            "dirty": None,
            "writeback": None,
        }
        if LINUX:
            mf = _linux_system_major_faults()
            if mf is not None:
                out["hard_faults_s"] = _r(self._rates("majflt", mf, mono), 0)
            out.update(_linux_meminfo())
        return out

    def _disk(self, mono: float) -> Dict[str, Any]:
        disks: List[Dict[str, Any]] = []
        counters = _call(psutil.disk_io_counters, perdisk=True) or {}
        for name, c in counters.items():
            if not _is_physical_disk(name):
                continue
            busy_ms = getattr(c, "busy_time", None)
            if busy_ms is None:
                busy_ms = (getattr(c, "read_time", 0) or 0) + (getattr(c, "write_time", 0) or 0)
            busy = min(100.0, self._rates(("dbusy", name), busy_ms, mono) / 10.0)  # ms/s -> %
            disks.append({
                "name": name,
                "read_bps": _r(self._rates(("drb", name), c.read_bytes, mono), 0),
                "write_bps": _r(self._rates(("dwb", name), c.write_bytes, mono), 0),
                "read_iops": _r(self._rates(("drc", name), c.read_count, mono), 1),
                "write_iops": _r(self._rates(("dwc", name), c.write_count, mono), 1),
                "busy_percent": round(busy, 1),
            })

        parts = []
        seen = set()
        for p in _call(psutil.disk_partitions, all=False, default=[]) or []:
            if p.mountpoint in seen or p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                continue
            if WINDOWS and "cdrom" in p.opts:
                continue
            u = _call(psutil.disk_usage, p.mountpoint)
            if u is None or u.total == 0:
                continue
            seen.add(p.mountpoint)
            parts.append({
                "mountpoint": p.mountpoint, "device": p.device, "fstype": p.fstype,
                "total": u.total, "used": u.used, "free": u.free, "percent": u.percent,
            })

        return {
            "read_bps": sum(d["read_bps"] for d in disks),
            "write_bps": sum(d["write_bps"] for d in disks),
            "read_iops": round(sum(d["read_iops"] for d in disks), 1),
            "write_iops": round(sum(d["write_iops"] for d in disks), 1),
            "busy_percent": max((d["busy_percent"] for d in disks), default=0.0),
            "disks": disks,
            "partitions": parts,
        }

    def _network(self, mono: float) -> Dict[str, Any]:
        counters = _call(psutil.net_io_counters, pernic=True) or {}
        stats = _call(psutil.net_if_stats) or {}
        addrs = _call(psutil.net_if_addrs) or {}
        nics = []
        for name, c in counters.items():
            st = stats.get(name)
            v4 = [a.address for a in addrs.get(name, ()) if a.family == socket.AF_INET]
            nics.append({
                "name": name,
                "is_up": bool(st.isup) if st else None,
                "speed_mbps": (st.speed or None) if st else None,
                "mtu": st.mtu if st else None,
                "ipv4": v4[0] if v4 else None,
                "loopback": _is_loopback(name, addrs.get(name)),
                "sent_bps": _r(self._rates(("ns", name), c.bytes_sent, mono), 0),
                "recv_bps": _r(self._rates(("nr", name), c.bytes_recv, mono), 0),
                "packets_sent_s": _r(self._rates(("nps", name), c.packets_sent, mono), 1),
                "packets_recv_s": _r(self._rates(("npr", name), c.packets_recv, mono), 1),
                "errors": c.errin + c.errout,
                "drops": c.dropin + c.dropout,
            })
        ext = [n for n in nics if not n["loopback"]]
        sent = sum(n["sent_bps"] for n in ext)
        recv = sum(n["recv_bps"] for n in ext)
        link = sum(n["speed_mbps"] or 0 for n in ext if n["is_up"])
        util = ((sent + recv) * 8 / (link * 1e6) * 100.0) if link else None
        conn = self._conn
        return {
            "sent_bps": sent,
            "recv_bps": recv,
            "utilization_percent": _r(min(100.0, util), 2) if util is not None else None,
            "interfaces": nics,
            "connections_available": conn["available"],
            "connections_by_status": conn["by_status"],
            "tcp": conn["tcp"],
            "listening": conn["listening"],
        }

    def _refresh_connections(self) -> None:
        try:
            conns = psutil.net_connections(kind="inet")
        except psutil.AccessDenied:
            self._conn = {"available": False, "tcp": [], "listening": [], "by_pid": {}, "by_status": {}}
            return
        except _EXPECTED:
            return
        tcp, listening, by_pid, by_status = [], [], {}, {}
        names = {pid: _call(p.name, default="?") for pid, p in self._procs.items()} if self._procs else {}

        def pname(pid):
            if pid is None:
                return "System"
            if pid not in names:
                names[pid] = _call(lambda: psutil.Process(pid).name(), default="?")
            return names[pid]

        for c in conns:
            proto = "TCP" if c.type == socket.SOCK_STREAM else "UDP"
            fam = "IPv6" if c.family == socket.AF_INET6 else "IPv4"
            entry_pid = by_pid.setdefault(c.pid, {"tcp": 0, "listening": 0}) if c.pid is not None else None
            is_listen = c.status == psutil.CONN_LISTEN or (proto == "UDP" and not c.raddr)
            if is_listen:
                listening.append({
                    "pid": c.pid, "name": pname(c.pid), "address": c.laddr.ip if c.laddr else "",
                    "port": c.laddr.port if c.laddr else None, "protocol": proto, "family": fam,
                })
                if entry_pid is not None:
                    entry_pid["listening"] += 1
            elif proto == "TCP":
                by_status[c.status] = by_status.get(c.status, 0) + 1
                if len(tcp) < 1000:
                    tcp.append({
                        "pid": c.pid, "name": pname(c.pid),
                        "laddr": c.laddr.ip if c.laddr else "", "lport": c.laddr.port if c.laddr else None,
                        "raddr": c.raddr.ip if c.raddr else "", "rport": c.raddr.port if c.raddr else None,
                        "status": c.status, "family": fam,
                    })
                if entry_pid is not None:
                    entry_pid["tcp"] += 1
        self._conn = {"available": True, "tcp": tcp, "listening": listening,
                      "by_pid": by_pid, "by_status": by_status}

    def _refresh_sensors(self) -> None:
        temps, fans = [], []
        if hasattr(psutil, "sensors_temperatures"):
            for chip, entries in (_call(psutil.sensors_temperatures) or {}).items():
                for e in entries:
                    temps.append({"chip": chip, "label": e.label or chip, "current": e.current,
                                  "high": e.high, "critical": e.critical})
        if hasattr(psutil, "sensors_fans"):
            for chip, entries in (_call(psutil.sensors_fans) or {}).items():
                for e in entries:
                    fans.append({"chip": chip, "label": e.label or chip, "rpm": e.current})
        bat = _call(psutil.sensors_battery) if hasattr(psutil, "sensors_battery") else None
        battery = None
        if bat is not None:
            secs = bat.secsleft if isinstance(bat.secsleft, (int, float)) and bat.secsleft >= 0 else None
            battery = {"percent": round(bat.percent, 1), "plugged": bat.power_plugged, "secs_left": secs}
        self._sensors = {"temperatures": temps, "fans": fans, "battery": battery}

    # ------------------------------------------------------------------ #
    def _processes(self, mono: float, dt: Optional[float]) -> Dict[str, Any]:
        alpha = 1.0 - math.exp(-dt / self.avg_window) if dt else 1.0
        pids = _call(psutil.pids, default=[]) or []
        rows: List[Dict[str, Any]] = []
        alive: Dict[int, psutil.Process] = {}
        status_counts: Dict[str, int] = {}
        threads_total = 0
        handles_total = 0
        logical_total = physical_total = 0.0
        conn_by_pid = self._conn["by_pid"]

        for pid in pids:
            p = self._procs.get(pid)
            if p is None:
                p = _call(psutil.Process, pid)
                if p is None:
                    continue
            try:
                with p.oneshot():
                    try:
                        name = p.name()
                    except psutil.AccessDenied:
                        name = "?"
                    cpu_raw = _call(p.cpu_percent, None, default=0.0) or 0.0
                    mem = _call(p.memory_info)
                    status = _call(p.status, default="?")
                    threads = _call(p.num_threads, default=0) or 0
                    user = _call(p.username)
                    io = _call(p.io_counters)
                    handles = _call(p.num_handles if WINDOWS else p.num_fds)
                    ppid = _call(p.ppid)
                    ctime = _call(p.create_time)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            alive[pid] = p
            cpu = cpu_raw / self.ncpu
            prev_avg = self._cpu_avg.get(pid)
            avg = cpu if prev_avg is None else prev_avg + alpha * (cpu - prev_avg)
            self._cpu_avg[pid] = avg
            rb = self._rates(("prb", pid), io.read_bytes if io else None, mono)
            rchars = getattr(io, "read_chars", None) if io else None
            rl = self._rates(("prl", pid), rchars, mono) if rchars is not None else None
            wb = self._rates(("pwb", pid), io.write_bytes if io else None, mono)
            rss = mem.rss if mem else 0
            shared = getattr(mem, "shared", None) if mem else None
            private = getattr(mem, "private", None) if mem else None
            if private is None and mem is not None:
                private = rss - (shared or 0)
            # Windows: all page faults; Linux: major (hard) faults like Resource Monitor
            faults = _linux_major_faults(pid) if LINUX else (getattr(mem, "num_page_faults", None) if mem else None)
            cinfo = conn_by_pid.get(pid)

            status_counts[status] = status_counts.get(status, 0) + 1
            threads_total += threads
            handles_total += handles or 0
            if rl is not None:
                logical_total += rl
                physical_total += rb
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "name": name,
                "user": user,
                "status": status,
                "created": ctime,
                "cpu": round(cpu, 2),
                "cpu_avg": round(avg, 2),
                "threads": threads,
                "handles": handles,
                "rss": rss,
                "vms": mem.vms if mem else 0,
                "shared": shared,
                "private": private,
                "mem_percent": round(rss / self.info["memory_total"] * 100.0, 2) if mem else 0.0,
                "faults_s": _r(self._rates(("pf", pid), faults, mono), 0) if faults is not None else None,
                "read_bps": round(rb),
                "write_bps": round(wb),
                "io_available": io is not None,
                "read_cache_bps": round(max(0.0, rl - rb)) if rl is not None else None,
                "tcp": cinfo["tcp"] if cinfo else 0,
                "listening": cinfo["listening"] if cinfo else 0,
            })

        # forget processes that have exited
        self._procs = alive
        self._cpu_avg = {k: v for k, v in self._cpu_avg.items() if k in alive}
        self._rates.prune(lambda k: not (isinstance(k, tuple) and k[0] in ("prb", "pwb", "pf", "prl")
                                         and k[1] not in alive))

        rows.sort(key=lambda r: r["cpu"], reverse=True)
        if self.top_n:
            rows = rows[: self.top_n]
        return {
            "count": len(alive),
            "threads": threads_total,
            "handles": handles_total,
            "by_status": status_counts,
            "read_logical_bps": round(logical_total) if LINUX else None,
            "read_disk_bps": round(physical_total) if LINUX else None,
            "list": rows,
        }


# --------------------------------------------------------------------------- #
def process_detail(pid: int) -> Dict[str, Any]:
    """Detailed, on-demand information about one process (raises psutil.NoSuchProcess)."""
    p = psutil.Process(pid)
    with p.oneshot():
        parent = _call(p.parent)
        mem_full = _call(p.memory_full_info)
        ctx = _call(p.num_ctx_switches)
        files = _call(p.open_files, default=[]) or []
        conns = _call(p.net_connections, kind="inet", default=[]) or []
        return {
            "pid": pid,
            "name": _call(p.name, default="?"),
            "exe": _call(p.exe),
            "cmdline": _call(p.cmdline, default=[]),
            "cwd": _call(p.cwd),
            "user": _call(p.username),
            "status": _call(p.status),
            "created": _call(p.create_time),
            "ppid": _call(p.ppid),
            "parent_name": _call(parent.name) if parent else None,
            "children": len(_call(p.children, default=[]) or []),
            "nice": _call(p.nice),
            "threads": _call(p.num_threads),
            "cpu_affinity": _call(p.cpu_affinity) if hasattr(p, "cpu_affinity") else None,
            "uss": getattr(mem_full, "uss", None) if mem_full else None,
            "ctx_switches": {"voluntary": ctx.voluntary, "involuntary": ctx.involuntary} if ctx else None,
            "open_files": [{"path": f.path, "fd": getattr(f, "fd", None), "mode": getattr(f, "mode", None)}
                           for f in files[:500]],
            "connections": [{
                "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                "status": c.status, "type": "TCP" if c.type == socket.SOCK_STREAM else "UDP",
            } for c in conns[:500]],
        }
