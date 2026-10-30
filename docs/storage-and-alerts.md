# Storage & alerts

`restrack web` and `restrack run` (or `Tracker` in Python) do three things on top of
live monitoring, all controlled by the [config file](configuration.md):

1. write a **metrics log** (SQLite) that the History tab and `restrack history` read;
2. check **alert** thresholds;
3. write a rotating **text log**.

## File locations

| | Linux / macOS | Windows |
|---|---|---|
| metrics log | `~/.local/share/restrack/metrics.db` | `%LOCALAPPDATA%\restrack\metrics.db` |
| text log | `~/.local/share/restrack/restrack.log` (+ `.1`, `.2`, …) | `%LOCALAPPDATA%\restrack\restrack.log` |
| config | `~/.config/restrack/config.toml` | `%APPDATA%\restrack\config.toml` |

Override them with `storage.path` and `logging.file`.

## What is stored

Every `storage.every`-th sample becomes one row in the `samples` table:

| Column | Meaning |
|---|---|
| `ts` | Unix timestamp (seconds) |
| `cpu`, `cpu_freq` | total CPU %, frequency as % of max |
| `load1` | 1-minute load average (Windows: psutil's emulation) |
| `mem`, `mem_used`, `swap` | memory %, used bytes, swap % |
| `hard_faults` | hard page faults/s (Linux) |
| `disk_read`, `disk_write`, `disk_busy` | B/s, and the busiest disk's active time % |
| `cache_read`, `cache_hit` | B/s read without disk I/O, and its % of all reads (Linux) |
| `dirty` | bytes waiting to be written to disk (Linux) |
| `net_sent`, `net_recv` | B/s over non-loopback adapters |
| `procs`, `threads` | counts |
| `temp_max` | hottest sensor °C |
| `gpu` | highest GPU utilization % |

Each row also stores its top `storage.top_processes` processes by CPU (`proc_samples`:
name, pid, user, cpu, rss, read and write B/s), and alerts go into an `events` table.

Rows hold numbers only: process names and users, but **no command lines, file paths or
connection details**.

### Size and retention

With the defaults (one row every 5 s, 10 processes per row) the log grows by roughly
**12–15 MB per day**, or about 400 MB at the default 30-day retention. Rows older than
`retention_days` are deleted hourly.

| To shrink it | Setting |
|---|---|
| fewer rows | raise `storage.every` (15 means one row per 15 samples) |
| smaller rows | lower `storage.top_processes` (0 stores no processes) |
| shorter history | lower `storage.retention_days` |

SQLite doesn't give space back to the OS after deletes. To compact the file, run
`sqlite3 metrics.db "VACUUM"` while restrack is stopped.

### Querying it yourself

The database is ordinary SQLite, so any tool can read it:

```sql
-- hourly CPU average and peak for the last day
SELECT datetime(CAST(ts/3600 AS INT)*3600, 'unixepoch', 'localtime') AS hour,
       ROUND(AVG(cpu),1) AS cpu_avg, MAX(cpu) AS cpu_max
FROM samples WHERE ts > strftime('%s','now') - 86400
GROUP BY 1 ORDER BY 1;

-- which processes appeared most often in the top list this week
SELECT name, COUNT(*) AS hits, ROUND(AVG(cpu),1) AS avg_cpu
FROM proc_samples WHERE ts > strftime('%s','now') - 7*86400
GROUP BY name ORDER BY hits DESC LIMIT 10;
```

Or from Python with `restrack.Store(path).query(start, end, points)`, or with pandas:
`pd.read_sql("select * from samples", sqlite3.connect(path))`.

The database is opened in WAL mode, so reading it while restrack is writing is safe.

## Alerts

Each rule in `[alerts]` is checked on every sample, not only on stored ones:

1. When the condition first becomes true, a timer starts.
2. If it's still true after `sustain` seconds, the alert **fires**:
    - a `WARNING ALERT <message>` line goes to the text log (and the console);
    - an event is stored and shown in the History tab, with an orange marker on the CPU chart.
3. The same rule can't fire again for `cooldown` seconds.
4. When the condition clears after firing, a `resolved` event is recorded.

Partitions, sensors and processes are tracked individually, so `/data` filling up and
`/` filling up are separate alerts.

Example text log:

```text
2026-10-29 19:13:27,370 INFO    restrack: restrack started: interval=1.0s, storage=/home/me/.local/share/restrack/metrics.db, config=/home/me/.config/restrack/config.toml
2026-10-29 19:13:42,389 WARNING restrack: ALERT CPU at 93% (>= 90%)
2026-10-29 19:14:11,386 INFO    restrack: resolved: cpu back below threshold
```

### Reacting to alerts in code

The text log is easy to tail. To act on alerts directly (send an email, post to Teams…),
give your callback its **own** `Alerts` instance with the same rules:

```python
import restrack

cfg = restrack.load_config()
tracker = restrack.Tracker(cfg).start()      # stores and logs as usual
my_alerts = restrack.Alerts(cfg["alerts"])   # separate state: sustain/cooldown per instance

def notify(snap):
    for msg in my_alerts.check(snap):        # messages that fired on this sample
        send_somewhere(msg)                  # your code

tracker.monitor.subscribe(notify)
```

Don't call `tracker.alerts.check()` from your callback: the tracker has already run
that instance on the sample, so its cooldown would swallow the alert.

## Running in the background

History only accumulates while restrack runs. Pick one option:

**Linux (systemd user service).** Create `~/.config/systemd/user/restrack.service`:

```ini
[Unit]
Description=restrack resource logger

[Service]
ExecStart=%h/.local/bin/restrack web
Restart=on-failure

[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now restrack`. Use `restrack run` instead of `web`
if you don't want the UI server. Point `ExecStart` at wherever pip installed the
`restrack` script (`which restrack`).

**macOS.** Use a LaunchAgent plist running `restrack web` with `RunAtLoad` set to true,
or run it in a terminal tab.

**Windows.** In Task Scheduler, create a task that runs *At log on* with the program
`pythonw.exe` and arguments `-m restrack web`. `pythonw` runs it without a console
window.

**Anywhere, for a quick session:** `restrack web`, and leave the terminal open.
