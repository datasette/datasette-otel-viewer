"""HTML page routes. Each renders the single base template with a Vite
entrypoint and a Pydantic page-data blob; the Svelte page mounts into
``#app-root`` and reads the blob (see frontend/src/page_data/load.ts)."""

from datasette import Response
from pydantic import ValidationError

from .. import queries, store
from ..page_data import (
    DEFAULT_SIZE,
    EndpointsQuery,
    HttpSummaryPageData,
    MetricDetailPageData,
    MetricsListPageData,
    MetricsListQuery,
    OtelIndexPageData,
    SqlQueriesQuery,
    SqlSummaryPageData,
    TraceDetailPageData,
    TracesListPageData,
    TracesQuery,
)
from ..router import check_viewer, router

TEMPLATE = "otel_viewer_base.html"

# The viewer's sections, in the order the landing page lists them. Every page
# below the landing page hangs off one of these, and the trail they make is
# rendered twice: in Datasette's own header (otel_viewer_base.html) and next
# to the page's <h1> (components/Breadcrumbs.svelte).
SECTIONS = {
    "traces": ("/-/otel/traces", "Traces"),
    "http": ("/-/otel/http", "HTTP endpoints"),
    "sql": ("/-/otel/sql", "SQL queries"),
    "metrics": ("/-/otel/metrics", "Metrics"),
}


def _crumbs(datasette, *, section=None, page=None, page_href=None):
    """``[{href, label}, ...]`` from the viewer root down to this page.
    ``page`` is the leaf label for a detail page (a trace's title, a metric's
    name); it links to itself, the way Datasette's own crumbs do."""
    path = datasette.urls.path
    trail = [{"href": path("/-/otel"), "label": "OpenTelemetry"}]
    if section:
        href, label = SECTIONS[section]
        trail.append({"href": path(href), "label": label})
    if page:
        trail.append({"href": path(page_href), "label": page})
    return trail


async def _render(datasette, request, *, title, entrypoint, page_data, crumbs):
    return Response.html(
        await datasette.render_template(
            TEMPLATE,
            {
                "page_title": title,
                "entrypoint": entrypoint,
                "page_data": page_data.model_dump(),
                "otel_crumbs": crumbs,
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
        crumbs=_crumbs(datasette),
    )


def _http_filters(request) -> dict:
    """The filter querystring both trace views share: ``?service=&path=&
    route=&method=&status=&min_duration_ms=``. Plain names, not Datasette's
    underscored ones -- these are this plugin's own filters, while ``_sort``
    and friends deliberately mirror a table page."""
    args = request.args
    return {
        "service": args.get("service") or None,
        "path": args.get("path") or None,
        "route": args.get("route") or None,
        "method": args.get("method") or None,
        "status": args.get("status") or None,
        "min_duration_ms": args.get("min_duration_ms") or None,
    }


def _traces_query(request) -> TracesQuery:
    """``?_sort_desc=duration_ms&_size=50&_next=50`` -> the TracesQuery the
    JSON API takes. Datasette's own underscore-prefixed names, on purpose:
    the list page's URL *is* its state, and it reads like a table page's."""
    args = request.args
    return TracesQuery(
        **_http_filters(request),
        size=args.get("_size") or DEFAULT_SIZE,
        root=args.get("root") or None,
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
        crumbs=_crumbs(datasette, section="traces"),
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
        crumbs=_crumbs(
            datasette,
            section="traces",
            page=detail.title,
            page_href=f"/-/otel/traces/{trace_id}",
        ),
    )


@router.GET(r"^/-/otel/http$")
@check_viewer()
async def http_summary_page(datasette, request):
    try:
        query = EndpointsQuery(**_http_filters(request))
    except ValidationError as error:
        return Response.text(f"Bad endpoint query: {error}", status=400)
    summary = await queries.http_endpoints(datasette, query)
    page_data = HttpSummaryPageData(
        **summary.model_dump(),
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title="HTTP endpoints",
        entrypoint="src/pages/http_summary/index.ts",
        page_data=page_data,
        crumbs=_crumbs(datasette, section="http"),
    )


def _sql_filters(request) -> dict:
    "``?service=&sql=&database=&operation=&min_duration_ms=`` for /-/otel/sql."
    args = request.args
    return {
        "service": args.get("service") or None,
        "sql": args.get("sql") or None,
        "access": args.get("access") or None,
        "database": args.get("database") or None,
        "operation": args.get("operation") or None,
        "min_duration_ms": args.get("min_duration_ms") or None,
    }


@router.GET(r"^/-/otel/sql$")
@check_viewer()
async def sql_summary_page(datasette, request):
    try:
        query = SqlQueriesQuery(**_sql_filters(request))
    except ValidationError as error:
        return Response.text(f"Bad SQL query filter: {error}", status=400)
    summary = await queries.sql_queries(datasette, query)
    page_data = SqlSummaryPageData(
        **summary.model_dump(),
        database=store.db_name(datasette),
    )
    return await _render(
        datasette,
        request,
        title="SQL queries",
        entrypoint="src/pages/sql_summary/index.ts",
        page_data=page_data,
        crumbs=_crumbs(datasette, section="sql"),
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
        crumbs=_crumbs(datasette, section="metrics"),
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
        crumbs=_crumbs(
            datasette,
            section="metrics",
            page=metric.name,
            page_href=f"/-/otel/metrics/{name}",
        ),
    )
