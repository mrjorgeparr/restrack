# Configuration

restrack reads one TOML file. **Every key is optional**: whatever you leave out falls
back to the defaults below, so a config can be as small as:

```toml
[monitor]
interval = 2.0

[alerts]
cpu_percent = 80
```

## Where the file is found

The first match wins:

1. `restrack -c PATH …` (an explicit path; it's an error if the file doesn't exist)
2. the `RESTRACK_CONFIG` environment variable
3. `./restrack.toml` in the current directory
4. the user config file:
    - Linux / macOS: `~/.config/restrack/config.toml` (respects `$XDG_CONFIG_HOME`)
    - Windows: `%APPDATA%\restrack\config.toml`

With no file at all, the defaults are used. Helpful commands:

```bash
restrack config init        # write the commented template to the user config location
restrack config path        # show which file is used
restrack config show        # print the effective settings (file merged over defaults)
```

## All settings

### `[monitor]`: sampling

| Key | Default | Meaning |
|---|---|---|
| `interval` | `1.0` | seconds between samples |
| `history` | `300` | samples kept in memory, which feed the live graphs and `/api/history` |
| `processes` | `true` | collect the per-process table |
| `connections_every` | `3` | refresh TCP/UDP connections every N samples; `0` turns it off |
| `sensors_every` | `5` | refresh temperatures, fans and battery every N samples; `0` turns it off |
| `gpu` | `true` | NVIDIA metrics if `nvidia-ml-py` is installed |

### `[storage]`: long-term metrics log

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | write the SQLite log (needed for the History tab) |
| `path` | `""` | database file; `""` means `<data dir>/metrics.db`; `~` is expanded |
| `every` | `5` | store every Nth sample (with `interval = 1`, one row per 5 s) |
| `retention_days` | `30` | delete older rows; `0` keeps them forever |
| `top_processes` | `10` | top-N processes (by CPU) stored with each row; `0` stores none |

The data dir is `~/.local/share/restrack` on Linux and macOS (respects `$XDG_DATA_HOME`)
and `%LOCALAPPDATA%\restrack` on Windows.

### `[logging]`: text log

| Key | Default | Meaning |
|---|---|---|
| `level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `file` | `""` | `""` means `<data dir>/restrack.log`; `"-"` means console only |
| `max_bytes` | `5000000` | rotate when the file reaches this size |
| `backups` | `3` | rotated files to keep (`restrack.log.1` …) |

### `[alerts]`: thresholds

A rule fires when its condition holds for `sustain` seconds. After that it stays quiet
for `cooldown` seconds, even if the condition persists. Set any threshold to `0` to
disable that rule.

| Key | Default | Fires when |
|---|---|---|
| `enabled` | `true` | master switch |
| `sustain` | `30` | seconds a condition must hold |
| `cooldown` | `300` | seconds before the same rule can fire again |
| `cpu_percent` | `90` | total CPU ≥ value |
| `memory_percent` | `90` | physical memory used ≥ value |
| `swap_percent` | `80` | swap used ≥ value (only if the machine has swap) |
| `disk_busy_percent` | `95` | the busiest disk's active time ≥ value |
| `disk_free_percent` | `10` | any partition's free space ≤ value (checked per partition) |
| `temperature_c` | `90` | any sensor ≥ value (°C) |
| `process_cpu_percent` | `0` (off) | any single process ≥ value, as % of the whole machine |

### `[web]`: UI server

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | bind address; see [security notes](platform-notes.md#security) before changing it |
| `port` | `8765` | port |
| `open_browser` | `false` | open the browser on start |
| `allow_control` | `false` | enable End / Suspend / Resume process in the UI |

## Example: a laptop that logs all day

```toml
[monitor]
interval = 2.0
connections_every = 5

[storage]
every = 15            # one row every 30 s, a few MB per day
retention_days = 90
top_processes = 5

[alerts]
cpu_percent = 85
sustain = 60
temperature_c = 85
process_cpu_percent = 50

[logging]
level = "WARNING"     # keep the text log to alerts and errors
```

## Example: a short, detailed capture

```toml
[monitor]
interval = 0.5

[storage]
path = "./capture.db"
every = 1
retention_days = 0
top_processes = 25

[alerts]
enabled = false
```

Run it with `restrack -c capture.toml run`, then review it with
`restrack -c capture.toml history 30m` or in the UI.

## Using it from Python

```python
from restrack import load_config, Tracker

cfg = load_config("restrack.toml")     # a dict: cfg["monitor"]["interval"], ...
cfg["alerts"]["cpu_percent"] = 75      # adjust in code if you like
with Tracker(cfg):
    ...
```

`load_config()` fills in the default storage and log paths (expanding `~`) and records
the file it read in `cfg["_source"]`. Relative paths are relative to the directory you run from.
