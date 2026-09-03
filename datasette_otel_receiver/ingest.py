"""OTLP ingest routes, ported from datasette-otel-debugger.

Top-level ``POST /v1/traces`` and ``POST /v1/metrics`` (stock OTel exporters
append these paths to ``OTEL_EXPORTER_OTLP_ENDPOINT``, so they must not live
under ``/-/``), plus an accept-and-discard stub for ``/v1/logs`` so the
default ``opentelemetry-instrument`` logs export doesn't spam the sender with
404s.

One change from the donor: ``insert_spans`` already runs inside
``store.suppress()`` — necessary here because this instance may have a live
TracerProvider (self mode, or a sibling plugin's), which the debugger never
did. The ingest request's own HTTP span still records (one span per POST,
that's the instrumentation working); the spans about *storing* the batch do
not.
"""

from __future__ import annotations

import gzip
import secrets

from datasette.utils.asgi import Response
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)

from . import otlp, store

STUB_COUNTERS = {"logs": 0}


def _plugin_config(datasette) -> dict:
    return datasette.plugin_config(store.PLUGIN_NAME) or {}


def ingest_enabled(datasette) -> bool:
    """The receiver is opt-in: enabled only when an ``ingest_token`` is
    configured, or ``allow_unauthenticated_ingest: true`` explicitly waives
    auth (the localhost-dev escape hatch)."""
    config = _plugin_config(datasette)
    return bool(config.get("ingest_token")) or (
        config.get("allow_unauthenticated_ingest") is True
    )


def _check_auth(datasette, request) -> Response | None:
    "Error Response when the request fails auth, else None to proceed."
    config = _plugin_config(datasette)
    token = config.get("ingest_token")

    if not token:
        if config.get("allow_unauthenticated_ingest") is True:
            return None
        return Response.text(
            f"{store.PLUGIN_NAME}: ingest is disabled - no ingest_token "
            "configured and allow_unauthenticated_ingest is not set",
            status=503,
        )

    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, token):
        return Response.text("Unauthorized", status=401)
    return None


def _normalize_content_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


async def _read_body(request) -> bytes:
    "Raw POST body, transparently un-gzipped when Content-Encoding: gzip."
    body = await request.post_body()
    encoding = (request.headers.get("content-encoding") or "").strip().lower()
    if encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise otlp.DecodeError(f"invalid gzip body: {exc}") from exc
    return body


def _success_response(content_type: str, response_message_cls) -> Response:
    "Encoding-matched empty success response."
    if _normalize_content_type(content_type) == "application/x-protobuf":
        return Response(
            response_message_cls().SerializeToString(),
            status=200,
            content_type="application/x-protobuf",
        )
    return Response.json({"partialSuccess": {}})


async def traces_view(request, datasette):
    if request.method != "POST":
        return Response.text("Method not allowed", status=405)

    auth_error = _check_auth(datasette, request)
    if auth_error is not None:
        return auth_error

    content_type = request.headers.get("content-type") or ""
    try:
        body = await _read_body(request)
        req = otlp.parse_body(body, content_type)
    except otlp.UnsupportedContentType:
        return Response.text(f"unsupported content-type: {content_type!r}", status=415)
    except otlp.DecodeError as exc:
        return Response.text(str(exc), status=400)

    rows = otlp.request_to_rows(req)
    await store.insert_spans(datasette, rows)
    await store.maybe_prune(datasette)

    return _success_response(content_type, ExportTraceServiceResponse)


async def metrics_view(request, datasette):
    if request.method != "POST":
        return Response.text("Method not allowed", status=405)

    auth_error = _check_auth(datasette, request)
    if auth_error is not None:
        return auth_error

    content_type = request.headers.get("content-type") or ""
    try:
        body = await _read_body(request)
        req = otlp.parse_metrics_body(body, content_type)
    except otlp.UnsupportedContentType:
        return Response.text(f"unsupported content-type: {content_type!r}", status=415)
    except otlp.DecodeError as exc:
        return Response.text(str(exc), status=400)

    metrics, points = otlp.metrics_request_to_rows(req)
    await store.insert_metrics(datasette, metrics, points)
    await store.maybe_prune(datasette)

    return _success_response(content_type, ExportMetricsServiceResponse)


async def stub_view(request, datasette):
    "Accept-and-discard handler for /v1/logs."
    if request.method != "POST":
        return Response.text("Method not allowed", status=405)

    auth_error = _check_auth(datasette, request)
    if auth_error is not None:
        return auth_error

    await request.post_body()
    STUB_COUNTERS["logs"] += 1
    content_type = request.headers.get("content-type") or ""
    return _success_response(content_type, ExportLogsServiceResponse)
