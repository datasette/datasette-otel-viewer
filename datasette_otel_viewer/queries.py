"""Read-side queries behind both the page routes and the JSON API, so the
embedded page data and the API return identical shapes."""

from __future__ import annotations

import json

from . import metrics_math, store
from .page_data import (
    POINT_LIMIT,
    SPAN_LIMIT,
    MetricsListQuery,
    MetricsQuery,
    MetricsQueryResponse,
    MetricSummaryRow,
    Series,
    SeriesPoint,
    SpanRow,
    TraceDetail,
    TraceRow,
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


async def store_summary(datasette) -> dict:
    """Row counts for the four store tables plus the union of service names
    seen in traces and metrics; feeds the /-/otel landing page."""
    db = datasette.get_database(store.db_name(datasette))
    counts = (
        await db.execute(
            "select "
            "(select count(*) from traces) as trace_count, "
            "(select count(*) from spans) as span_count, "
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
