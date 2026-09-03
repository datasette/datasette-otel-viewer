"""The metrics JSON API: catalogue, bucketing, group-by, histogram deltas
and percentiles. Points are seeded straight through ``store.insert_metrics``
so these tests do not depend on the OTLP ingest route."""

import json
import time

import pytest

from datasette_otel_receiver import queries, store
from datasette_otel_receiver.metrics_math import NS
from datasette_otel_receiver.router import router

# An hour ago, floored to a minute: every bucket assertion below is exact
# against a 60 s step.
BASE_NS = ((time.time_ns() - 3600 * NS) // (60 * NS)) * (60 * NS)
UNTIL_NS = BASE_NS + 3600 * NS


def metric(name, type, **kw):
    return {"name": name, "type": type, **kw}


def point(name, offset_s, service="svc-a", attributes=None, **kw):
    return {
        "metric_name": name,
        "service_name": service,
        "time_ns": BASE_NS + int(offset_s * NS),
        "attributes": json.dumps(attributes or {}),
        **kw,
    }


def histogram(name, offset_s, bounds, counts, service="svc-a", attributes=None, **kw):
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


async def query(ds, **body):
    body.setdefault("since_ns", BASE_NS)
    body.setdefault("until_ns", UNTIL_NS)
    return await ds.client.post("/-/otel/api/metrics/query", json=body)


@pytest.mark.asyncio
async def test_list_metrics_shape(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [
            metric("cpu", "gauge", unit="1", description="CPU"),
            metric("reqs", "sum", temporality="cumulative", monotonic=1),
            metric("empty", "gauge"),
        ],
        [
            point("cpu", 0, value_double=0.5),
            point("cpu", 30, service="svc-b", value_double=0.7),
            point("reqs", 10, value_int=3),
        ],
    )
    response = await ds.client.post("/-/otel/api/metrics/list", json={})
    assert response.status_code == 200
    rows = {m["name"]: m for m in response.json()["metrics"]}
    assert set(rows) == {"cpu", "empty", "reqs"}
    assert rows["cpu"] == {
        "name": "cpu",
        "description": "CPU",
        "unit": "1",
        "type": "gauge",
        "temporality": None,
        "monotonic": None,
        "last_seen_ns": BASE_NS + 30 * NS,
        "point_count": 2,
        "services": ["svc-a", "svc-b"],
    }
    assert rows["reqs"]["temporality"] == "cumulative"
    assert rows["reqs"]["monotonic"] is True
    assert rows["empty"]["point_count"] == 0
    assert rows["empty"]["services"] == []
    assert rows["empty"]["last_seen_ns"] is None


@pytest.mark.asyncio
async def test_list_metrics_service_filter(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge"), metric("mem", "gauge")],
        [
            point("cpu", 0, value_double=1),
            point("mem", 0, service="svc-b", value_double=2),
        ],
    )
    response = await ds.client.post(
        "/-/otel/api/metrics/list", json={"service": "svc-a"}
    )
    assert [m["name"] for m in response.json()["metrics"]] == ["cpu"]


@pytest.mark.asyncio
async def test_query_403_without_viewer(make_ds):
    ds = await make_ds()
    assert (
        await ds.client.post("/-/otel/api/metrics/list", json={})
    ).status_code == 403
    assert (await query(ds, name="cpu")).status_code == 403


@pytest.mark.asyncio
async def test_query_404_unknown_metric(make_ds):
    ds = await make_ds(public_viewer=True)
    response = await query(ds, name="nope")
    assert response.status_code == 404
    assert response.json() == {"error": "metric not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"since_ns": BASE_NS, "until_ns": BASE_NS},
        {"since_ns": BASE_NS, "until_ns": UNTIL_NS, "step_s": 0},
        {"since_ns": 0, "until_ns": 40 * 24 * 3600 * NS},
        {"since_ns": BASE_NS, "until_ns": UNTIL_NS, "percentiles": [1.5]},
    ],
)
async def test_query_validation(make_ds, body):
    ds = await make_ds(public_viewer=True)
    response = await ds.client.post(
        "/-/otel/api/metrics/query", json={"name": "cpu", **body}
    )
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.asyncio
async def test_gauge_bucketing_last_wins(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge")],
        [
            point("cpu", 5, value_double=1),
            point("cpu", 59, value_double=2),
            point("cpu", 60, value_double=3),
        ],
    )
    data = (await query(ds, name="cpu", step_s=60)).json()
    (series,) = data["series"]
    assert [(p["t"], p["value"]) for p in series["points"]] == [
        (BASE_NS, 2.0),
        (BASE_NS + 60 * NS, 3.0),
    ]
    assert data["step_s"] == 60
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_gauge_group_by_merges_with_avg(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge")],
        [
            point("cpu", 1, attributes={"h": "a"}, value_double=2),
            point("cpu", 2, attributes={"h": "b"}, value_double=4),
        ],
    )
    merged = (await query(ds, name="cpu", group_by=[])).json()["series"]
    assert len(merged) == 1
    assert merged[0]["attributes"] == {}
    assert [p["value"] for p in merged[0]["points"]] == [3.0]

    native = (await query(ds, name="cpu")).json()["series"]
    assert len(native) == 2
    assert sorted(s["attributes"]["h"] for s in native) == ["a", "b"]
    assert sorted(p["value"] for s in native for p in s["points"]) == [2.0, 4.0]

    by_key = (await query(ds, name="cpu", group_by=["missing"])).json()["series"]
    assert len(by_key) == 1


