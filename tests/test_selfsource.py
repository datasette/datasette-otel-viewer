"""Ticket 03 acceptance: self mode stores this instance's spans, captures the
startup trace, and - the load-bearing assertion - does NOT feed back on
itself."""

import asyncio

import pytest

from datasette_otel_receiver import selfsource
from conftest import drain, raw_span_rows, reset_tracer_state


@pytest.mark.asyncio
async def test_request_spans_land_in_store(make_ds, tmp_path):
    ds = await make_ds(public_viewer=True)
    response = await ds.client.get("/")
    assert response.status_code == 200
    await drain()

    names = {row[1] for row in raw_span_rows(tmp_path / "otel.db")}
    assert any(name.startswith("GET ") for name in names)
    # The startup trace is captured too (buffered from import until armed).
    assert "datasette.startup" in names


@pytest.mark.asyncio
async def test_no_feedback_loop(make_ds, tmp_path):
    """The measured runaway is ~10-14k spans/s. With suppression, span count
    stabilizes once activity stops: the store's own writes emit nothing."""
    ds = await make_ds()
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

    ds = await make_ds()
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

    from datasette_otel_receiver import store

    collected = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(collected))
    tracer = provider.get_tracer("test-scope", "1.2.3")
    with tracer.start_as_current_span(
        "parent", attributes={"db.query.text": "select 1", "list": ("a", "b")}
    ):
        with tracer.start_as_current_span("child"):
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
