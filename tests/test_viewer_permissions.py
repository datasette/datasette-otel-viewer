"""Viewer pages and JSON API: render with typed page data; private by
default; public_viewer opens only the viewer, never the raw tables."""

import pytest
from conftest import drain, page_data
from test_ingest import TRACE_ID, protobuf_body


async def seed(ds):
    response = await ds.client.post(
        "/v1/traces",
        content=protobuf_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_viewer_list_and_waterfall(make_ds):
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    listing = await ds.client.get("/-/otel/traces")
    assert listing.status_code == 200
    # The page is a Vite entrypoint plus an embedded page-data blob.
    assert "src/pages/traces_list/index.ts" in listing.text
    data = page_data(listing.text)
    assert data["database"] == "otel"
    assert data["limit"] == 100
    assert "flask-app" in data["services"]
    trace = next(t for t in data["traces"] if t["trace_id"] == TRACE_ID.hex())
    assert trace["service_name"] == "flask-app"
    assert trace["span_count"] >= 1

    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID.hex()}")
    assert waterfall.status_code == 200
    assert "src/pages/trace_detail/index.ts" in waterfall.text
    detail = page_data(waterfall.text)
    assert detail["trace_id"] == TRACE_ID.hex()
    assert "remote-span" in {s["name"] for s in detail["spans"]}
    # attributes/resource arrive JSON-decoded, ready for the inspector
    assert all(isinstance(s["attributes"], dict) for s in detail["spans"])


@pytest.mark.asyncio
async def test_json_api_matches_page_data(make_ds):
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    listed = await ds.client.post("/-/otel/api/traces/list", json={"limit": 10})
    assert listed.status_code == 200
    rows = listed.json()["traces"]
    assert [t["trace_id"] for t in rows] == [
        t["trace_id"]
        for t in page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    ]

    detail = await ds.client.get(f"/-/otel/api/traces/{TRACE_ID.hex()}")
    assert detail.status_code == 200
    assert detail.json() == page_data(
        (await ds.client.get(f"/-/otel/traces/{TRACE_ID.hex()}")).text
    )


@pytest.mark.asyncio
async def test_json_api_filters_and_validates(make_ds):
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    hit = await ds.client.post("/-/otel/api/traces/list", json={"service": "flask-app"})
    assert [t["trace_id"] for t in hit.json()["traces"]] == [TRACE_ID.hex()]
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
    "Ingested foreign spans without url.path keep their name as the label."
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    rows = page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    row = next(t for t in rows if t["trace_id"] == TRACE_ID.hex())
    assert row["label"] == "remote-span"
    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID.hex()}")
    assert "<title>remote-span</title>" in waterfall.text
    assert page_data(waterfall.text)["route"] is None


@pytest.mark.asyncio
async def test_viewer_private_by_default(make_ds):
    ds = await make_ds(ingest_token="s3cret")
    await seed(ds)
    assert (await ds.client.get("/-/otel/traces")).status_code == 403
    assert (await ds.client.get(f"/-/otel/traces/{TRACE_ID.hex()}")).status_code == 403
    assert (await ds.client.post("/-/otel/api/traces/list", json={})).status_code == 403
    assert (
        await ds.client.get(f"/-/otel/api/traces/{TRACE_ID.hex()}")
    ).status_code == 403


@pytest.mark.asyncio
async def test_raw_tables_denied_anonymously_even_with_public_viewer(make_ds):
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/otel/spans.json")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_root_actor_sees_everything(make_ds):
    ds = await make_ds(ingest_token="s3cret")
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
    from test_ingest_metrics import protobuf_metrics_body

    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    response = await ds.client.post(
        "/v1/metrics",
        content=protobuf_metrics_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200
    assert (await ds.client.get("/otel/metric_points.json")).status_code == 403
    assert (await ds.client.get("/otel/metrics.json")).status_code == 403


@pytest.mark.asyncio
async def test_root_sees_metric_tables(make_ds):
    ds = await make_ds(ingest_token="s3cret")
    ds.root_enabled = True
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    assert (
        await ds.client.get("/otel/metric_points.json", cookies=cookies)
    ).status_code == 200


@pytest.mark.asyncio
async def test_other_databases_unaffected(make_ds):
    "The permission deny row is scoped to the otel database only."
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.get("/_memory.json")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_built_manifest_serves_hashed_assets(make_ds, tmp_path):
    """Without a dev path configured, vite_entry resolves the built
    manifest (present after `just frontend`, which CI runs first)."""
    from datasette.app import Datasette

    from datasette_otel_receiver import store

    ds = Datasette(
        [],
        memory=True,
        config={
            "plugins": {
                "datasette-otel-receiver": {
                    "public_viewer": True,
                    "db_path": str(tmp_path / "otel.db"),
                }
            }
        },
    )
    await ds.invoke_startup()
    response = await ds.client.get("/-/otel/traces")
    assert response.status_code == 200
    assert "/-/static-plugins/datasette_otel_receiver/gen/" in response.text
    assert page_data(response.text)["database"] == store.db_name(ds)
