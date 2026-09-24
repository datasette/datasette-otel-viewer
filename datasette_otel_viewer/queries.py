"""Read-side queries behind both the page routes and the JSON API, so the
embedded page data and the API return identical shapes."""

from __future__ import annotations

import json
import re

from . import metrics_math, store
from .page_data import (
    ACCESS_READ,
    ACCESS_WRITE,
    ATTRIBUTE_SAMPLE,
    ENDPOINT_LIMIT,
    NESTING_NESTED,
    NESTING_ROOT,
    POINT_LIMIT,
    ROOT_HTTP,
    ROOT_NONE,
    ROUTE_NONE,
    SPAN_CHART_POINTS,
    SPAN_GROUP_LIMIT,
    SPAN_LIMIT,
    SPAN_SORT_COLUMNS,
    SQL_QUERY_LIMIT,
    SQL_TEXT_LIMIT,
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
    SpanChartPoint,
    SpanFilters,
    SpanGroupRow,
    SpanListQuery,
    SpanListResponse,
    SpanListRow,
    SpanRow,
    SpansQuery,
    SpansResponse,
    SqlFilters,
    SqlQueriesQuery,
    SqlQueriesResponse,
    SqlQueryRow,
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
    assert column is not None  # the model validator defaults sort_desc
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
        -- Aggregate first, then join -- see span_groups; `grouped` also
        -- feeds `pct` the group sizes it ranks against.
        grouped as materialized (
          select h.http_method as http_method, h.http_route as http_route,
                 count(*) as request_count,
                 sum(case when h.http_status >= 500 or h.error_count > 0
                          then 1 else 0 end) as error_count,
                 max(h.duration_ms) as max_ms, max(h.start_ns) as last_seen_ns,
                 count(h.duration_ms) as n_timed
          from http h
          group by h.http_method, h.http_route
        ),
        {_percentile_ctes("http", "http_method, http_route")}
        select g.http_method as method, g.http_route as route,
               g.request_count, g.error_count, g.max_ms, g.last_seen_ns,
               p.p50 as p50_ms, p.p95 as p95_ms
        from grouped g
        left join pct p
          on p.http_method is g.http_method and p.http_route is g.http_route
        order by g.request_count desc limit ?
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
                scope=r["root_scope"],
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


# A span is SQL work if it carries query text or a callback name, rather than
# because of its span name: Datasette records `datasette.callback` in place of
# `db.query.text` for execute_fn()-style calls, and both are one `db.query`.
_SPAN_CALLBACK = "json_extract(s.attributes, '$.\"datasette.callback\"')"
_SPAN_ROWS = "json_extract(s.attributes, '$.\"datasette.rows_returned\"')"

# Statements that went through the write path. Datasette gives a write its
# own `db.write.queue_wait`/`db.write.execute` child spans, which is a better
# signal than reading the SQL: it catches `execute_write_fn()` callbacks,
# which carry no SQL text at all, and it does not mistake a `WITH ... select`
# read for a write. `datasette.operation` (read|write) would be the direct
# answer, but the registry declares it without setting it on any span.
# Correlated, not a joined subquery: `left join (select distinct
# parent_span_id ...) w on w.span_id = s.span_id` is the same answer, but
# SQLite materialises that subquery and then nested-loops it once per span
# without building an index -- 100k spans x 23k write children, 20+ seconds,
# past every sql_time_limit_ms there is. As an `exists` it is an index probe
# per span (idx_spans_parent_name): that scan drops from 20.8s to 30ms, and
# the page as a whole from "never" to ~700ms. The index is not optional --
# without it this same `exists` is a table scan per span and the page is
# back over 60 seconds.
_IS_WRITE_CHILD = """
  exists (
    select 1 from spans c
    where c.parent_span_id = s.span_id
      and c.name in ('db.write.execute', 'db.write.queue_wait')
  )"""
# ...with the statement keyword as a fallback, for a write whose child spans
# were pruned or never arrived.
_WRITE_KEYWORDS = (
    "'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER', "
    "'VACUUM', 'BEGIN', 'COMMIT'"
)
# coalesce, not a bare column: db_operation is NULL for every callback, and
# `upper(NULL) in (...)` is NULL rather than false -- which would drop
# callbacks out of *both* sides of the read/write filter.
_IS_WRITE = (
    f"({_IS_WRITE_CHILD} or upper(coalesce(s.db_operation, '')) in ({_WRITE_KEYWORDS}))"
)

# The statement a span ran, and the identity rows are grouped by: text when
# there is text, else the callback's name.
_SQL_SPANS = f"""
  select s.span_id, s.trace_id, s.start_ns, s.duration_ms, s.status,
         s.service_name, s.db_namespace, s.db_operation, s.db_query_text,
         {_SPAN_CALLBACK} as callback,
         coalesce(s.db_query_text, {_SPAN_CALLBACK}) as statement,
         {_SPAN_ROWS} as rows_returned,
         {_IS_WRITE} as is_write
  from spans s
  where (s.db_query_text is not null or {_SPAN_CALLBACK} is not null)
  {{extra_where}}
"""


def _sql_where(filters: SqlFilters) -> tuple[str, list]:
    "The ``and ...`` tail for _SQL_SPANS, from one SqlFilters."
    clauses: list[str] = []
    params: list = []
    if filters.service:
        clauses.append("s.service_name = ?")
        params.append(filters.service)
    if filters.sql:
        # The needle matches the callback name too, so filtering by "plants"
        # doesn't silently hide the callbacks that touched it.
        clauses.append(
            f"(s.db_query_text like ? escape '\\' "
            f"or {_SPAN_CALLBACK} like ? escape '\\')"
        )
        params += [_like_param(filters.sql)] * 2
    if filters.database:
        clauses.append("s.db_namespace = ?")
        params.append(filters.database)
    if filters.operation:
        clauses.append("s.db_operation = ?")
        params.append(filters.operation)
    if filters.access == ACCESS_WRITE:
        clauses.append(_IS_WRITE)
    elif filters.access == ACCESS_READ:
        clauses.append(f"not {_IS_WRITE}")
    if filters.min_duration_ms is not None:
        clauses.append("s.duration_ms >= ?")
        params.append(filters.min_duration_ms)
    if not clauses:
        return "", params
    return "and " + " and ".join(clauses), params


async def _sql_facet(datasette, query: SqlQueriesQuery, column: str) -> list[str]:
    """Distinct values of one span column under every filter *except* the one
    that column drives -- the options you could switch to."""
    db = datasette.get_database(store.db_name(datasette))
    field = {"db_namespace": "database", "db_operation": "operation"}[column]
    where, params = _sql_where(query.model_copy(update={field: None}))
    result = await db.execute(
        f"select distinct {column} from ({_SQL_SPANS.format(extra_where=where)}) "
        f"where {column} is not null order by 1 limit 100",
        params,
    )
    return [r[column] for r in result.rows]


async def sql_queries(datasette, query: SqlQueriesQuery) -> SqlQueriesResponse:
    """The SQL summary behind ``/-/otel/sql``: one row per statement, ordered
    by the time it accounts for in total.

    Total time first rather than the single slowest run, because a 2ms query
    run ten thousand times costs more than a 400ms one run twice -- and every
    other column is sortable in the browser (the whole list arrives in one
    response, see lib/sort.ts).

    Percentiles are exact, nearest-rank over the stored durations; the window
    definitions are split for the same reason as in http_endpoints.
    """
    db = datasette.get_database(store.db_name(datasette))
    where, params = _sql_where(query)
    spans = _SQL_SPANS.format(extra_where=where)
    totals = (
        await db.execute(
            f"select count(*) as run_count, sum(duration_ms) as total_ms "
            f"from ({spans})",
            params,
        )
    ).first()
    result = await db.execute(
        f"""
        with sql_spans as ({spans}),
        -- Aggregate first, then join -- see span_groups. It matters more
        -- here: the join key is the statement *text*, so joining before the
        -- group by compares full SQL strings once per run, not once per row.
        grouped as materialized (
          select q.db_namespace as db_namespace,
                 max(q.db_operation) as operation,
                 q.statement as statement,
                 max(q.db_query_text is null) as is_callback,
                 max(q.is_write) as is_write,
                 max(q.callback) as callback,
                 count(*) as run_count,
                 sum(q.status = 'ERROR') as error_count,
                 sum(q.duration_ms) as total_ms, max(q.duration_ms) as max_ms,
                 max(q.rows_returned) as max_rows,
                 max(q.start_ns) as last_seen_ns,
                 count(q.duration_ms) as n_timed
          from sql_spans q
          group by q.db_namespace, q.statement
        ),
        {_percentile_ctes("sql_spans", "db_namespace, statement", slowest=True)}
        select g.db_namespace as database, g.operation, g.statement,
               g.is_callback, g.is_write,
               g.callback, g.run_count, g.error_count, g.total_ms, g.max_ms,
               g.max_rows, g.last_seen_ns,
               p.p50 as p50_ms, p.p95 as p95_ms,
               p.slowest_trace_id, p.slowest_span_id
        from grouped g
        left join pct p
          on p.db_namespace is g.db_namespace and p.statement is g.statement
        order by g.total_ms desc limit ?
        """,
        params + [SQL_QUERY_LIMIT + 1],
    )
    rows = list(result.rows)
    queries = [
        SqlQueryRow(
            query=(r["statement"] or "")[:SQL_TEXT_LIMIT],
            text_truncated=len(r["statement"] or "") > SQL_TEXT_LIMIT,
            callback=r["callback"] if r["is_callback"] else None,
            database=r["database"],
            operation=r["operation"],
            is_write=bool(r["is_write"]),
            run_count=r["run_count"],
            error_count=r["error_count"] or 0,
            total_ms=r["total_ms"],
            p50_ms=r["p50_ms"],
            p95_ms=r["p95_ms"],
            max_ms=r["max_ms"],
            max_rows=r["max_rows"],
            last_seen_ns=r["last_seen_ns"],
            slowest_trace_id=r["slowest_trace_id"],
            slowest_span_id=r["slowest_span_id"],
        )
        for r in rows[:SQL_QUERY_LIMIT]
    ]
    return SqlQueriesResponse(
        queries=queries,
        query=query,
        databases=await _sql_facet(datasette, query, "db_namespace"),
        operations=await _sql_facet(datasette, query, "db_operation"),
        services=await list_services(datasette),
        run_count=totals["run_count"] or 0,
        total_ms=totals["total_ms"],
        truncated=len(rows) > SQL_QUERY_LIMIT,
    )


def _percentile_ctes(
    source: str, partition: str, *, grouped: str = "grouped", slowest: bool = False
) -> str:
    """``ranked``/``pct`` CTEs over ``source``, giving nearest-rank p50 and
    p95 per group. Every summary page reads its percentiles this way: the
    durations are all in the store, so there is nothing to estimate.

    ``grouped`` names a CTE the caller has already defined -- one row per
    group, holding the ``partition`` columns under their own names plus
    ``n_timed`` (``count(duration_ms)``, so nulls are excluded exactly as the
    ranking excludes them). Reading the group size from there rather than
    from a second ``count(*) over`` window is the whole trick: ``grouped``
    scans every row anyway, so the size is free, and ``ranked`` is left with
    a single window and a single sort. It also lets ``pct`` throw away all
    but the three rows per group it actually reads -- the two percentile
    boundaries and the last -- before it aggregates.

    A percentile is the *first* row whose rank reaches the threshold, and
    rank rises with duration, so that boundary row is the answer: ``rn >= k*n
    and rn - 1 < k*n``. The float comparison is spelled the same on both
    sides of that test as in the ``min(case ...)`` it replaced, so no group
    can land on a different row through rounding.

    ``slowest`` adds the slowest span's ids to the same ``pct`` row. Ranking
    by duration *ascending* puts the slowest row last, so it is the one where
    ``rn = n_timed``. The secondary ``span_id desc`` only breaks ties, and
    does it so that the last of a tied run is the lowest span_id -- the same
    span a separate ``order by duration_ms desc, span_id`` pass would pick.

    One group differs from that older pass: one whose durations are *all*
    null. The ranking runs over ``duration_ms is not null``, so such a group
    now reports no slowest span rather than the lowest span_id among its
    untimed rows. Its ``max_ms`` is null either way, so the cell the id
    would have linked from is empty -- and every span this instance records
    is timed (``selfsource.span_to_row`` always derives duration_ms)."""
    columns = [column.strip() for column in partition.split(",")]
    on = " and ".join(f"g.{column} is r.{column}" for column in columns)
    keys = ", ".join(f"r.{column}" for column in columns)
    order = "order by duration_ms, span_id desc" if slowest else "order by duration_ms"
    carry = ", trace_id, span_id" if slowest else ""
    pick = (
        """,
             max(case when r.rn = g.n_timed then r.trace_id end)
               as slowest_trace_id,
             max(case when r.rn = g.n_timed then r.span_id end)
               as slowest_span_id"""
        if slowest
        else ""
    )
    return f"""
    ranked as (
      select {partition}, duration_ms{carry},
             row_number() over (partition by {partition} {order}) as rn
      from {source} where duration_ms is not null
    ),
    pct as (
      select {keys},
             min(case when r.rn >= 0.5 * g.n_timed then r.duration_ms end) as p50,
             min(case when r.rn >= 0.95 * g.n_timed then r.duration_ms end)
               as p95{pick}
      from ranked r join {grouped} g on {on}
      where r.rn = g.n_timed
         or (r.rn >= 0.5 * g.n_timed and r.rn - 1 < 0.5 * g.n_timed)
         or (r.rn >= 0.95 * g.n_timed and r.rn - 1 < 0.95 * g.n_timed)
      group by {keys}
    )"""


# Any span, with the pieces the catalogue groups and filters by. `split_value`
# is the split_by attribute's value, or NULL when not splitting -- bound as a
# parameter and reduced to a JSON path, never concatenated raw (the key is
# also pattern-checked in page_data.SpanFilters).
_SPAN_GROUPS_SQL = """
  select s.rowid as rowid,
         s.span_id, s.trace_id, s.name, s.kind, s.scope_name, s.service_name,
         s.parent_span_id, s.start_ns, s.duration_ms, s.status,
         case when ?1 is null then null
              else json_extract(s.attributes, '$."' || ?1 || '"') end
           as split_value
  from spans s
  {where}
"""


def _span_where(filters: SpanFilters) -> tuple[str, list]:
    "``(where clause, params)`` for a SpanFilters, over _SPAN_GROUPS_SQL."
    clauses: list[str] = []
    params: list = []
    if filters.service:
        clauses.append("s.service_name = ?")
        params.append(filters.service)
    if filters.scope:
        clauses.append("s.scope_name = ?")
        params.append(filters.scope)
    if filters.name:
        clauses.append("s.name like ? escape '\\'")
        params.append(_like_param(filters.name))
    if filters.kind:
        clauses.append("upper(s.kind) = ?")
        params.append(filters.kind.upper())
    if filters.nesting == NESTING_ROOT:
        clauses.append("s.parent_span_id is null")
    elif filters.nesting == NESTING_NESTED:
        clauses.append("s.parent_span_id is not null")
    if filters.min_duration_ms is not None:
        clauses.append("s.duration_ms >= ?")
        params.append(filters.min_duration_ms)
    if not clauses:
        return "", params
    return "where " + " and ".join(clauses), params


def _span_source(query: SpansQuery) -> tuple[str, list]:
    """The matching-spans SQL and its params. ``split_by`` is parameter 1
    throughout (``?1``), so the rest keep their order whether or not a split
    is on."""
    where, params = _span_where(query)
    return _SPAN_GROUPS_SQL.format(where=where), [query.split_by] + params


async def span_attribute_keys(datasette, query: SpansQuery) -> list[str]:
    """Attribute keys on the newest matching spans: what ``split_by`` can be
    set to. Bounded by ATTRIBUTE_SAMPLE spans because attributes are JSON --
    every key here is one a row in view actually carries.

    "Newest" is by rowid, not start_ns: spans arrive in batches in roughly
    start order, so insertion order picks the same sample, and there is no
    index on start_ns -- ordering by it sorted the whole table (100k rows,
    ~100ms) on every load of /-/otel/spans to take 500 rows."""
    db = datasette.get_database(store.db_name(datasette))
    source, params = _span_source(query.model_copy(update={"split_by": None}))
    result = await db.execute(
        f"""
        select distinct j.key from (
          select span_id, attributes from spans s
          where s.rowid in (select rowid from ({source})
                            order by rowid desc limit ?)
        ) p, json_each(p.attributes) j
        order by j.key limit 200
        """,
        params + [ATTRIBUTE_SAMPLE],
    )
    return [r["key"] for r in result.rows]


async def _span_facet(datasette, query: SpansQuery, column: str) -> list[str]:
    "Distinct values of one column under every filter except that column's."
    db = datasette.get_database(store.db_name(datasette))
    field = {"scope_name": "scope", "kind": "kind"}[column]
    source, params = _span_source(
        query.model_copy(update={field: None, "split_by": None})
    )
    result = await db.execute(
        f"select distinct {column} from ({source}) "
        f"where {column} is not null order by 1 limit 100",
        params,
    )
    return [r[column] for r in result.rows]


async def span_groups(datasette, query: SpansQuery) -> SpansResponse:
    """The span catalogue behind ``/-/otel/spans``: every kind of work this
    instance records, grouped by instrumentation scope and span name and
    ordered by the time it accounts for.

    Scope is half the key on purpose. It is the library that created the span,
    so a plugin's spans (``datasette_cron.run``, scope ``datasette_cron``)
    group together and apart from Datasette's own -- without this plugin
    knowing any of their names. ``split_by`` breaks one row into one per value
    of an attribute, which is how that run becomes one row per cron task.
    """
    db = datasette.get_database(store.db_name(datasette))
    source, params = _span_source(query)
    totals = (
        await db.execute(
            f"select count(*) as span_count, sum(duration_ms) as total_ms "
            f"from ({source})",
            params,
        )
    ).first()
    partition = "name, scope_name, split_value"
    result = await db.execute(
        f"""
        with matching as ({source}),
        -- Aggregate first, then join. `pct` holds one row per group, so
        -- joining it to `grouped` is a handful of rows; joining it to
        -- `matching` -- every span -- and aggregating afterwards is the same
        -- answer for ~100x the work. `grouped` also feeds `pct` its group
        -- sizes, which is why it is materialized and comes first.
        grouped as materialized (
          select m.name as name, m.scope_name as scope_name,
                 max(m.kind) as kind, m.split_value as split_value,
                 count(*) as span_count,
                 count(distinct m.trace_id) as trace_count,
                 sum(m.status = 'ERROR') as error_count,
                 sum(m.duration_ms) as total_ms, max(m.duration_ms) as max_ms,
                 max(m.start_ns) as last_seen_ns,
                 count(m.duration_ms) as n_timed
          from matching m
          group by m.name, m.scope_name, m.split_value
        ),
        {_percentile_ctes("matching", partition, slowest=True)}
        select g.name, g.scope_name as scope, g.kind, g.split_value,
               g.span_count, g.trace_count, g.error_count, g.total_ms,
               g.max_ms, g.last_seen_ns,
               p.p50 as p50_ms, p.p95 as p95_ms,
               p.slowest_trace_id, p.slowest_span_id
        from grouped g
        left join pct p
          on p.name is g.name and p.scope_name is g.scope_name
         and p.split_value is g.split_value
        order by g.total_ms desc limit ?
        """,
        params + [SPAN_GROUP_LIMIT + 1],
    )
    rows = list(result.rows)
    spans = [
        SpanGroupRow(
            name=r["name"],
            scope=r["scope"],
            kind=r["kind"],
            split_value=None if r["split_value"] is None else str(r["split_value"]),
            span_count=r["span_count"],
            trace_count=r["trace_count"],
            error_count=r["error_count"] or 0,
            total_ms=r["total_ms"],
            p50_ms=r["p50_ms"],
            p95_ms=r["p95_ms"],
            max_ms=r["max_ms"],
            last_seen_ns=r["last_seen_ns"],
            slowest_trace_id=r["slowest_trace_id"],
            slowest_span_id=r["slowest_span_id"],
        )
        for r in rows[:SPAN_GROUP_LIMIT]
    ]
    return SpansResponse(
        spans=spans,
        query=query,
        scopes=await _span_facet(datasette, query, "scope_name"),
        kinds=await _span_facet(datasette, query, "kind"),
        services=await list_services(datasette),
        attribute_keys=await span_attribute_keys(datasette, query),
        span_count=totals["span_count"] or 0,
        total_ms=totals["total_ms"],
        truncated=len(rows) > SPAN_GROUP_LIMIT,
    )


# What each sortable span column means in SQL, over the _SPAN_GROUPS_SQL
# select. Same contract as TRACE_ORDER_BY: the API validates against
# SPAN_SORT_COLUMNS and this holds the SQL behind each name.
SPAN_ORDER_BY = {
    "name": "name",
    "scope": "scope_name",
    "duration_ms": "duration_ms",
    "start_ns": "start_ns",
    "status": "status",
}
assert set(SPAN_ORDER_BY) == set(SPAN_SORT_COLUMNS)


def _span_order_clause(query: SpanListQuery) -> str:
    "``order by`` for one SpanListQuery, nulls last and always a total order."
    column = query.sort or query.sort_desc
    assert column is not None  # the model validator defaults sort_desc
    direction = "asc" if query.sort else "desc"
    clause = f"{SPAN_ORDER_BY[column]} {direction} nulls last"
    if column != "start_ns":
        clause += ", start_ns desc"
    return clause + ", span_id"


def _span_list_where(query: SpanListQuery) -> tuple[str, list]:
    """The catalogue's own filters plus the exact keys that pin one of its
    rows: a drill-through shows the spans that row counted, no more."""
    where, params = _span_where(query)
    clauses = [where[len("where ") :]] if where else []
    if query.name_exact:
        clauses.append("s.name = ?")
        params.append(query.name_exact)
    if query.statement:
        # The SQL page's rows are keyed on the text, or on the callback name
        # when there is no text (see sql_queries).
        clauses.append(f"coalesce(s.db_query_text, {_SPAN_CALLBACK}) = ?")
        params.append(query.statement)
    if query.split_by and query.split_value is not None:
        clauses.append("json_extract(s.attributes, '$.\"' || ?1 || '\"') = ?")
        params.append(query.split_value)
    if not clauses:
        return "", params
    return "where " + " and ".join(clauses), params


async def _span_chart(
    db, source: str, params: list, query: SpanListQuery, total: int
) -> tuple[list[SpanChartPoint], int]:
    """Every matching span as a (time, duration) dot, sampled to at most
    SPAN_CHART_POINTS of them.

    The sample is `rowid % stride`, not a window function or a limit: a limit
    would draw the first slice of the time range and call it the whole cloud,
    and a window means sorting every matching span a third time (the count and
    the page already scan them once each). Rowid order is insertion order,
    which is roughly start order -- the same assumption span_attribute_keys
    samples on -- so every stretch of the range keeps its share of the dots.
    The pinned span is drawn whether or not the sample caught it: it is the
    reason for coming here.
    """
    stride = max(1, -(-total // SPAN_CHART_POINTS))
    result = await db.execute(
        f"""
        select span_id, trace_id, start_ns, duration_ms, status
        from ({source})
        where start_ns is not null and duration_ms is not null
          and (rowid % ? = 0 or span_id = ?)
        order by start_ns
        """,
        params + [stride, query.highlight or ""],
    )
    points = [
        SpanChartPoint(
            span_id=r["span_id"],
            trace_id=r["trace_id"],
            start_ns=r["start_ns"],
            duration_ms=r["duration_ms"],
            status=r["status"],
        )
        for r in result.rows
    ]
    return points, stride


async def span_list(datasette, query: SpanListQuery) -> SpanListResponse:
    """The spans behind one catalogue row, newest-slowest first.

    Paged like the trace list (offset behind an opaque cursor) rather than
    capped: a busy `db.query` row can be thousands of spans, and the point of
    opening it is to look through them.
    """
    db = datasette.get_database(store.db_name(datasette))
    where, params = _span_list_where(query)
    source = _SPAN_GROUPS_SQL.format(where=where)
    params = [query.split_by] + params
    order = _span_order_clause(query)
    total = (
        await db.execute(f"select count(*) from ({source})", params)
    ).single_value()
    if query.highlight and query.next is None:
        # Arriving with a span to pin and no cursor of your own: open on the
        # page that holds it. Its rank is one window over the matching spans
        # -- the same set the page query sorts anyway -- and the answer is
        # written back onto `next`, so paging, the URL and the Previous
        # button all see an ordinary offset from here on.
        # .first(), not .single_value(): a pin that matches nothing (filters
        # changed under it, or the span aged out) returns no row at all.
        found = (
            await db.execute(
                f"""
                with matching as ({source}),
                ranked as (
                  select span_id, row_number() over (order by {order}) as rn
                  from matching
                )
                select rn from ranked where span_id = ?
                """,
                params + [query.highlight],
            )
        ).first()
        if found is not None:
            query.next = str((found["rn"] - 1) // query.size * query.size)
    result = await db.execute(
        f"""
        with matching as ({source})
        select m.*, t.name as trace_root_name
        from matching m
        left join traces t on t.trace_id = m.trace_id
        order by {order} limit ? offset ?
        """,
        # One row past the page, as in list_traces: its presence is the
        # "is there a next page?" test.
        params + [query.size + 1, query.offset],
    )
    rows = list(result.rows)
    spans = [
        SpanListRow(
            span_id=r["span_id"],
            trace_id=r["trace_id"],
            name=r["name"],
            scope=r["scope_name"],
            kind=r["kind"],
            parent_span_id=r["parent_span_id"],
            start_ns=r["start_ns"],
            duration_ms=r["duration_ms"],
            status=r["status"],
            status_description=None,
            split_value=(None if r["split_value"] is None else str(r["split_value"])),
            # The trace's root span name, readable: for an HTTP root that is a
            # route pattern, and a regex is not a useful label.
            trace_label=(
                pretty_route(r["trace_root_name"]) if r["trace_root_name"] else None
            ),
        )
        for r in rows[: query.size]
    ]
    has_more = len(rows) > query.size
    chart, stride = await _span_chart(db, source, params, query, total)
    return SpanListResponse(
        spans=spans,
        query=query,
        next=str(query.offset + query.size) if has_more else None,
        total=total,
        chart=chart,
        chart_stride=stride,
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
