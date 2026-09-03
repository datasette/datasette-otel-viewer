"""Self metrics: store the metrics this Datasette records, in this Datasette.

Unlike selfsource.py we do not have to own the provider. When the metrics
global is still the API's proxy/no-op we install our own MeterProvider — a
PeriodicExportingMetricReader around an exporter that writes through
store.insert_metrics. When an SDK provider is already there we hand the same
reader to its add_metric_reader() and keep capturing; only a provider that
cannot take a reader makes self_metrics disable itself with one stderr line.

Unlike spans there is no runaway to cut: the store's own writes add one
measurement to an already-existing series, so a quiet instance exports the
same handful of points every cycle whatever it stores. Those points describe
the store rather than the operator's data, though, so the exporter drops
every point whose db.namespace is the otel database.

Import-time install is not load-bearing for correctness the way it is for
tracing — a _ProxyMeter's instruments forward to a provider installed later
— only for catching the measurements taken during startup.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import threading

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.metrics._internal import _ProxyMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    ExponentialHistogram,
    Gauge,
    Histogram,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
    Sum,
)
from opentelemetry.sdk.resources import Resource

from . import selfsource, store

EXPORT_INTERVAL_MILLIS = 60_000
# Exports (not points) held while the loop is still unknown.
PENDING_LIMIT = 256

# SDK data classes -> the `metrics.type` vocabulary stored in `metrics`.
_TYPES = {
    Gauge: "gauge",
    Sum: "sum",
    Histogram: "histogram",
    ExponentialHistogram: "exponential_histogram",
}


def _log(message):
    print(f"{store.PLUGIN_NAME}: {message}", file=sys.stderr)


def _jsonable(value):
    return list(value) if isinstance(value, tuple) else value


def _attributes(mapping) -> dict:
    return {k: _jsonable(v) for k, v in dict(mapping or {}).items()}


def _temporality(value) -> str | None:
    "SDK AggregationTemporality -> the store's delta/cumulative/None."
    if value == AggregationTemporality.DELTA:
        return "delta"
    if value == AggregationTemporality.CUMULATIVE:
        return "cumulative"
    return None


def _finite(value):
    "Histograms with no recorded values carry inf/-inf min/max; store NULL."
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return value


def sdk_metrics_to_rows(
    data: MetricsData | None, exclude_namespace: str | None = None
) -> tuple[list[dict], list[dict]]:
    """MetricsData -> (metric rows, point rows) shaped like
    store.METRIC_COLUMNS / store.METRIC_POINT_COLUMNS: the single writer for
    the ``metrics`` / ``metric_points`` tables."""
    metrics_by_name: dict[str, dict] = {}
    points: list[dict] = []
    if data is None:
        return [], []

    for rm in data.resource_metrics:
        resource_attrs = _attributes(rm.resource.attributes if rm.resource else {})
        resource_json = json.dumps(resource_attrs)
        service_name = resource_attrs.get("service.name")

        for sm in rm.scope_metrics:
            scope_name = (sm.scope.name or None) if sm.scope else None

            for metric in sm.metrics:
                data_ = metric.data
                type_name = _TYPES.get(type(data_))
                if type_name is None:
                    continue
                metrics_by_name[metric.name] = {
                    "name": metric.name,
                    "description": metric.description or None,
                    "unit": metric.unit or None,
                    "type": type_name,
                    "temporality": _temporality(
                        getattr(data_, "aggregation_temporality", None)
                    ),
                    "monotonic": (
                        int(data_.is_monotonic) if type_name == "sum" else None
                    ),
                }

                for p in data_.data_points:
                    attrs = _attributes(p.attributes)
                    if (
                        exclude_namespace is not None
                        and attrs.get("db.namespace") == exclude_namespace
                    ):
                        # The store observing its own writes: see the module
                        # docstring.
                        continue
                    row = {
                        "metric_name": metric.name,
                        "service_name": service_name,
                        "scope_name": scope_name,
                        "start_ns": p.start_time_unix_nano or None,
                        "time_ns": p.time_unix_nano,
                        "attributes": json.dumps(attrs),
                        # The SDK's default exemplar filter needs a sampled
                        # trace context, which self metrics never have.
                        "exemplars": json.dumps([]),
                        "resource": resource_json,
                        "flags": None,
                    }
                    if type_name in ("gauge", "sum"):
                        value = p.value
                        is_int = isinstance(value, int)
                        row["value_int"] = value if is_int else None
                        row["value_double"] = None if is_int else float(value)
                    elif type_name == "histogram":
                        row.update(
                            count=p.count,
                            sum=p.sum,
                            min=_finite(p.min),
                            max=_finite(p.max),
                            bucket_counts=json.dumps(list(p.bucket_counts)),
                            explicit_bounds=json.dumps(list(p.explicit_bounds)),
                        )
                    else:
                        row.update(
                            count=p.count,
                            sum=p.sum,
                            min=_finite(p.min),
                            max=_finite(p.max),
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
                    points.append(row)

    return list(metrics_by_name.values()), points


class SelfMetricsExporter(MetricExporter):
    """Buffers exports until armed; then schedules suppressed inserts onto
    the instance's event loop. Runs on the reader's collection thread."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._armed = False
        self._disabled = False
        self._loop = None
        self._datasette = None
        self._pending: list[MetricsData] = []
        # Futures for scheduled inserts, so tests can drain deterministically.
        self.futures = []

    def arm(self, loop, datasette):
        with self._lock:
            self._loop = loop
            self._datasette = datasette
            self._armed = True
            self._disabled = False
            pending, self._pending = self._pending, []
        for data in pending:
            self._schedule(data)

    def disable(self):
        "self_metrics: false: drop everything, forever."
        with self._lock:
            self._disabled = True
            self._armed = False
            self._loop = None
            self._datasette = None
            self._pending = []

    def _schedule(self, data: MetricsData):
        with self._lock:
            loop, datasette = self._loop, self._datasette
        if loop is None:
            return
        metrics, points = sdk_metrics_to_rows(
            data, exclude_namespace=store.db_name(datasette)
        )
        if not points:
            return

        async def do_insert():
            with store.suppress():
                await store.insert_metrics(datasette, metrics, points)
                await store.maybe_prune(datasette)

        try:
            future = asyncio.run_coroutine_threadsafe(do_insert(), loop)
        except RuntimeError:
            # Startup ran on a loop that has since closed (pre-1.0a39
            # embedder lifecycle). Dropping beats crashing the pipeline.
            return
        self.futures.append(future)
        del self.futures[:-64]

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs
    ):
        with self._lock:
            if self._disabled:
                return MetricExportResult.SUCCESS
            if not self._armed:
                if len(self._pending) < PENDING_LIMIT:
                    self._pending.append(metrics_data)
                return MetricExportResult.SUCCESS
        self._schedule(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000):
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs):
        self.disable()


