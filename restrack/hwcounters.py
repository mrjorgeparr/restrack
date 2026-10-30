"""CPU hardware counters (cache hits/misses, instructions, cycles) via Linux ``perf_event_open``.

psutil can't see inside the CPU, so this talks to the kernel directly through
``ctypes``; no extra packages and no ``perf`` binary are needed.

    from restrack.hwcounters import PerfCounters, derive

    pc = PerfCounters()                 # counts this thread + threads/children started later
    before = pc.read()
    work()
    print(derive(PerfCounters.delta(before, pc.read())))  # {'ipc': 2.1, 'llc_miss_pct': 12.0, 'l1d_miss_pct': 3.4, ...}

Availability (check :func:`status` or ``PerfCounters().available``):

* **Linux only.** Windows and macOS don't expose these counters to normal programs
  (use Intel VTune / AMD uProf / Apple Instruments there).
* The CPU's PMU must be visible: most bare-metal machines yes; many VMs and cloud
  instances **no** (the hypervisor hides it); containers often block the syscall.
* ``/proc/sys/kernel/perf_event_paranoid`` must be ≤ 2 for your own processes
  (the default on most distros); 3+ blocks everything unless you are root / have CAP_PERFMON.

Software events (task clock, context switches, page faults) work on any Linux kernel.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
from typing import Dict, List, Optional, Tuple

import psutil

# perf_event_attr.type
_HW, _SW, _HW_CACHE = 0, 1, 3
# PERF_TYPE_HW_CACHE config = cache_id | (op << 8) | (result << 16)
_L1D, _LL, _OP_READ, _ACCESS, _MISS = 0, 2, 0, 0, 1


def _cache(cache: int, result: int) -> int:
    return cache | (_OP_READ << 8) | (result << 16)


#: name -> (type, config)
EVENTS: Dict[str, Tuple[int, int]] = {
    "cycles": (_HW, 0),
    "instructions": (_HW, 1),
    "cache_references": (_HW, 2),     # usually last-level-cache accesses
    "cache_misses": (_HW, 3),         # usually last-level-cache misses
    "branches": (_HW, 4),
    "branch_misses": (_HW, 5),
    "l1d_loads": (_HW_CACHE, _cache(_L1D, _ACCESS)),
    "l1d_misses": (_HW_CACHE, _cache(_L1D, _MISS)),
    "llc_loads": (_HW_CACHE, _cache(_LL, _ACCESS)),
    "llc_misses": (_HW_CACHE, _cache(_LL, _MISS)),
    "task_clock_ns": (_SW, 1),
    "context_switches": (_SW, 3),
    "cpu_migrations": (_SW, 4),
    "minor_faults": (_SW, 5),
    "major_faults": (_SW, 6),
}
DEFAULT_EVENTS = ["cycles", "instructions", "cache_references", "cache_misses", "branches", "branch_misses",
                  "l1d_loads", "l1d_misses", "llc_loads", "llc_misses", "context_switches"]
HARDWARE = {k for k, (t, _) in EVENTS.items() if t in (_HW, _HW_CACHE)}

_SYSCALL = {"x86_64": 298, "amd64": 298, "aarch64": 241, "arm64": 241, "riscv64": 241,
            "i386": 336, "i686": 336, "armv7l": 364, "ppc64le": 319, "ppc64": 319, "s390x": 331}

# flag bits in the u64 after read_format
_DISABLED, _INHERIT, _EXCL_KERNEL, _EXCL_HV = 1 << 0, 1 << 1, 1 << 5, 1 << 6
_FMT_ENABLED, _FMT_RUNNING = 1, 2


def _paranoid() -> Optional[int]:
    try:
        with open("/proc/sys/kernel/perf_event_paranoid") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _open(type_: int, config: int) -> int:
    """Open one counter for the calling thread (inherit to new threads/children). Returns fd or -errno."""
    # perf_event_attr, PERF_ATTR_SIZE_VER0 layout (64 bytes) - accepted by every kernel since 2.6.31
    flags = _INHERIT | _EXCL_KERNEL | _EXCL_HV
    attr = struct.pack("=IIQQQQQIIQ", type_, 64, config, 0, 0, _FMT_ENABLED | _FMT_RUNNING, flags, 0, 0, 0)
    buf = ctypes.create_string_buffer(attr, len(attr))
    libc = ctypes.CDLL(None, use_errno=True)
    nr = _SYSCALL.get(platform.machine().lower())
    if nr is None:
        return -errno.ENOSYS
    fd = libc.syscall(nr, buf, 0, -1, -1, ctypes.c_ulong(0x8))  # pid=self, any cpu, no group, CLOEXEC
    return fd if fd >= 0 else -ctypes.get_errno()


class PerfCounters:
    """A set of perf counters for this process (calling thread + threads/children it starts later).

    Counters that can't be opened are skipped; see :attr:`errors`. Values are scaled
    for multiplexing (when the CPU has fewer counters than requested).
    """

    def __init__(self, events: Optional[List[str]] = None) -> None:
        self.fds: Dict[str, int] = {}
        self.errors: Dict[str, str] = {}
        if not psutil.LINUX:
            self.errors["*"] = "hardware counters need Linux perf_event"
            return
        for name in events or DEFAULT_EVENTS:
            t, c = EVENTS[name]
            fd = _open(t, c)
            if fd >= 0:
                self.fds[name] = fd
            else:
                self.errors[name] = os.strerror(-fd)

    @property
    def available(self) -> bool:
        """True if at least one *hardware* counter opened."""
        return any(n in HARDWARE for n in self.fds)

    def read(self) -> Dict[str, float]:
        out = {}
        for name, fd in self.fds.items():
            try:
                raw = os.read(fd, 24)
                val, enabled, running = struct.unpack("=QQQ", raw)
                out[name] = float(val) if running in (0, enabled) else val * enabled / running
            except (OSError, struct.error):
                continue
        return out

    @staticmethod
    def delta(before: Dict[str, float], after: Dict[str, float]) -> Dict[str, float]:
        return {k: max(0.0, after[k] - before[k]) for k in after if k in before}

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()

    def __enter__(self) -> "PerfCounters":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # pragma: no cover
        self.close()


def derive(c: Dict[str, float]) -> Dict[str, Optional[float]]:
    """Ratios from raw counts: IPC and miss percentages (None where inputs are missing)."""
    def pct(a, b):
        return round(c[a] / c[b] * 100, 2) if c.get(b) and a in c else None

    return {
        "ipc": round(c["instructions"] / c["cycles"], 3) if c.get("cycles") and "instructions" in c else None,
        "cache_miss_pct": pct("cache_misses", "cache_references"),
        "l1d_miss_pct": pct("l1d_misses", "l1d_loads"),
        "llc_miss_pct": pct("llc_misses", "llc_loads"),
        "branch_miss_pct": pct("branch_misses", "branches"),
    }


def status() -> Dict[str, object]:
    """Why hardware counters are (un)available here, in plain words."""
    info: Dict[str, object] = {"os": platform.system(), "paranoid": _paranoid() if psutil.LINUX else None}
    if not psutil.LINUX:
        info.update(available=False, reason="CPU cache counters are only readable on Linux (perf_event). "
                    "On Windows use Intel VTune or AMD uProf; on macOS, Instruments.")
        return info
    pc = PerfCounters(["cycles", "instructions", "l1d_loads", "task_clock_ns"])
    try:
        ok = pc.available
        info.update(available=ok, opened=sorted(pc.fds), errors=pc.errors)
        if ok:
            info["reason"] = "hardware counters available"
        else:
            errs = set(pc.errors.values())
            par = info["paranoid"]
            if par is not None and par > 2:
                why = f"kernel.perf_event_paranoid is {par}; set it to 2 (sudo sysctl kernel.perf_event_paranoid=2)"
            elif any("Operation not permitted" in e or "Permission denied" in e for e in errs):
                why = "the perf_event_open syscall is blocked (container seccomp profile or missing CAP_PERFMON)"
            elif any("No such file" in e or "not supported" in e.lower() or "No such device" in e for e in errs):
                why = ("the CPU's performance counters aren't exposed to this OS; common in VMs and cloud "
                       "instances (enable vPMU in the hypervisor) — software events still work")
            else:
                why = "; ".join(sorted(errs)) or "unknown"
            info["reason"] = why
        return info
    finally:
        pc.close()
