"""Read-side queries behind both the page routes and the JSON API, so the
embedded page data and the API return identical shapes."""

from __future__ import annotations

import json
import re

from . import metrics_math, store
from .page_data import (
    ENDPOINT_LIMIT,
    POINT_LIMIT,
    ROOT_HTTP,
    ROOT_NONE,
    ROUTE_NONE,
    SPAN_LIMIT,
    TRACE_SORT_COLUMNS,
    EndpointRow,
    EndpointsQuery,
    EndpointsResponse,
    MetricsListQuery,
    MetricsQuery,
    MetricsQueryResponse,
    MetricSummaryRow,
    Series,
    SeriesPoint,
    SpanRow,
    TraceDetail,
    TraceRootKind,
    TraceRow,
    TracesListResponse,
    TracesQuery,
)


def http_label(name: str | None, attributes: dict) -> tuple[str, str | None]:
    """``(label, route)`` for a root span. HTTP roots are *named* after the
    low-cardinality route pattern (semconv); the concrete path lives in
    ``url.path``. Show "<method> <path>" and keep the pattern as ``route``;
    non-HTTP roots (startup, background work) keep their name."""
    url_path = attributes.get("url.path")
    if url_path:
        method = attributes.get("http.request.method")
        label = " ".join(part for part in (method, url_path) if part)
        return label, name
    return name or "(no root span)", None


# What each sortable column name means in SQL, over the ``matching`` CTE in
# list_traces. Sorting by "label" orders by the URL path (or the root span
# name for non-HTTP roots): the method prefix the label carries is not
# something anyone wants to sort on.
TRACE_ORDER_BY = {
    "label": "coalesce(url_path, name)",
    "service_name": "service_name",
    "http_status": "http_status",
    "span_count": "span_count",
    "error_count": "error_count",
    "duration_ms": "duration_ms",
    "start_ns": "start_ns",
    "status": "status",
}
# The allowlist the API validates against and the SQL behind it are two halves
# of one contract: drifting apart would 400 a sortable column or, worse, order
# by the wrong one.
assert set(TRACE_ORDER_BY) == set(TRACE_SORT_COLUMNS)


def _order_clause(query: TracesQuery) -> str:
    """``order by`` for one TracesQuery. Nulls sort last in both directions (a
    trace with no HTTP status shouldn't lead the ascending sort), and every
    sort ends in a total order so paging can't repeat or skip a row."""
    column = query.sort or query.sort_desc
    direction = "asc" if query.sort else "desc"
    clause = f"{TRACE_ORDER_BY[column]} {direction} nulls last"
    if column != "start_ns":
        clause += ", start_ns desc"
    return clause + ", trace_id"


# `url.path` on the root span is what makes a trace an HTTP one, here and in
# http_label: the two must agree or a row would land in a bucket whose label
# it does not wear.
_ROOT_URL_PATH = "json_extract(r.attributes, '$.\"url.path\"')"
_ROOT_METHOD = "json_extract(r.attributes, '$.\"http.request.method\"')"

# traces joined to their root span. The trace list, the root-kind facet and
# the HTTP endpoint summary all read from this, so a trace is filtered and
# classified the same way whichever page asked.
_MATCHING_SQL = f"""
  select t.trace_id, t.name, t.service_name, t.span_count, t.error_count,
         t.duration_ms, t.start_ns, t.http_status, t.status, t.http_route,
         r.scope_name as root_scope,
         {_ROOT_URL_PATH} as url_path,
         {_ROOT_METHOD} as http_method
  from traces t
  left join spans r on r.span_id = t.root_span_id
  {{where}}
"""


