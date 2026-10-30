# Command line

```
restrack [-c CONFIG] <command> [options]
python -m restrack ...            # equivalent
```

`-c/--config` is a **global** option, so it goes *before* the command:
`restrack -c my.toml web`. Without it, restrack searches `$RESTRACK_CONFIG`,
then `./restrack.toml`, then the user config file (see [Configuration](configuration.md)).

| Command | Purpose | Uses config? |
|---|---|---|
| [`web`](#web) | Resource Monitor UI, plus storage and alerts | yes |
| [`run`](#run) | storage and alerts, headless | yes |
| [`history`](#history) | summarise or export the stored log | yes (storage path) |
| [`config`](#config) | create or inspect the config file | — |
| [`top`](#top) | live terminal view | no |
| [`snapshot`](#snapshot) | print one reading | no |
| [`record`](#record) | ad-hoc recording to CSV / JSON Lines | no |
| [`profile`](profiling.md#command-line-reference) | profile a Python script → HTML report | no |
| [`dtypes`](dataframes.md) | memory per column of a data file and safe smaller dtypes | no |
| `counters` | check whether CPU cache counters work on this machine | no |

## web

```bash
restrack web [--host HOST] [-p PORT] [-i SECONDS] [--open] [--allow-control] [--no-store]
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `web.host` (`127.0.0.1`) | interface to bind; prints a warning for anything other than localhost |
| `-p, --port` | `web.port` (`8765`) | port |
| `-i, --interval` | `monitor.interval` (`1.0`) | seconds between samples |
| `--open` | off | open the default browser |
| `--allow-control` | off | enable End / Suspend / Resume process from the UI |
| `--no-store` | — | don't write the metrics log for this run |

Command-line options override the config file.

## run

```bash
restrack run [-i SECONDS]
```

This samples, stores and alerts according to the config until Ctrl+C. Use it as a
background service when you want continuous history.

## history

```bash
restrack history [SINCE] [--points N] [--csv FILE]
```

| Argument | Default | Meaning |
|---|---|---|
| `SINCE` | `1h` | how far back: a number plus `s`, `m`, `h`, `d` or `w` (`30m`, `6h`, `7d`), or plain seconds |
| `--points` | `24` | number of rows (time buckets) to summarise into |
| `--csv FILE` | — | export the raw stored rows for the period instead of summarising |

Example output:

```text
metrics.db: 17280 rows, 12.4 MB; showing last 24h in 24 buckets of 3600s
time                  cpu%    max   mem%   disk r/s   disk w/s   net rx/s   net tx/s
2026-10-29 09:00:00   12.4   88.1   61.0     1.2 MB     3.4 MB    210 KB      40 KB
...
Events:
  2026-10-29 10:42:13 WARNING CPU at 93% (>= 90%)
```

`cpu%` is the average over each bucket and `max` is the highest sample in it.

## config

```bash
restrack config init [PATH] [--force]   # write a commented template
restrack config show                    # effective settings as JSON
restrack config path                    # which file is in use
```

`init` writes to the user config location unless you pass a path, and refuses to
overwrite an existing file without `--force`.

## top

```bash
restrack top [-i SECONDS] [-n N] [-s {cpu,mem,io}]
```

This is a refreshing terminal view showing CPU and per-core bars, memory, swap, disk,
network, GPU and temperatures (when available), plus the top `N` processes (default 15).
It works in Windows Terminal, PowerShell and cmd as well as Unix terminals.

## snapshot

```bash
restrack snapshot [-i SECONDS] [--json] [--pretty] [--top N] [--no-processes]
```

This takes one reading, with rates measured over `-i` seconds (default 0.5).
`--json` prints the full [snapshot](metrics.md) instead of the text view.

```bash
restrack snapshot --json --top 5 | jq '.cpu.percent, .processes.list[0].name'
```

## record

```bash
restrack record -o FILE [-i SECONDS] [-d SECONDS] [--fields LIST] [--processes] [--top N]
```

This records samples to a file. It's independent of the long-term log, so use it for a
quick test or benchmark.

- **`.csv`** writes one row per sample with every scalar metric flattened into a column
  (`cpu.percent`, `cpu.per_cpu.0`, `disk.disks.sda.read_bps`, …). Columns are fixed by
  the first row. `--fields ts,cpu.percent,memory.percent` keeps only those columns.
- **`.jsonl`** writes one full snapshot per line. Add `--processes` to keep the process
  and connection tables, and `--top N` to limit the processes to the top N by CPU.

```bash
restrack record -o bench.csv -i 0.5 -d 120 --fields ts,cpu.percent,memory.used,disk.write_bps
```
