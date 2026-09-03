"""Ticket 02 acceptance: OTLP metrics decoding (protobuf + JSON) into rows.

Pure decoder tests, no Datasette/store involved (only the store's column
tuples are imported, to assert row-dict keys match the storage contract).
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import AggregationTemporality
from test_ingest import protobuf_body

from datasette_otel_receiver import otlp

TRACE_ID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
SPAN_ID = bytes.fromhex("b7ad6b7169203331")
TIME_NS = 1_700_000_000_000_000_000
START_NS = TIME_NS - 1_000_000_000


def build_request() -> ExportMetricsServiceRequest:
    """One request touching all five metric types; tests pull points out by
    metric name. Point order within each metric is fixed and documented at
    each call site below."""
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    kv = rm.resource.attributes.add()
    kv.key = "service.name"
    kv.value.string_value = "flask-app"
    sm = rm.scope_metrics.add()
    sm.scope.name = "myscope"

    # app.gauge: point 0 as_int (+ exemplar), point 1 as_double.
    gauge = sm.metrics.add()
    gauge.name = "app.gauge"
    g0 = gauge.gauge.data_points.add()
    g0.time_unix_nano = TIME_NS
    g0.as_int = 7
    kv = g0.attributes.add()
    kv.key = "host"
    kv.value.string_value = "a"
    ex = g0.exemplars.add()
    ex.time_unix_nano = TIME_NS
    ex.as_double = 1.5
    ex.span_id = SPAN_ID
    ex.trace_id = TRACE_ID
    g1 = gauge.gauge.data_points.add()
    g1.time_unix_nano = TIME_NS
    g1.as_double = 3.5

    # app.sum.cumulative: monotonic cumulative sum, start_ns set.
    csum = sm.metrics.add()
    csum.name = "app.sum.cumulative"
    csum.sum.aggregation_temporality = (
        AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    )
    csum.sum.is_monotonic = True
    c0 = csum.sum.data_points.add()
    c0.start_time_unix_nano = START_NS
    c0.time_unix_nano = TIME_NS
    c0.as_int = 42

    # app.sum.delta: non-monotonic delta sum.
    dsum = sm.metrics.add()
    dsum.name = "app.sum.delta"
    dsum.sum.aggregation_temporality = (
        AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
    )
    dsum.sum.is_monotonic = False
    d0 = dsum.sum.data_points.add()
    d0.time_unix_nano = TIME_NS
    d0.as_int = 5

    # app.histogram: point 0 has sum/min/max, point 1 omits sum.
    hist = sm.metrics.add()
    hist.name = "app.histogram"
    hist.histogram.aggregation_temporality = (
        AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    )
    h0 = hist.histogram.data_points.add()
    h0.time_unix_nano = TIME_NS
    h0.count = 10
    h0.sum = 55.0
    h0.min = 1.0
    h0.max = 9.0
    h0.bucket_counts.extend([1, 2, 7])
    h0.explicit_bounds.extend([1.0, 5.0])
    h1 = hist.histogram.data_points.add()
    h1.time_unix_nano = TIME_NS
    h1.count = 3
    h1.bucket_counts.extend([1, 1, 1])
    h1.explicit_bounds.extend([1.0, 5.0])

    # app.exp_histogram
    ehist = sm.metrics.add()
    ehist.name = "app.exp_histogram"
    ehist.exponential_histogram.aggregation_temporality = (
        AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    )
    e0 = ehist.exponential_histogram.data_points.add()
    e0.time_unix_nano = TIME_NS
    e0.count = 5
    e0.sum = 10.0
    e0.scale = 2
    e0.zero_count = 0
    e0.positive.offset = -2
    e0.positive.bucket_counts.extend([1, 1])
    e0.negative.offset = 0
    e0.negative.bucket_counts.extend([2])

    # app.summary
    summary = sm.metrics.add()
    summary.name = "app.summary"
    s0 = summary.summary.data_points.add()
    s0.time_unix_nano = TIME_NS
    s0.count = 100
    s0.sum = 42.0
    q0 = s0.quantile_values.add()
    q0.quantile = 0.5
    q0.value = 1.0
    q1 = s0.quantile_values.add()
    q1.quantile = 0.99
    q1.value = 2.0

    return req


def build_json_body() -> bytes:
    """OTLP/JSON mirror of build_request(): int64 fields as strings, exemplar
    span/trace ids as hex text (the quirk `_rewrite_metrics_hex_ids` fixes)."""
    return json.dumps(
        {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "flask-app"},
                            }
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "myscope"},
                            "metrics": [
                                {
                                    "name": "app.gauge",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "asInt": "7",
                                                "attributes": [
                                                    {
                                                        "key": "host",
                                                        "value": {"stringValue": "a"},
                                                    }
                                                ],
                                                "exemplars": [
                                                    {
                                                        "timeUnixNano": str(TIME_NS),
                                                        "asDouble": 1.5,
                                                        "spanId": SPAN_ID.hex(),
                                                        "traceId": TRACE_ID.hex(),
                                                    }
                                                ],
                                            },
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "asDouble": 3.5,
                                            },
                                        ]
                                    },
                                },
                                {
                                    "name": "app.sum.cumulative",
                                    "sum": {
                                        "aggregationTemporality": 2,
                                        "isMonotonic": True,
                                        "dataPoints": [
                                            {
                                                "startTimeUnixNano": str(START_NS),
                                                "timeUnixNano": str(TIME_NS),
                                                "asInt": "42",
                                            }
                                        ],
                                    },
                                },
                                {
                                    "name": "app.sum.delta",
                                    "sum": {
                                        "aggregationTemporality": 1,
                                        "isMonotonic": False,
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "asInt": "5",
                                            }
                                        ],
                                    },
                                },
                                {
                                    "name": "app.histogram",
                                    "histogram": {
                                        "aggregationTemporality": 2,
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "count": "10",
                                                "sum": 55.0,
                                                "min": 1.0,
                                                "max": 9.0,
                                                "bucketCounts": ["1", "2", "7"],
                                                "explicitBounds": [1.0, 5.0],
                                            },
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "count": "3",
                                                "bucketCounts": ["1", "1", "1"],
                                                "explicitBounds": [1.0, 5.0],
                                            },
                                        ],
                                    },
                                },
                                {
                                    "name": "app.exp_histogram",
                                    "exponentialHistogram": {
                                        "aggregationTemporality": 2,
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "count": "5",
                                                "sum": 10.0,
                                                "scale": 2,
                                                "zeroCount": "0",
                                                "positive": {
                                                    "offset": -2,
                                                    "bucketCounts": ["1", "1"],
                                                },
                                                "negative": {
                                                    "offset": 0,
                                                    "bucketCounts": ["2"],
                                                },
                                            }
                                        ],
                                    },
                                },
                                {
                                    "name": "app.summary",
                                    "summary": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(TIME_NS),
                                                "count": "100",
                                                "sum": 42.0,
                                                "quantileValues": [
                                                    {"quantile": 0.5, "value": 1.0},
                                                    {"quantile": 0.99, "value": 2.0},
                                                ],
                                            }
                                        ]
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()


def _by_metric(metrics, name):
    return next(m for m in metrics if m["name"] == name)


def _points_for(points, name):
    return [p for p in points if p["metric_name"] == name]


def test_gauge_int_and_double():
    metrics, points = otlp.metrics_request_to_rows(build_request())
    m = _by_metric(metrics, "app.gauge")
    assert m["type"] == "gauge"
    assert m["temporality"] is None
    assert m["monotonic"] is None
    p0, p1 = _points_for(points, "app.gauge")
    assert p0["value_int"] == 7 and p0["value_double"] is None
    assert p1["value_double"] == 3.5 and p1["value_int"] is None


def test_monotonic_cumulative_sum():
    metrics, points = otlp.metrics_request_to_rows(build_request())
    m = _by_metric(metrics, "app.sum.cumulative")
    assert m["type"] == "sum"
    assert m["temporality"] == "cumulative"
    assert m["monotonic"] == 1
    (p,) = _points_for(points, "app.sum.cumulative")
    assert p["start_ns"] == START_NS
    assert p["value_int"] == 42


def test_delta_sum():
    metrics, _points = otlp.metrics_request_to_rows(build_request())
    m = _by_metric(metrics, "app.sum.delta")
    assert m["temporality"] == "delta"
    assert m["monotonic"] == 0


def test_histogram_explicit_bounds():
    _metrics, points = otlp.metrics_request_to_rows(build_request())
    p0, p1 = _points_for(points, "app.histogram")
    assert p0["count"] == 10
    assert p0["sum"] == 55.0
    assert p0["min"] == 1.0
    assert p0["max"] == 9.0
    assert json.loads(p0["bucket_counts"]) == [1, 2, 7]
    assert json.loads(p0["explicit_bounds"]) == [1.0, 5.0]
    assert p1["sum"] is None


def test_exponential_histogram():
    _metrics, points = otlp.metrics_request_to_rows(build_request())
    (p,) = _points_for(points, "app.exp_histogram")
    assert p["exp_scale"] == 2
    assert p["exp_zero_count"] == 0
    assert json.loads(p["exp_positive"]) == {"offset": -2, "bucket_counts": [1, 1]}
    assert json.loads(p["exp_negative"]) == {"offset": 0, "bucket_counts": [2]}


def test_summary():
    _metrics, points = otlp.metrics_request_to_rows(build_request())
    (p,) = _points_for(points, "app.summary")
    assert json.loads(p["quantile_values"]) == [[0.5, 1.0], [0.99, 2.0]]
    assert p["count"] == 100
    assert p["sum"] == 42.0


def test_attributes_resource_scope_service():
    _metrics, points = otlp.metrics_request_to_rows(build_request())
    p0, _p1 = _points_for(points, "app.gauge")
    assert json.loads(p0["attributes"]) == {"host": "a"}
    assert json.loads(p0["resource"]) == {"service.name": "flask-app"}
    assert p0["service_name"] == "flask-app"
    assert p0["scope_name"] == "myscope"


def test_exemplars_hex_ids():
    _metrics, points = otlp.metrics_request_to_rows(build_request())
    p0, _p1 = _points_for(points, "app.gauge")
    (exemplar,) = json.loads(p0["exemplars"])
    assert exemplar["span_id"] == "b7ad6b7169203331"
    assert exemplar["trace_id"] == TRACE_ID.hex()
    assert exemplar["value"] == 1.5


def test_json_body_parses_int64_strings_and_hex_exemplar_ids():
    req = otlp.parse_metrics_body(build_json_body(), "application/json")
    metrics, points = otlp.metrics_request_to_rows(req)

    proto_metrics, proto_points = otlp.metrics_request_to_rows(build_request())
    assert metrics == proto_metrics
    assert points == proto_points

    p0, _p1 = _points_for(points, "app.gauge")
    (exemplar,) = json.loads(p0["exemplars"])
    assert exemplar["span_id"] == SPAN_ID.hex()
    assert exemplar["trace_id"] == TRACE_ID.hex()


def test_metric_rows_deduped_within_request():
    req = ExportMetricsServiceRequest()

    rm0 = req.resource_metrics.add()
    rm0.resource.attributes.add(key="service.name").value.string_value = "svc-a"
    metric0 = rm0.scope_metrics.add().metrics.add()
    metric0.name = "dup.metric"
    p0 = metric0.gauge.data_points.add()
    p0.time_unix_nano = TIME_NS
    p0.as_int = 1

    rm1 = req.resource_metrics.add()
    rm1.resource.attributes.add(key="service.name").value.string_value = "svc-b"
    metric1 = rm1.scope_metrics.add().metrics.add()
    metric1.name = "dup.metric"
    p1 = metric1.gauge.data_points.add()
    p1.time_unix_nano = TIME_NS
    p1.as_int = 2

    metrics, points = otlp.metrics_request_to_rows(req)
    assert len(metrics) == 1
    assert metrics[0]["name"] == "dup.metric"
    assert len(points) == 2
    assert {p["service_name"] for p in points} == {"svc-a", "svc-b"}


def test_unknown_content_type_and_bad_bodies():
    with pytest.raises(otlp.UnsupportedContentType):
        otlp.parse_metrics_body(b"x", "text/plain")
    with pytest.raises(otlp.DecodeError):
        otlp.parse_metrics_body(b"\xff", "application/x-protobuf")
    with pytest.raises(otlp.DecodeError):
        otlp.parse_metrics_body(b"{", "application/json")
    with pytest.raises(otlp.DecodeError):
        otlp.parse_metrics_body(b"[]", "application/json")


def test_row_keys_match_store_contract():
    from datasette_otel_receiver import store

    metrics, points = otlp.metrics_request_to_rows(build_request())
    for m in metrics:
        assert set(m.keys()) == set(store.METRIC_COLUMNS)
    for p in points:
        assert set(p.keys()) <= set(store.METRIC_POINT_COLUMNS)


def test_parse_body_for_traces_unchanged():
    req = otlp.parse_body(protobuf_body(), "application/x-protobuf")
    assert len(req.resource_spans[0].scope_spans[0].spans) == 1
