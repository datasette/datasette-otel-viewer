"""Read-side queries behind both the page routes and the JSON API, so the
embedded page data and the API return identical shapes."""

from __future__ import annotations

import json

from . import store
from .page_data import (
    SPAN_LIMIT,
    SpanRow,
    TraceDetail,
    TraceRow,
    TracesQuery,
)


def http_label(name: str | None, attributes: dict) -> tuple[str, str | None]:
    """``(label, route)`` for a root span. HTTP roots are *named* after the
    low-cardinality route pattern (semconv); the concrete path lives in
    ``url.path``. Show "<method> <path>" and keep the pattern as ``route``;
    non-HTTP roots and ingested foreign spans keep their name."""
    url_path = attributes.get("url.path")
    if url_path:
        method = attributes.get("http.request.method")
        label = " ".join(part for part in (method, url_path) if part)
        return label, name
    return name or "(no root span)", None


async def list_traces(datasette, query: TracesQuery) -> list[TraceRow]:
    db = datasette.get_database(store.db_name(datasette))
    where = ""
    params: list = []
    if query.service:
        where = "where t.service_name = ?"
        params.append(query.service)
    params.append(query.limit)
    # root_span_id is a primary-key probe into spans: display-time lookup
    # of the two url fields rather than promoting them into the traces
    # table keeps the v1 schema contract untouched.
    result = await db.execute(
        f"""
        select t.trace_id, t.name, t.service_name, t.span_count,
               t.error_count, t.duration_ms, t.start_ns, t.http_status,
               t.status,
               json_extract(r.attributes, '$."url.path"') as url_path,
               json_extract(r.attributes, '$."http.request.method"')
                 as http_method
        from traces t
        left join spans r on r.span_id = t.root_span_id
        {where}
        order by t.start_ns desc limit ?
        """,
        params,
    )
    rows = []
    for r in result.rows:
        attrs = {}
        if r["url_path"]:
            attrs = {
                "url.path": r["url_path"],
                "http.request.method": r["http_method"],
            }
        label, _ = http_label(r["name"], attrs)
        rows.append(
            TraceRow(
                trace_id=r["trace_id"],
                name=r["name"],
                label=label,
                service_name=r["service_name"],
                span_count=r["span_count"] or 0,
                error_count=r["error_count"] or 0,
                duration_ms=r["duration_ms"],
                start_ns=r["start_ns"],
                http_status=r["http_status"],
                status=r["status"],
            )
        )
    return rows


async def list_services(datasette) -> list[str]:
    db = datasette.get_database(store.db_name(datasette))
    result = await db.execute(
        "select distinct service_name from traces "
        "where service_name is not null order by service_name limit 200"
    )
    return [r["service_name"] for r in result.rows]


def _row_to_span(r) -> SpanRow:
    d = dict(r)
    d["attributes"] = json.loads(d.get("attributes") or "{}")
    d["resource"] = json.loads(d.get("resource") or "{}")
    return SpanRow(**d)


async def get_trace(datasette, trace_id: str) -> TraceDetail | None:
    db_name = store.db_name(datasette)
    db = datasette.get_database(db_name)
    result = await db.execute(
        "select * from spans where trace_id = ? order by start_ns limit ?",
        [trace_id, SPAN_LIMIT],
    )
    spans = [_row_to_span(r) for r in result.rows]
    if not spans:
        return None
    root = next((s for s in spans if s.parent_span_id is None), spans[0])
    title, route = http_label(root.name, root.attributes)
    return TraceDetail(
        trace_id=trace_id,
        title=title,
        route=route,
        service_name=root.service_name,
        spans=spans,
        truncated=len(spans) == SPAN_LIMIT,
        database=db_name,
    )