def _like_param(value: str) -> str:
    "``%value%`` with LIKE's own wildcards escaped, for use with ESCAPE '\\'."
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(filters, *, root: str | None = None, http_only: bool = False):
    """``(where clause, params)`` for a TraceFilters (or anything extending
    it). ``root`` is passed separately rather than read off the model because
    the root facet counts the buckets *without* the root filter applied, and
    the endpoint summary has no root filter at all -- it passes
    ``http_only``, which is the same restriction by another name."""
    clauses: list[str] = []
    params: list = []
    if filters.service:
        clauses.append("t.service_name = ?")
        params.append(filters.service)
    if http_only:
        clauses.append(f"{_ROOT_URL_PATH} is not null")
    if root:
        if root == ROOT_HTTP:
            clauses.append(f"{_ROOT_URL_PATH} is not null")
        elif root == ROOT_NONE:
            clauses.append("t.name is null")
        else:
            # A named root is non-HTTP by construction: an HTTP root's name is
            # its route pattern, and those live in the ROOT_HTTP bucket.
            clauses.append(f"t.name = ? and {_ROOT_URL_PATH} is null")
            params.append(root)
    if filters.path:
        clauses.append(f"{_ROOT_URL_PATH} like ? escape '\\'")
        params.append(_like_param(filters.path))
    if filters.route:
        if filters.route == ROUTE_NONE:
            clauses.append(f"t.http_route is null and {_ROOT_URL_PATH} is not null")
        else:
            clauses.append("t.http_route = ?")
            params.append(filters.route)
    if filters.method:
        clauses.append(f"{_ROOT_METHOD} = ?")
        params.append(filters.method)
    if filters.status:
        low, high = filters.status_range
        clauses.append("t.http_status >= ? and t.http_status < ?")
        params += [low, high]
    if filters.min_duration_ms is not None:
        clauses.append("t.duration_ms >= ?")
        params.append(filters.min_duration_ms)
    if not clauses:
        return "", params
    return "where " + " and ".join(clauses), params


# Datasette names its routes with regexes: `/(?P<database>[^\/\.]+)/...`.
# Named groups are the readable part, so keep those and drop the rest.
_ROUTE_GROUP = re.compile(r"\(\?P<(\w+)>[^)]*\)")
_OPTIONAL_GROUP = re.compile(r"\(([^()]*)\)\?")


def pretty_route(route: str | None) -> str:
    """A route pattern as something you would recognise in a URL bar:
    ``/(?P<database>[^\\/\\.]+)/(?P<table>[^\\/\\.]+)(\\.(?P<format>\\w+))?$``
    reads as ``/{database}/{table}[.{format}]``. Display only -- the raw
    pattern stays on the row as the drill-through key and the tooltip."""
    if not route:
        return "(no route)"
    text = _ROUTE_GROUP.sub(r"{\1}", route)
    for old, new in (("\\/", "/"), ("\\.", "."), ("^", ""), ("$", "")):
        text = text.replace(old, new)
    # What is left of an optional group -- almost always the trailing
    # ``(.{format})?`` -- reads better in brackets than in regex.
    return _OPTIONAL_GROUP.sub(r"[\1]", text)


