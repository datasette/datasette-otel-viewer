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
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
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


def parse_body(body: bytes, content_type: str) -> ExportTraceServiceRequest:
    """Decode an OTLP/HTTP request body into an ExportTraceServiceRequest.

    Both `application/x-protobuf` and `application/json` decode through this
    one message type, per the OTLP/HTTP spec: both encodings, one message type.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    req = ExportTraceServiceRequest()

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
        data = _rewrite_hex_ids_to_base64(data)
        try:
            Parse(json.dumps(data), req)
        except ParseError as exc:
            raise DecodeError(f"invalid OTLP JSON body: {exc}") from exc
        return req

    raise UnsupportedContentType(content_type)


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
