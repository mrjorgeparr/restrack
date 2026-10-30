"""Persist snapshots to CSV (flat scalar metrics) or JSON Lines (everything)."""
from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, Iterable, Optional

from .monitor import lighten

# lists we never expand into CSV columns (variable length / per-row tables)
_SKIP = {"processes.list", "network.tcp", "network.listening", "disk.partitions",
         "sensors.temperatures", "sensors.fans", "gpu"}


def flatten(snap: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten a snapshot into ``{"cpu.percent": 12.3, "cpu.per_cpu.0": 4.0, ...}``.

    Lists of dicts keyed by ``name`` (disks, interfaces) become
    ``disk.disks.sda.read_bps`` etc.
    """
    out: Dict[str, Any] = {}
    for key, val in snap.items():
        path = f"{prefix}{key}"
        if path in _SKIP:
            continue
        if isinstance(val, dict):
            out.update(flatten(val, path + "."))
        elif isinstance(val, list):
            if val and isinstance(val[0], dict) and "name" in val[0]:
                for item in val:
                    sub = {k: v for k, v in item.items() if k != "name"}
                    out.update(flatten(sub, f"{path}.{item['name']}."))
            elif all(isinstance(v, (int, float)) or v is None for v in val):
                for i, v in enumerate(val):
                    out[f"{path}.{i}"] = v
        else:
            out[path] = val
    return out


class Recorder:
    """Write snapshots to ``path``. Usable directly or as a Monitor callback.

    >>> with Monitor() as mon, Recorder("usage.csv") as rec:
    ...     mon.subscribe(rec.write)
    ...     time.sleep(60)

    CSV columns are fixed by the first row; ``fields`` restricts them
    (e.g. ``["ts", "cpu.percent", "memory.percent"]``).
    JSONL keeps the full snapshot (set ``include_processes=False`` to drop
    the per-process and connection tables).
    """

    def __init__(self, path: str, fmt: Optional[str] = None,
                 fields: Optional[Iterable[str]] = None, include_processes: bool = True) -> None:
        self.path = path
        ext = os.path.splitext(path)[1].lower()
        self.fmt = (fmt or ("csv" if ext == ".csv" else "jsonl")).lower()
        if self.fmt not in ("csv", "jsonl"):
            raise ValueError("fmt must be 'csv' or 'jsonl'")
        self.fields = list(fields) if fields else None
        self.include_processes = include_processes
        self.rows = 0
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer: Optional[csv.DictWriter] = None

    def write(self, snap: Dict[str, Any]) -> None:
        if self._fh.closed:
            return
        if self.fmt == "jsonl":
            data = snap if self.include_processes else lighten(snap)
            self._fh.write(json.dumps(data, separators=(",", ":")) + "\n")
        else:
            flat = flatten(snap)
            if self._writer is None:
                cols = self.fields or list(flat.keys())
                self._writer = csv.DictWriter(self._fh, fieldnames=cols, extrasaction="ignore")
                self._writer.writeheader()
            self._writer.writerow(flat)
        self.rows += 1
        self._fh.flush()

    __call__ = write

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
