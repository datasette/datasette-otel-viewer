"""GET /-/otel: the landing page that points at the traces and metrics
viewers, with store counts; private by default like the pages it links."""

import pytest
from conftest import page_data
from test_metrics_pages import seed

from datasette_otel_viewer.router import FORBIDDEN_TEXT


@pytest.mark.asyncio
async def test_index_renders_counts_and_links(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/-/otel")
    assert response.status_code == 200
    assert "src/pages/index/index.ts" in response.text
    assert "<title>OpenTelemetry</title>" in response.text
    data = page_data(response.text)
    assert data["database"] == "otel"
    assert data["trace_count"] == 0
    assert data["span_count"] == 0
    assert data["metric_count"] > 0
    assert data["metric_point_count"] > 0
    assert set(data["services"]) == {"datasette", "flask-app"}


@pytest.mark.asyncio
async def test_index_trailing_slash(make_ds):
    ds = await make_ds(public_viewer=True)
    response = await ds.client.get("/-/otel/")
    assert response.status_code == 200
    assert "src/pages/index/index.ts" in response.text


@pytest.mark.asyncio
async def test_index_empty_store(make_ds):
    ds = await make_ds(public_viewer=True)
    data = page_data((await ds.client.get("/-/otel")).text)
    assert data["trace_count"] == 0
    assert data["metric_point_count"] == 0
    assert data["services"] == []


@pytest.mark.asyncio
async def test_index_private_by_default(make_ds):
    ds = await make_ds()
    response = await ds.client.get("/-/otel")
    assert response.status_code == 403
    assert response.text == FORBIDDEN_TEXT
