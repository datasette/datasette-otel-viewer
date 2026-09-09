"""``/-/otel/http``: endpoints grouped by method + matched route, with exact
percentiles, and the HTTP filters it shares with the trace list."""

import json
import time

import pytest
from conftest import page_data

from datasette_otel_viewer import store

START_NS = time.time_ns() - 3600 * 1_000_000_000
# Real Datasette route patterns: the summary groups by these and prints them
# readably, so the test carries the regexes verbatim.
TABLE_ROUTE = r"/(?P<database>[^\/\.]+)/(?P<table>[^\/\.]+)(\.(?P<format>\w+))?$"
QUERY_ROUTE = r"/(?P<database>[^\/\.]+)/-/query$"


def http_span_row(
    i,
    *,
    path="/demo/plants",
    method="GET",
    route=TABLE_ROUTE,
    http_status=200,
    duration_ms=1.0,
    status="UNSET",
):
    """One root SERVER span, the shape selfsource.span_to_row produces for an
    HTTP request. The trace's duration is derived from start/end, so
    ``duration_ms`` here is what the summary will aggregate."""
    start = START_NS + i * 1_000_000_000
    return {
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": None,
        "name": f"{method} {route}",
        "kind": "SERVER",
        "start_ns": start,
        "end_ns": start + int(duration_ms * 1e6),
        "duration_ms": duration_ms,
        "status": status,
        "service_name": "datasette",
        "http_route": route,
        "http_status": http_status,
        "attributes": json.dumps({"url.path": path, "http.request.method": method}),
        "resource": json.dumps({"service.name": "datasette"}),
        "scope_name": "datasette",
    }


async def endpoints(ds, **body):
    response = await ds.client.post("/-/otel/api/http/endpoints", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_percentiles_are_exact_over_the_matching_requests(make_ds):
    """Durations 1..100 ms on one endpoint: nearest-rank p50 is the 50th
    smallest and p95 the 95th. Reading them off the stored durations is the
    whole reason this page can be exact where a histogram metric cannot --
    and a running-count window would collapse both onto the minimum."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [http_span_row(i, duration_ms=float(i)) for i in range(1, 101)],
    )
    summary = await endpoints(ds)
    assert summary["request_count"] == 100
    (row,) = summary["endpoints"]
    assert row["request_count"] == 100
    assert (row["p50_ms"], row["p95_ms"], row["max_ms"]) == (50.0, 95.0, 100.0)


@pytest.mark.asyncio
async def test_endpoints_group_by_method_and_route(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            http_span_row(1, path="/demo/plants"),
            # Same route, different table: one endpoint, two requests.
            http_span_row(2, path="/demo/trees"),
            # Same route, different method: its own endpoint.
            http_span_row(3, method="HEAD"),
            http_span_row(
                4,
                method="POST",
                path="/demo/-/query",
                route=QUERY_ROUTE,
                http_status=500,
            ),
            # An errored span that still answered 200 counts as an error too.
            http_span_row(5, path="/demo/plants", status="ERROR"),
            # No route matched: its own bucket, drilled through as route=none.
            http_span_row(6, path="/nope", route=None, http_status=404),
        ],
    )
    rows = {r["label"]: r for r in (await endpoints(ds))["endpoints"]}
    # Route patterns are regexes; the label is the readable form of one.
    table = rows["GET /{database}/{table}[.{format}]"]
    assert table["request_count"] == 3
    assert table["error_count"] == 1
    assert table["route"] == TABLE_ROUTE
    assert rows["HEAD /{database}/{table}[.{format}]"]["request_count"] == 1
    assert rows["POST /{database}/-/query"]["error_count"] == 1
    assert rows["GET (no route)"]["route"] is None
    assert (await endpoints(ds))["methods"] == ["GET", "HEAD", "POST"]


@pytest.mark.asyncio
async def test_http_filters(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            http_span_row(1, path="/demo/plants", duration_ms=5.0),
            http_span_row(2, path="/demo/plants", duration_ms=500.0),
            http_span_row(3, path="/other/rows", http_status=404),
            http_span_row(4, method="POST", path="/demo/-/query", route=QUERY_ROUTE),
        ],
    )

    async def count(**body):
        return (await endpoints(ds, **body))["request_count"]

    assert await count(path="/demo/") == 3
    # A substring match, not a prefix or a glob.
    assert await count(path="rows") == 1
    assert await count(status="404") == 1
    assert await count(status="2xx") == 3
    assert await count(method="POST") == 1
    assert await count(min_duration_ms=100) == 1
    # Filters compose.
    assert await count(path="/demo/", min_duration_ms=100) == 1
    assert await count(path="/demo/", status="404") == 0
    # LIKE metacharacters in the needle are literal, not wildcards.
    assert await count(path="%") == 0

    bad = await ds.client.post("/-/otel/api/http/endpoints", json={"status": "boom"})
    assert bad.status_code == 400
    assert "status must be a code" in bad.json()["error"]


@pytest.mark.asyncio
async def test_the_trace_list_takes_the_same_filters(make_ds):
    "What the summary's drill-through relies on: same names, same meaning."
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            http_span_row(1, path="/demo/plants", duration_ms=5.0),
            http_span_row(2, path="/demo/plants", duration_ms=500.0),
            http_span_row(3, method="POST", path="/demo/-/query", route=QUERY_ROUTE),
            http_span_row(4, path="/nope", route=None, http_status=404),
        ],
    )

    async def listed(**body):
        response = await ds.client.post("/-/otel/api/traces/list", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    drilled = await listed(root="http", method="GET", route=TABLE_ROUTE)
    assert drilled["total"] == 2
    assert {t["label"] for t in drilled["traces"]} == {"GET /demo/plants"}
    # The readable route comes back for the filter chip: no second
    # implementation of pretty_route in the frontend.
    assert drilled["route_label"] == "/{database}/{table}[.{format}]"
    assert (await listed(route="none"))["total"] == 1
    assert (await listed(path="/demo/", min_duration_ms=100))["total"] == 1
    assert (await listed(status="4xx"))["total"] == 1


@pytest.mark.asyncio
async def test_page_renders_and_is_gated(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(ds, [http_span_row(1)])
    response = await ds.client.get("/-/otel/http?path=/demo/&status=2xx")
    assert response.status_code == 200
    assert "src/pages/http_summary/index.ts" in response.text
    data = page_data(response.text)
    assert data["query"]["path"] == "/demo/"
    assert data["query"]["status"] == "2xx"
    assert data["endpoints"][0]["request_count"] == 1
    assert data["database"] == "otel"

    assert (await ds.client.get("/-/otel/http?status=nope")).status_code == 400

    private = await make_ds()
    assert (await private.client.get("/-/otel/http")).status_code == 403
    assert (
        await private.client.post("/-/otel/api/http/endpoints", json={})
    ).status_code == 403
