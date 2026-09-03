"""Seed plugin for `just shots`: deterministic, self-mode-shaped rows written
straight through the store at startup.

Loaded with `datasette --plugins-dir scripts/shots_plugins` by
frontend/scripts/screenshots.mjs; never installed, never imported by the
package. The rows are built by hand against `store.COLUMNS`,
`store.METRIC_COLUMNS` and `store.METRIC_POINT_COLUMNS` and mirror what
`selfsource.span_to_row` / `selfmetrics.sdk_metrics_to_rows` would produce —
one service, `datasette`, because that is all a self-viewer ever sees.

Everything is a pure function of SHOTS_NOW (the same instant the harness pins
the browser clock to), so two runs write byte-identical rows.
"""

import json
import math
import os
from datetime import datetime

from datasette import hookimpl

from datasette_otel_viewer import store

NOW = datetime.fromisoformat(
    os.environ.get("SHOTS_NOW", "2026-09-01T12:00:00+00:00").replace("Z", "+00:00")
)
NOW_NS = int(NOW.timestamp() * 1_000_000_000)

SERVICE = "datasette"
RESOURCE = {"service.name": SERVICE, "service.version": "1.0a39"}
RESOURCE_JSON = json.dumps(RESOURCE)
SCOPE_NAME = "screenshots"
SCOPE_VERSION = "0"


# JS Math.round semantics (half away from zero for positives): the seed data
# was ported from screenshots.mjs and Python's banker's rounding would move
# the odd value.
def jsround(value: float) -> int:
    return math.floor(value + 0.5)


# ---------------------------------------------------------------------------
# Traces. `start_ms` is "milliseconds before NOW" (bigger = earlier), `dur_ms`
# is the span's duration. Span ids come from a counter so the ids in the
# committed screenshots stay put: the slow query in trace A is 0x1003.
_next_span = [0x1000]


def span(
    trace,
    name,
    start_ms,
    dur_ms,
    *,
    parent=None,
    kind="INTERNAL",
    attrs=None,
    error=None,
):
    attrs = attrs or {}
    span_id = format(_next_span[0], "016x")
    _next_span[0] += 1
    start_ns = NOW_NS - jsround(start_ms * 1e6)
    return {
        "trace_id": trace,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "kind": kind,
        "start_ns": start_ns,
        "end_ns": start_ns + jsround(dur_ms * 1e6),
        "status": "ERROR" if error else "UNSET",
        "status_description": error,
        "service_name": SERVICE,
        "http_route": attrs.get("http.route"),
        "http_status": attrs.get("http.response.status_code"),
        "db_namespace": attrs.get("db.namespace"),
        "db_operation": attrs.get("db.operation.name"),
        "db_query_text": attrs.get("db.query.text"),
        "attributes": json.dumps(attrs),
        "resource": RESOURCE_JSON,
        "scope_name": SCOPE_NAME,
        "scope_version": SCOPE_VERSION,
    }


def sql_attrs(namespace, operation, text):
    return {
        "db.system.name": "sqlite",
        "db.namespace": namespace,
        "db.operation.name": operation,
        "db.query.text": text,
    }


def table_page_trace():
    "Trace A: a table page — the one the trace.png waterfall shows."
    trace = format(0xA1, "032x")
    root = span(
        trace,
        "GET /{database}/{table}",
        45_000,
        38.4,
        kind="SERVER",
        attrs={
            "http.request.method": "GET",
            "url.path": "/demo/plants",
            "http.route": "/{database}/{table}",
            "http.response.status_code": 200,
        },
    )
    view = span(
        trace,
        "datasette.view.table",
        44_998.8,
        35.1,
        parent=root["span_id"],
        attrs={"datasette.database": "demo", "datasette.table": "plants"},
    )
    count = span(
        trace,
        "db.query",
        44_996,
        4.2,
        parent=view["span_id"],
        kind="CLIENT",
        attrs=sql_attrs("demo", "SELECT", "select count(*) from [plants]"),
    )
    # 0x1003: screenshots.mjs deep-links the inspector to this span.
    rows = span(
        trace,
        "db.query",
        44_991,
        21.7,
        parent=view["span_id"],
        kind="CLIENT",
        attrs=sql_attrs(
            "demo",
            "SELECT",
            "select id, name, height_cm from [plants] order by id limit 101",
        ),
    )
    facet = span(
        trace,
        "db.query",
        44_968,
        6.3,
        parent=view["span_id"],
        kind="CLIENT",
        attrs=sql_attrs(
            "demo",
            "SELECT",
            "select height_cm as value, count(*) as count from [plants] "
            "group by height_cm order by count desc limit 31",
        ),
    )
    render = span(
        trace,
        "datasette.render_template",
        44_963,
        2.9,
        parent=root["span_id"],
        attrs={"datasette.template": "table.html"},
    )
    return [root, view, count, rows, facet, render]


