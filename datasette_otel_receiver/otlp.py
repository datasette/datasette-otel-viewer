"""Decode OTLP trace export requests (protobuf and JSON) into span rows.

Pure functions over `opentelemetry-proto` message objects. This module must
not import anything from the rest of `datasette_otel_receiver` so it stays
unit-testable in isolation and safe to develop in parallel with the rest of
the plugin.
"""

from __future__ import annotations

import base64
import json

from google.protobuf.json_format import Parse, ParseError
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.metrics.v1.metrics_pb2 import AggregationTemporality
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status


class UnsupportedContentType(Exception):
    """Raised when `parse_body` is given a Content-Type it doesn't decode."""


class DecodeError(Exception):
    """Raised when the request body can't be decoded as OTLP protobuf/JSON."""


# The three OTLP/JSON fields that the spec encodes as hex strings, unlike
# every other protobuf `bytes` field (which uses base64 per the standard
# protobuf JSON mapping). See the OTLP 1.x spec, "JSON Protobuf Encoding":
# traceId/spanId/parentSpanId are hex; everything else stays base64.
_HEX_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def _hex_to_base64(value: str) -> str:
    try:
        raw = bytes.fromhex(value)
    except (ValueError, TypeError) as exc:
        raise DecodeError(f"invalid hex id {value!r}: {exc}") from exc
    return base64.b64encode(raw).decode("ascii")


def _fix_hex_ids(obj: dict) -> None:
    for key in _HEX_ID_FIELDS:
        value = obj.get(key)
        if value:
            obj[key] = _hex_to_base64(value)


def _rewrite_hex_ids_to_base64(data: dict) -> dict:
    """Walk a parsed OTLP/JSON dict, re-encoding hex ids to base64 in place.

    Covers span-level traceId/spanId/parentSpanId as well as the same fields
    inside each span's `links[]` (Span.Link only carries traceId/spanId, no
    parent).
    """
    for resource_span in data.get("resourceSpans") or []:
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                _fix_hex_ids(span)
                for link in span.get("links") or []:
                    _fix_hex_ids(link)
    return data