async def http_endpoints(datasette, query: EndpointsQuery) -> EndpointsResponse:
    """The HTTP endpoint summary behind ``/-/otel/http``: one row per
    (method, matched route), commonest first.

    Percentiles are nearest-rank, read straight off the stored durations with
    a window function -- every request is in the store, so there is no reason
    to estimate from buckets the way the metrics pages must. p50 is the
    smallest duration whose rank reaches half the group; SQLite has no
    percentile function, and ordering by duration makes that first qualifying
    row the answer.
    """
    db = datasette.get_database(store.db_name(datasette))
    where, params = _where(query, http_only=True)
    matching = _MATCHING_SQL.format(where=where)
    request_count = (
        await db.execute(f"select count(*) from ({matching})", params)
    ).single_value()
    result = await db.execute(
        f"""
        with http as ({matching}),
        ranked as (
          select http_method, http_route, duration_ms,
                 row_number() over ranked_w as rn, count(*) over whole_w as n
          from http where duration_ms is not null
          -- Two windows on purpose: an ORDER BY window frames rows up to the
          -- current one, so count(*) over the *ordered* window would be a
          -- running count -- every row would look like the last one and every
          -- percentile would collapse onto the minimum.
          window ranked_w as (
                   partition by http_method, http_route order by duration_ms
                 ),
                 whole_w as (partition by http_method, http_route)
        ),
        pct as (
          select http_method, http_route,
                 min(case when rn >= 0.5 * n then duration_ms end) as p50,
                 min(case when rn >= 0.95 * n then duration_ms end) as p95
          from ranked group by http_method, http_route
        )
        select h.http_method as method, h.http_route as route,
               count(*) as request_count,
               sum(case when h.http_status >= 500 or h.error_count > 0
                        then 1 else 0 end) as error_count,
               max(h.duration_ms) as max_ms, max(h.start_ns) as last_seen_ns,
               max(p.p50) as p50_ms, max(p.p95) as p95_ms
        from http h
        left join pct p
          on p.http_method is h.http_method and p.http_route is h.http_route
        group by h.http_method, h.http_route
        order by request_count desc limit ?
        """,
        params + [ENDPOINT_LIMIT + 1],
    )
    rows = list(result.rows)
    endpoints = [
        EndpointRow(
            method=r["method"],
            route=r["route"],
            label=" ".join(
                part for part in (r["method"], pretty_route(r["route"])) if part
            ),
            request_count=r["request_count"],
            error_count=r["error_count"] or 0,
            p50_ms=r["p50_ms"],
            p95_ms=r["p95_ms"],
            max_ms=r["max_ms"],
            last_seen_ns=r["last_seen_ns"],
        )
        for r in rows[:ENDPOINT_LIMIT]
    ]
    return EndpointsResponse(
        endpoints=endpoints,
        query=query,
        methods=await http_methods(datasette, query),
        services=await list_services(datasette),
        request_count=request_count,
        truncated=len(rows) > ENDPOINT_LIMIT,
    )


async def http_methods(datasette, query: EndpointsQuery) -> list[str]:
    "Methods to offer in the filter: every one the *other* filters leave."
    db = datasette.get_database(store.db_name(datasette))
    where, params = _where(query.model_copy(update={"method": None}), http_only=True)
    result = await db.execute(
        f"select distinct http_method from ({_MATCHING_SQL.format(where=where)}) "
        "where http_method is not null order by 1 limit 50",
        params,
    )
    return [r["http_method"] for r in result.rows]


async def root_kinds(datasette, query: TracesQuery) -> list[TraceRootKind]:
    """The root-filter buckets present in the store: one for HTTP traces,
    one per distinct non-HTTP root span name. HTTP first, then grouped by
    the scope that emitted the root span (so a plugin's roots sit together),
    commonest first within a scope."""
    db = datasette.get_database(store.db_name(datasette))
    where, params = _where(query)
    result = await db.execute(
        f"""
        select case when url_path is not null then '{ROOT_HTTP}'
                    when name is null then '{ROOT_NONE}'
                    else name end as key,
               root_scope, count(*) as count
        from ({_MATCHING_SQL.format(where=where)})
        group by key, root_scope
        order by count desc limit 200
        """,
        params,
    )
    kinds = [
        TraceRootKind(
            key=r["key"],
            label={ROOT_HTTP: "HTTP requests", ROOT_NONE: "(no root span)"}.get(
                r["key"], r["key"]
            ),
            # The HTTP bucket spans every scope that serves requests; naming
            # one of them would be a lie.
            scope=None if r["key"] == ROOT_HTTP else r["root_scope"],
            count=r["count"],
        )
        for r in result.rows
    ]
    return sorted(kinds, key=lambda k: (k.key != ROOT_HTTP, k.scope or "", -k.count))


