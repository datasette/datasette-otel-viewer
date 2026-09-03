"""Self mode for metrics: this instance's own measurements land in the store,
the store's own points are filtered out, and a MeterProvider someone else
installed gets our reader attached rather than a half-working plugin."""

import asyncio
import json
import sqlite3

import pytest
from conftest import drain_metrics, raw_metric_rows, reset_meter_state
from datasette.telemetry_registry import DURATION_BUCKETS

from datasette_otel_viewer import selfmetrics, store

DURATION = "db.client.operation.duration"


def metric_row(db_path, name):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from metrics where name = ?", [name]).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_owner_mode_at_import():
    from opentelemetry import metrics as otel_metrics

    assert selfmetrics._state["mode"] == "owner"
    assert otel_metrics.get_meter_provider() is selfmetrics._state["provider"]


def test_resource_shared_with_tracer():
    "One Resource for both signals, so service_name config reaches metrics."
    from datasette_otel_viewer import selfsource

    assert selfmetrics._state["resource"] is selfsource._state["resource"]


@pytest.mark.asyncio
async def test_request_metrics_land_in_store(make_ds, tmp_path):
    ds = await make_ds(public_viewer=True)
    for _ in range(2):
        assert (await ds.client.get("/")).status_code == 200
    await drain_metrics()

    db_path = tmp_path / "otel.db"
    points = raw_metric_rows(db_path, DURATION)
    assert points
    assert any("datasette.operation" in row[1] for row in points)

    definition = metric_row(db_path, DURATION)
    assert definition["type"] == "histogram"
    assert definition["unit"] == "s"
    assert definition["temporality"] == "cumulative"

    bounds = query(
        db_path,
        "select explicit_bounds from metric_points where metric_name = ? limit 1",
        [DURATION],
    )[0][0]
    assert json.loads(bounds) == list(DURATION_BUCKETS)


@pytest.mark.asyncio
async def test_otel_namespace_points_filtered(make_ds, tmp_path):
    "Points about the store's own writes say nothing about the operator's data."
    ds = await make_ds()
    await ds.client.get("/")
    await drain_metrics()

    otel_db = store.db_name(ds)
    rows = query(
        tmp_path / "otel.db",
        "select count(*) from metric_points "
        "where json_extract(attributes, '$.\"db.namespace\"') = ?",
        [otel_db],
    )
    assert rows[0][0] == 0
    assert raw_metric_rows(tmp_path / "otel.db")


@pytest.mark.asyncio
async def test_gauges_collected(make_ds, tmp_path):
    "Observable gauge callbacks only run on a collection cycle."
    await make_ds()
    await drain_metrics()

    assert raw_metric_rows(tmp_path / "otel.db", "datasette.sql.threads.limit")


@pytest.mark.asyncio
async def test_growth_without_activity_is_bounded(make_ds, tmp_path):
    """Unlike spans there is no runaway: cumulative instruments re-export one
    point per series per cycle whether or not anything happened."""
    ds = await make_ds()
    await ds.client.get("/")
    await drain_metrics()

    db_path = tmp_path / "otel.db"
    count_1 = len(raw_metric_rows(db_path))
    series = query(
        db_path, "select count(distinct metric_name || attributes) from metric_points"
    )[0][0]
    assert count_1 > 0

    await drain_metrics()
    await drain_metrics()
    count_3 = len(raw_metric_rows(db_path))
    assert count_3 - count_1 <= 2 * series


@pytest.mark.asyncio
async def test_self_metrics_false_stores_nothing(make_ds, tmp_path):
    ds = await make_ds(self_metrics=False)
    await ds.client.get("/")
    selfmetrics._state["reader"].collect()
    await asyncio.sleep(0.05)
    assert raw_metric_rows(tmp_path / "otel.db") == []


def readers_on(provider):
    "The readers actually collecting from an SDK provider, in order."
    return list(provider._measurement_consumer._reader_storages)


def install_foreign_sdk_provider():
    "Play the provider game another plugin would win, and return its provider."
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider

    reset_meter_state()
    foreign = MeterProvider(shutdown_on_exit=False)
    otel_metrics.set_meter_provider(foreign)
    return foreign


@pytest.mark.asyncio
async def test_attaches_to_foreign_sdk_provider(make_ds, tmp_path, capsys):
    "Someone else owns the provider; our reader rides along on it."
    foreign = install_foreign_sdk_provider()

    selfmetrics.install()
    assert selfmetrics._state["mode"] == "attached"
    assert selfmetrics._state["provider"] is foreign
    assert selfmetrics._state["owns_provider"] is False
    assert "attaching" in capsys.readouterr().err

    ds = await make_ds(public_viewer=True)
    for _ in range(2):
        assert (await ds.client.get("/")).status_code == 200
    await drain_metrics()

    assert raw_metric_rows(tmp_path / "otel.db", DURATION)


@pytest.mark.asyncio
async def test_attached_mode_respects_self_metrics_false(make_ds, tmp_path):
    "The off switch is still the config, not who owns the provider."
    foreign = install_foreign_sdk_provider()
    selfmetrics.install()

    ds = await make_ds(self_metrics=False)
    await ds.client.get("/")
    selfmetrics._state["reader"].collect()
    await asyncio.sleep(0.05)

    assert raw_metric_rows(tmp_path / "otel.db") == []
    assert selfmetrics._state["exporter"]._disabled
    assert selfmetrics._state["reader"] in readers_on(foreign)


