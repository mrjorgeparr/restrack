# Development

## Setup

```bash
git clone https://github.com/mrjorgeparr/restrack
cd restrack
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[test,docs]" pyarrow
pytest                  # run from the repo root, or `cd tests && pytest` to test an installed wheel
```

An editable install serves the web UI files straight from the source tree, so after
editing `restrack/web/static/*` you only need to reload the browser.

## Layout

```text
restrack/
  collector.py        psutil sampling -> snapshot dict (rates from counter deltas)
  monitor.py          background thread, light history, subscribe / wait / stream
  recorder.py         CSV / JSONL output, flatten()
  storage.py          SQLite Store, Alerts, logging setup, Tracker
  config.py           TOML loading, defaults, template
  gpu.py              optional NVIDIA metrics (NVML)
  profiler.py         Profiler: sections, timeline, stack sampling, tracemalloc, report data
  hwcounters.py       CPU cache counters via Linux perf_event_open (ctypes)
  dataframes.py       pandas memory report, precision-checked dtype suggestions, optimize()
  report.py           renders a profile as one self-contained HTML file
  cli.py              `restrack` command
  web/server.py       stdlib HTTP server, SSE, JSON API
  web/static/         index.html, style.css, app.js (live UI); report.html, report.js (profile report)
tests/                pytest suite
docs/                 this documentation (MkDocs)
```

Design rules:

- **The only runtime dependency is psutil.** Extras are optional and must degrade
  gracefully when missing.
- **Snapshots are plain dicts that serialise to JSON.** New fields are fine; renaming
  or removing fields breaks users' scripts and stored CSVs.
- **New stored metrics** go in `storage.COLUMNS`. Existing databases gain the column
  automatically on the next start.
- **The frontend has no build step and no CDN.** It must work offline and under the
  server's Content-Security-Policy (no inline scripts).

## Documentation

```bash
mkdocs serve        # live preview on http://127.0.0.1:8000
mkdocs build        # static site in ./site
```

Pushing to `main` publishes the site to GitHub Pages
(`.github/workflows/docs.yml`). In the repository settings, set **Pages → Source** to
**GitHub Actions** once.

## Releasing to PyPI

Releases use [Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no API
token is stored anywhere.

One-time setup on pypi.org: go to **Account → Publishing → Add a pending publisher**
and enter:

| Field | Value |
|---|---|
| PyPI project name | `restrack` |
| Owner | `mrjorgeparr` |
| Repository | `restrack` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

For each release:

1. Bump the version in **both** `pyproject.toml` and `restrack/__init__.py`.
2. Commit and push.
3. Create a GitHub release with a tag such as `v0.1.1`.
4. `.github/workflows/publish.yml` runs the tests, builds, checks the metadata with
   `twine check --strict` and uploads.

`.github/workflows/tests.yml` runs the suite on Linux, Windows and macOS (Python 3.9 and
3.13) on every push and pull request. Watch it on the first push: the Windows and macOS
paths can't be exercised from a Linux machine.

A version number can never be uploaded twice to PyPI, even after deleting it, so always
bump before releasing.
