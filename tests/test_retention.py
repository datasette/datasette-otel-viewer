"""Ticket 02: trace-granular retention - age, size, throttle."""

import sqlite3
import time

import pytest

from datasette_otel_viewer import store


def make_row(trace_id, span_id, start_ns, name="span"):
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": None,
        "name": name,
        "kind": "INTERNAL",
        "start_ns": start_ns,
        "end_ns": start_ns + 1_000_000,
        "status": "UNSET",
        "attributes": "{}",
        "resource": "{}",
    }


def counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        spans = conn.execute("select count(*) from spans").fetchone()[0]
        traces = conn.execute("select count(*) from traces").fetchone()[0]
        return spans, traces
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_age_prune_deletes_whole_traces(make_ds, tmp_path):
    ds = await make_ds(retention_hours=1)
    now_ns = int(time.time() * 1e9)
    old_ns = now_ns - int(2 * 3600 * 1e9)
    await store.insert_spans(
        ds,
        [
            make_row("aa" * 16, "01" * 8, old_ns),
            make_row("aa" * 16, "02" * 8, old_ns + 10),
            make_row("bb" * 16, "03" * 8, now_ns),
        ],
    )
    await store.maybe_prune(ds, force=True)
    assert counts(tmp_path / "otel.db") == (1, 1)


@pytest.mark.asyncio
async def test_size_prune_keeps_newest_whole_traces(make_ds, tmp_path):
    ds = await make_ds(max_spans=3)
    now_ns = int(time.time() * 1e9)
    rows = []
    for t in range(4):  # 4 traces x 2 spans, oldest first
        for s in range(2):
            rows.append(make_row(f"{t:02d}" * 16, f"{t}{s}" * 4, now_ns + t * 1000 + s))
    await store.insert_spans(ds, rows)
    await store.maybe_prune(ds, force=True)
    spans, traces = counts(tmp_path / "otel.db")
    # Newest trace (2 spans) fits; the second-newest crosses the limit and
    # everything from it on down goes - whole traces only, never partial.
    assert (spans, traces) == (2, 1)
    conn = sqlite3.connect(tmp_path / "otel.db")
    survivor = conn.execute("select distinct trace_id from spans").fetchall()
    conn.close()
    assert survivor == [("03" * 16,)]


@pytest.mark.asyncio
async def test_prune_is_throttled(make_ds, tmp_path):
    ds = await make_ds(retention_hours=0)
    now_ns = int(time.time() * 1e9)
    await store.insert_spans(ds, [make_row("cc" * 16, "0a" * 8, now_ns - 10)])
    await store.maybe_prune(ds, force=True)  # sets the throttle clock
    await store.insert_spans(ds, [make_row("dd" * 16, "0b" * 8, now_ns - 10)])
    await store.maybe_prune(ds)  # throttled: within the interval, no-op
    assert counts(tmp_path / "otel.db")[0] == 1
    await store.maybe_prune(ds, force=True)
    assert counts(tmp_path / "otel.db")[0] == 0
