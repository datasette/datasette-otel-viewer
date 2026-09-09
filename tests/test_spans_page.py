"""``/-/otel/spans``: the span catalogue. Every kind of work recorded, keyed
on span name *and* instrumentation scope, which is what separates a plugin's
spans from Datasette's own without this plugin knowing their names."""

import json
import time

import pytest
from conftest import page_data

from datasette_otel_viewer import store

START_NS = time.time_ns() - 3600 * 1_000_000_000


def span_row(
    i,
    *,
    name,
    scope="datasette",
    kind="INTERNAL",
    parent=None,
    trace=None,
    duration_ms=1.0,
    status="UNSET",
    attributes=None,
):
    start = START_NS + i * 1_000_000_000
    return {
        "trace_id": trace or f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": parent,
        "name": name,
        "kind": kind,
        "start_ns": start,
        "end_ns": start + int(duration_ms * 1e6),
        "duration_ms": duration_ms,
        "status": status,
        "service_name": "datasette",
        "attributes": json.dumps(attributes or {}),
        "resource": json.dumps({"service.name": "datasette"}),
        "scope_name": scope,
    }


def cron_trace(i, *, task, duration_ms, status="UNSET"):
    """A datasette-cron run: a root span of its own, with the task on it --
    the shape the plugin's telemetry registry describes."""
    trace = f"{i:032x}"
    return [
        span_row(
            i,
            name="datasette_cron.run",
            scope="datasette_cron",
            trace=trace,
            duration_ms=duration_ms,
            status=status,
            attributes={"datasette_cron.task": task, "datasette_cron.attempts": 1},
        ),
        span_row(
            i + 100,
            name="datasette_cron.attempt",
            scope="datasette_cron",
            trace=trace,
            parent=f"{i:016x}",
            duration_ms=duration_ms - 0.5,
            attributes={"datasette_cron.task": task},
        ),
    ]


async def grouped(ds, **body):
    response = await ds.client.post("/-/otel/api/spans/groups", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_scope_separates_a_plugins_spans_from_datasettes(make_ds):
    ds = await make_ds(public_viewer=True)
    rows = [
        span_row(1, name="datasette.startup", duration_ms=12.0),
        span_row(2, name="db.query", kind="CLIENT", duration_ms=3.0),
        *cron_trace(3, task="backup", duration_ms=40.0),
        *cron_trace(4, task="cleanup", duration_ms=10.0),
    ]
    await store.insert_spans(ds, rows)

    summary = await grouped(ds)
    assert summary["scopes"] == ["datasette", "datasette_cron"]
    assert summary["span_count"] == 6
    by_name = {r["name"]: r for r in summary["spans"]}
    assert by_name["datasette_cron.run"]["scope"] == "datasette_cron"
    assert by_name["datasette_cron.run"]["span_count"] == 2
    assert by_name["datasette.startup"]["scope"] == "datasette"
    # Ordered by the time each kind of work accounts for.
    assert summary["spans"][0]["name"] == "datasette_cron.run"

    only_cron = await grouped(ds, scope="datasette_cron")
    assert {r["name"] for r in only_cron["spans"]} == {
        "datasette_cron.run",
        "datasette_cron.attempt",
    }


@pytest.mark.asyncio
async def test_nesting_filter_and_trace_counts(make_ds):
    ds = await make_ds(public_viewer=True)
    trace = f"{9:032x}"
    await store.insert_spans(
        ds,
        [
            span_row(9, name="GET /{database}", kind="SERVER", trace=trace),
            # Three db.query spans inside the one request.
            *[
                span_row(
                    10 + n,
                    name="db.query",
                    kind="CLIENT",
                    trace=trace,
                    parent=f"{9:016x}",
                    duration_ms=1.0 + n,
                )
                for n in range(3)
            ],
        ],
    )
    nested = await grouped(ds, nesting="nested")
    assert [r["name"] for r in nested["spans"]] == ["db.query"]
    (row,) = nested["spans"]
    # Three spans in one trace: the "how many per request" signal.
    assert (row["span_count"], row["trace_count"]) == (3, 1)

    roots = await grouped(ds, nesting="root")
    assert [r["name"] for r in roots["spans"]] == ["GET /{database}"]

    assert (await grouped(ds, kind="client"))["spans"][0]["name"] == "db.query"
    assert (await grouped(ds, name="db."))["span_count"] == 3
    assert (await grouped(ds, min_duration_ms=2.5))["span_count"] == 1


@pytest.mark.asyncio
async def test_split_by_attribute(make_ds):
    """The reason this page can explore a plugin's spans: one row per value
    of an attribute, so `datasette_cron.run` becomes one row per task."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            *cron_trace(3, task="backup", duration_ms=40.0),
            *cron_trace(4, task="backup", duration_ms=20.0),
            *cron_trace(5, task="cleanup", duration_ms=10.0, status="ERROR"),
        ],
    )
    summary = await grouped(
        ds, name="datasette_cron.run", split_by="datasette_cron.task"
    )
    rows = {r["split_value"]: r for r in summary["spans"]}
    assert rows["backup"]["span_count"] == 2
    assert rows["backup"]["total_ms"] == pytest.approx(60.0)
    assert (rows["backup"]["p50_ms"], rows["backup"]["max_ms"]) == (20.0, 40.0)
    assert rows["cleanup"]["error_count"] == 1

    # The keys offered to split by are the ones the matching spans carry.
    assert "datasette_cron.task" in summary["attribute_keys"]
    assert "datasette_cron.attempts" in summary["attribute_keys"]

    # split_by lands in a JSON path, so it is held to the shape of an
    # attribute key -- nothing that could close the quote.
    bad = await ds.client.post(
        "/-/otel/api/spans/groups", json={"split_by": 'a"' + "]"}
    )
    assert bad.status_code == 400
    assert "split_by is not an attribute key" in bad.json()["error"]


@pytest.mark.asyncio
async def test_page_renders_and_is_gated(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(ds, cron_trace(3, task="backup", duration_ms=5.0))
    response = await ds.client.get("/-/otel/spans?scope=datasette_cron&nesting=root")
    assert response.status_code == 200
    assert "src/pages/spans_summary/index.ts" in response.text
    data = page_data(response.text)
    assert data["query"]["scope"] == "datasette_cron"
    assert [r["name"] for r in data["spans"]] == ["datasette_cron.run"]
    assert data["database"] == "otel"

    assert (await ds.client.get("/-/otel/spans?kind=sideways")).status_code == 400
    assert (await ds.client.get("/-/otel/spans?nesting=maybe")).status_code == 400

    private = await make_ds()
    assert (await private.client.get("/-/otel/spans")).status_code == 403
    assert (
        await private.client.post("/-/otel/api/spans/groups", json={})
    ).status_code == 403
