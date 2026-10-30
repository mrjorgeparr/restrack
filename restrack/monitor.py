"""Background sampling with history, callbacks and blocking waits."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from .collector import Collector

log = logging.getLogger(__name__)

Snapshot = Dict[str, Any]


def lighten(snap: Snapshot) -> Snapshot:
    """Copy of a snapshot without the bulky lists (process/connection tables)."""
    light = dict(snap)
    if snap.get("processes"):
        light["processes"] = {k: v for k, v in snap["processes"].items() if k != "list"}
    net = dict(snap["network"])
    net.pop("tcp", None)
    net.pop("listening", None)
    light["network"] = net
    return light


def get_path(snap: Snapshot, path: str):
    """Resolve a dotted path such as ``"cpu.percent"`` or ``"cpu.per_cpu.0"``."""
    cur: Any = snap
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


class Monitor:
    """Samples a :class:`Collector` on a background thread.

    >>> with Monitor(interval=1.0) as mon:
    ...     time.sleep(3)
    ...     print(mon.latest["cpu"]["percent"], mon.series("memory.percent"))

    The newest snapshot is kept in full; older ones are kept "light"
    (no process / connection tables) to bound memory use.
    """

    def __init__(self, interval: float = 1.0, history: int = 300,
                 collector: Optional[Collector] = None, **collector_kwargs) -> None:
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.interval = interval
        self.collector = collector or Collector(**collector_kwargs)
        self._history: Deque[Snapshot] = deque(maxlen=history)
        self._latest: Optional[Snapshot] = None
        self._seq = 0
        self._cond = threading.Condition()
        self._callbacks: List[Callable[[Snapshot], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # lifecycle ---------------------------------------------------------- #
    def start(self) -> "Monitor":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self.collector.sample()  # prime counters so the first published sample has rates
        self._thread = threading.Thread(target=self._run, name="restrack-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def __enter__(self) -> "Monitor":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        next_t = time.monotonic() + self.interval
        while not self._stop.wait(max(0.0, next_t - time.monotonic())):
            try:
                snap = self.collector.sample()
            except Exception:  # noqa: BLE001 - keep the monitor alive
                log.exception("restrack: sampling failed")
                next_t += self.interval
                continue
            with self._cond:
                self._latest = snap
                self._history.append(lighten(snap))
                self._seq += 1
                self._cond.notify_all()
            for cb in list(self._callbacks):
                try:
                    cb(snap)
                except Exception:  # noqa: BLE001
                    log.exception("restrack: callback %r failed", cb)
            next_t += self.interval
            if next_t < time.monotonic():  # we fell behind; don't burst
                next_t = time.monotonic() + self.interval

    # data access -------------------------------------------------------- #
    @property
    def latest(self) -> Optional[Snapshot]:
        return self._latest

    @property
    def seq(self) -> int:
        return self._seq

    def history(self, n: Optional[int] = None) -> List[Snapshot]:
        with self._cond:
            items = list(self._history)
        return items[-n:] if n else items

    def series(self, path: str, n: Optional[int] = None) -> List[Tuple[float, Any]]:
        """``[(timestamp, value), ...]`` for a dotted metric path, e.g. ``"network.recv_bps"``."""
        out = []
        for s in self.history(n):
            try:
                out.append((s["ts"], get_path(s, path)))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return out

    def wait(self, after_seq: int, timeout: Optional[float] = None) -> Optional[Tuple[int, Snapshot]]:
        """Block until a snapshot newer than ``after_seq`` exists; ``None`` on timeout/stop."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._seq > after_seq or self._stop.is_set(), timeout)
            if not ok or self._latest is None or self._seq <= after_seq:
                return None
            return self._seq, self._latest

    def stream(self, timeout: Optional[float] = None) -> Iterator[Snapshot]:
        """Yield each new snapshot as it arrives (starts the monitor if needed)."""
        self.start()
        seq = self._seq
        while not self._stop.is_set():
            item = self.wait(seq, timeout)
            if item is None:
                if timeout is not None:
                    return
                continue
            seq, snap = item
            yield snap

    def subscribe(self, callback: Callable[[Snapshot], None]) -> Callable[[], None]:
        """Call ``callback(snapshot)`` on every sample. Returns an unsubscribe function."""
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe
