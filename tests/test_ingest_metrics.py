"""Ticket 03 acceptance: POST /v1/metrics decodes, stores and prunes;
POST /v1/logs stays a stub."""

from __future__ import annotations

import gzip
import json
import sqlite3
import time

import pytest
import test_otlp_metrics as _otlp_metrics_tests
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from test_otlp_metrics import SPAN_ID, TRACE_ID

from datasette_otel_receiver import ingest, store


def _recent_timestamps() -> tuple[int, int]:
    "Recent enough to survive the retention prune that runs after every ingest."
    now = time.time_ns() - 2_000_000_000
    return now, now - 1_000_000_000


def protobuf_metrics_body() -> bytes:
    "build_request() from test_otlp_metrics, retimed to the present."
    time_ns, start_ns = _recent_timestamps()
    saved = (_otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS)
    _otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS = time_ns, start_ns
    try:
        return _otlp_metrics_tests.build_request().SerializeToString()
    finally:
        _otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS = saved


def json_metrics_body() -> bytes:
    "build_json_body() from test_otlp_metrics, retimed like protobuf_metrics_body."
    time_ns, start_ns = _recent_timestamps()
    saved = (_otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS)
    _otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS = time_ns, start_ns
    try:
        return _otlp_metrics_tests.build_json_body()
    finally:
        _otlp_metrics_tests.TIME_NS, _otlp_metrics_tests.START_NS = saved


def _three_point_gauge_body() -> bytes:
    "A minimal single-metric request, for exercising size-based pruning."
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.add(key="service.name").value.string_value = "svc"
    metric = rm.scope_metrics.add().metrics.add()
    metric.name = "app.gauge3"
    now, _start = _recent_timestamps()
    for i in range(3):
        point = metric.gauge.data_points.add()
        point.time_unix_nano = now + i
        point.as_int = i
    return req.SerializeToString()


def raw_metric_rows(db_path, metric_name=None):
    "Read metric_points through plain sqlite3, like conftest.raw_span_rows."
    conn = sqlite3.connect(db_path)
    try:
        sql = "select metric_name, service_name, time_ns, exemplars from metric_points"
        params: tuple = ()
        if metric_name is not None:
            sql += " where metric_name = ?"
            params = (metric_name,)
        sql += " order by id"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def raw_metrics_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("select count(*) from metrics").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unconfigured_503(make_ds):
    ds = await make_ds()
    response = await ds.client.post(
        "/v1/metrics",
        content=protobuf_metrics_body(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_bad_token_401(make_ds):
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.post(
        "/v1/metrics",
        content=protobuf_metrics_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer wrong",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_protobuf_all_types_stored(make_ds, tmp_path):
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.post(
        "/v1/metrics",
        content=protobuf_metrics_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"

    db_path = tmp_path / "otel.db"
    assert raw_metrics_count(db_path) == 6  # one row per metric name
    # gauge x2, cumulative sum, delta sum, histogram x2, exp histogram, summary
    assert len(raw_metric_rows(db_path)) == 8


@pytest.mark.asyncio
async def test_json_all_types_stored(make_ds, tmp_path):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    response = await ds.client.post(
        "/v1/metrics",
        content=json_metrics_body(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"partialSuccess": {}}

    db_path = tmp_path / "otel.db"
    assert raw_metrics_count(db_path) == 6
    assert len(raw_metric_rows(db_path)) == 8

    gauge_rows = raw_metric_rows(db_path, metric_name="app.gauge")
    assert len(gauge_rows) == 2
    (exemplar,) = json.loads(gauge_rows[0][3])
    assert exemplar["span_id"] == SPAN_ID.hex()
    assert exemplar["trace_id"] == TRACE_ID.hex()


@pytest.mark.asyncio
async def test_gzip_body(make_ds, tmp_path):
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.post(
        "/v1/metrics",
        content=gzip.compress(protobuf_metrics_body()),
        headers={
            "content-type": "application/x-protobuf",
            "content-encoding": "gzip",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200
    db_path = tmp_path / "otel.db"
    assert raw_metrics_count(db_path) == 6
    assert len(raw_metric_rows(db_path)) == 8


@pytest.mark.asyncio
async def test_malformed_400_and_415(make_ds):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    bad_protobuf = await ds.client.post(
        "/v1/metrics",
        content=b"\xff\xfe",
        headers={"content-type": "application/x-protobuf"},
    )
    assert bad_protobuf.status_code == 400

    bad_json = await ds.client.post(
        "/v1/metrics", content=b"{", headers={"content-type": "application/json"}
    )
    assert bad_json.status_code == 400

    unsupported = await ds.client.post(
        "/v1/metrics", content=b"whatever", headers={"content-type": "text/plain"}
    )
    assert unsupported.status_code == 415


@pytest.mark.asyncio
async def test_resend_appends_no_dedupe(make_ds, tmp_path):
    ds = await make_ds(ingest_token="s3cret")
    body = protobuf_metrics_body()
    for _ in range(2):  # retried batch must double points, not metrics rows
        response = await ds.client.post(
            "/v1/metrics",
            content=body,
            headers={
                "content-type": "application/x-protobuf",
                "authorization": "Bearer s3cret",
            },
        )
        assert response.status_code == 200

    db_path = tmp_path / "otel.db"
    assert raw_metrics_count(db_path) == 6
    assert len(raw_metric_rows(db_path)) == 16


@pytest.mark.asyncio
async def test_empty_object_is_ok(make_ds, tmp_path):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    response = await ds.client.post(
        "/v1/metrics", content=b"{}", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    db_path = tmp_path / "otel.db"
    assert raw_metrics_count(db_path) == 0
    assert raw_metric_rows(db_path) == []


@pytest.mark.asyncio
async def test_prune_runs_after_ingest(make_ds, tmp_path):
    ds = await make_ds(ingest_token="s3cret", max_metric_points=2)
    response = await ds.client.post(
        "/v1/metrics",
        content=_three_point_gauge_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200
    await store.maybe_prune(ds, force=True)
    assert len(raw_metric_rows(tmp_path / "otel.db")) == 2


@pytest.mark.asyncio
async def test_logs_still_stub(make_ds):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    before = ingest.STUB_COUNTERS["logs"]
    response = await ds.client.post(
        "/v1/logs", content=b"{}", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert ingest.STUB_COUNTERS["logs"] == before + 1
