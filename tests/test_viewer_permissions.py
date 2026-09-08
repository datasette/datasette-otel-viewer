"""Viewer pages and JSON API: render with typed page data; private by
default; public_viewer opens only the viewer, never the raw tables."""

import time

import pytest
from conftest import drain, page_data

from datasette_otel_viewer import store
from datasette_otel_viewer.permissions import VIEW_ACTION_NAME

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
START_NS = time.time_ns() - 1_000_000_000
END_NS = START_NS + 5_000_000
ROOT_NAME = "datasette.startup"


def span_row(**overrides):
    "A store.COLUMNS row dict, the shape selfsource.span_to_row produces."
    row = {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "parent_span_id": None,
        "name": ROOT_NAME,
        "kind": "INTERNAL",
        "start_ns": START_NS,
        "end_ns": END_NS,
        "duration_ms": (END_NS - START_NS) / 1e6,
        "status": "UNSET",
        "service_name": "datasette",
        "attributes": "{}",
        "resource": '{"service.name": "datasette"}',
    }
    row.update(overrides)
    return row


async def seed(ds):
    await store.insert_spans(ds, [span_row()])


@pytest.mark.asyncio
async def test_viewer_list_and_waterfall(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    listing = await ds.client.get("/-/otel/traces")
    assert listing.status_code == 200
    # The page is a Vite entrypoint plus an embedded page-data blob.
    assert "src/pages/traces_list/index.ts" in listing.text
    data = page_data(listing.text)
    assert data["database"] == "otel"
    assert data["limit"] == 100
    assert "datasette" in data["services"]
    trace = next(t for t in data["traces"] if t["trace_id"] == TRACE_ID)
    assert trace["service_name"] == "datasette"
    assert trace["span_count"] >= 1

    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID}")
    assert waterfall.status_code == 200
    assert "src/pages/trace_detail/index.ts" in waterfall.text
    detail = page_data(waterfall.text)
    assert detail["trace_id"] == TRACE_ID
    assert ROOT_NAME in {s["name"] for s in detail["spans"]}
    # attributes/resource arrive JSON-decoded, ready for the inspector
    assert all(isinstance(s["attributes"], dict) for s in detail["spans"])


@pytest.mark.asyncio
async def test_json_api_matches_page_data(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    listed = await ds.client.post("/-/otel/api/traces/list", json={"limit": 10})
    assert listed.status_code == 200
    rows = listed.json()["traces"]
    assert [t["trace_id"] for t in rows] == [
        t["trace_id"]
        for t in page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    ]

    detail = await ds.client.get(f"/-/otel/api/traces/{TRACE_ID}")
    assert detail.status_code == 200
    assert detail.json() == page_data(
        (await ds.client.get(f"/-/otel/traces/{TRACE_ID}")).text
    )


@pytest.mark.asyncio
async def test_json_api_filters_and_validates(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    hit = await ds.client.post("/-/otel/api/traces/list", json={"service": "datasette"})
    assert [t["trace_id"] for t in hit.json()["traces"]] == [TRACE_ID]
    miss = await ds.client.post("/-/otel/api/traces/list", json={"service": "nope"})
    assert miss.json() == {"traces": []}
    # Pydantic validation: limit is capped, malformed bodies are 400s.
    too_big = await ds.client.post("/-/otel/api/traces/list", json={"limit": 10_000})
    assert too_big.status_code == 400
    assert "limit" in too_big.json()["error"]
    missing = await ds.client.get("/-/otel/api/traces/" + "0" * 32)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_self_stored_trace_round_trips(make_ds):
    ds = await make_ds(public_viewer=True)
    await ds.client.get("/")
    await drain()
    listing = await ds.client.get("/-/otel/traces")
    assert listing.status_code == 200
    labels = [t["label"] for t in page_data(listing.text)["traces"]]
    assert any(label.startswith("GET ") for label in labels)


@pytest.mark.asyncio
async def test_http_roots_show_path_not_route_pattern(make_ds):
    """Root spans are named after the low-cardinality route *pattern*
    (semconv); the UI shows the concrete "<method> <url.path>" instead,
    keeping the pattern as `name` (list tooltip) / `route` (waterfall)."""
    ds = await make_ds(public_viewer=True)
    await ds.client.get("/-/versions.json")
    await drain()

    rows = page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    row = next(t for t in rows if t["label"] == "GET /-/versions.json")
    route_pattern = row["name"]
    assert route_pattern != "GET /-/versions.json"

    waterfall = await ds.client.get(f"/-/otel/traces/{row['trace_id']}")
    assert "<title>GET /-/versions.json</title>" in waterfall.text
    detail = page_data(waterfall.text)
    assert detail["title"] == "GET /-/versions.json"
    assert detail["route"] == route_pattern


@pytest.mark.asyncio
async def test_non_http_roots_fall_back_to_span_name(make_ds):
    """Non-HTTP roots (startup, background work) keep their span name as the
    label."""
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    rows = page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    row = next(t for t in rows if t["trace_id"] == TRACE_ID)
    assert row["label"] == ROOT_NAME
    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID}")
    assert f"<title>{ROOT_NAME}</title>" in waterfall.text
    assert page_data(waterfall.text)["route"] is None


@pytest.mark.asyncio
async def test_viewer_private_by_default(make_ds):
    ds = await make_ds()
    await seed(ds)
    assert (await ds.client.get("/-/otel/traces")).status_code == 403
    assert (await ds.client.get(f"/-/otel/traces/{TRACE_ID}")).status_code == 403
    assert (await ds.client.post("/-/otel/api/traces/list", json={})).status_code == 403
    assert (await ds.client.get(f"/-/otel/api/traces/{TRACE_ID}")).status_code == 403


@pytest.mark.asyncio
async def test_raw_tables_denied_anonymously_even_with_public_viewer(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/otel/spans.json")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_config_grant_by_actor_id(make_ds):
    """The `just dev` setup: `permissions.datasette-otel-viewer.id clark`
    lets clark (a datasette-debug-gotham actor) into the pages, the API and
    the raw tables; every other actor and anonymous get the 403."""
    assert VIEW_ACTION_NAME == "datasette-otel-viewer"
    ds = await make_ds(permissions={"datasette-otel-viewer": {"id": "clark"}})
    await seed(ds)
    clark = {"ds_actor": ds.client.actor_cookie({"id": "clark"})}
    lois = {"ds_actor": ds.client.actor_cookie({"id": "lois"})}
    for path in ("/-/otel", "/-/otel/traces", "/-/otel/metrics", "/otel/spans.json"):
        assert (await ds.client.get(path, cookies=clark)).status_code == 200, path
        assert (await ds.client.get(path, cookies=lois)).status_code == 403, path
        assert (await ds.client.get(path)).status_code == 403, path
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=clark)
    ).status_code == 200
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=lois)
    ).status_code == 403


