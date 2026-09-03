"""GET /-/otel/metrics and GET /-/otel/metrics/<name>: render with typed
page data, private by default, mirroring the traces pages."""

import json
import time

import pytest
from conftest import page_data

from datasette_otel_viewer import page_data as page_data_module
from datasette_otel_viewer import store
from datasette_otel_viewer.metrics_math import NS
from datasette_otel_viewer.router import FORBIDDEN_TEXT

BASE_NS = ((time.time_ns() - 3600 * NS) // (60 * NS)) * (60 * NS)

METRIC_NAME = "db.client.operation.duration"


def metric(name, type, **kw):
    return {"name": name, "type": type, **kw}


def point(name, offset_s, service="datasette", attributes=None, **kw):
    return {
        "metric_name": name,
        "service_name": service,
        "time_ns": BASE_NS + int(offset_s * NS),
        "attributes": json.dumps(attributes or {}),
        **kw,
    }


def histogram(
    name, offset_s, bounds, counts, service="datasette", attributes=None, **kw
):
    return point(
        name,
        offset_s,
        service=service,
        attributes=attributes,
        count=sum(counts),
        sum=kw.pop("sum", float(sum(counts))),
        bucket_counts=json.dumps(counts),
        explicit_bounds=json.dumps(bounds),
        **kw,
    )


async def seed(ds):
    await store.insert_metrics(
        ds,
        [
            metric(
                METRIC_NAME,
                "histogram",
                description="Duration of database client operations",
                unit="s",
                temporality="cumulative",
            ),
            metric("cpu", "gauge", unit="1"),
        ],
        [
            histogram(
                METRIC_NAME,
                0,
                [0.1, 1],
                [1, 2, 0],
                service="datasette",
                attributes={"db.namespace": "otel", "datasette.operation": "select"},
            ),
            histogram(
                METRIC_NAME,
                30,
                [0.1, 1],
                [2, 3, 0],
                service="flask-app",
                attributes={"db.namespace": "otel", "datasette.operation": "insert"},
            ),
            point("cpu", 0, service="flask-app", value_double=0.5),
        ],
    )


@pytest.mark.asyncio
async def test_list_page_renders_with_page_data(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/-/otel/metrics")
    assert response.status_code == 200
    assert "src/pages/metrics_list/index.ts" in response.text
    data = page_data(response.text)
    names = [m["name"] for m in data["metrics"]]
    assert METRIC_NAME in names
    assert data["metrics"][0]["name"]
    assert data["database"] == "otel"
    assert data["services"] == sorted(data["services"])
    assert set(data["services"]) == {"datasette", "flask-app"}


@pytest.mark.asyncio
async def test_detail_page_renders(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get(f"/-/otel/metrics/{METRIC_NAME}")
    assert response.status_code == 200
    assert "src/pages/metric_detail/index.ts" in response.text
    assert f"<title>{METRIC_NAME}</title>" in response.text
    data = page_data(response.text)
    assert data["attribute_keys"] == sorted(["db.namespace", "datasette.operation"])
    assert data["metric"]["type"] == "histogram"
    assert data["metric"]["name"] == METRIC_NAME
    assert set(data["services"]) == {"datasette", "flask-app"}
    assert data["database"] == "otel"


@pytest.mark.asyncio
async def test_detail_404(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/-/otel/metrics/does.not.exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pages_private_by_default(make_ds):
    ds = await make_ds()
    await seed(ds)
    listing = await ds.client.get("/-/otel/metrics")
    assert listing.status_code == 403
    assert FORBIDDEN_TEXT in listing.text
    detail = await ds.client.get(f"/-/otel/metrics/{METRIC_NAME}")
    assert detail.status_code == 403
    assert FORBIDDEN_TEXT in detail.text


@pytest.mark.asyncio
async def test_root_actor_allowed(make_ds):
    ds = await make_ds()
    ds.root_enabled = True
    await seed(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    listing = await ds.client.get("/-/otel/metrics", cookies=cookies)
    assert listing.status_code == 200
    detail = await ds.client.get(f"/-/otel/metrics/{METRIC_NAME}", cookies=cookies)
    assert detail.status_code == 200


def test_page_data_models_exported():
    assert page_data_module.MetricsListPageData in page_data_module.__exports__
    assert page_data_module.MetricDetailPageData in page_data_module.__exports__
