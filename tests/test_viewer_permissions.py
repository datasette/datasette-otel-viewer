"""Tickets 05 + 06: viewer pages render; private by default; public_viewer
opens only the viewer."""

import pytest

from conftest import drain
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
    listing = await ds.client.get("/-/traces")
    assert listing.status_code == 200
    assert TRACE_ID.hex() in listing.text
    assert "flask-app" in listing.text

    waterfall = await ds.client.get(f"/-/traces/{TRACE_ID.hex()}")
    assert waterfall.status_code == 200
    assert "remote-span" in waterfall.text


@pytest.mark.asyncio
async def test_self_stored_trace_round_trips(make_ds):
    ds = await make_ds(public_viewer=True)
    await ds.client.get("/")
    await drain()
    listing = await ds.client.get("/-/traces")
    assert listing.status_code == 200
    assert "GET " in listing.text


@pytest.mark.asyncio
async def test_http_roots_show_path_not_route_pattern(make_ds):
    """Root spans are named after the low-cardinality route *pattern*
    (semconv); the UI shows the concrete "<method> <url.path>" instead,
    keeping the pattern as the tooltip (list) / route: line (waterfall)."""
    import re

    ds = await make_ds(public_viewer=True)
    await ds.client.get("/-/versions.json")
    await drain()

    listing = await ds.client.get("/-/traces")
    # The concrete path is the link label...
    assert "GET /-/versions.json" in listing.text
    # ...and the route-pattern span name survives as the title tooltip.
    match = re.search(r"title='(GET [^']*)'", listing.text)
    assert match, listing.text
    route_pattern = match.group(1)
    assert route_pattern != "GET /-/versions.json"

    trace_id = re.search(r"/-/traces/([0-9a-f]{32})", listing.text).group(1)
    waterfall = await ds.client.get(f"/-/traces/{trace_id}")
    assert "<title>GET /-/versions.json</title>" in waterfall.text
    assert "route: <code>" in waterfall.text


@pytest.mark.asyncio
async def test_non_http_roots_fall_back_to_span_name(make_ds):
    "Ingested foreign spans without url.path keep their name as the label."
    ds = await make_ds(ingest_token="s3cret", public_viewer=True)
    await seed(ds)
    listing = await ds.client.get("/-/traces")
    assert ">remote-span</a>" in listing.text
    waterfall = await ds.client.get(f"/-/traces/{TRACE_ID.hex()}")
    assert "<title>remote-span</title>" in waterfall.text
    assert "route: <code>" not in waterfall.text


@pytest.mark.asyncio
async def test_viewer_private_by_default(make_ds):
    ds = await make_ds(ingest_token="s3cret")
    await seed(ds)
    assert (await ds.client.get("/-/traces")).status_code == 403
    assert (
        await ds.client.get(f"/-/traces/{TRACE_ID.hex()}")
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
    assert (await ds.client.get("/-/traces", cookies=cookies)).status_code == 200
    assert (
        await ds.client.get("/otel/spans.json", cookies=cookies)
    ).status_code == 200


@pytest.mark.asyncio
async def test_other_databases_unaffected(make_ds):
    "The permission deny row is scoped to the otel database only."
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.get("/_memory.json")
    assert response.status_code == 200
