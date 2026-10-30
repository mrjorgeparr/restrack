# HTTP API

`restrack web` serves a small JSON API, which the UI uses and you can call too.
Responses are JSON unless noted. Base URL: `http://localhost:8765`.

## Live data

| Endpoint | Returns |
|---|---|
| `GET /api/info` | static machine info (hostname, OS, CPU model, cores, memory, boot time) plus server settings: `interval`, `allow_control`, `storage`, `config_source`, `alerts` |
| `GET /api/snapshot` | the latest full [snapshot](metrics.md) (`503` before the first sample) |
| `GET /api/history?n=60` | the last `n` light snapshots kept in memory (max 3600) |
| `GET /api/stream` | [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events): one `snapshot` event per sample, the latest sent immediately, keep-alive comments every 10 s |
| `GET /api/process/<pid>` | process details: exe, cmdline, cwd, user, parent, children, nice, threads, CPU affinity, USS, context switches, up to 500 open files and sockets (`404` no such process, `403` access denied) |

```bash
curl -s localhost:8765/api/snapshot | jq '{cpu: .cpu.percent, mem: .memory.percent}'
curl -sN localhost:8765/api/stream            # live stream in the terminal
```

```python
import json, urllib.request
with urllib.request.urlopen("http://localhost:8765/api/stream") as r:
    for line in r:
        if line.startswith(b"data: "):
            snap = json.loads(line[6:])
            print(snap["cpu"]["percent"])
```

## Stored history

These require storage to be enabled; otherwise they return `404`. Times are Unix
seconds, and `start` and `end` default to the last hour.

| Endpoint | Returns |
|---|---|
| `GET /api/storage` | `path`, `first`, `last`, `rows`, `bytes`, `retention_days` |
| `GET /api/range?start=&end=&points=600` | downsampled series: `{start, end, bucket, series: [{ts, cpu, cpu_max, mem, …, n}]}` (`points` 10–5000) |
| `GET /api/top?ts=&window=60&n=10` | busiest processes in the `window` seconds around `ts` |
| `GET /api/events?start=&end=` | alerts and start/stop events, newest first |
| `GET /api/export.csv?start=&end=` | raw rows as a CSV download |

```bash
now=$(date +%s)
curl -s "localhost:8765/api/range?start=$((now-86400))&end=$now&points=24" | jq '.series[] | [.ts, .cpu, .cpu_max]'
```

## Process control

```
POST /api/process/<pid>/<action>      action: terminate | kill | suspend | resume
Header: X-Restrack-Control: 1
```

This only works when the server runs with `--allow-control`; otherwise it returns `403`.
The custom header is required so that other web pages open in your browser can't
trigger it (browsers won't send a custom header cross-origin without a CORS preflight,
and the server never grants one). PID ≤ 1 and the server's own PID are refused.

```bash
curl -X POST -H "X-Restrack-Control: 1" localhost:8765/api/process/12345/suspend
```

## Status codes

| Code | When |
|---|---|
| `403` | `Host` header isn't localhost while bound to localhost (DNS-rebinding guard); control disabled or header missing; OS access denied |
| `404` | unknown path or PID; storage endpoints with storage disabled |
| `503` | `/api/snapshot` before the first sample |
