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

from typing import Any

from pydantic import BaseModel, Field, model_validator

from .metrics_math import NS

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SPAN_LIMIT = 5000

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


class TracesQuery(BaseModel):
    "Body of ``POST /-/otel/api/traces/list``: filter + page size."

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    service: str | None = None


class TracesListResponse(BaseModel):
    traces: list[TraceRow]


class TracesListPageData(BaseModel):
    "Embedded by ``GET /-/otel/traces``: first page + the service filter choices."

    traces: list[TraceRow]
    services: list[str]
    limit: int
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
    TraceDetailPageData,
    MetricsListPageData,
    MetricDetailPageData,
]
