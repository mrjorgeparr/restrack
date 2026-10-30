# Platform notes

restrack runs wherever psutil does, but the operating system decides what can be
measured. Here's what to expect.

## Feature matrix

| Feature | Linux | Windows | macOS |
|---|---|---|---|
| CPU, per-core, frequency, time breakdown | ✅ | ✅ | ✅ (frequency may be fixed on Apple Silicon) |
| Load average | ✅ | ✅ emulated by psutil (starts at 0) | ✅ |
| Memory, swap | ✅ | ✅ (page file) | ✅ |
| Hard faults/s (system) | ✅ | — | — |
| OS page cache: reads from cache vs disk | ✅ | — | — |
| Dirty / write-back memory | ✅ | — | — |
| **CPU cache counters** (IPC, L1/LLC misses) | ✅ if the CPU's counters are exposed (not in most VMs) | **—** | **—** |
| Hard faults/s (per process) | ✅ major faults | ✅ all page faults | — |
| Disk throughput, IOPS | ✅ | ✅ (may need `diskperf -y` on very old systems) | ✅ |
| Disk active time | ✅ exact | ≈ estimated | ≈ estimated |
| Per-process disk I/O | ✅ (own processes; all with root) | ✅ | — |
| Network per adapter | ✅ | ✅ | ✅ |
| Connections table | ✅ (other users' PIDs need root) | ✅ | needs `sudo` |
| Temperatures, fans | ✅ | — | — |
| Battery | ✅ | ✅ | ✅ |
| GPU | NVIDIA | NVIDIA | — |

## CPU cache counters

CPU cache hits and misses come from the processor's performance counters, which restrack
reads through Linux `perf_event` (see [Profiling → CPU caches](profiling.md#cpu-caches)).
**Windows and macOS don't let normal programs read them, so restrack can't report CPU
cache misses there.** Intel VTune, AMD uProf (Windows) and Instruments (macOS) can,
using their own drivers. Everything else in the profiler works on all three systems.

Run `restrack counters` on a machine to see whether they're available and why not.

## Things psutil can't do

- **Per-process network bytes/second.** No OS exposes this cheaply (resmon uses ETW
  tracing on Windows). restrack shows socket counts per process instead, while adapter
  totals are exact.
- **Per-file disk activity** (resmon's "Disk Activity" by file) also requires kernel
  tracing. The Disk tab shows per-disk activity instead.

## Permissions

restrack works as a normal user. With more privileges you get more detail:

- **Linux.** Without root, other users' processes show no disk I/O or open files, and
  connection PIDs are hidden for them.
- **Windows.** An elevated prompt adds details for system and service processes and
  allows controlling them.
- **macOS.** `sudo restrack web` enables the connection table and more per-process
  fields.

Processes that can't be read still appear, with the unreadable fields left empty.

## Containers and VMs

Inside containers, disk counters and sensors are often missing, and network link speed
is usually unknown (so utilization shows `–`). CPU and memory figures describe the host
or VM as the kernel reports them, and usually don't reflect cgroup limits.

## Performance

Sampling cost grows with the number of processes. In testing it took about 9 ms of CPU
per sample with 64 processes, under 1 % of one core at a 1-second interval. Expect a few
percent with several hundred processes, mostly for the process table. To reduce that:

- set `processes = false` if you only need machine totals;
- raise `interval`;
- raise `connections_every`, which matters on machines with thousands of sockets.

## Security

- The server binds to `127.0.0.1` by default and only answers requests whose `Host`
  header is a loopback name. This blocks DNS-rebinding attacks from web pages.
- The UI shows process names, users, command lines (in Properties), open files and
  connections. If you bind to `0.0.0.0`, anyone who can reach the port sees all of that.
  There's no authentication, so put it behind an authenticating reverse proxy or an SSH
  tunnel (`ssh -L 8765:localhost:8765 host`) instead.
- Process control is off unless explicitly enabled, and requires a custom request
  header (see [HTTP API](http-api.md#process-control)).
- Pages are served with a strict Content-Security-Policy, and no third-party scripts or
  fonts are loaded.
- The metrics log stores numbers and process names and users, but no command lines or
  paths.
