"""HTML page routes. Each renders the single base template with a Vite
entrypoint and a Pydantic page-data blob; the Svelte page mounts into
``#app-root`` and reads the blob (see frontend/src/page_data/load.ts)."""

from datasette import Response
from pydantic import ValidationError

from .. import queries, store
from ..page_data import (
    DEFAULT_SIZE,
    MetricDetailPageData,
    MetricsListPageData,
    MetricsListQuery,
    OtelIndexPageData,
    TraceDetailPageData,
    TracesListPageData,
    TracesQuery,
)
from ..router import check_viewer, router

TEMPLATE = "otel_viewer_base.html"


async def _render(datasette, request, *, title, entrypoint, page_data):
    return Response.html(
        await datasette.render_template(
            TEMPLATE,
            {
                "page_title": title,
                "entrypoint": entrypoint,
                "page_data": page_data.model_dump(),
            },
            request=request,
        )
    )


@router.GET(r"^/-/otel/?$")
@check_viewer()
async def index_page(datasette, request):
    page_data = OtelIndexPageData(
        **await queries.store_summary(datasette),
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title="OpenTelemetry",
        entrypoint="src/pages/index/index.ts",
        page_data=page_data,
    )


def _traces_query(request) -> TracesQuery:
    """``?_sort_desc=duration_ms&_size=50&_next=50`` -> the TracesQuery the
    JSON API takes. Datasette's own underscore-prefixed names, on purpose:
    the list page's URL *is* its state, and it reads like a table page's."""
    args = request.args
    return TracesQuery(
        size=args.get("_size") or DEFAULT_SIZE,
        service=args.get("service") or None,
        sort=args.get("_sort") or None,
        sort_desc=args.get("_sort_desc") or None,
        next=args.get("_next") or None,
    )


@router.GET(r"^/-/otel/traces$")
@check_viewer()
async def traces_list_page(datasette, request):
    try:
        query = _traces_query(request)
    except ValidationError as error:
        # Same shape as Datasette's own "Cannot sort table by ..." 400.
        return Response.text(f"Bad traces query: {error}", status=400)
    listed = await queries.list_traces(datasette, query)
    page_data = TracesListPageData(
        **listed.model_dump(),
        services=await queries.list_services(datasette),
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title="Traces",
        entrypoint="src/pages/traces_list/index.ts",
        page_data=page_data,
    )


@router.GET(r"^/-/otel/traces/(?P<trace_id>[0-9a-f]{32})$")
@check_viewer()
async def trace_detail_page(datasette, request, trace_id: str):
    detail = await queries.get_trace(datasette, trace_id)
    if detail is None:
        return Response.text("No spans with this trace_id", status=404)
    page_data = TraceDetailPageData(**detail.model_dump())
    return await _render(
        datasette,
        request,
        title=detail.title,
        entrypoint="src/pages/trace_detail/index.ts",
        page_data=page_data,
    )


@router.GET(r"^/-/otel/metrics$")
@check_viewer()
async def metrics_list_page(datasette, request):
    metrics = await queries.list_metrics(datasette, MetricsListQuery())
    page_data = MetricsListPageData(
        metrics=metrics,
        services=sorted({s for m in metrics for s in m.services}),
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title="Metrics",
        entrypoint="src/pages/metrics_list/index.ts",
        page_data=page_data,
    )


@router.GET(r"^/-/otel/metrics/(?P<name>[^/]+)$")
@check_viewer()
async def metric_detail_page(datasette, request, name: str):
    metric = await queries.get_metric(datasette, name)
    if metric is None:
        return Response.text("No metric with this name", status=404)
    page_data = MetricDetailPageData(
        metric=metric,
        attribute_keys=await queries.metric_attribute_keys(datasette, name),
        services=metric.services,
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title=metric.name,
        entrypoint="src/pages/metric_detail/index.ts",
        page_data=page_data,
    )