# Module state, rebuilt by install().
# mode: "owner" | "attached" | "foreign"
_state = {}


def _supports_attach(provider) -> bool:
    "An SDK provider new enough to take another reader after construction."
    return isinstance(provider, MeterProvider) and hasattr(
        provider, "add_metric_reader"
    )


def _new_reader():
    exporter = SelfMetricsExporter()
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=EXPORT_INTERVAL_MILLIS
    )
    return exporter, reader


def install():
    "Runs at module import; re-runnable by tests after resetting otel globals."
    if _state.get("mode") == "attached":
        # Re-installing would otherwise leave the previous reader collecting
        # into an exporter nothing can reach.
        _state["provider"].remove_metric_reader(_state["reader"])
    _state.clear()
    existing = otel_metrics.get_meter_provider()

    if isinstance(existing, (_ProxyMeterProvider, NoOpMeterProvider)):
        exporter, reader = _new_reader()
        # Share the tracer's Resource when we own tracing too, so the
        # service_name retrofit in selfsource.configure() reaches both signals.
        resource = selfsource._state.get("resource") or Resource.create(
            {"service.name": "datasette"}
        )
        provider = MeterProvider(metric_readers=[reader], resource=resource)
        otel_metrics.set_meter_provider(provider)
        _state.update(
            mode="owner",
            owns_provider=True,
            provider=provider,
            exporter=exporter,
            reader=reader,
            resource=resource,
        )
        return

    if _supports_attach(existing):
        # Someone else's provider, our reader: the exporter-side namespace
        # filter works the same, and the owner's Resource applies.
        exporter, reader = _new_reader()
        existing.add_metric_reader(reader)
        _log("attaching to the already-installed MeterProvider")
        _state.update(
            mode="attached",
            owns_provider=False,
            provider=existing,
            exporter=exporter,
            reader=reader,
            resource=None,
        )
        return

    _state.update(
        mode="foreign",
        owns_provider=False,
        provider=existing,
        exporter=None,
        reader=None,
    )


def add_metric_reader(reader) -> bool:
    """Attach point for sibling plugins that import us; True when the reader
    joined the provider this plugin owns."""
    if _state.get("mode") != "owner":
        return False
    _state["provider"].add_metric_reader(reader)
    return True


def configure(datasette, loop):
    "Called from the startup() hook once config is readable."
    config = datasette.plugin_config(store.PLUGIN_NAME) or {}
    want_self = config.get("self_metrics", True)

    if _state.get("mode") == "foreign":
        if want_self:
            _log(
                "the MeterProvider installed before this plugin imported "
                "does not accept metric readers - self_metrics is disabled"
            )
        return

    if not want_self:
        _state["exporter"].disable()
        return

    _state["exporter"].arm(loop, datasette)
