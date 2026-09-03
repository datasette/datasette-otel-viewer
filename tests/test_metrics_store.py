"""Ticket 01: metrics/metric_points schema, batch insert, retention."""

import sqlite3
import time

import pytest

from datasette_otel_receiver import store


def make_metric(name, type="gauge", **kw):
    row = {
        "name": name,
        "description": None,
        "unit": None,
        "type": type,
        "temporality": None,
        "monotonic": None,
    }
    row.update(kw)
    return row


def make_point(name, time_ns, **kw):
    row = {"metric_name": name, "time_ns": time_ns}
    row.update(kw)
    return row


def metric_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        metrics = conn.execute("select count(*) from metrics").fetchone()[0]
        points = conn.execute("select count(*) from metric_points").fetchone()[0]
        return metrics, points
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_schema_creates_metrics_tables(make_ds, tmp_path):
    await make_ds()
    conn = sqlite3.connect(tmp_path / "otel.db")
    try:
        names = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type in ('table', 'index')"
            )
        }
    finally:
        conn.close()
    assert {
        "metrics",
        "metric_points",
        "idx_metric_points_name_time",
        "idx_metric_points_service_time",
    } <= names


@pytest.mark.asyncio
async def test_schema_is_idempotent(make_ds):
    ds = await make_ds()
    await store.ensure_db(ds)  # must not raise


@pytest.mark.asyncio
async def test_insert_metrics_upserts_definition_and_appends_points(make_ds, tmp_path):
    ds = await make_ds()
    now_ns = int(time.time() * 1e9)
    await store.insert_metrics(
        ds, [make_metric("req.count", unit="1")], [make_point("req.count", now_ns)]
    )
    await store.insert_metrics(
        ds,
        [make_metric("req.count", unit="ms")],
        [make_point("req.count", now_ns)],
    )
    conn = sqlite3.connect(tmp_path / "otel.db")
    try:
        rows = conn.execute(
            "select unit from metrics where name = 'req.count'"
        ).fetchall()
        points = conn.execute(
            "select count(*) from metric_points where metric_name = 'req.count'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == [("ms",)]
    assert points == 2


@pytest.mark.asyncio
async def test_insert_missing_columns_are_null(make_ds, tmp_path):
    ds = await make_ds()
    now_ns = int(time.time() * 1e9)
    await store.insert_metrics(
        ds, [make_metric("m")], [{"metric_name": "m", "time_ns": now_ns}]
    )
    conn = sqlite3.connect(tmp_path / "otel.db")
    try:
        row = conn.execute(
            "select attributes, value_double, service_name from metric_points"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("{}", None, None)


@pytest.mark.asyncio
async def test_age_prune_deletes_old_points_only(make_ds, tmp_path):
    ds = await make_ds(retention_hours=1)
    now_ns = int(time.time() * 1e9)
    old_ns = now_ns - int(2 * 3600 * 1e9)
    await store.insert_metrics(
        ds,
        [make_metric("m")],
        [make_point("m", old_ns), make_point("m", now_ns)],
    )
    await store.maybe_prune(ds, force=True)
    assert metric_counts(tmp_path / "otel.db")[1] == 1


@pytest.mark.asyncio
async def test_size_cap_keeps_newest_points(make_ds, tmp_path):
    ds = await make_ds(max_metric_points=3)
    now_ns = int(time.time() * 1e9)
    await store.insert_metrics(
        ds,
        [make_metric("m")],
        [make_point("m", now_ns + i) for i in range(5)],
    )
    await store.maybe_prune(ds, force=True)
    conn = sqlite3.connect(tmp_path / "otel.db")
    try:
        ids = [
            row[0] for row in conn.execute("select id from metric_points order by id")
        ]
    finally:
        conn.close()
    assert ids == [3, 4, 5]


@pytest.mark.asyncio
async def test_orphan_metric_rows_removed(make_ds, tmp_path):
    ds = await make_ds(retention_hours=1)
    now_ns = int(time.time() * 1e9)
    old_ns = now_ns - int(2 * 3600 * 1e9)
    await store.insert_metrics(
        ds,
        [make_metric("gone"), make_metric("stays")],
        [make_point("gone", old_ns), make_point("stays", now_ns)],
    )
    await store.maybe_prune(ds, force=True)
    conn = sqlite3.connect(tmp_path / "otel.db")
    try:
        names = {row[0] for row in conn.execute("select name from metrics")}
    finally:
        conn.close()
    assert names == {"stays"}


def make_span_row(trace_id, span_id, start_ns):
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": None,
        "name": "span",
        "kind": "INTERNAL",
        "start_ns": start_ns,
        "end_ns": start_ns + 1_000_000,
        "status": "UNSET",
        "attributes": "{}",
        "resource": "{}",
    }


@pytest.mark.asyncio
async def test_metrics_prune_does_not_touch_spans_and_vice_versa(make_ds, tmp_path):
    ds = await make_ds(max_metric_points=3, max_spans=100)
    now_ns = int(time.time() * 1e9)
    await store.insert_spans(
        ds,
        [
            make_span_row("aa" * 16, "01" * 8, now_ns),
            make_span_row("aa" * 16, "02" * 8, now_ns + 10),
        ],
    )
    await store.insert_metrics(
        ds, [make_metric("m")], [make_point("m", now_ns + i) for i in range(5)]
    )
    await store.maybe_prune(ds, force=True)

    def counts(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("select count(*) from spans").fetchone()[0]
        finally:
            conn.close()

    assert counts(tmp_path / "otel.db") == 2
    assert metric_counts(tmp_path / "otel.db")[1] == 3

    # Flip the caps (new Datasette, same db file/tmp_path): spans get pruned
    # to fit, metric points are left alone this time.
    store._last_prune = 0.0
    ds2 = await make_ds(max_spans=1, max_metric_points=100)
    await store.maybe_prune(ds2, force=True)
    assert counts(tmp_path / "otel.db") < 2
    assert metric_counts(tmp_path / "otel.db")[1] == 3


@pytest.mark.asyncio
async def test_prune_throttle_shared(make_ds, tmp_path):
    ds = await make_ds(max_metric_points=1)
    now_ns = int(time.time() * 1e9)
    await store.insert_metrics(
        ds, [make_metric("m")], [make_point("m", now_ns + i) for i in range(3)]
    )
    await store.maybe_prune(ds, force=True)  # sets the throttle clock
    await store.insert_metrics(ds, [], [make_point("m", now_ns + 100)])
    await store.maybe_prune(ds)  # throttled: within the interval, no-op
    assert metric_counts(tmp_path / "otel.db")[1] == 2
