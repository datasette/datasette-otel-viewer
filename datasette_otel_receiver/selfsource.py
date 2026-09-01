"""Self source: store the spans this Datasette emits, in this Datasette.

The productized demos/otel/self_storage workaround. The feedback loop — the
store's own INSERTs go through Datasette's instrumented write path and emit
three spans each, gain 3 per generation — is cut by a context key
(store.SUPPRESS_KEY) attached around every store write plus a sampler that
drops spans started under it. That suppression only works when THIS plugin
owns the provider's sampler, which is the one hard rule here:

- Global provider untouched at import -> install our TracerProvider
  (SuppressingSampler + BatchSpanProcessor around a buffering exporter).
  Import-time because a span started through the API's ProxyTracer before a
  provider exists is non-recording forever, and ``datasette.startup`` starts
  before any hook runs. The sibling plugins (datasette-otel-otlp,
  datasette-otel-parquet) attach their processors to this provider and keep
  working.
- A real SDK provider already installed (agent, or a sibling imported
  first) -> mode "foreign": self mode is structurally DISABLED (one loud
  stderr line at startup). Storing our own spans without the suppressing
  sampler is the measured runaway (~10-14k spans/s); never attach-and-store.

The exporter buffers span rows until the startup hook arms it with the event
loop and the Datasette instance. It leans on the 1.0a39 lifecycle guarantee
that ``datasette serve`` runs startup and the server on a single event loop:
the loop captured at startup is the loop ``run_coroutine_threadsafe`` later
targets from the BatchSpanProcessor's worker thread. On older lifecycles the
scheduled insert fails with "Event loop is closed" - dropped, never raised.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

from opentelemetry import context as otel_context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

from . import store

PENDING_LIMIT = 8192
SCHEDULE_DELAY_MILLIS = 1000


def _log(message):
    print(f"{store.PLUGIN_NAME}: {message}", file=sys.stderr)


class SuppressingSampler(Sampler):
    "Drop any span started while store.SUPPRESS_KEY is set in its context."

    def should_sample(
        self, parent_context, trace_id, name, kind=None, attributes=None,
        links=None, trace_state=None,
    ):
        if otel_context.get_value(store.SUPPRESS_KEY, context=parent_context):
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE)

    def get_description(self):
        return "SuppressingSampler"


def span_to_row(span) -> dict:
    """ReadableSpan -> the store.COLUMNS row dict, mirroring
    otlp.request_to_rows so self-stored and ingested spans are
    indistinguishable in the store."""
    attrs = {}
    for key, value in (span.attributes or {}).items():
        attrs[key] = list(value) if isinstance(value, tuple) else value
    resource_attrs = dict(span.resource.attributes) if span.resource else {}
    scope = span.instrumentation_scope
    start_ns, end_ns = span.start_time, span.end_time
    return {
        "trace_id": format(span.context.trace_id, "032x"),
        "span_id": format(span.context.span_id, "016x"),
        "parent_span_id": (
            format(span.parent.span_id, "016x") if span.parent else None
        ),
        "name": span.name,
        "kind": span.kind.name,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "duration_ms": (end_ns - start_ns) / 1e6,
        "status": span.status.status_code.name,
        "status_description": span.status.description or None,
        "service_name": resource_attrs.get("service.name"),
        "http_route": attrs.get("http.route"),
        "http_status": attrs.get("http.response.status_code"),
        "db_namespace": attrs.get("db.namespace"),
        "db_operation": attrs.get("db.operation.name"),
        "db_query_text": attrs.get("db.query.text"),
        "attributes": json.dumps(attrs),
        "resource": json.dumps(
            {
                k: (list(v) if isinstance(v, tuple) else v)
                for k, v in resource_attrs.items()
            }
        ),
        "scope_name": scope.name if scope else None,
        "scope_version": scope.version if scope else None,
        "schema_url": (scope.schema_url or None) if scope else None,
    }


class SelfStoreExporter(SpanExporter):
    """Buffers rows until armed; then schedules suppressed inserts onto the
    instance's event loop. Runs on the BatchSpanProcessor worker thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._armed = False
        self._disabled = False
        self._loop = None
        self._datasette = None
        self._pending = []
        # Futures for scheduled inserts, so tests can drain deterministically.
        self.futures = []

    def arm(self, loop, datasette):
        with self._lock:
            self._loop = loop
            self._datasette = datasette
            self._armed = True
            self._disabled = False
            pending, self._pending = self._pending, []
        if pending:
            self._schedule(pending)

    def disable(self):
        "self_traces: false, or foreign provider: drop everything, forever."
        with self._lock:
            self._disabled = True
            self._armed = False
            self._loop = None
            self._datasette = None
            self._pending = []

    def _schedule(self, rows):
        with self._lock:
            loop, datasette = self._loop, self._datasette
        if loop is None:
            return

        async def do_insert():
            # suppress() around the writes; Datasette's default block=True
            # write APIs carry this context to the write thread, which is
            # what lets the sampler drop the insert's own spans.
            with store.suppress():
                await store.insert_spans(datasette, rows)
                await store.maybe_prune(datasette)

        try:
            future = asyncio.run_coroutine_threadsafe(do_insert(), loop)
        except RuntimeError:
            # Startup ran on a loop that has since closed (pre-1.0a39
            # embedder lifecycle). Dropping beats crashing the pipeline.
            return
        self.futures.append(future)
        del self.futures[:-64]

    def export(self, spans):
        rows = [span_to_row(s) for s in spans]
        with self._lock:
            if self._disabled:
                return SpanExportResult.SUCCESS
            if not self._armed:
                if len(self._pending) + len(rows) <= PENDING_LIMIT:
                    self._pending.extend(rows)
                return SpanExportResult.SUCCESS
        self._schedule(rows)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.disable()

    def force_flush(self, timeout_millis=30000):
        return True


