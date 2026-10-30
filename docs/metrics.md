# Metrics reference

A snapshot is a `dict` (and JSON object) with the sections below. Units:

- sizes are **bytes**;
- `*_bps` fields are **bytes per second**, not bits (the UI converts network figures to bits);
- `*_s` fields are **per second**;
- percentages are **0–100**.

Every rate is averaged over the time since the previous sample. The snapshot's
`interval` field gives that time.

A field is `null` (`None`) when the OS doesn't provide it. For example, `cpu.load_avg`
can be null on platforms where psutil can't compute it, and `memory.wired` is
macOS/BSD only.

## Top level

| Field | Type | Meaning |
|---|---|---|
| `ts` | float | Unix time of the sample |
| `seq` | int | sample counter of the collector |
| `interval` | float | seconds since the previous sample |
| `uptime` | float | seconds since boot |
| `cpu`, `memory`, `disk`, `network`, `processes`, `sensors` | object | see below |
| `gpu` | list | NVIDIA GPUs (empty without `nvidia-ml-py`) |

## `cpu`

| Field | Meaning |
|---|---|
| `percent` | total utilization across all cores |
| `per_cpu[]` | utilization per logical core |
| `times_percent` | share of time per state: `user`, `system`, `idle`, plus OS-specific ones (`nice`, `iowait`, `irq`, `softirq`, `steal` on Linux; `interrupt`, `dpc` on Windows) |
| `freq_mhz`, `freq_max_mhz`, `freq_percent` | current and maximum frequency; current as % of max |
| `load_avg[3]` | 1, 5 and 15-minute load average |
| `ctx_switches_s`, `interrupts_s`, `syscalls_s` | scheduler activity (`syscalls_s` is null where the OS doesn't report it, e.g. Linux) |

## `memory`

| Field | Meaning |
|---|---|
| `total`, `available`, `used`, `free`, `percent` | physical memory; `percent` = (total − available) / total |
| `cached`, `buffers`, `shared`, `active`, `inactive`, `wired` | OS-specific breakdowns (null where absent) |
| `swap_total`, `swap_used`, `swap_percent` | swap / page file |
| `swap_in_bps`, `swap_out_bps` | bytes swapped in and out per second (0 on Windows) |
| `hard_faults_s` | major page faults per second, system-wide (Linux; null elsewhere) |
| `dirty`, `writeback` | bytes written by programs but not yet on disk / being written now (Linux; null elsewhere) |

## `disk`

| Field | Meaning |
|---|---|
| `read_bps`, `write_bps`, `read_iops`, `write_iops` | totals over physical disks |
| `busy_percent` | active time of the **busiest** disk (like resmon's "Highest Active Time") |
| `cache_read_bps` | bytes/s that processes read **without** touching the disk: OS page cache (plus pipes/sockets). Linux; null elsewhere |
| `cache_hit_percent` | `cache_read_bps` as % of everything processes read. Linux; null elsewhere |
| `disks[]` | per physical disk: `name`, `read_bps`, `write_bps`, `read_iops`, `write_iops`, `busy_percent` |
| `partitions[]` | per mounted filesystem: `mountpoint`, `device`, `fstype`, `total`, `used`, `free`, `percent` (used) |

On Linux, loop, ram, zram, device-mapper (`dm-*`) and md devices are excluded from
`disks[]` so that totals aren't counted twice. Squashfs, tmpfs, devtmpfs and overlay
mounts are excluded from `partitions[]`.

Active time comes from the kernel's busy time on Linux. On Windows and macOS it's
estimated from read and write time and capped at 100 %.

## `network`

| Field | Meaning |
|---|---|
| `sent_bps`, `recv_bps` | totals over **non-loopback** adapters |
| `utilization_percent` | (sent + received) / combined link speed of adapters that are up; null when link speed is unknown (e.g. many VMs, Wi-Fi on some OSes) |
| `interfaces[]` | per adapter: `name`, `is_up`, `speed_mbps`, `mtu`, `ipv4`, `loopback`, `sent_bps`, `recv_bps`, `packets_sent_s`, `packets_recv_s`, `errors` and `drops` (cumulative counts) |
| `connections_available` | `false` when the OS denied access to the connection table (macOS without sudo), `null` before the first refresh |
| `connections_by_status` | count of TCP connections per state (`ESTABLISHED`, `TIME_WAIT`, …), excluding listeners |
| `tcp[]` | up to 1000 non-listening TCP connections: `pid`, `name`, `laddr`, `lport`, `raddr`, `rport`, `status`, `family` |
| `listening[]` | TCP listeners and unconnected UDP sockets: `pid`, `name`, `address`, `port`, `protocol`, `family` |

The connection fields are refreshed every `connections_every` samples, so between
refreshes they repeat the last values.

## `processes`

| Field | Meaning |
|---|---|
| `count`, `threads`, `handles` | totals |
| `read_logical_bps`, `read_disk_bps` | all bytes processes read vs bytes that hit the disk, summed over processes restrack can read (Linux; run as root for all) |
| `by_status` | processes per state (`running`, `sleeping`, `idle`, `stopped`, `zombie`, …) |
| `list[]` | one entry per process, sorted by `cpu` (highest first) |

Each `list[]` entry:

| Field | Meaning |
|---|---|
| `pid`, `ppid`, `name`, `user`, `status`, `created` | identity (`created` is Unix time) |
| `cpu` | % of the **whole machine** (all cores = 100) |
| `cpu_avg` | exponential average of `cpu` over `avg_window` (default 60 s) |
| `threads`, `handles` | threads; handles (Windows) or open file descriptors (POSIX) |
| `rss`, `vms` | resident set (working set) and virtual size (commit) |
| `shared`, `private` | shared pages; private = rss − shared (Windows: private bytes) |
| `mem_percent` | `rss` as % of physical memory |
| `faults_s` | Linux: major faults/s; Windows: all page faults/s; macOS: null |
| `read_bps`, `write_bps` | disk I/O of the process |
| `read_cache_bps` | bytes/s read without disk I/O (page cache, pipes, sockets); Linux, else null |
| `io_available` | `false` when per-process I/O couldn't be read (no permission, or macOS) |
| `tcp`, `listening` | sockets owned by the process |

Fields that can't be read for a process (usually due to permissions) are `null` or 0
rather than dropping the process.

## `sensors`

| Field | Meaning |
|---|---|
| `temperatures[]` | `chip`, `label`, `current`, `high`, `critical` (°C) |
| `fans[]` | `chip`, `label`, `rpm` |
| `battery` | `percent`, `plugged`, `secs_left` (null when unknown or charging), or null without a battery |

Sensors are refreshed every `sensors_every` samples.

## `gpu[]`

With `pip install "restrack[gpu]"` and an NVIDIA driver, each GPU has: `index`, `name`,
`util_percent`, `mem_util_percent`, `mem_used`, `mem_total`, `temperature` (°C) and
`power_w`. AMD, Intel and Apple GPUs aren't supported.

## Light snapshots

`Monitor.history()`, `/api/history` and `Recorder(include_processes=False)` return
**light** snapshots. These are the same structure without `processes.list`,
`network.tcp` and `network.listening`.

## Flattened names (CSV)

`restrack.flatten()` and CSV recordings use dotted paths. Lists of scalars are indexed
and lists of named objects are keyed by name:

```text
cpu.percent  cpu.per_cpu.0  cpu.load_avg.0  memory.available
disk.disks.sda.read_bps  network.interfaces.eth0.recv_bps  sensors.battery.percent
```

The process, connection, partition, sensor and GPU lists are left out of CSV output.