def json_api_trace():
    "Trace B: the same table as JSON — a short two-span request."
    trace = format(0xB2, "032x")
    root = span(
        trace,
        "GET /{database}/{table}.json",
        190_000,
        12.0,
        kind="SERVER",
        attrs={
            "http.request.method": "GET",
            "url.path": "/demo/plants.json",
            "http.route": "/{database}/{table}.json",
            "http.response.status_code": 200,
        },
    )
    query = span(
        trace,
        "db.query",
        189_997,
        8.4,
        parent=root["span_id"],
        kind="CLIENT",
        attrs=sql_attrs(
            "demo",
            "SELECT",
            "select id, name, height_cm from [plants] order by id limit 101",
        ),
    )
    return [root, query]


def bad_query_trace():
    "Trace C: arbitrary SQL against a typo'd table — the errored row."
    trace = format(0xC3, "032x")
    message = "no such table: plnts"
    root = span(
        trace,
        "POST /{database}/-/query",
        610_000,
        5.6,
        kind="SERVER",
        attrs={
            "http.request.method": "POST",
            "url.path": "/demo/-/query",
            "http.route": "/{database}/-/query",
            "http.response.status_code": 500,
        },
        error=message,
    )
    query = span(
        trace,
        "db.query",
        609_997,
        1.9,
        parent=root["span_id"],
        kind="CLIENT",
        attrs=sql_attrs("demo", "SELECT", "select * from plnts"),
        error=message,
    )
    return [root, query]


def startup_trace():
    "Trace D: a non-HTTP root, so the list's name fallback is visible."
    trace = format(0xD4, "032x")
    root = span(trace, "datasette.startup", 900_000, 46.2)
    plugins = span(
        trace,
        "datasette.startup.plugins",
        899_998,
        31.5,
        parent=root["span_id"],
        attrs={"datasette.plugin_count": 7},
    )
    pragma = span(
        trace,
        "db.query",
        899_960,
        3.1,
        parent=root["span_id"],
        kind="CLIENT",
        attrs=sql_attrs("demo", "PRAGMA", "PRAGMA journal_mode"),
    )
    return [root, plugins, pragma]


def trace_rows():
    # Trace A first: its span ids must stay 0x1000-0x1005.
    return [
        *table_page_trace(),
        *json_api_trace(),
        *bad_query_trace(),
        *startup_trace(),
    ]


# ---------------------------------------------------------------------------
# Metrics: a gauge (two attribute sets), a cumulative monotonic sum (two
# routes) and a histogram with Datasette-like duration buckets. Points land
# every 30s over the last hour: 30s is the bucket width the "1h" range picker
# uses by default (metricsMath.ts stepForRange(3600) === 30), so every bucket
# in the query window has data and no chart shows a gap from mismatched
# cadence. Values come from fixed sin/cos formulas (no PRNG).
DURATION_BOUNDS = [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10]
METRICS_STEP_S = 30
# 3600 … 0 seconds before NOW, i.e. the last point lands exactly at NOW.
METRICS_STEPS = [3600 - i * METRICS_STEP_S for i in range(121)]
START_NS = NOW_NS - METRICS_STEPS[0] * 1_000_000_000


def point(name, attrs, time_ns, **kw):
    return {
        "metric_name": name,
        "service_name": SERVICE,
        "scope_name": SCOPE_NAME,
        "time_ns": time_ns,
        "attributes": json.dumps(attrs),
        "resource": RESOURCE_JSON,
        **kw,
    }