@pytest.mark.asyncio
async def test_root_actor_sees_everything(make_ds):
    ds = await make_ds()
    ds.root_enabled = True
    await seed(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    assert (await ds.client.get("/-/otel/traces", cookies=cookies)).status_code == 200
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=cookies)
    ).status_code == 200
    assert (await ds.client.get("/otel/spans.json", cookies=cookies)).status_code == 200


@pytest.mark.asyncio
async def test_metric_tables_denied_anonymously_even_with_public_viewer(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [{"name": "cpu", "type": "gauge"}],
        [
            {
                "metric_name": "cpu",
                "service_name": "datasette",
                "time_ns": START_NS,
                "attributes": "{}",
                "value_double": 0.5,
            }
        ],
    )
    assert (await ds.client.get("/otel/metric_points.json")).status_code == 403
    assert (await ds.client.get("/otel/metrics.json")).status_code == 403


@pytest.mark.asyncio
async def test_root_sees_metric_tables(make_ds):
    ds = await make_ds()
    ds.root_enabled = True
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    assert (
        await ds.client.get("/otel/metric_points.json", cookies=cookies)
    ).status_code == 200


@pytest.mark.asyncio
async def test_other_databases_unaffected(make_ds):
    "The permission deny row is scoped to the otel database only."
    ds = await make_ds()
    response = await ds.client.get("/_memory.json")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_ingest_routes(make_ds):
    """The OTLP receiver is gone: /v1/* is Datasette's 404, not a plugin
    response. A regression here would reopen the write-over-HTTP surface."""
    ds = await make_ds(public_viewer=True)
    for path in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        assert (await ds.client.get(path)).status_code == 404, path
        assert (await ds.client.post(path, content=b"")).status_code == 404, path


@pytest.mark.asyncio
async def test_built_manifest_serves_hashed_assets(make_ds, tmp_path):
    """Without a dev path configured, vite_entry resolves the built
    manifest (present after `just frontend`, which CI runs first)."""
    from datasette.app import Datasette

    ds = Datasette(
        [],
        memory=True,
        config={
            "plugins": {
                "datasette-otel-viewer": {
                    "public_viewer": True,
                    "db_path": str(tmp_path / "otel.db"),
                }
            }
        },
    )
    await ds.invoke_startup()
    response = await ds.client.get("/-/otel/traces")
    assert response.status_code == 200
    assert "/-/static-plugins/datasette_otel_viewer/gen/" in response.text
    assert page_data(response.text)["database"] == store.db_name(ds)
