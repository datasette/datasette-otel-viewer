"""Ticket 04 acceptance: OTLP protobuf + JSON ingest, auth, stubs."""

import json

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from conftest import raw_span_rows

TRACE_ID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
SPAN_ID = bytes.fromhex("b7ad6b7169203331")

# Recent timestamps: spans dated 1970 would be silently age-pruned by the
# retention pass that runs right after every ingest.
import time

START_NS = time.time_ns() - 1_000_000_000
END_NS = START_NS + 5_000_000


def protobuf_body(name="remote-span", service="flask-app"):
    req = ExportTraceServiceRequest()
    resource_span = req.resource_spans.add()
    kv = resource_span.resource.attributes.add()
    kv.key = "service.name"
    kv.value.string_value = service
    scope_span = resource_span.scope_spans.add()
    span = scope_span.spans.add()
    span.trace_id = TRACE_ID
    span.span_id = SPAN_ID
    span.name = name
    span.kind = 2  # SERVER
    span.start_time_unix_nano = START_NS
    span.end_time_unix_nano = END_NS
    return req.SerializeToString()


def json_body(name="json-span"):
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "deno-app"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": TRACE_ID.hex(),
                                    "spanId": SPAN_ID.hex(),
                                    "name": name,
                                    "kind": 2,
                                    "startTimeUnixNano": str(START_NS),
                                    "endTimeUnixNano": str(END_NS),
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_unconfigured_ingest_503s(make_ds):
    ds = await make_ds()
    response = await ds.client.post(
        "/v1/traces",
        content=protobuf_body(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_protobuf_ingest_with_token(make_ds, tmp_path):
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.post(
        "/v1/traces",
        content=protobuf_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer s3cret",
        },
    )
    assert response.status_code == 200
    rows = raw_span_rows(tmp_path / "otel.db")
    assert (TRACE_ID.hex(), "remote-span") in {(r[0], r[1]) for r in rows}


@pytest.mark.asyncio
async def test_bad_token_401(make_ds):
    ds = await make_ds(ingest_token="s3cret")
    response = await ds.client.post(
        "/v1/traces",
        content=protobuf_body(),
        headers={
            "content-type": "application/x-protobuf",
            "authorization": "Bearer wrong",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_json_ingest_unauthenticated_escape_hatch(make_ds, tmp_path):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    response = await ds.client.post(
        "/v1/traces",
        content=json_body(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"partialSuccess": {}}
    names = {r[1] for r in raw_span_rows(tmp_path / "otel.db")}
    assert "json-span" in names


@pytest.mark.asyncio
async def test_metrics_and_logs_stubs_accept(make_ds):
    ds = await make_ds(allow_unauthenticated_ingest=True)
    for path in ("/v1/metrics", "/v1/logs"):
        response = await ds.client.post(
            path, content=b"{}", headers={"content-type": "application/json"}
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_traces_summary_derived(make_ds, tmp_path):
    import sqlite3

    ds = await make_ds(ingest_token="s3cret")
    for _ in range(2):  # retried batch must not double-count
        await ds.client.post(
            "/v1/traces",
            content=protobuf_body(),
            headers={
                "content-type": "application/x-protobuf",
                "authorization": "Bearer s3cret",
            },
        )
    conn = sqlite3.connect(tmp_path / "otel.db")
    row = conn.execute(
        "select span_count, name, service_name from traces where trace_id = ?",
        [TRACE_ID.hex()],
    ).fetchone()
    conn.close()
    assert row == (1, "remote-span", "flask-app")
