"""Command line interface: ``restrack {top,snapshot,record,web}``."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .collector import Collector
from .monitor import Monitor
from .recorder import Recorder


def fmt_bytes(n: Optional[float], suffix: str = "B") -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or unit == "P":
            return f"{n:.0f} {unit}{suffix}" if unit == "" else f"{n:.1f} {unit}{suffix}"
        n /= 1024
    return f"{n:.1f} P{suffix}"


def fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = int(round(pct / 100 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def render_top(snap: Dict[str, Any], info: Dict[str, Any], n: int = 15, sort: str = "cpu") -> str:
    cols = shutil.get_terminal_size((100, 30)).columns
    cpu, mem, disk, net = snap["cpu"], snap["memory"], snap["disk"], snap["network"]
    lines: List[str] = []
    lines.append(f"restrack {__version__} — {info['hostname']} ({info['os']} {info['os_release']})"
                 f"  up {fmt_duration(snap.get('uptime'))}   {time.strftime('%H:%M:%S')}")
    load = " ".join(f"{x:.2f}" for x in cpu["load_avg"]) if cpu.get("load_avg") else "-"
    freq = f"{cpu['freq_mhz']:.0f} MHz" if cpu.get("freq_mhz") else "-"
    lines.append(f"CPU  {_bar(cpu['percent'])} {cpu['percent']:5.1f}%   freq {freq}   load {load}")
    per = cpu["per_cpu"]
    for i in range(0, len(per), 4):
        lines.append("     " + "  ".join(f"cpu{j:<3d}{_bar(per[j], 10)}{per[j]:5.1f}%"
                                         for j in range(i, min(i + 4, len(per)))))
    lines.append(f"MEM  {_bar(mem['percent'])} {mem['percent']:5.1f}%   "
                 f"{fmt_bytes(mem['used'])} / {fmt_bytes(mem['total'])}   avail {fmt_bytes(mem['available'])}")
    if mem.get("swap_total"):
        lines.append(f"SWAP {_bar(mem['swap_percent'])} {mem['swap_percent']:5.1f}%   "
                     f"{fmt_bytes(mem['swap_used'])} / {fmt_bytes(mem['swap_total'])}")
    lines.append(f"DISK read {fmt_bytes(disk['read_bps'])}/s  write {fmt_bytes(disk['write_bps'])}/s  "
                 f"active {disk['busy_percent']:.0f}%")
    lines.append(f"NET  recv {fmt_bytes(net['recv_bps'])}/s  sent {fmt_bytes(net['sent_bps'])}/s")
    for g in snap.get("gpu") or []:
        lines.append(f"GPU{g['index']} {g['name']}: {g['util_percent']}%  "
                     f"{fmt_bytes(g['mem_used'])}/{fmt_bytes(g['mem_total'])}  {g['temperature']}°C")
    temps = [t for t in snap["sensors"]["temperatures"] if t["current"]]
    if temps:
        lines.append("TEMP " + "  ".join(f"{t['label']} {t['current']:.0f}°C" for t in temps[:6]))
    procs = snap.get("processes")
    if procs:
        lines.append(f"Processes {procs['count']}   threads {procs['threads']}")
        lines.append("")
        key = {"cpu": "cpu", "mem": "rss", "io": "io"}[sort]
        rows = procs["list"]
        if key == "io":
            rows = sorted(rows, key=lambda r: r["read_bps"] + r["write_bps"], reverse=True)
        else:
            rows = sorted(rows, key=lambda r: r[key], reverse=True)
        hdr = f"{'PID':>7} {'NAME':<22} {'CPU%':>6} {'AVG%':>6} {'RSS':>10} {'READ/s':>10} {'WRITE/s':>10} {'THR':>4}"
        lines.append(hdr)
        for r in rows[:n]:
            lines.append(f"{r['pid']:>7} {r['name'][:22]:<22} {r['cpu']:>6.1f} {r['cpu_avg']:>6.1f} "
                         f"{fmt_bytes(r['rss']):>10} {fmt_bytes(r['read_bps']):>10} "
                         f"{fmt_bytes(r['write_bps']):>10} {r['threads']:>4}")
    return "\n".join(line[:cols] for line in lines)


def cmd_top(args) -> int:
    mon = Monitor(interval=args.interval, history=10)
    info = mon.collector.info
    if os.name == "nt":
        os.system("")  # enable ANSI escapes on Windows terminals
    try:
        for snap in mon.stream():
            sys.stdout.write("\x1b[H\x1b[2J" + render_top(snap, info, args.n, args.sort) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
    return 0


def cmd_snapshot(args) -> int:
    c = Collector(processes=not args.no_processes, top_n=args.top)
    c.sample()
    time.sleep(args.interval)
    snap = c.sample()
    if args.json:
        print(json.dumps(snap, indent=2 if args.pretty else None))
    else:
        print(render_top(snap, c.info, args.top or 15))
    return 0


def cmd_record(args) -> int:
    mon = Monitor(interval=args.interval, history=2, processes=args.processes or args.output.endswith(".jsonl"),
                  top_n=args.top)
    fields = args.fields.split(",") if args.fields else None
    rec = Recorder(args.output, fields=fields, include_processes=args.processes)
    mon.subscribe(rec.write)
    print(f"Recording every {args.interval}s to {args.output}"
          + (f" for {args.duration}s" if args.duration else "") + "  (Ctrl+C to stop)")
    mon.start()
    try:
        if args.duration:
            time.sleep(args.duration + args.interval / 2)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
        rec.close()
    print(f"Wrote {rec.rows} samples to {args.output}")
    return 0


def _cfg(args):
    from .config import load_config

    cfg = load_config(args.config)
    if getattr(args, "no_store", False):
        cfg["storage"]["enabled"] = False
    return cfg


def cmd_web(args) -> int:
    from .web.server import serve

    cfg = _cfg(args)
    host = args.host or cfg["web"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the UI exposes process, user and connection details to anyone who can reach "
              f"{host}", file=sys.stderr)
    serve(cfg, host=args.host, port=args.port, interval=args.interval,
          allow_control=True if args.allow_control else None, open_browser=True if args.open else None)
    return 0


def cmd_run(args) -> int:
    """Headless: sample, store and alert according to the config (for a service / cron @reboot)."""
    from .storage import Tracker

    cfg = _cfg(args)
    if args.interval:
        cfg["monitor"]["interval"] = args.interval
    tracker = Tracker(cfg).start()
    print(f"restrack logging every {tracker.monitor.interval}s"
          + (f" to {tracker.store.path}" if tracker.store else " (storage disabled)") + "  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
    return 0


def _parse_since(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = text.strip().lower()
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def cmd_history(args) -> int:
    from .storage import Store

    cfg = _cfg(args)
    path = cfg["storage"]["path"]
    if not os.path.exists(path):
        print(f"no metrics log at {path} yet — run `restrack web` or `restrack run` first", file=sys.stderr)
        return 1
    st = Store(path, retention_days=0)
    end = time.time()
    start = end - _parse_since(args.since)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            n = st.export_csv(start, end, fh)
        print(f"Wrote {n} rows to {args.csv}")
        return 0
    res = st.query(start, end, points=args.points)
    rows = res["series"]
    b = st.bounds()
    print(f"{path}: {b['rows']} rows, {fmt_bytes(b['bytes'])}; showing last {args.since} "
          f"in {len(rows)} buckets of {res['bucket']:.0f}s")
    if not rows:
        return 0
    print(f"{'time':<19} {'cpu%':>6} {'max':>6} {'mem%':>6} {'disk r/s':>10} {'disk w/s':>10} "
          f"{'net rx/s':>10} {'net tx/s':>10}")
    for r in rows:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts'])):<19} {r['cpu'] or 0:6.1f} "
              f"{r['cpu_max'] or 0:6.1f} {r['mem'] or 0:6.1f} {fmt_bytes(r['disk_read']):>10} "
              f"{fmt_bytes(r['disk_write']):>10} {fmt_bytes(r['net_recv']):>10} {fmt_bytes(r['net_sent']):>10}")
    ev = st.events(start, end, limit=20)
    if ev:
        print("\nEvents:")
        for e in reversed(ev):
            print(f"  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts']))} {e['level']:<7} {e['message']}")
    return 0


def cmd_config(args) -> int:
    from .config import default_config_path, find_config, load_config, write_template

    if args.action == "init":
        try:
            p = write_template(args.path, force=args.force)
        except FileExistsError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Wrote {p}")
    elif args.action == "path":
        found = find_config()
        print(found or f"(none found; default location would be {default_config_path()})")
    else:
        print(json.dumps(load_config(args.config), indent=2))
    return 0


def cmd_profile(args) -> int:
    from .profiler import run_script

    if not os.path.isfile(args.script):
        print(f"no such script: {args.script}", file=sys.stderr)
        return 2
    hw = {"auto": "auto", "on": True, "off": False}[args.hw]
    prof = run_script(args.script, args.script_args, track_modules=args.track_module,
                      interval=args.interval, functions=None if args.functions == "off" else args.functions,
                      sample_interval=args.sample_interval, memory=args.memory, where=args.where, hw=hw)
    print("\n" + prof.summary(args.top), file=sys.stderr)
    if args.json:
        prof.save_json(args.json)
        print(f"JSON report: {args.json}", file=sys.stderr)
    if not args.no_html:
        path = prof.save_html(args.html)
        print(f"HTML report: {os.path.abspath(path)}", file=sys.stderr)
        if args.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(path))
    return 1 if getattr(prof, "error", None) is not None else 0


def _read_table(path: str, nrows: Optional[int]):
    import pandas as pd

    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        return pd.read_csv(path, nrows=nrows, sep="\t" if ext == ".tsv" else ",")
    if ext in (".parquet", ".pq"):
        df = pd.read_parquet(path)
    elif ext == ".feather":
        df = pd.read_feather(path)
    elif ext in (".json", ".jsonl"):
        df = pd.read_json(path, lines=ext == ".jsonl", nrows=nrows if ext == ".jsonl" else None)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, nrows=nrows)
    elif ext in (".pkl", ".pickle"):
        print("refusing to unpickle: pickle files can run arbitrary code", file=sys.stderr)
        raise SystemExit(2)
    else:
        raise SystemExit(f"unsupported file type: {ext} (csv, tsv, parquet, feather, json, jsonl, xlsx)")
    return df.head(nrows) if nrows else df


def cmd_dtypes(args) -> int:
    try:
        import pandas  # noqa: F401
    except ImportError:
        print("restrack dtypes needs pandas: pip install pandas", file=sys.stderr)
        return 2
    from .dataframes import memory_report, optimize

    df = _read_table(args.file, args.rows)
    rep = memory_report(df)
    a = rep.attrs
    print(f"{args.file}: {a['rows']:,} rows x {a['columns_count']} columns, {fmt_bytes(a['total'])} in memory"
          f" -> {fmt_bytes(a['est_total'])} with suggested dtypes ({a['saving_pct']}% smaller)"
          + (f"  [first {args.rows:,} rows]" if args.rows else ""))
    print()
    print(f"{'column':<28} {'dtype':<14} {'memory':>10} {'%':>6} {'suggested':<16} {'saving':>10}  note")
    for r in rep.to_dict("records"):
        r = {k: (None if isinstance(v, float) and v != v else v) for k, v in r.items()}  # NaN -> None
        print(f"{str(r['column'])[:28]:<28} {r['dtype'][:14]:<14} {fmt_bytes(r['memory']):>10} {r['pct']:>6.1f} "
              f"{(r['suggested'] or '-')[:16]:<16} {fmt_bytes(r['saving']) if r['saving'] else '':>10}  {r['note'] or ''}")
    if args.write:
        out = optimize(df, floats={"safe": "safe", "all": True, "none": False}[args.floats])
        ext = os.path.splitext(args.write)[1].lower()
        try:
            if ext in (".parquet", ".pq"):
                out.to_parquet(args.write)
            elif ext == ".feather":
                out.reset_index(drop=True).to_feather(args.write)
            else:
                print("--write supports .parquet or .feather (CSV can't store dtypes)", file=sys.stderr)
                return 2
        except ImportError:
            print("writing Parquet/Feather needs pyarrow: pip install pyarrow", file=sys.stderr)
            return 2
        print(f"\nWrote {args.write} ({fmt_bytes(int(out.memory_usage(deep=True).sum()))} in memory)")
    return 0


def cmd_counters(args) -> int:
    from .hwcounters import DEFAULT_EVENTS, PerfCounters, derive, status

    st = status()
    print(f"CPU hardware counters: {'available' if st.get('available') else 'NOT available'}")
    print(f"  {st.get('reason')}")
    if st.get("paranoid") is not None:
        print(f"  kernel.perf_event_paranoid = {st['paranoid']}")
    if st.get("available"):
        pc = PerfCounters(DEFAULT_EVENTS)
        before = pc.read()
        buf = bytearray(64 * 2**20)
        for i in range(0, len(buf), 4096 * 7):  # strided writes: stress the caches a little
            buf[i] = 1
        sum(range(3_000_000))
        d = PerfCounters.delta(before, pc.read())
        pc.close()
        print("  self-test:", ", ".join(f"{k}={v}" for k, v in derive(d).items() if v is not None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="restrack", description="Real-time machine resource tracking (psutil).")
    p.add_argument("--version", action="version", version=f"restrack {__version__}")
    p.add_argument("-c", "--config", help="TOML config file (default: $RESTRACK_CONFIG, ./restrack.toml, "
                                          "~/.config/restrack/config.toml)")
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("top", help="live terminal view")
    t.add_argument("-i", "--interval", type=float, default=1.0)
    t.add_argument("-n", type=int, default=15, help="processes to show")
    t.add_argument("-s", "--sort", choices=["cpu", "mem", "io"], default="cpu")
    t.set_defaults(func=cmd_top)

    s = sub.add_parser("snapshot", help="print one reading")
    s.add_argument("-i", "--interval", type=float, default=0.5, help="measurement window for rates")
    s.add_argument("--json", action="store_true")
    s.add_argument("--pretty", action="store_true")
    s.add_argument("--top", type=int, default=None, help="keep only the top N processes by CPU")
    s.add_argument("--no-processes", action="store_true")
    s.set_defaults(func=cmd_snapshot)

    r = sub.add_parser("record", help="record samples to .csv or .jsonl")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("-i", "--interval", type=float, default=1.0)
    r.add_argument("-d", "--duration", type=float, default=None, help="seconds (default: until Ctrl+C)")
    r.add_argument("--fields", help="comma-separated CSV columns, e.g. ts,cpu.percent,memory.percent")
    r.add_argument("--processes", action="store_true", help="include process tables (JSONL)")
    r.add_argument("--top", type=int, default=None, help="with --processes: keep top N by CPU")
    r.set_defaults(func=cmd_record)

    w = sub.add_parser("web", help="Resource Monitor-style web UI (+ storage & alerts from the config)")
    w.add_argument("--host", default=None, help="default from config: 127.0.0.1")
    w.add_argument("-p", "--port", type=int, default=None, help="default from config: 8765")
    w.add_argument("-i", "--interval", type=float, default=None)
    w.add_argument("--open", action="store_true", help="open the browser")
    w.add_argument("--allow-control", action="store_true",
                   help="enable End / Suspend / Resume process from the UI")
    w.add_argument("--no-store", action="store_true", help="don't write the metrics log")
    w.set_defaults(func=cmd_web)

    rn = sub.add_parser("run", help="headless: log metrics + alerts per the config")
    rn.add_argument("-i", "--interval", type=float, default=None)
    rn.set_defaults(func=cmd_run)

    h = sub.add_parser("history", help="summarise or export the stored metrics log")
    h.add_argument("since", nargs="?", default="1h", help="how far back: 30m, 6h, 7d ... (default 1h)")
    h.add_argument("--points", type=int, default=24, help="rows to summarise into")
    h.add_argument("--csv", help="export raw rows to this CSV file instead")
    h.set_defaults(func=cmd_history)

    pr = sub.add_parser("profile", help="profile a Python script: time, memory, disk vs cache reads, "
                                         "CPU caches, hot lines -> HTML report")
    pr.usage = "restrack profile [options] SCRIPT [script args...]   (options go BEFORE the script)"
    pr.add_argument("script")
    pr.add_argument("script_args", nargs=argparse.REMAINDER, help="arguments passed to the script")
    pr.add_argument("-o", "--html", help="HTML report path (default: restrack-profile-<time>.html)")
    pr.add_argument("--json", help="also save the raw results as JSON")
    pr.add_argument("--no-html", action="store_true")
    pr.add_argument("--open", action="store_true", help="open the HTML report when done")
    pr.add_argument("--memory", action="store_true", help="trace Python allocations (exact peaks, slower)")
    pr.add_argument("--where", action="store_true", help="also find the lines holding memory (implies --memory)")
    pr.add_argument("--functions", choices=["sample", "cprofile", "off"], default="sample",
                    help="sample (default, low overhead) or cprofile (exact call counts, slower)")
    pr.add_argument("--hw", choices=["auto", "on", "off"], default="auto",
                    help="CPU cache counters (Linux perf): auto, on (explain if missing), off")
    pr.add_argument("--track-module", action="append", metavar="MODULE",
                    help="make every function of this module a section (repeatable)")
    pr.add_argument("-i", "--interval", type=float, default=0.1, help="timeline sampling period (s)")
    pr.add_argument("--sample-interval", type=float, default=0.005, help="stack sampling period (s)")
    pr.add_argument("--top", type=int, default=15, help="sections in the console summary")
    pr.set_defaults(func=cmd_profile)

    dt = sub.add_parser("dtypes", help="memory per column of a data file and safe smaller dtypes (precision-checked)")
    dt.add_argument("file", help="csv, tsv, parquet, feather, json, jsonl or xlsx")
    dt.add_argument("--rows", type=int, help="only read the first N rows")
    dt.add_argument("--write", help="write an optimised copy (.parquet or .feather)")
    dt.add_argument("--floats", choices=["safe", "all", "none"], default="safe",
                    help="float64 -> float32: only where values stay exact (safe), always, or never")
    dt.set_defaults(func=cmd_dtypes)

    hc = sub.add_parser("counters", help="check whether CPU cache counters are available here")
    hc.set_defaults(func=cmd_counters)

    cf = sub.add_parser("config", help="create or inspect the config file")
    cf.add_argument("action", choices=["init", "show", "path"])
    cf.add_argument("path", nargs="?", help="for init: where to write (default: user config dir)")
    cf.add_argument("--force", action="store_true")
    cf.set_defaults(func=cmd_config)
    return p


def _safe_streams() -> None:
    """Never crash on characters a Windows console code page can't show (e.g. cp850)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _safe_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