@pytest.mark.asyncio
async def test_sum_group_by_sums(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("reqs", "sum", temporality="cumulative", monotonic=1)],
        [
            point("reqs", 1, attributes={"h": "a"}, value_int=2),
            point("reqs", 2, attributes={"h": "b"}, value_int=4),
        ],
    )
    merged = (await query(ds, name="reqs", group_by=[])).json()["series"]
    assert [p["value"] for p in merged[0]["points"]] == [6.0]


@pytest.mark.asyncio
async def test_cumulative_counter_reset_shows_raw_values(make_ds):
    """Raw cumulative values are served as-is (the rate is computed client
    side, ticket 07) — a reset just makes the series step back down."""
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("reqs", "sum", temporality="cumulative", monotonic=1)],
        [
            point("reqs", 0, value_int=10),
            point("reqs", 60, value_int=25),
            point("reqs", 120, value_int=4),  # process restarted
        ],
    )
    (series,) = (await query(ds, name="reqs", step_s=60)).json()["series"]
    assert [p["value"] for p in series["points"]] == [10.0, 25.0, 4.0]


@pytest.mark.asyncio
async def test_service_filter_and_series_metadata(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge")],
        [
            point("cpu", 1, service="svc-a", attributes={"h": "a"}, value_double=1),
            point("cpu", 1, service="svc-b", attributes={"h": "b"}, value_double=2),
        ],
    )
    both = (await query(ds, name="cpu")).json()["series"]
    assert sorted(s["service_name"] for s in both) == ["svc-a", "svc-b"]

    (only,) = (await query(ds, name="cpu", service="svc-a")).json()["series"]
    assert only["service_name"] == "svc-a"
    assert only["attributes"] == {"h": "a"}
    assert "svc-a" in only["key"]


@pytest.mark.asyncio
async def test_query_time_range_excludes_outside_points(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge")],
        [
            point("cpu", -120, value_double=1),
            point("cpu", 120, value_double=2),
            point("cpu", 3600, value_double=3),  # == until_ns, exclusive
        ],
    )
    (series,) = (await query(ds, name="cpu")).json()["series"]
    assert [(p["t"], p["value"]) for p in series["points"]] == [
        (BASE_NS + 120 * NS, 2.0)
    ]


@pytest.mark.asyncio
async def test_cumulative_histogram_becomes_deltas(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("dur", "histogram", temporality="cumulative")],
        [
            histogram("dur", 0, [1, 2], [5, 5, 0]),
            histogram("dur", 60, [1, 2], [7, 8, 1]),
        ],
    )
    (series,) = (await query(ds, name="dur", step_s=60)).json()["series"]
    first, second = series["points"]
    assert (first["bucket_counts"], first["count"]) == ([5, 5, 0], 10)
    assert (second["bucket_counts"], second["count"]) == ([2, 3, 1], 6)


@pytest.mark.asyncio
async def test_delta_histogram_passes_through(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("dur", "histogram", temporality="delta")],
        [
            histogram("dur", 1, [1, 2], [1, 2, 0]),
            histogram("dur", 2, [1, 2], [3, 0, 1]),
        ],
    )
    (series,) = (await query(ds, name="dur", step_s=60)).json()["series"]
    (only,) = series["points"]
    assert only["bucket_counts"] == [4, 2, 1]
    assert only["count"] == 7


@pytest.mark.asyncio
async def test_histogram_percentiles(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("dur", "histogram", temporality="delta")],
        [histogram("dur", 1, [0.1, 0.5, 1], [10, 20, 10, 0])],
    )
    data = (await query(ds, name="dur", step_s=60)).json()
    assert data["explicit_bounds"] == [0.1, 0.5, 1]
    (series,) = data["series"]
    assert series["explicit_bounds"] == [0.1, 0.5, 1]
    (only,) = series["points"]
    assert only["count"] == 40
    percentiles = only["percentiles"]
    assert percentiles["0.5"] == pytest.approx(0.3)
    assert percentiles["0.9"] == pytest.approx(0.8)
    assert percentiles["0.99"] == pytest.approx(0.98)


@pytest.mark.asyncio
async def test_mixed_bounds_reported_as_none(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [metric("dur", "histogram", temporality="delta")],
        [
            histogram("dur", 1, [0.1, 0.5, 1], [10, 20, 10, 0], attributes={"h": "a"}),
            histogram("dur", 1, [1, 2], [1, 2, 0], attributes={"h": "b"}),
        ],
    )
    data = (await query(ds, name="dur", step_s=60)).json()
    assert data["explicit_bounds"] is None
    bounds = {json.dumps(s["explicit_bounds"]) for s in data["series"]}
    assert bounds == {"[0.1, 0.5, 1.0]", "[1.0, 2.0]"}
    assert all(p["percentiles"] for s in data["series"] for p in s["points"])


@pytest.mark.asyncio
async def test_truncated_flag(make_ds, monkeypatch):
    ds = await make_ds(public_viewer=True)
    monkeypatch.setattr(queries, "POINT_LIMIT", 3)
    await store.insert_metrics(
        ds,
        [metric("cpu", "gauge")],
        [point("cpu", i, value_double=i) for i in range(4)],
    )
    assert (await query(ds, name="cpu")).json()["truncated"] is True


def test_openapi_document_lists_routes():
    document = router.openapi_document_json()
    for path in ("/-/otel/api/metrics/list", "/-/otel/api/metrics/query"):
        operation = document["paths"][path]["post"]
        assert operation["requestBody"]["content"]["application/json"]["schema"]
        assert operation["responses"]["200"]
