"""Pydantic contracts shared by the page routes, the JSON API and the
frontend.

Two type pipelines read these (see CLAUDE.md "Type safety"):

- ``__exports__`` models are dumped to JSON Schema by
  ``scripts/typegen-pagedata.py`` and turned into ``*.types.ts`` for the
  ``<script id="pageData">`` blob each page embeds.
- Models used as ``output=`` / ``Body()`` on router routes flow through
  ``router.openapi_document_json()`` into ``frontend/api.d.ts``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .metrics_math import NS

DEFAULT_SIZE = 100
MAX_SIZE = 500
SPAN_LIMIT = 5000

# Columns the traces list may be ordered by -- an allowlist, like Datasette's
# own ?_sort/?_sort_desc: a request naming anything else is a 400 rather than
# a chance to inject SQL. `queries.TRACE_ORDER_BY` holds the matching SQL and
# asserts it covers exactly these names.
TRACE_SORT_COLUMNS = (
    "label",
    "service_name",
    "http_status",
    "span_count",
    "error_count",
    "duration_ms",
    "start_ns",
    "status",
)
# Newest first, the ordering the list had before it was sortable at all.
DEFAULT_SORT_DESC = "start_ns"

# Ceilings for the metrics query API: a query reads at most POINT_LIMIT raw
# points (beyond that the response is flagged truncated), and the range/step
# bounds keep one request from asking for millions of buckets.
POINT_LIMIT = 50_000
MAX_STEP_S = 86_400
MAX_RANGE_NS = 30 * 24 * 3600 * NS


class TraceRow(BaseModel):
    "One row of the traces list: the derived ``traces`` summary + a label."

    trace_id: str
    # Root span name. Per OTel semconv that is the low-cardinality route
    # pattern for HTTP roots; the concrete path lives in url.path.
    name: str | None = None
    # What the list shows: "<method> <url.path>" for HTTP roots, else name.
    label: str
    service_name: str | None = None
    span_count: int
    error_count: int
    duration_ms: float | None = None
    start_ns: int | None = None
    http_status: int | None = None
    status: str | None = None


# Endpoints are one row per (method, matched route); a Datasette instance has
# a few dozen routes, so this cap is a guard, not a paging scheme.
ENDPOINT_LIMIT = 200

# ``TraceFilters.status``: an exact code ("404") or a class ("4xx").
STATUS_PATTERN = re.compile(r"^(?:[1-5][0-9][0-9]|[1-5]xx)$")

# Reserved ``TraceFilters.route`` value for HTTP requests that matched no
# route. Datasette's route patterns are regexes anchored on "/" or "^", so
# none of them can be the literal string "none".
ROUTE_NONE = "none"


class TraceFilters(BaseModel):
    """What both trace views can narrow by. The HTTP fields are shared on
    purpose: ``/-/otel/http`` drills through to ``/-/otel/traces`` carrying
    the filters the summary was showing, so the two must read the same
    fields under the same querystring names."""

    service: str | None = None
    # Substring of the root span's url.path -- "endpoint contains".
    path: str | None = None
    # Exact ``traces.http_route`` (the matched route pattern), or ROUTE_NONE
    # for HTTP requests that matched none. This is what an endpoint row on
    # /-/otel/http links through as.
    route: str | None = None
    method: str | None = None
    # "500" or "5xx".
    status: str | None = None
    min_duration_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_status(self):
        if self.status is not None and not STATUS_PATTERN.match(self.status):
            raise ValueError(
                f'status must be a code ("500") or a class ("5xx"), not {self.status!r}'
            )
        return self

    @property
    def status_range(self) -> tuple[int, int]:
        "``[low, high)`` for the status filter: 500 -> (500, 501), 5xx -> (500, 600)."
        if self.status.endswith("xx"):
            low = int(self.status[0]) * 100
            return low, low + 100
        code = int(self.status)
        return code, code + 1


class EndpointRow(BaseModel):
    """One HTTP endpoint on ``/-/otel/http``: a method and the route pattern
    it matched, with the stats of the requests behind it. Percentiles are
    nearest-rank over the matching requests -- exact, since they are computed
    from the stored durations rather than from a histogram."""

    method: str | None = None
    # The raw route pattern (a regex); None when the request matched no route.
    route: str | None = None
    # "<method> <readable route>", e.g. "GET /{database}/{table}".
    label: str
    request_count: int
    # Requests that failed: a 5xx response, or any errored span in the trace.
    error_count: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    last_seen_ns: int | None = None


class EndpointsQuery(TraceFilters):
    "Body of ``POST /-/otel/api/http/endpoints``."


class EndpointsResponse(BaseModel):
    endpoints: list[EndpointRow]
    query: EndpointsQuery
    # Every method seen under the current filters *except* the method one --
    # facet counts show the options you could switch to.
    methods: list[str] = []
    services: list[str] = []
    # Matching requests, across every endpoint (not just the listed ones).
    request_count: int
    # True when more endpoints matched than ENDPOINT_LIMIT.
    truncated: bool = False


class HttpSummaryPageData(EndpointsResponse):
    "Embedded by ``GET /-/otel/http``."

    database: str


# Reserved ``TracesQuery.root`` values: every other value is a root span
# name. A plugin would have to name a *non-HTTP* root span literally "http"
# or "none" to collide.
ROOT_HTTP = "http"
ROOT_NONE = "none"


class TraceRootKind(BaseModel):
    """One bucket of the "what started this trace" filter.

    HTTP roots collapse into a single bucket -- their span names are the
    matched route, one per endpoint -- while every other root is its own
    span name, grouped by the instrumentation scope that created it. That
    grouping is what separates a plugin's roots (``datasette_cron.run``,
    scope ``datasette_cron``) from Datasette's own (``datasette.startup``,
    scope ``datasette``) without this plugin knowing either of them."""

    key: str  # ROOT_HTTP, ROOT_NONE, or the root span's name
    label: str
    # Instrumentation scope of the root span: the library that emitted it.
    scope: str | None = None
    count: int


class TracesQuery(TraceFilters):
    """Body of ``POST /-/otel/api/traces/list``: filter, sort, one page.

    Deliberately Datasette's own vocabulary -- ``sort``/``sort_desc``/``size``
    /``next`` are the ``?_sort``/``?_sort_desc``/``?_size``/``?_next`` the
    table pages use, and ``GET /-/otel/traces`` reads them under those
    underscored names (see ``routes/pages.py``) so a sorted list is a URL you
    can paste to someone. Ordering happens in SQL over the whole table, not
    over the page: "slowest traces" means slowest of all of them."""

    size: int = Field(default=DEFAULT_SIZE, ge=1, le=MAX_SIZE)
    # A TraceRootKind key: "http" for anything with a url.path, "none" for a
    # trace whose root span isn't in the store, else a root span name.
    root: str | None = None
    # At most one of these, and only a TRACE_SORT_COLUMNS name.
    sort: str | None = None
    sort_desc: str | None = None
    # Cursor for the page to return, taken from a previous response's
    # ``next``. Opaque to the client; an offset underneath (see
    # queries.list_traces for why offset rather than keyset).
    next: str | None = None

    @model_validator(mode="after")
    def _check_sort(self):
        if self.sort and self.sort_desc:
            raise ValueError("cannot use sort and sort_desc at the same time")
        for value in (self.sort, self.sort_desc):
            if value is not None and value not in TRACE_SORT_COLUMNS:
                raise ValueError(
                    "cannot sort traces by {} (sortable: {})".format(
                        value, ", ".join(TRACE_SORT_COLUMNS)
                    )
                )
        if not self.sort and not self.sort_desc:
            # Resolve the default here rather than in the SQL, so the query
            # the API echoes back is the one the headers should light up.
            self.sort_desc = DEFAULT_SORT_DESC
        if self.next is not None and not self.next.isdigit():
            raise ValueError("next must be a cursor from a previous response")
        return self

    @property
    def offset(self) -> int:
        return int(self.next or 0)


class TracesListResponse(BaseModel):
    """One page of traces plus what the pager needs: ``next`` is the cursor
    for the following page (``None`` on the last one) and ``total`` counts
    every trace matching the filter, not just this page."""

    traces: list[TraceRow]
    query: TracesQuery
    next: str | None = None
    total: int
    # The root-filter buckets, counted under the current *service* filter but
    # not the current root one -- a facet shows you the alternatives.
    root_kinds: list[TraceRootKind] = []
    # queries.pretty_route() of an active ``route`` filter: the list shows the
    # filter it arrived with, and route patterns are regexes.
    route_label: str | None = None


class TracesListPageData(TracesListResponse):
    "Embedded by ``GET /-/otel/traces``: first page + the service filter choices."

    services: list[str]
    database: str


class SpanRow(BaseModel):
    "A ``spans`` row with attributes/resource already JSON-decoded."

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str | None = None
    start_ns: int
    end_ns: int
    duration_ms: float | None = None
    status: str | None = None
    status_description: str | None = None
    service_name: str | None = None
    http_route: str | None = None
    http_status: int | None = None
    db_namespace: str | None = None
    db_operation: str | None = None
    db_query_text: str | None = None
    attributes: dict[str, Any] = {}
    resource: dict[str, Any] = {}
    scope_name: str | None = None
    scope_version: str | None = None
    schema_url: str | None = None


class TraceDetail(BaseModel):
    """Everything the waterfall needs. Served both embedded (page data of
    ``GET /-/otel/traces/<id>``) and as JSON (``GET /-/otel/api/traces/<id>``)."""

    trace_id: str
    # "<method> <url.path>" for HTTP roots, else the root span name.
    title: str
    # The route-pattern root name when it differs from title, else None.
    route: str | None = None
    service_name: str | None = None
    spans: list[SpanRow]
    truncated: bool = False
    database: str


class TraceDetailPageData(TraceDetail):
    pass


class MetricSummaryRow(BaseModel):
    "One row of the metric catalogue: the ``metrics`` definition + point stats."

    name: str
    description: str | None = None
    unit: str | None = None
    # gauge | sum | histogram | exponential_histogram | summary
    type: str
    temporality: str | None = None  # delta | cumulative
    monotonic: bool | None = None
    last_seen_ns: int | None = None
    point_count: int
    services: list[str] = []


class MetricsListQuery(BaseModel):
    "Body of ``POST /-/otel/api/metrics/list``."

    service: str | None = None


class MetricsListResponse(BaseModel):
    metrics: list[MetricSummaryRow]


class MetricsQuery(BaseModel):
    "Body of ``POST /-/otel/api/metrics/query``."

    name: str
    since_ns: int
    until_ns: int
    step_s: int = Field(default=60, ge=1, le=MAX_STEP_S)
    # None = keep the full attribute set (one series per native OTel series);
    # [] = merge everything per service; ["k", ...] = keep only those keys.
    group_by: list[str] | None = None
    service: str | None = None
    percentiles: list[float] = Field(default=[0.5, 0.9, 0.99])

    @model_validator(mode="after")
    def _check_range(self):
        if self.until_ns <= self.since_ns:
            raise ValueError("until_ns must be greater than since_ns")
        if self.until_ns - self.since_ns > MAX_RANGE_NS:
            raise ValueError("range too large (max 30 days)")
        if any(not 0 < p <= 1 for p in self.percentiles):
            raise ValueError("percentiles must be in (0, 1]")
        return self


class SeriesPoint(BaseModel):
    "One bucket of one series. Buckets with no points are omitted entirely."

    t: int  # bucket start, ns
    # gauge: avg of the per-series last values; sum: their sum (raw, not rate)
    value: float | None = None
    count: int | None = None  # histogram: observations in this interval
    sum: float | None = None
    # histogram: per-interval counts (differenced when cumulative), one more
    # entry than explicit_bounds -- the trailing (last bound, +Inf) bucket.
    bucket_counts: list[int] | None = None
    percentiles: dict[str, float | None] | None = None  # {"0.5": ...}


class Series(BaseModel):
    key: str  # metrics_math.series_key
    service_name: str | None = None
    attributes: dict[str, Any] = {}  # the attributes group_by kept
    explicit_bounds: list[float] | None = None
    points: list[SeriesPoint]


class MetricsQueryResponse(BaseModel):
    metric: MetricSummaryRow
    since_ns: int
    until_ns: int
    step_s: int
    # Shared histogram bounds, or None when the series disagree.
    explicit_bounds: list[float] | None = None
    series: list[Series]
    truncated: bool = False


class OtelIndexPageData(BaseModel):
    """Embedded by ``GET /-/otel``: the landing page that points at the
    traces and metrics viewers, with a few counts so it doubles as a quick
    "is anything being recorded?" check."""

    trace_count: int
    span_count: int
    # Traces whose root span is an HTTP request: what /-/otel/http summarises.
    http_request_count: int
    metric_count: int
    metric_point_count: int
    services: list[str]
    database: str


class MetricsListPageData(BaseModel):
    "Embedded by ``GET /-/otel/metrics``."

    metrics: list[MetricSummaryRow]
    services: list[str]
    database: str


class MetricDetailPageData(BaseModel):
    """Embedded by ``GET /-/otel/metrics/<name>``; series are fetched
    client-side via ``POST /-/otel/api/metrics/query`` so the browser can
    compute the time range itself."""

    metric: MetricSummaryRow
    attribute_keys: list[str]
    services: list[str]
    database: str


__exports__ = [
    OtelIndexPageData,
    TracesListPageData,
    HttpSummaryPageData,
    TraceDetailPageData,
    MetricsListPageData,
    MetricDetailPageData,
]