# Module state, rebuilt by install(). mode: "owner" | "foreign"
_state = {}


def install():
    "Runs at module import; re-runnable by tests after resetting otel globals."
    _state.clear()
    existing = trace.get_tracer_provider()
    if not isinstance(
        existing, (trace.ProxyTracerProvider, trace.NoOpTracerProvider)
    ):
        _state.update(mode="foreign", provider=existing, exporter=None)
        return

    exporter = SelfStoreExporter()
    resource = Resource.create({"service.name": "datasette"})
    provider = TracerProvider(sampler=SuppressingSampler(), resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter, schedule_delay_millis=SCHEDULE_DELAY_MILLIS
        )
    )
    trace.set_tracer_provider(provider)
    _state.update(
        mode="owner", provider=provider, exporter=exporter, resource=resource
    )


def configure(datasette, loop):
    "Called from the startup() hook once config is readable."
    config = datasette.plugin_config(store.PLUGIN_NAME) or {}
    want_self = config.get("self_traces", True)

    if _state.get("mode") == "foreign":
        if want_self:
            _log(
                "another TracerProvider was installed before this plugin "
                "imported - self_traces is disabled (storing this "
                "instance's own spans requires this plugin's suppressing "
                "sampler; without it the store feeds back on itself)"
            )
        return

    if not want_self:
        _state["exporter"].disable()
        return

    service_name = config.get("service_name")
    if service_name:
        # Spans hold the Resource by reference; swap its (immutable)
        # attribute mapping to retrofit the name onto the queued startup
        # trace. Same trick as the sibling plugins.
        from opentelemetry.attributes import BoundedAttributes

        attributes = dict(_state["resource"].attributes)
        attributes["service.name"] = str(service_name)
        _state["resource"]._attributes = BoundedAttributes(
            attributes=attributes, immutable=True
        )

    _state["exporter"].arm(loop, datasette)
