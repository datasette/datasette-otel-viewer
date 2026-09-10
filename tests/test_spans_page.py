"""``/-/otel/spans``: the span catalogue. Every kind of work recorded, keyed
on span name *and* instrumentation scope, which is what separates a plugin's
spans from Datasette's own without this plugin knowing their names."""

import json
import sqlite3
import time

import pytest
from conftest import page_data
from datasette.database import QueryInterrupted

from datasette_otel_viewer import queries, store

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
    sql=None,
):
    start = START_NS + i * 1_000_000_000
    attributes = dict(attributes or {})
    if sql:
        # selfsource.span_to_row promotes the text into its own column *and*
        # leaves it in the attributes; the statement filter reads the column.
        attributes["db.query.text"] = sql
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
        "db_query_text": sql,
        "attributes": json.dumps(attributes),
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


async def listed_spans(ds, **body):
    response = await ds.client.post("/-/otel/api/spans/list", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_a_catalogue_row_opens_the_spans_behind_it(make_ds):
    """Opening a row on /-/otel/spans is a drill-through to its members, not
    a jump into one of them: the row is a group, so the answer is a list."""
    ds = await make_ds(public_viewer=True)
    trace = f"{9:032x}"
    await store.insert_spans(
        ds,
        [
            span_row(9, name="GET /{database}", kind="SERVER", trace=trace),
            *[
                span_row(
                    20 + n,
                    name="db.query",
                    kind="CLIENT",
                    trace=trace,
                    parent=f"{9:016x}",
                    duration_ms=1.0 + n,
                )
                for n in range(5)
            ],
            *cron_trace(3, task="backup", duration_ms=40.0),
        ],
    )
    listed = await listed_spans(ds, name_exact="db.query")
    assert listed["total"] == 5
    # Slowest first by default -- you opened the row to see the time.
    assert [s["duration_ms"] for s in listed["spans"]] == [5.0, 4.0, 3.0, 2.0, 1.0]
    assert listed["query"]["sort_desc"] == "duration_ms"
    row = listed["spans"][0]
    assert row["trace_id"] == trace
    # Each row says what it sits inside, for a nested span that is the point.
    assert row["trace_label"] == "GET /{database}"
    assert row["parent_span_id"] == f"{9:016x}"

    # The row's identity, not just its name: scope pins it to one plugin.
    cron = await listed_spans(
        ds, name_exact="datasette_cron.run", scope="datasette_cron"
    )
    assert cron["total"] == 1
    assert cron["spans"][0]["scope"] == "datasette_cron"
    assert cron["spans"][0]["parent_span_id"] is None


@pytest.mark.asyncio
async def test_span_list_pages_and_sorts_in_sql(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            span_row(n, name="db.query", kind="CLIENT", duration_ms=float(n))
            for n in range(1, 13)
        ],
    )
    first = await listed_spans(ds, name_exact="db.query", size=5)
    assert first["total"] == 12
    assert [s["duration_ms"] for s in first["spans"]] == [12.0, 11.0, 10.0, 9.0, 8.0]

    second = await listed_spans(ds, name_exact="db.query", size=5, next=first["next"])
    assert [s["duration_ms"] for s in second["spans"]] == [7.0, 6.0, 5.0, 4.0, 3.0]
    last = await listed_spans(ds, name_exact="db.query", size=5, next=second["next"])
    assert last["next"] is None

    oldest = await listed_spans(ds, name_exact="db.query", size=3, sort="start_ns")
    assert [s["duration_ms"] for s in oldest["spans"]] == [1.0, 2.0, 3.0]

    bad = await ds.client.post("/-/otel/api/spans/list", json={"sort": "attributes"})
    assert bad.status_code == 400
    assert "cannot sort spans by attributes" in bad.json()["error"]


@pytest.mark.asyncio
async def test_split_value_and_statement_pin_a_row(make_ds):
    """The two other ways a row is identified: a split_by value (the span
    catalogue) and an exact statement (the SQL page, whose rows are keyed on
    query text rather than span name)."""
    ds = await make_ds(public_viewer=True)
    sql = "select count(*) from [plants]"
    await store.insert_spans(
        ds,
        [
            *cron_trace(3, task="backup", duration_ms=40.0),
            *cron_trace(4, task="cleanup", duration_ms=10.0),
            span_row(30, name="db.query", kind="CLIENT", duration_ms=2.0, sql=sql),
            span_row(
                31, name="db.query", kind="CLIENT", duration_ms=3.0, sql="select 1"
            ),
        ],
    )
    split = await listed_spans(
        ds,
        name_exact="datasette_cron.run",
        split_by="datasette_cron.task",
        split_value="backup",
    )
    assert split["total"] == 1
    assert split["spans"][0]["split_value"] == "backup"

    statement = await listed_spans(ds, name_exact="db.query", statement=sql)
    assert statement["total"] == 1
    assert statement["spans"][0]["duration_ms"] == 2.0