async def list_traces(datasette, query: TracesQuery) -> TracesListResponse:
    """One page of the traces list: **one row per trace**, summarised by its
    root span.

    Ordering is done here rather than in the browser so it means what it
    says: "slowest first" is the slowest of every stored trace, not of
    whichever page happened to be loaded.

    Paging is by offset (``next`` is an offset in disguise) rather than
    keyset. Keyset would need a null-aware cursor per sortable column for
    columns that are routinely null (duration_ms, http_status); against a
    ring buffer of at most a few tens of thousands of traces, an offset scan
    is not worth that.
    """
    db = datasette.get_database(store.db_name(datasette))
    where, params = _where(query, root=query.root)
    # root_span_id is a primary-key probe into spans: display-time lookup of
    # the two url fields rather than promoting them into the traces table
    # keeps the v1 schema contract untouched.
    matching = _MATCHING_SQL.format(where=where)
    total = (
        await db.execute(f"select count(*) from ({matching})", params)
    ).single_value()
    result = await db.execute(
        f"""
        with matching as ({matching})
        select * from matching
        order by {_order_clause(query)} limit ? offset ?
        """,
        # One row past the page: its presence is the whole "is there a next
        # page?" test, and it never reaches the client.
        params + [query.size + 1, query.offset],
    )
    rows = []
    for r in list(result.rows)[: query.size]:
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
    has_more = len(result.rows) > query.size
    return TracesListResponse(
        traces=rows,
        query=query,
        next=str(query.offset + query.size) if has_more else None,
        total=total,
        root_kinds=await root_kinds(datasette, query),
        route_label=pretty_route(query.route) if query.route else None,
    )


async def list_services(datasette) -> list[str]:
    db = datasette.get_database(store.db_name(datasette))
    result = await db.execute(
        "select distinct service_name from traces "
        "where service_name is not null order by service_name limit 200"
    )
    return [r["service_name"] for r in result.rows]


async def store_summary(datasette) -> dict:
    """Row counts for the four store tables plus the union of service names
    seen in traces and metrics; feeds the /-/otel landing page."""
    db = datasette.get_database(store.db_name(datasette))
    counts = (
        await db.execute(
            "select "
            "(select count(*) from traces) as trace_count, "
            "(select count(*) from spans) as span_count, "
            # The same test /-/otel/http counts by, so the landing card and
            # that page never disagree.
            "(select count(*) from traces t left join spans r "
            "  on r.span_id = t.root_span_id "
            f" where {_ROOT_URL_PATH} is not null) as http_request_count, "
            "(select count(*) from metrics) as metric_count, "
            "(select count(*) from metric_points) as metric_point_count"
        )
    ).first()
    services = await db.execute(
        "select service_name from traces where service_name is not null "
        "union select service_name from metric_points "
        "where service_name is not null order by 1 limit 200"
    )
    return {
        **dict(counts),
        "services": [r["service_name"] for r in services.rows],
    }


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


async def list_metrics(datasette, query: MetricsListQuery) -> list[MetricSummaryRow]:
    "The metric catalogue: definition plus point stats. Zero-point metrics stay."
    db = datasette.get_database(store.db_name(datasette))
    where = ""
    params: list = []
    if query.service:
        # Turns the left join into an inner one on purpose: filtering by
        # service means "metrics this service reports", not all metrics.
        where = "where p.service_name = ?"
        params.append(query.service)
    result = await db.execute(
        f"""
        select m.name, m.description, m.unit, m.type, m.temporality, m.monotonic,
               max(p.time_ns) as last_seen_ns, count(p.id) as point_count,
               json_group_array(distinct p.service_name) as services
        from metrics m left join metric_points p on p.metric_name = m.name
        {where}
        group by m.name order by m.name limit 500
        """,
        params,
    )
    return [
        MetricSummaryRow(
            name=r["name"],
            description=r["description"],
            unit=r["unit"],
            type=r["type"],
            temporality=r["temporality"],
            monotonic=None if r["monotonic"] is None else bool(r["monotonic"]),
            last_seen_ns=r["last_seen_ns"],
            point_count=r["point_count"],
            services=sorted(s for s in json.loads(r["services"]) if s),
        )
        for r in result.rows
    ]