def test_repeated_install_attaches_one_reader():
    "install() is re-runnable, so it must not stack readers on the provider."
    foreign = install_foreign_sdk_provider()

    selfmetrics.install()
    first = selfmetrics._state["reader"]
    selfmetrics.install()

    assert selfmetrics._state["mode"] == "attached"
    assert selfmetrics._state["reader"] is not first
    assert readers_on(foreign) == [selfmetrics._state["reader"]]


@pytest.mark.asyncio
async def test_non_sdk_provider_still_disables(make_ds, tmp_path, capsys):
    "A provider with no add_metric_reader leaves nowhere to put our reader."
    from opentelemetry import metrics as otel_metrics

    class Weird(otel_metrics.MeterProvider):
        def get_meter(self, name, version=None, schema_url=None, attributes=None):
            return otel_metrics.NoOpMeter(name, version, schema_url)

    reset_meter_state()
    otel_metrics.set_meter_provider(Weird())

    selfmetrics.install()
    assert selfmetrics._state["mode"] == "foreign"

    ds = await make_ds()
    assert "self_metrics is disabled" in capsys.readouterr().err
    await ds.client.get("/")
    await asyncio.sleep(0.05)
    assert raw_metric_rows(tmp_path / "otel.db") == []


@pytest.mark.asyncio
async def test_provider_without_add_metric_reader_falls_back(
    make_ds, tmp_path, capsys, monkeypatch
):
    "opentelemetry-sdk below 1.44: same behaviour as a non-SDK provider."
    monkeypatch.setattr(selfmetrics, "_supports_attach", lambda provider: False)
    install_foreign_sdk_provider()

    selfmetrics.install()
    assert selfmetrics._state["mode"] == "foreign"

    ds = await make_ds()
    assert "self_metrics is disabled" in capsys.readouterr().err
    await ds.client.get("/")
    await asyncio.sleep(0.05)
    assert raw_metric_rows(tmp_path / "otel.db") == []


@pytest.mark.asyncio
async def test_add_metric_reader_helper(make_ds):
    "Sibling plugins join the provider we own from the other direction."
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    assert selfmetrics.add_metric_reader(reader) is True
    try:
        ds = await make_ds()
        await ds.client.get("/")
        names = {
            metric.name
            for rm in reader.get_metrics_data().resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
        }
        assert DURATION in names
    finally:
        selfmetrics._state["provider"].remove_metric_reader(reader)


def test_add_metric_reader_declines_when_attached():
    "We cannot hand out a provider we do not own."
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    install_foreign_sdk_provider()
    selfmetrics.install()

    assert selfmetrics.add_metric_reader(InMemoryMetricReader()) is False


def test_sdk_metrics_to_rows_matches_store_columns():
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    # Never set globally: a standalone provider, like
    # datasette.telemetry_testing.install_metric_reader builds.
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    meter = provider.get_meter("test-scope", "1.2.3")
    histogram = meter.create_histogram(
        "test.duration", unit="s", explicit_bucket_boundaries_advisory=[0.1, 1.0]
    )
    histogram.record(0.5, {"db.namespace": "x"})
    counter = meter.create_counter("test.count", unit="{query}")
    counter.add(2, {"db.namespace": "keep"})
    meter.create_observable_gauge(
        "test.gauge",
        callbacks=[lambda options: [Observation(7, {"db.namespace": "keep"})]],
        unit="{thread}",
    )

    metrics, points = selfmetrics.sdk_metrics_to_rows(reader.get_metrics_data())
    by_name = {m["name"]: m for m in metrics}
    for metric in metrics:
        assert set(metric) == set(store.METRIC_COLUMNS)
    for point in points:
        assert set(point) <= set(store.METRIC_POINT_COLUMNS)

    assert by_name["test.duration"]["type"] == "histogram"
    assert by_name["test.count"] == {
        "name": "test.count",
        "description": None,
        "unit": "{query}",
        "type": "sum",
        "temporality": "cumulative",
        "monotonic": 1,
    }
    gauge = next(p for p in points if p["metric_name"] == "test.gauge")
    assert (gauge["value_int"], gauge["value_double"]) == (7, None)
    histogram_point = next(p for p in points if p["metric_name"] == "test.duration")
    assert json.loads(histogram_point["explicit_bounds"]) == [0.1, 1.0]
    assert histogram_point["scope_name"] == "test-scope"

    _, kept = selfmetrics.sdk_metrics_to_rows(
        reader.get_metrics_data(), exclude_namespace="x"
    )
    namespaces = {json.loads(p["attributes"]).get("db.namespace") for p in kept}
    assert namespaces == {"keep"}


@pytest.mark.asyncio
async def test_export_before_arm_is_buffered(make_ds):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    ds = await make_ds()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    provider.get_meter("test-scope").create_counter("test.buffered").add(1)

    exporter = selfmetrics.SelfMetricsExporter()
    exporter.export(reader.get_metrics_data())
    assert len(exporter._pending) == 1

    exporter.arm(asyncio.get_running_loop(), ds)
    assert exporter._pending == []
    assert exporter.futures
    for future in exporter.futures:
        await asyncio.wrap_future(future)