def _parse(body: bytes, content_type: str, message_cls, rewrite_json):
    """Shared protobuf/JSON decoding for both `parse_body` and
    `parse_metrics_body`: only the message type and the JSON hex-id rewrite
    differ between signals.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    req = message_cls()

    if ct == "application/x-protobuf":
        try:
            req.ParseFromString(body)
        except Exception as exc:
            raise DecodeError(f"invalid protobuf body: {exc}") from exc
        return req

    if ct == "application/json":
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DecodeError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise DecodeError("invalid JSON body: expected a JSON object")
        data = rewrite_json(data)
        try:
            Parse(json.dumps(data), req)
        except ParseError as exc:
            raise DecodeError(f"invalid OTLP JSON body: {exc}") from exc
        return req

    raise UnsupportedContentType(content_type)


def parse_body(body: bytes, content_type: str) -> ExportTraceServiceRequest:
    """Decode an OTLP/HTTP request body into an ExportTraceServiceRequest.

    Both `application/x-protobuf` and `application/json` decode through this
    one message type, per the OTLP/HTTP spec: both encodings, one message type.
    """
    return _parse(
        body, content_type, ExportTraceServiceRequest, _rewrite_hex_ids_to_base64
    )


_METRIC_DATA_KEYS = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")


def _rewrite_metrics_hex_ids(data: dict) -> dict:
    """OTLP/JSON encodes exemplar spanId/traceId as hex (like span ids) while
    protobuf-JSON expects base64. Without this rewrite Parse() silently
    decodes the hex text as base64 into wrong bytes."""
    for resource_metrics in data.get("resourceMetrics") or []:
        for scope_metrics in resource_metrics.get("scopeMetrics") or []:
            for metric in scope_metrics.get("metrics") or []:
                for key in _METRIC_DATA_KEYS:
                    for point in (metric.get(key) or {}).get("dataPoints") or []:
                        for exemplar in point.get("exemplars") or []:
                            _fix_hex_ids(exemplar)
    return data


def parse_metrics_body(body: bytes, content_type: str) -> ExportMetricsServiceRequest:
    """Decode an OTLP/HTTP request body into an ExportMetricsServiceRequest."""
    return _parse(
        body, content_type, ExportMetricsServiceRequest, _rewrite_metrics_hex_ids
    )


def _flatten_any_value(value: AnyValue):
    """Flatten an OTLP AnyValue into a plain Python value.

    Covers all 7 value kinds: string, bool, int, double, array, kvlist,
    bytes (rendered as hex). The experimental `*_strindex` dictionary-encoded
    variants are not part of the stable v1 wire format and are not handled.
    """
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    if kind == "bytes_value":
        return value.bytes_value.hex()
    if kind == "array_value":
        return [_flatten_any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return {
            kv.key: _flatten_any_value(kv.value) for kv in value.kvlist_value.values
        }
    return None


def _flatten_attributes(attributes) -> dict:
    """Flatten a repeated KeyValue field into a plain dict."""
    result = {}
    for kv in attributes:
        kv: KeyValue
        result[kv.key] = _flatten_any_value(kv.value)
    return result


def request_to_rows(req: ExportTraceServiceRequest) -> list[dict]:
    """Flatten resource_spans[] -> scope_spans[] -> spans[] into row dicts.

    Row shape matches `store.COLUMNS`: trace_id, span_id,
    parent_span_id, name, kind, start_ns, end_ns, duration_ms, status,
    status_description, service_name, http_route, http_status,
    db_namespace, db_operation, db_query_text, attributes, resource,
    scope_name, scope_version, schema_url.
    """
    rows: list[dict] = []

    for resource_span in req.resource_spans:
        resource_attrs = _flatten_attributes(resource_span.resource.attributes)
        resource_json = json.dumps(resource_attrs)
        service_name = resource_attrs.get("service.name")
        resource_schema_url = resource_span.schema_url or None

        for scope_span in resource_span.scope_spans:
            scope = scope_span.scope
            scope_name = scope.name or None
            scope_version = scope.version or None
            # Prefer the scope_spans schema_url, fall back to resource_spans.
            # Recorded as-claimed by the sender, never normalized.
            schema_url = scope_span.schema_url or resource_schema_url

            for span in scope_span.spans:
                attrs = _flatten_attributes(span.attributes)

                start_ns = span.start_time_unix_nano
                end_ns = span.end_time_unix_nano

                kind_name = Span.SpanKind.Name(span.kind).removeprefix("SPAN_KIND_")
                status_name = Status.StatusCode.Name(span.status.code).removeprefix(
                    "STATUS_CODE_"
                )

                rows.append(
                    {
                        "trace_id": span.trace_id.hex(),
                        "span_id": span.span_id.hex(),
                        "parent_span_id": span.parent_span_id.hex() or None,
                        "name": span.name,
                        "kind": kind_name,
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                        "duration_ms": (end_ns - start_ns) / 1_000_000,
                        "status": status_name,
                        "status_description": span.status.message or None,
                        "service_name": service_name,
                        "http_route": attrs.get("http.route"),
                        "http_status": attrs.get("http.response.status_code"),
                        "db_namespace": attrs.get("db.namespace"),
                        "db_operation": attrs.get("db.operation.name"),
                        "db_query_text": attrs.get("db.query.text"),
                        "attributes": json.dumps(attrs),
                        "resource": resource_json,
                        "scope_name": scope_name,
                        "scope_version": scope_version,
                        "schema_url": schema_url,
                    }
                )

    return rows


def _temporality(value: int) -> str | None:
    """AggregationTemporality enum -> store's `delta`/`cumulative`/None.

    0 (unspecified) covers gauges and summaries, which don't carry the field.
    """
    if value == AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA:
        return "delta"
    if value == AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE:
        return "cumulative"
    return None


def _exemplars(exemplars) -> str:
    """Flatten Exemplar[] into the JSON array stored in metric_points.exemplars.

    `span_id`/`trace_id` render as hex (empty bytes -> None) rather than the
    raw bytes protobuf gives back, since this is going straight into a JSON
    column, not another protobuf message.
    """
    out = []
    for e in exemplars:
        kind = e.WhichOneof("value")
        out.append(
            {
                "time_ns": e.time_unix_nano,
                "value": getattr(e, kind) if kind else None,
                "span_id": e.span_id.hex() or None,
                "trace_id": e.trace_id.hex() or None,
                "filtered_attributes": _flatten_attributes(e.filtered_attributes),
            }
        )
    return json.dumps(out)


def _number_value(point) -> tuple[float | None, int | None]:
    """NumberDataPoint's `value` oneof -> (value_double, value_int)."""
    which = point.WhichOneof("value")
    return (
        point.as_double if which == "as_double" else None,
        point.as_int if which == "as_int" else None,
    )