async def get_metric(datasette, name: str) -> MetricSummaryRow | None:
    rows = await list_metrics(datasette, MetricsListQuery())
    return next((m for m in rows if m.name == name), None)


async def metric_attribute_keys(datasette, name: str) -> list[str]:
    "Distinct attribute keys seen on the newest 1000 points of a metric."
    db = datasette.get_database(store.db_name(datasette))
    result = await db.execute(
        """
        select distinct j.key from (
          select attributes from metric_points where metric_name = ?
          order by time_ns desc limit 1000
        ) p, json_each(p.attributes) j order by j.key
        """,
        [name],
    )
    return [r["key"] for r in result.rows]


def _number_value(point: dict) -> float | None:
    value = point["value_double"]
    if value is None:
        value = point["value_int"]
    return None if value is None else float(value)


def _histogram(point: dict) -> dict | None:
    bounds = json.loads(point["explicit_bounds"] or "null")
    counts = json.loads(point["bucket_counts"] or "null")
    if bounds is None or counts is None:
        return None
    return {
        "count": point["count"] or 0,
        "sum": point["sum"],
        "min": point["min"],
        "max": point["max"],
        "bucket_counts": [int(c) for c in counts],
        "explicit_bounds": [float(b) for b in bounds],
    }


def _merge(histograms: list[dict]) -> dict | None:
    """merge_histograms, but tolerant of a series whose bounds changed
    mid-range: only the entries agreeing with the newest layout survive."""
    if not histograms:
        return None
    bounds = histograms[-1]["explicit_bounds"]
    return metrics_math.merge_histograms(
        h for h in histograms if h["explicit_bounds"] == bounds
    )


def _reduce_number_series(points: list[dict], step_ns: int) -> dict[int, float]:
    "One value per bucket: the last observation in it (gauge and sum alike)."
    reduced = {}
    for bucket, point in metrics_math.last_per_bucket(points, step_ns).items():
        value = _number_value(point)
        if value is not None:
            reduced[bucket] = value
    return reduced


def _reduce_histogram_series(
    points: list[dict], step_ns: int, cumulative: bool
) -> dict[int, dict]:
    """One per-interval histogram per bucket. Cumulative points are
    differenced against their predecessor first (counter resets and bounds
    changes fall back to the raw point), then summed within the bucket;
    delta points are already per-interval and only get summed."""
    per_bucket: dict[int, list[dict]] = {}
    previous = None
    for point in points:
        histogram = _histogram(point)
        if histogram is None:
            continue
        interval = histogram
        if cumulative:
            interval = metrics_math.histogram_delta(previous, histogram)
            previous = histogram
        bucket = metrics_math.bucket_start(point["time_ns"], step_ns)
        per_bucket.setdefault(bucket, []).append(interval)
    merged = {}
    for bucket, histograms in per_bucket.items():
        one = _merge(histograms)
        if one is not None:
            merged[bucket] = one
    return merged


def _reduce_other_series(points: list[dict], step_ns: int) -> dict[int, dict]:
    "Exponential histograms and summaries: count/sum of the last point only."
    return {
        bucket: {"count": point["count"], "sum": point["sum"]}
        for bucket, point in metrics_math.last_per_bucket(points, step_ns).items()
    }


