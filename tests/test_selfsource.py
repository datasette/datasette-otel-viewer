"""Ticket 03 acceptance: self mode stores this instance's spans, captures the
startup trace, and - the load-bearing assertion - does NOT feed back on
itself."""

import asyncio
import json

import pytest
from conftest import drain, raw_span_rows, reset_tracer_state

from datasette_otel_viewer import selfsource, store


@pytest.mark.asyncio
async def test_request_spans_land_in_store(make_ds, tmp_path):
    ds = await make_ds(self_traces=True, public_viewer=True)
    response = await ds.client.get("/")
    assert response.status_code == 200
    await drain()

    names = {row[1] for row in raw_span_rows(tmp_path / "otel.db")}
    assert any(name.startswith("GET ") for name in names)
    # The startup trace is captured too (buffered from import until armed).
    assert "datasette.startup" in names


@pytest.mark.asyncio
async def test_creation_time_attributes_are_stored(make_ds, tmp_path):
    """Attributes passed to start_span(attributes=...) must survive the
    SuppressingSampler - the SDK takes a span's initial attributes from the
    SamplingResult, so a sampler that returns a bare decision drops them."""
    from opentelemetry import trace

    await make_ds(self_traces=True, public_viewer=True)  # arms the store
    tracer = trace.get_tracer("attribute-test")
    with tracer.start_as_current_span(
        "with-initial-attributes", attributes={"at.start": "kept"}
    ) as span:
        span.set_attribute("after.start", "kept too")
    await drain()

    (attributes,) = [
        json.loads(row[2])
        for row in raw_span_rows(tmp_path / "otel.db")
        if row[1] == "with-initial-attributes"
    ]
    assert attributes == {"at.start": "kept", "after.start": "kept too"}


@pytest.mark.asyncio
async def test_writer_task_stores_spans(make_ds, tmp_path, monkeypatch):
    "Without drain(): the background task startup registered does the writing."
    monkeypatch.setattr(store, "WRITE_INTERVAL_SECONDS", 0.01)
    ds = await make_ds(self_traces=True)
    await ds.client.get("/")  # first request launches background tasks
    selfsource._state["provider"].force_flush()
    for _ in range(100):
        if raw_span_rows(tmp_path / "otel.db"):
            break
        await asyncio.sleep(0.01)
    names = {row[1] for row in raw_span_rows(tmp_path / "otel.db")}
    assert "datasette.startup" in names


@pytest.mark.asyncio
async def test_shutdown_writes_what_is_buffered(make_ds, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "WRITE_INTERVAL_SECONDS", 3600)
    ds = await make_ds(self_traces=True)
    await ds.client.get("/")
    selfsource._state["provider"].force_flush()
    assert raw_span_rows(tmp_path / "otel.db") == []
    await ds.invoke_shutdown()
    assert raw_span_rows(tmp_path / "otel.db")


@pytest.mark.asyncio
async def test_no_feedback_loop(make_ds, tmp_path):
    """The measured runaway is ~10-14k spans/s. With suppression, span count
    stabilizes once activity stops: the store's own writes emit nothing."""
    ds = await make_ds(self_traces=True)
    await ds.client.get("/")
    await drain()
    count_1 = len(raw_span_rows(tmp_path / "otel.db"))
    assert count_1 > 0

    # No new activity; give any would-be feedback two full flush cycles.
    await asyncio.sleep(0.1)
    await drain()
    await asyncio.sleep(0.1)
    await drain()
    count_2 = len(raw_span_rows(tmp_path / "otel.db"))
    assert count_2 == count_1


@pytest.mark.asyncio
async def test_self_traces_false_stores_nothing(make_ds, tmp_path):
    ds = await make_ds(self_traces=False)
    await ds.client.get("/")
    selfsource._state["provider"].force_flush()
    await asyncio.sleep(0.05)
    assert raw_span_rows(tmp_path / "otel.db") == []


@pytest.mark.asyncio
async def test_foreign_provider_disables_self_mode(make_ds, tmp_path, capsys):
    """A real SDK provider installed before this plugin imports means no
    suppressing sampler, so self mode must refuse to run (the alternative is
    the measured runaway)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    reset_tracer_state()
    foreign = TracerProvider(shutdown_on_exit=False)
    trace.set_tracer_provider(foreign)

    selfsource.install()
    assert selfsource._state["mode"] == "foreign"

    ds = await make_ds(self_traces=True)
    assert "self_traces is disabled" in capsys.readouterr().err
    await ds.client.get("/")
    await asyncio.sleep(0.05)
    assert raw_span_rows(tmp_path / "otel.db") == []


def test_span_to_row_matches_store_columns():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from datasette_otel_viewer import store

    collected = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(collected))
    tracer = provider.get_tracer("test-scope", "1.2.3")
    with (
        tracer.start_as_current_span(
            "parent", attributes={"db.query.text": "select 1", "list": ("a", "b")}
        ),
        tracer.start_as_current_span("child"),
    ):
        pass

    spans = collected.get_finished_spans()
    rows = [selfsource.span_to_row(s) for s in spans]
    for row in rows:
        assert set(row) == set(store.COLUMNS)
    child, parent = rows  # children end first
    assert parent["parent_span_id"] is None
    assert child["parent_span_id"] == parent["span_id"]
    assert parent["db_query_text"] == "select 1"
    assert parent["scope_name"] == "test-scope"
    assert '"list": ["a", "b"]' in parent["attributes"]