def metrics_request_to_rows(
    req: ExportMetricsServiceRequest,
) -> tuple[list[dict], list[dict]]:
    """Flatten resource_metrics[] -> scope_metrics[] -> metrics[] -> data
    points into row dicts.

    Row shapes match `store.METRIC_COLUMNS` / `store.METRIC_POINT_COLUMNS`.
    Metric rows are deduped by name within one request (last definition
    wins), matching the upsert `store.insert_metrics` does with them.
    """
    metrics_by_name: dict[str, dict] = {}
    points: list[dict] = []

    for rm in req.resource_metrics:
        resource_attrs = _flatten_attributes(rm.resource.attributes)
        resource_json = json.dumps(resource_attrs)
        service_name = resource_attrs.get("service.name")

        for sm in rm.scope_metrics:
            scope_name = sm.scope.name or None

            for metric in sm.metrics:
                # One of gauge/sum/histogram/exponential_histogram/summary;
                # a metric with none set carries no data points to record.
                kind = metric.WhichOneof("data")
                if kind is None:
                    continue
                data = getattr(metric, kind)
                metrics_by_name[metric.name] = {
                    "name": metric.name,
                    "description": metric.description or None,
                    "unit": metric.unit or None,
                    "type": kind,
                    "temporality": _temporality(
                        getattr(data, "aggregation_temporality", 0)
                    ),
                    "monotonic": (int(data.is_monotonic) if kind == "sum" else None),
                }

                for p in data.data_points:
                    row = {
                        "metric_name": metric.name,
                        "service_name": service_name,
                        "scope_name": scope_name,
                        "start_ns": p.start_time_unix_nano or None,
                        "time_ns": p.time_unix_nano,
                        "attributes": json.dumps(_flatten_attributes(p.attributes)),
                        "exemplars": _exemplars(getattr(p, "exemplars", ())),
                        "resource": resource_json,
                        "flags": p.flags or None,
                    }
                    if kind in ("gauge", "sum"):
                        row["value_double"], row["value_int"] = _number_value(p)
                    elif kind == "histogram":
                        row.update(
                            count=p.count,
                            sum=p.sum if p.HasField("sum") else None,
                            min=p.min if p.HasField("min") else None,
                            max=p.max if p.HasField("max") else None,
                            bucket_counts=json.dumps(list(p.bucket_counts)),
                            explicit_bounds=json.dumps(list(p.explicit_bounds)),
                        )
                    elif kind == "exponential_histogram":
                        row.update(
                            count=p.count,
                            sum=p.sum if p.HasField("sum") else None,
                            min=p.min if p.HasField("min") else None,
                            max=p.max if p.HasField("max") else None,
                            exp_scale=p.scale,
                            exp_zero_count=p.zero_count,
                            exp_positive=json.dumps(
                                {
                                    "offset": p.positive.offset,
                                    "bucket_counts": list(p.positive.bucket_counts),
                                }
                            ),
                            exp_negative=json.dumps(
                                {
                                    "offset": p.negative.offset,
                                    "bucket_counts": list(p.negative.bucket_counts),
                                }
                            ),
                        )
                    elif kind == "summary":
                        row.update(
                            count=p.count,
                            sum=p.sum,
                            quantile_values=json.dumps(
                                [[q.quantile, q.value] for q in p.quantile_values]
                            ),
                        )
                    points.append(row)

    return list(metrics_by_name.values()), points