def metric_time_ns(seconds_before):
    return NOW_NS - seconds_before * 1_000_000_000


# Two attribute sets (db.namespace) on one gauge; phase offsets the wave so
# the two lines are visibly distinct rather than overlapping.
def gauge_points(name, namespace, phase):
    return [
        point(
            name,
            {"db.namespace": namespace},
            metric_time_ns(s),
            value_int=max(0, jsround(4 + 3 * math.sin(i * 0.21 + phase))),
        )
        for i, s in enumerate(METRICS_STEPS)
    ]


# A monotonic cumulative counter: non-negative increments accumulate, so the
# per-second rate view (the metric detail page's default for cumulative sums)
# traces a smooth wave rather than a flat line.
def sum_points(name, attrs, phase):
    rows = []
    cumulative = 0
    for i, s in enumerate(METRICS_STEPS):
        cumulative += max(0, jsround(3 + 2 * math.sin(i * 0.17 + phase)))
        rows.append(
            point(
                name,
                attrs,
                metric_time_ns(s),
                start_ns=START_NS,
                value_int=cumulative,
            )
        )
    return rows


# A cumulative histogram: per-bucket counts must be individually
# non-decreasing over time (the server differences consecutive points), so
# accumulate non-negative per-interval deltas whose peak bucket drifts over
# time — a diagonal band in the heatmap instead of a static one.
def histogram_points(name):
    n_buckets = len(DURATION_BOUNDS) + 1
    bounds_json = json.dumps(DURATION_BOUNDS)
    cumulative = [0] * n_buckets
    total_count = 0
    total_sum = 0.0
    rows = []
    for i, s in enumerate(METRICS_STEPS):
        for k in range(n_buckets):
            delta = max(0, jsround(4 + 3 * math.cos((i - k * 7) * 0.13)))
            lower = 0 if k == 0 else DURATION_BOUNDS[k - 1]
            upper = DURATION_BOUNDS[k] if k < len(DURATION_BOUNDS) else lower * 2
            cumulative[k] += delta
            total_count += delta
            total_sum += delta * ((lower + upper) / 2)
        rows.append(
            point(
                name,
                {
                    "db.system": "sqlite",
                    "db.namespace": "demo",
                    "datasette.operation": "read",
                },
                metric_time_ns(s),
                start_ns=START_NS,
                count=total_count,
                sum=total_sum,
                bucket_counts=json.dumps(list(cumulative)),
                explicit_bounds=bounds_json,
            )
        )
    return rows


def metric_rows():
    gauge = "datasette.sql.threads.queue_depth"
    counter = "http.server.request.count"
    histogram = "db.client.operation.duration"
    metrics = [
        {
            "name": gauge,
            "description": "Read queries waiting for a free worker thread",
            "unit": "{query}",
            "type": "gauge",
            # Gauges carry no temporality in the SDK, so neither does the row.
            "temporality": None,
            "monotonic": None,
        },
        {
            "name": counter,
            "description": "HTTP requests served",
            "unit": "{request}",
            "type": "sum",
            "temporality": "cumulative",
            "monotonic": 1,
        },
        {
            "name": histogram,
            "description": "Duration of a SQL operation issued by Datasette",
            "unit": "s",
            "type": "histogram",
            "temporality": "cumulative",
            "monotonic": None,
        },
    ]
    points = [
        *gauge_points(gauge, "demo", 0),
        *gauge_points(gauge, "otel", math.pi / 2),
        *sum_points(counter, {"http.route": "/{database}/{table}"}, 0),
        *sum_points(counter, {"http.route": "/{database}/{table}.json"}, math.pi),
        *histogram_points(histogram),
    ]
    return metrics, points


# ---------------------------------------------------------------------------
@hookimpl
def startup(datasette):
    async def inner():
        # pluggy gives no ordering guarantee against the package's own
        # startup hook, and ensure_db is idempotent — so call it here too.
        await store.ensure_db(datasette)
        await store.insert_spans(datasette, trace_rows())
        metrics, points = metric_rows()
        await store.insert_metrics(datasette, metrics, points)

    return inner