async def query_metric(datasette, q: MetricsQuery) -> MetricsQueryResponse | None:
    """Bucketed series for one metric. Bucketing happens here rather than in
    SQL because the reduction is per-series and stateful (last-wins for
    numbers, cumulative differencing for histograms)."""
    metric = await get_metric(datasette, q.name)
    if metric is None:
        return None
    db = datasette.get_database(store.db_name(datasette))
    step_ns = q.step_s * metrics_math.NS
    where = ""
    params: list = [
        q.name,
        metrics_math.bucket_start(q.since_ns, step_ns),
        q.until_ns,
    ]
    if q.service:
        where = "and service_name = ?"
        params.append(q.service)
    result = await db.execute(
        f"""
        select service_name, time_ns, attributes, value_double, value_int,
               count, sum, min, max, bucket_counts, explicit_bounds
        from metric_points
        where metric_name = ? and time_ns >= ? and time_ns < ? {where}
        order by time_ns limit ?
        """,
        params + [POINT_LIMIT],
    )
    rows = [dict(r) for r in result.rows]
    truncated = len(rows) == POINT_LIMIT

    # 1. Native OTel series: identity is (service, full attribute set).
    native: dict[str, dict] = {}
    for row in rows:
        attributes = json.loads(row["attributes"] or "{}")
        key = metrics_math.series_key(row["service_name"], attributes, None)
        native.setdefault(
            key,
            {
                "service_name": row["service_name"],
                "attributes": attributes,
                "points": [],
            },
        )["points"].append(row)

    is_histogram = metric.type == "histogram"
    is_number = metric.type in ("gauge", "sum")
    cumulative = metric.temporality == "cumulative"

    # 2. Reduce each native series to one value/histogram per bucket, then
    #    regroup the natives under the requested group_by.
    groups: dict[str, dict] = {}
    for entry in native.values():
        attributes = entry["attributes"]
        kept = (
            attributes
            if q.group_by is None
            else {k: attributes[k] for k in q.group_by if k in attributes}
        )
        key = metrics_math.series_key(entry["service_name"], attributes, q.group_by)
        group = groups.setdefault(
            key,
            {
                "service_name": entry["service_name"],
                "attributes": kept,
                "buckets": {},
            },
        )
        if is_histogram:
            reduced = _reduce_histogram_series(entry["points"], step_ns, cumulative)
        elif is_number:
            reduced = _reduce_number_series(entry["points"], step_ns)
        else:
            reduced = _reduce_other_series(entry["points"], step_ns)
        for bucket, value in reduced.items():
            group["buckets"].setdefault(bucket, []).append(value)

    # 3. Aggregate the natives sharing a bucket, then read percentiles off
    #    the merged histogram counts.
    how = "sum" if metric.type == "sum" else "avg"
    series: list[Series] = []
    all_bounds: set[tuple[float, ...]] = set()
    for key, group in sorted(groups.items()):
        points: list[SeriesPoint] = []
        series_bounds: list[float] | None = None
        for bucket in sorted(group["buckets"]):
            values = group["buckets"][bucket]
            if is_histogram:
                merged = _merge(values)
                if merged is None:
                    continue
                series_bounds = merged["explicit_bounds"]
                all_bounds.add(tuple(series_bounds))
                points.append(
                    SeriesPoint(
                        t=bucket,
                        count=merged["count"],
                        sum=merged["sum"],
                        bucket_counts=merged["bucket_counts"],
                        percentiles={
                            str(p): metrics_math.histogram_percentile(
                                series_bounds,
                                merged["bucket_counts"],
                                p,
                                merged.get("min"),
                                merged.get("max"),
                            )
                            for p in q.percentiles
                        },
                    )
                )
            elif is_number:
                points.append(
                    SeriesPoint(
                        t=bucket, value=metrics_math.aggregate_numbers(values, how)
                    )
                )
            else:
                points.append(
                    SeriesPoint(
                        t=bucket,
                        count=sum(v["count"] or 0 for v in values),
                        sum=sum(v["sum"] or 0 for v in values),
                    )
                )
        series.append(
            Series(
                key=key,
                service_name=group["service_name"],
                attributes=group["attributes"],
                explicit_bounds=series_bounds,
                points=points,
            )
        )
    return MetricsQueryResponse(
        metric=metric,
        since_ns=q.since_ns,
        until_ns=q.until_ns,
        step_s=q.step_s,
        explicit_bounds=list(next(iter(all_bounds))) if len(all_bounds) == 1 else None,
        series=series,
        truncated=truncated,
    )