@pytest.mark.asyncio
async def test_highlight_opens_the_page_holding_that_span(make_ds):
    """A span in the trace waterfall links here to see its own kind of work in
    context, so the list has to *land* on it: with a span pinned and no cursor
    of its own, the server pages to wherever the ordering puts it."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            span_row(n, name="db.query", kind="CLIENT", duration_ms=float(n))
            for n in range(1, 13)
        ],
    )
    # Slowest first, so span 2 (2.0ms) is 11th of 12: page 3 at size 5.
    pinned = await listed_spans(
        ds, name_exact="db.query", size=5, highlight=f"{2:016x}"
    )
    assert pinned["query"]["next"] == "10"
    assert [s["duration_ms"] for s in pinned["spans"]] == [2.0, 1.0]
    assert pinned["spans"][0]["span_id"] == f"{2:016x}"

    # The sort is still yours: pinned under the oldest-first order it is 2nd.
    oldest = await listed_spans(
        ds, name_exact="db.query", size=5, sort="start_ns", highlight=f"{2:016x}"
    )
    assert oldest["query"]["next"] == "0"
    assert oldest["spans"][0]["duration_ms"] == 1.0

    # An explicit cursor wins: paging on from the pinned page must not snap
    # back to it, and the pin stays on the query for the row marking.
    paged = await listed_spans(
        ds, name_exact="db.query", size=5, highlight=f"{2:016x}", next="0"
    )
    assert [s["duration_ms"] for s in paged["spans"]] == [12.0, 11.0, 10.0, 9.0, 8.0]
    assert paged["query"]["highlight"] == f"{2:016x}"

    # A span that no longer matches the filters is simply not found: the list
    # answers from row one and the frontend says the pin is not on this page.
    missing = await listed_spans(ds, name_exact="db.query", highlight=f"{999:016x}")
    assert missing["query"]["next"] is None
    assert missing["total"] == 12

    bad = await ds.client.post(
        "/-/otel/api/spans/list", json={"highlight": "../../etc"}
    )
    assert bad.status_code == 400
    assert "highlight is not a span id" in bad.json()["error"]


@pytest.mark.asyncio
async def test_span_list_page_renders_and_is_gated(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(ds, cron_trace(3, task="backup", duration_ms=5.0))
    response = await ds.client.get(
        "/-/otel/spans/list?name_exact=datasette_cron.run&scope=datasette_cron"
        f"&highlight={3:016x}"
    )
    assert response.status_code == 200
    assert "src/pages/spans_list/index.ts" in response.text
    data = page_data(response.text)
    assert data["total"] == 1
    assert data["query"]["name_exact"] == "datasette_cron.run"
    # The pin arrives from the trace waterfall in the URL and rides the page
    # data through to the row marking.
    assert data["query"]["highlight"] == f"{3:016x}"
    assert data["database"] == "otel"
    # The page names the row it opened, in the title and the crumbs.
    assert "<title>datasette_cron.run</title>" in response.text

    private = await make_ds()
    assert (await private.client.get("/-/otel/spans/list")).status_code == 403
    assert (
        await private.client.post("/-/otel/api/spans/list", json={})
    ).status_code == 403


@pytest.mark.asyncio
async def test_a_store_too_slow_to_summarise_is_503_not_500(make_ds, monkeypatch):
    """The summary pages read every stored span, so a big enough store on a
    slow enough box outruns sql_time_limit_ms. That has to arrive as an
    answer, not as the unhandled QueryInterrupted it used to be -- which
    Datasette turns into a 500 and a traceback in the log.

    The timeout is injected at the query function rather than by dropping
    sql_time_limit_ms, which would interrupt Datasette's own startup queries
    long before a route ran."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(ds, [span_row(1, name="db.query")])

    def interrupt(*args, **kwargs):
        raise QueryInterrupted(sqlite3.OperationalError("interrupted"), "select 1", [])

    for name in ("span_groups", "sql_queries", "list_traces"):
        monkeypatch.setattr(queries, name, interrupt)

    for path in ("/-/otel/spans", "/-/otel/sql", "/-/otel/traces"):
        response = await ds.client.get(path)
        assert response.status_code == 503, f"{path} gave {response.status_code}"
        assert "timed out reading the trace store" in response.text

    # The JSON API is behind the same gate, so it answers the same way.
    api = await ds.client.post("/-/otel/api/spans/groups", json={})
    assert api.status_code == 503


@pytest.mark.asyncio
async def test_the_list_charts_every_matching_span(make_ds, monkeypatch):
    """The table shows one page, sorted; the scatter above it is the whole
    matching set, which is what makes a single span's duration mean anything.
    So it answers the filters, not the page -- and it is capped, so a busy row
    samples one span in n rather than shipping a quarter of a million dots."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            span_row(n, name="db.query", kind="CLIENT", duration_ms=float(n))
            for n in range(1, 13)
        ]
        # Same store, different work: the chart must not draw these.
        + [span_row(50, name="other.thing", duration_ms=99.0)],
    )

    listed = await listed_spans(ds, name_exact="db.query", size=5)
    assert listed["chart_stride"] == 1
    # Every matching span, whichever page you are on, oldest dot first.
    assert len(listed["chart"]) == 12
    assert [p["duration_ms"] for p in listed["chart"]] == [
        float(n) for n in range(1, 13)
    ]
    assert listed["chart"][0]["start_ns"] < listed["chart"][-1]["start_ns"]
    assert {p["span_id"] for p in listed["chart"]} == {
        f"{n:016x}" for n in range(1, 13)
    }

    # A filter narrows the cloud too, or the dots would answer a question
    # nobody asked.
    filtered = await listed_spans(ds, name_exact="db.query", min_duration_ms=10)
    assert [p["duration_ms"] for p in filtered["chart"]] == [10.0, 11.0, 12.0]

    # Past the cap the chart samples: one span in `chart_stride`, spread over
    # the range rather than the first slice of it.
    monkeypatch.setattr(queries, "SPAN_CHART_POINTS", 4)
    sampled = await listed_spans(ds, name_exact="db.query", highlight=f"{2:016x}")
    assert sampled["chart_stride"] == 3
    assert sampled["total"] == 12
    # 4 sampled dots, plus the pinned span: it is why you came, so it is drawn
    # whether or not the sample happened to catch it.
    ids = [p["span_id"] for p in sampled["chart"]]
    assert f"{2:016x}" in ids
    assert len(ids) == 5
