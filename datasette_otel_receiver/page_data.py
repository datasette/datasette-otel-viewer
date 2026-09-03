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

from pydantic import BaseModel, Field

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SPAN_LIMIT = 5000


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
    "Body of ``POST /-/api/traces/list``: filter + page size."

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    service: str | None = None


class TracesListResponse(BaseModel):
    traces: list[TraceRow]


class TracesListPageData(BaseModel):
    "Embedded by ``GET /-/traces``: first page + the service filter choices."

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
    ``GET /-/traces/<id>``) and as JSON (``GET /-/api/traces/<id>``)."""

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


__exports__ = [TracesListPageData, TraceDetailPageData]
