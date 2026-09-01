"""Server-rendered trace viewer: /-/traces (list) and /-/traces/<id>
(waterfall). Ported from demos/otel/self_storage's demo plugin, adapted to the
store schema — the list reads the derived ``traces`` summary table instead of
aggregating spans per request, and shows ``service_name`` because
multi-service is the receiver's point.

Gated by the ``otel-view`` action unless ``public_viewer: true`` — the raw
tables stay gated regardless (see permissions.py).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from datasette import Response

from . import store

SPAN_LIMIT = 5000

PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 1.5em; color: #222; }
a { color: #0b62a4; }
table.traces { border-collapse: collapse; width: 100%; }
table.traces th, table.traces td {
    text-align: left; padding: 0.3em 0.8em 0.3em 0;
    border-bottom: 1px solid #ddd; white-space: nowrap;
}
.waterfall { border-top: 1px solid #ddd; }
.waterfall details { border-bottom: 1px solid #eee; }
.waterfall summary {
    display: grid; grid-template-columns: 26em 7em 1fr;
    gap: 1em; align-items: center; padding: 2px 0;
    cursor: pointer; list-style: none;
}
.waterfall summary::-webkit-details-marker { display: none; }
.waterfall summary:hover { background: #f4f8fb; }
.span-name {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-family: ui-monospace, monospace; font-size: 0.85em;
}
.span-ms { text-align: right; font-variant-numeric: tabular-nums;
    font-size: 0.85em; color: #555; }
.lane { position: relative; height: 14px; background: #f3f3f3; }
.bar { position: absolute; top: 2px; bottom: 2px; background: #7cb5d2;
    border-radius: 2px; min-width: 2px; }
.bar.error { background: #d9634e; }
.attrs { margin: 0 0 0.6em 1em; font-size: 0.8em;
    font-family: ui-monospace, monospace; white-space: pre-wrap;
    color: #444; }
.muted { color: #777; }
.err { color: #b83a26; font-weight: 600; }
"""


def _page(title, body):
    return Response.html(
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{PAGE_CSS}</style>"
        f"<h1>{html.escape(title)}</h1>{body}"
    )


async def _check_viewer_allowed(datasette, request):
    "None when allowed; a 403 Response otherwise."
    config = datasette.plugin_config(store.PLUGIN_NAME) or {}
    if config.get("public_viewer") is True:
        return None
    if await datasette.allowed(action="otel-view", actor=request.actor):
        return None
    return Response.text(
        "Forbidden: viewing traces requires the otel-view permission "
        "(or public_viewer: true in this plugin's config)",
        status=403,
    )


async def traces_list(request, datasette):
    denied = await _check_viewer_allowed(datasette, request)
    if denied:
        return denied
    db = datasette.get_database(store.db_name(datasette))
    result = await db.execute(
        """
        select trace_id, name, service_name, span_count, error_count,
               duration_ms, start_ns
        from traces order by start_ns desc limit 100
        """
    )
    rows = []
    services = set()
    for r in result.rows:
        services.add(r["service_name"])
        started = datetime.fromtimestamp(
            (r["start_ns"] or 0) / 1e9, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        errors = (
            f"<span class='err'>{r['error_count']}</span>"
            if r["error_count"]
            else "0"
        )
        rows.append(
            "<tr>"
            f"<td><a href='/-/traces/{html.escape(r['trace_id'])}'>"
            f"{html.escape(r['name'] or '(no root span)')}</a></td>"
            f"<td>{html.escape(r['service_name'] or '')}</td>"
            f"<td>{r['span_count']}</td>"
            f"<td>{errors}</td>"
            f"<td class='span-ms'>{(r['duration_ms'] or 0):.1f} ms</td>"
            f"<td class='muted'>{started}</td>"
            "</tr>"
        )
    if not rows:
        body = "<p>No traces yet - make a request (or send some), then refresh.</p>"
    else:
        body = (
            "<table class='traces'>"
            "<tr><th>Root</th><th>Service</th><th>Spans</th><th>Errors</th>"
            "<th>Duration</th><th>Started (UTC)</th></tr>"
            + "".join(rows)
            + "</table>"
        )
    db_name = store.db_name(datasette)
    body += (
        f"<p class='muted'>Raw tables: <a href='/{db_name}/traces'>traces</a>"
        f" &middot; <a href='/{db_name}/spans'>spans</a></p>"
    )
    return _page("Traces", body)


async def trace_view(request, datasette):
    denied = await _check_viewer_allowed(datasette, request)
    if denied:
        return denied
    trace_id = request.url_vars["trace_id"]
    db = datasette.get_database(store.db_name(datasette))
    result = await db.execute(
        "select * from spans where trace_id = ? order by start_ns limit ?",
        [trace_id, SPAN_LIMIT],
    )
    spans = [dict(r) for r in result.rows]
    if not spans:
        return _page("Trace not found", "<p>No spans with this trace_id.</p>")

    t0 = min(s["start_ns"] for s in spans)
    t1 = max(s["end_ns"] for s in spans)
    total_ns = max(t1 - t0, 1)

    # Depth per span, walking parent chains iteratively: parents can be
    # missing (orphan/remote spans root at top level) and malformed data must
    # not hang the page, so a seen-set guards cycles.
    by_id = {s["span_id"]: s for s in spans}
    depths = {}
    for s in spans:
        chain = []
        current = s["span_id"]
        seen = set()
        while (
            current is not None
            and current in by_id
            and current not in depths
            and current not in seen
        ):
            seen.add(current)
            chain.append(current)
            current = by_id[current]["parent_span_id"]
        base = depths[current] + 1 if current in depths else 0
        for i, sid in enumerate(reversed(chain)):
            depths[sid] = base + i

    rows = []
    for s in spans:
        depth = depths[s["span_id"]]
        left = (s["start_ns"] - t0) / total_ns * 100
        width = (s["end_ns"] - s["start_ns"]) / total_ns * 100
        error = " error" if s["status"] == "ERROR" else ""
        indent = min(depth, 12) * 1.1
        attrs = json.loads(s["attributes"] or "{}")
        attrs_text = json.dumps(attrs, indent=2) if attrs else "(no attributes)"
        rows.append(
            "<details><summary>"
            f"<span class='span-name' style='padding-left:{indent:.1f}em'"
            f" title='{html.escape(s['name'])}'>{html.escape(s['name'])}</span>"
            f"<span class='span-ms'>{s['duration_ms']:.2f} ms</span>"
            f"<span class='lane'><span class='bar{error}'"
            f" style='left:{left:.2f}%;width:{max(width, 0.1):.2f}%'></span></span>"
            f"</summary><pre class='attrs'>{html.escape(attrs_text)}</pre>"
            "</details>"
        )

    truncated = (
        f"<p class='muted'>Showing first {SPAN_LIMIT} spans.</p>"
        if len(spans) == SPAN_LIMIT
        else ""
    )
    root = next((s for s in spans if s["parent_span_id"] is None), spans[0])
    title = root["name"]
    service = root.get("service_name")
    service_line = (
        f"<p class='muted'>service: <code>{html.escape(service)}</code></p>"
        if service
        else ""
    )
    body = (
        f"<p><a href='/-/traces'>&larr; all traces</a> &middot; "
        f"{len(spans)} spans &middot; {total_ns / 1e6:.1f} ms &middot; "
        f"<span class='muted'>{html.escape(trace_id)}</span></p>"
        f"{service_line}"
        f"{truncated}<div class='waterfall'>{''.join(rows)}</div>"
    )
    return _page(title, body)
