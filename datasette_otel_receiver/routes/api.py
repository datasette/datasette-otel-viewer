"""JSON routes under ``/-/otel/api/traces``. Typed end to end: ``output=`` and
``Body()`` feed the OpenAPI document that generates ``frontend/api.d.ts``.

The list fetch is a POST with a Pydantic body rather than GET with query
params because datasette-plugin-router types path params and bodies only.
"""

from typing import Annotated

from datasette import Response
from datasette_plugin_router import Body

from .. import queries
from ..page_data import (
    MetricsListQuery,
    MetricsListResponse,
    MetricsQuery,
    MetricsQueryResponse,
    TraceDetail,
    TracesListResponse,
    TracesQuery,
)
from ..router import check_viewer, router


@router.POST(r"^/-/otel/api/traces/list$", output=TracesListResponse)
@check_viewer()
async def api_traces_list(datasette, request, body: Annotated[TracesQuery, Body()]):
    traces = await queries.list_traces(datasette, body)
    return Response.json(TracesListResponse(traces=traces).model_dump())


@router.GET(r"^/-/otel/api/traces/(?P<trace_id>[0-9a-f]{32})$", output=TraceDetail)
@check_viewer()
async def api_trace_detail(datasette, request, trace_id: str):
    detail = await queries.get_trace(datasette, trace_id)
    if detail is None:
        return Response.json({"error": "trace not found"}, status=404)
    return Response.json(detail.model_dump())


@router.POST(r"^/-/otel/api/metrics/list$", output=MetricsListResponse)
@check_viewer()
async def api_metrics_list(
    datasette, request, body: Annotated[MetricsListQuery, Body()]
):
    metrics = await queries.list_metrics(datasette, body)
    return Response.json(MetricsListResponse(metrics=metrics).model_dump())


@router.POST(r"^/-/otel/api/metrics/query$", output=MetricsQueryResponse)
@check_viewer()
async def api_metrics_query(datasette, request, body: Annotated[MetricsQuery, Body()]):
    data = await queries.query_metric(datasette, body)
    if data is None:
        return Response.json({"error": "metric not found"}, status=404)
    return Response.json(data.model_dump())
