"""Stdlib-only HTTP server for the Resource Monitor-style frontend.

Endpoints
---------
GET  /                         the UI
GET  /api/info                 static system info + server options
GET  /api/snapshot             latest full snapshot
GET  /api/history?n=60         light snapshots for back-filling graphs
GET  /api/stream               Server-Sent Events, one ``snapshot`` event per sample
GET  /api/process/<pid>        on-demand process details (open files, connections, cmdline)
POST /api/process/<pid>/<act>  terminate | kill | suspend | resume  (only with allow_control)

With a Store (long-term log) attached:
GET  /api/storage                       db path, first/last timestamp, row count
GET  /api/range?start=&end=&points=     downsampled series for the History graphs
GET  /api/top?ts=&window=               top processes around a moment
GET  /api/events?start=&end=            alerts / start / stop events
GET  /api/export.csv?start=&end=        raw stored rows as CSV
"""
from __future__ import annotations

import io
import json
import logging
import time
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import psutil

from ..collector import process_detail
from ..monitor import Monitor
from ..storage import Store

log = logging.getLogger(__name__)

_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ACTIONS = {"terminate", "kill", "suspend", "resume"}
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")


def _static_dir():
    return resources.files("restrack.web").joinpath("static")


class ResmonServer:
    """Serve the UI for a :class:`Monitor`.

    Binds to localhost by default: snapshots expose process names, users and
    connections, so think before using ``host="0.0.0.0"``. Process control
    (end/suspend/resume) is off unless ``allow_control=True``.
    """

    def __init__(self, monitor: Optional[Monitor] = None, host: str = "127.0.0.1", port: int = 8765,
                 allow_control: bool = False, interval: float = 1.0, store: Optional[Store] = None,
                 config: Optional[dict] = None) -> None:
        self.monitor = monitor or Monitor(interval=interval)
        self.store = store
        self.config = config
        self.allow_control = allow_control
        self._closing = threading.Event()
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "::", "127.0.0.1") else self.host
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        self.monitor.start()
        try:
            self.httpd.serve_forever(poll_interval=0.25)
        finally:
            self.close()

    def start(self) -> "ResmonServer":
        """Serve on a background thread (handy in notebooks and tests)."""
        self.monitor.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="restrack-web", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        self.monitor.stop()
        if self._thread:  # serve_forever() running on another thread
            self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> "ResmonServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def _make_handler(app: ResmonServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "restrack"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            log.debug("%s - " + fmt, self.address_string(), *args)

        # helpers ------------------------------------------------------- #
        def _host_ok(self) -> bool:
            # DNS-rebinding guard: when bound to loopback only accept loopback Host headers
            if app.host not in ("127.0.0.1", "::1"):
                return True
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in _LOOPBACK

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def _error(self, status: int, msg: str) -> None:
            self._json({"error": msg}, status)

        # GET ----------------------------------------------------------- #
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "bad host")
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/info":
                info = dict(app.monitor.collector.info)
                info.update(interval=app.monitor.interval, allow_control=app.allow_control,
                            storage=app.store is not None,
                            config_source=(app.config or {}).get("_source"),
                            alerts={k: v for k, v in ((app.config or {}).get("alerts") or {}).items()})
                return self._json(info)
            if path.startswith(("/api/storage", "/api/range", "/api/top", "/api/events", "/api/export.csv")):
                return self._storage(path, parse_qs(url.query))
            if path == "/api/snapshot":
                snap = app.monitor.latest
                return self._json(snap) if snap else self._error(503, "no sample yet")
            if path == "/api/history":
                q = parse_qs(url.query)
                n = int(q.get("n", ["60"])[0])
                return self._json(app.monitor.history(max(1, min(n, 3600))))
            if path == "/api/stream":
                return self._stream()
            m = re.fullmatch(r"/api/process/(\d+)", path)
            if m:
                try:
                    return self._json(process_detail(int(m.group(1))))
                except psutil.NoSuchProcess:
                    return self._error(404, "no such process")
                except psutil.AccessDenied:
                    return self._error(403, "access denied")
            return self._error(404, "not found")

        do_HEAD = do_GET

        def _storage(self, path: str, q: dict) -> None:
            st = app.store
            if st is None:
                return self._error(404, "storage disabled (enable [storage] in the config)")

            def num(name, default):
                try:
                    return float(q[name][0])
                except (KeyError, ValueError, IndexError):
                    return default

            now = time.time()
            start, end = num("start", now - 3600), num("end", now)
            if path == "/api/storage":
                return self._json(st.bounds())
            if path == "/api/range":
                pts = int(max(10, min(num("points", 600), 5000)))
                return self._json(st.query(start, end, pts))
            if path == "/api/top":
                return self._json(st.top_processes_at(num("ts", now), num("window", 60), int(num("n", 10))))
            if path == "/api/events":
                return self._json(st.events(start, end))
            if path == "/api/export.csv":
                buf = io.StringIO()
                st.export_csv(start, end, buf)
                name = time.strftime("restrack-%Y%m%d-%H%M.csv", time.localtime(start))
                return self._send(200, buf.getvalue().encode(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            return self._error(404, "not found")

        def _static(self, name: str) -> None:
            if not re.fullmatch(r"[\w.-]+", name):
                return self._error(404, "not found")
            res = _static_dir().joinpath(name)
            if not res.is_file():
                return self._error(404, "not found")
            ctype = _MIME.get(os.path.splitext(name)[1], "application/octet-stream")
            self._send(200, res.read_bytes(), ctype)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            seq = app.monitor.seq - 1 if app.monitor.latest else 0  # send the latest immediately
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while not app._closing.is_set():
                    item = app.monitor.wait(seq, timeout=10.0)
                    if item is None:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    seq, snap = item
                    data = json.dumps(snap, separators=(",", ":"))
                    self.wfile.write(f"id: {seq}\nevent: snapshot\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        # POST ---------------------------------------------------------- #
        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "bad host")
            m = re.fullmatch(r"/api/process/(\d+)/(\w+)", urlparse(self.path).path)
            if not m:
                return self._error(404, "not found")
            if not app.allow_control:
                return self._error(403, "process control is disabled (start with --allow-control)")
            # A custom header forces a CORS preflight, which we never grant, so other
            # websites cannot drive this endpoint from the user's browser.
            if self.headers.get("X-Restrack-Control") != "1":
                return self._error(403, "missing X-Restrack-Control header")
            pid, action = int(m.group(1)), m.group(2)
            if action not in _ACTIONS:
                return self._error(400, f"unknown action {action!r}")
            if pid <= 1 or pid == os.getpid():
                return self._error(400, "refusing to act on this process")
            try:
                proc = psutil.Process(pid)
                getattr(proc, action)()
            except psutil.NoSuchProcess:
                return self._error(404, "no such process")
            except psutil.AccessDenied:
                return self._error(403, "access denied (insufficient privileges)")
            log.info("restrack: %s pid %s", action, pid)
            return self._json({"ok": True, "pid": pid, "action": action})

    return Handler


def serve(config: Optional[dict] = None, **overrides) -> None:
    """Run monitor + storage + alerts + web UI from a config dict until Ctrl+C.

    ``overrides`` (host, port, interval, allow_control, open_browser) win over the config.
    """
    from ..config import load_config
    from ..storage import Tracker

    cfg = config or load_config()
    w = cfg["web"]
    for k in ("host", "port", "allow_control", "open_browser"):
        if overrides.get(k) is not None:
            w[k] = overrides[k]
    if overrides.get("interval"):
        cfg["monitor"]["interval"] = overrides["interval"]
    tracker = Tracker(cfg)
    srv = ResmonServer(monitor=tracker.monitor, host=w["host"], port=w["port"],
                       allow_control=w["allow_control"], store=tracker.store, config=cfg)
    open_browser = w["open_browser"]
    tracker.start()
    print(f"restrack Resource Monitor running at {srv.url}  (Ctrl+C to stop)")
    if tracker.store:
        print(f"  metrics log: {tracker.store.path}")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, webbrowser.open, args=(srv.url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        tracker.stop()
