"""Configuration file support (TOML).

Lookup order when no path is given:
  1. ``$RESTRACK_CONFIG``
  2. ``./restrack.toml``
  3. ``~/.config/restrack/config.toml``  (``%APPDATA%\\restrack\\config.toml`` on Windows)

Every key is optional; missing keys fall back to the defaults below.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "restrack"


def default_config_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "restrack" / "config.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "restrack" / "config.toml"


DEFAULTS: Dict[str, Any] = {
    "monitor": {
        "interval": 1.0,         # seconds between samples
        "history": 300,          # samples kept in memory for the live graphs
        "processes": True,       # collect the per-process table
        "connections_every": 3,  # refresh TCP/UDP table every N samples (0 = off)
        "sensors_every": 5,      # refresh temperatures/battery every N samples (0 = off)
        "gpu": True,             # NVIDIA metrics if nvidia-ml-py is installed
    },
    "storage": {
        "enabled": True,
        "path": "",              # SQLite file; "" = <data dir>/metrics.db
        "every": 5,              # persist every Nth sample (5 x 1s = one row per 5 s)
        "retention_days": 30,    # delete rows older than this (0 = keep forever)
        "top_processes": 10,     # top-N processes (by CPU) stored with each row (0 = none)
    },
    "logging": {
        "level": "INFO",
        "file": "",              # "" = <data dir>/restrack.log ; "-" = no file
        "max_bytes": 5_000_000,
        "backups": 3,
    },
    "alerts": {
        "enabled": True,
        "sustain": 30,           # seconds a condition must hold before alerting
        "cooldown": 300,         # seconds before the same alert can fire again
        "cpu_percent": 90,       # total CPU %
        "memory_percent": 90,
        "swap_percent": 80,
        "disk_busy_percent": 95,
        "disk_free_percent": 10, # any partition below this % free
        "temperature_c": 90,     # any sensor above this
        "process_cpu_percent": 0,  # any single process above this (0 = off)
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": False,
        "allow_control": False,  # End/Suspend/Resume process from the UI
    },
}

TEMPLATE = '''# restrack configuration (TOML). Every key is optional.

[monitor]
interval = 1.0          # seconds between samples
history = 300           # samples kept in memory for the live graphs
processes = true        # collect the per-process table
connections_every = 3   # refresh TCP/UDP table every N samples (0 = off)
sensors_every = 5       # refresh temperatures/battery every N samples (0 = off)
gpu = true              # NVIDIA metrics if nvidia-ml-py is installed

[storage]               # long-term metrics log (SQLite) behind the History tab
enabled = true
path = ""               # "" = {data_dir}/metrics.db
every = 5               # persist every Nth sample (with interval=1 -> one row per 5 s)
retention_days = 30     # 0 = keep forever
top_processes = 10      # top-N processes by CPU stored with each row

[logging]               # text log: start/stop, alerts, errors
level = "INFO"
file = ""               # "" = {data_dir}/restrack.log ; "-" = console only
max_bytes = 5000000
backups = 3

[alerts]                # written to the log + the events table; 0 disables a rule
enabled = true
sustain = 30            # seconds a condition must hold before it fires
cooldown = 300          # seconds before the same alert can fire again
cpu_percent = 90
memory_percent = 90
swap_percent = 80
disk_busy_percent = 95
disk_free_percent = 10
temperature_c = 90
process_cpu_percent = 0

[web]
host = "127.0.0.1"      # keep on localhost unless you know who can reach it
port = 8765
open_browser = false
allow_control = false   # enable End / Suspend / Resume process in the UI
'''


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config() -> Optional[Path]:
    env = os.environ.get("RESTRACK_CONFIG")
    for cand in (env, "restrack.toml", default_config_path()):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the config (explicit path, or the lookup order above) merged over DEFAULTS."""
    p = Path(path) if path else find_config()
    data: Dict[str, Any] = {}
    if p is not None:
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        if tomllib is None:
            raise RuntimeError("reading TOML on Python < 3.11 needs: pip install tomli")
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
    cfg = _merge(DEFAULTS, data)
    cfg["_source"] = str(p) if p else None
    data_dir = default_data_dir()
    st, lg = cfg["storage"], cfg["logging"]
    st["path"] = os.path.expanduser(st["path"]) if st["path"] else str(data_dir / "metrics.db")
    if lg["file"] != "-":
        lg["file"] = os.path.expanduser(lg["file"]) if lg["file"] else str(data_dir / "restrack.log")
    return cfg


def write_template(path: Optional[str] = None, force: bool = False) -> Path:
    p = Path(path) if path else default_config_path()
    if p.exists() and not force:
        raise FileExistsError(f"{p} already exists (use --force to overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE.replace("{data_dir}", str(default_data_dir())), encoding="utf-8")
    return p
