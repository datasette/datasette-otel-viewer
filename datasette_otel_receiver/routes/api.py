"""JSON routes under ``/-/api/traces``. Typed end to end: ``output=`` and
``Body()`` feed the OpenAPI document that generates ``frontend/api.d.ts``.

The list fetch is a POST with a Pydantic body rather than GET with query
params because datasette-plugin-router types path params and bodies only.
"""

from typing import Annotated

from datasette import Response
from datasette_plugin_router import Body

from .. import queries
from ..page_data import TraceDetail, TracesListResponse, TracesQuery
from ..router import check_viewer, router


@router.POST(r"^/-/api/traces/list$", output=TracesListResponse)
@check_viewer()
async def api_traces_list(datasette, request, body: Annotated[TracesQuery, Body()]):
    traces = await queries.list_traces(datasette, body)
    return Response.json(TracesListResponse(traces=traces).model_dump())


@router.GET(r"^/-/api/traces/(?P<trace_id>[0-9a-f]{32})$", output=TraceDetail)
@check_viewer()
async def api_trace_detail(datasette, request, trace_id: str):
    detail = await queries.get_trace(datasette, trace_id)
    if detail is None:
        return Response.json({"error": "trace not found"}, status=404)
    return Response.json(detail.model_dump())
