"""Render a profiler result as one self-contained HTML file (no network access needed)."""
from __future__ import annotations

import html
import json
from importlib import resources
from typing import Any, Dict


def render_html(result: Dict[str, Any]) -> str:
    tpl = resources.files("restrack.web").joinpath("static", "report.html").read_text(encoding="utf-8")
    js = resources.files("restrack.web").joinpath("static", "report.js").read_text(encoding="utf-8")
    data = json.dumps(result, default=str, separators=(",", ":"))
    # keep "</script>" or "<!--" inside the data from ending the script block
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (tpl.replace("{{TITLE}}", html.escape(f"restrack profile — {result.get('name', 'run')}"))
               .replace("/*{{DATA}}*/", f"window.RESTRACK = {data};")
               .replace("/*{{SCRIPT}}*/", js))
