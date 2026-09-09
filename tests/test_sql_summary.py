"""``/-/otel/sql``: one row per statement, aggregated over every run of it,
with the callback-style work Datasette records instead of SQL text."""

import json
import time

import pytest
from conftest import page_data

from datasette_otel_viewer import store

START_NS = time.time_ns() - 3600 * 1_000_000_000
PLANTS_SQL = "select id, name from [plants] order by id limit 101"
COUNT_SQL = "select count(*) from [plants]"


def write_child_row(parent, *, name="db.write.execute"):
    """The child span Datasette gives a write. Its presence on a db.query is
    what marks that statement a write -- including for a write callback,
    which carries no SQL to sniff."""
    return {
        "trace_id": parent["trace_id"],
        "span_id": parent["span_id"][:-2] + "ff",
        "parent_span_id": parent["span_id"],
        "name": name,
        "kind": "INTERNAL",
        "start_ns": parent["start_ns"],
        "end_ns": parent["end_ns"],
        "duration_ms": parent["duration_ms"],
        "status": "UNSET",
        "service_name": "datasette",
        "attributes": "{}",
        "resource": json.dumps({"service.name": "datasette"}),
        "scope_name": "datasette",
    }


def sql_span_row(
    i,
    *,
    sql=PLANTS_SQL,
    callback=None,
    namespace="demo",
    operation="SELECT",
    duration_ms=1.0,
    status="UNSET",
    rows_returned=None,
):
    """One ``db.query`` span, the shape selfsource.span_to_row produces.
    ``callback`` replaces the SQL text, as Datasette does for execute_fn()."""
    start = START_NS + i * 1_000_000_000
    attributes = {"db.system": "sqlite", "db.namespace": namespace}
    if callback:
        attributes["datasette.callback"] = callback
    else:
        attributes["db.query.text"] = sql
    if rows_returned is not None:
        attributes["datasette.rows_returned"] = rows_returned
    return {
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": None,
        "name": "db.query",
        "kind": "CLIENT",
        "start_ns": start,
        "end_ns": start + int(duration_ms * 1e6),
        "duration_ms": duration_ms,
        "status": status,
        "service_name": "datasette",
        "db_namespace": namespace,
        "db_operation": None if callback else operation,
        "db_query_text": None if callback else sql,
        "attributes": json.dumps(attributes),
        "resource": json.dumps({"service.name": "datasette"}),
        "scope_name": "datasette",
    }


async def listed(ds, **body):
    response = await ds.client.post("/-/otel/api/sql/queries", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_percentiles_and_total_over_every_run(make_ds):
    """Durations 1..100 ms of one statement: nearest-rank percentiles off the
    stored durations, and the total that orders the page."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds, [sql_span_row(i, duration_ms=float(i)) for i in range(1, 101)]
    )
    summary = await listed(ds)
    assert summary["run_count"] == 100
    assert summary["total_ms"] == pytest.approx(5050.0)
    (row,) = summary["queries"]
    assert row["query"] == PLANTS_SQL
    assert row["run_count"] == 100
    assert (row["p50_ms"], row["p95_ms"], row["max_ms"]) == (50.0, 95.0, 100.0)
    assert row["total_ms"] == pytest.approx(5050.0)
    # The slowest run is the one a row links to: 100 ms, span 100.
    assert row["slowest_span_id"] == f"{100:016x}"


@pytest.mark.asyncio
async def test_statements_group_per_database_and_include_callbacks(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            sql_span_row(1, duration_ms=10.0, rows_returned=101),
            sql_span_row(2, duration_ms=30.0, rows_returned=7),
            # Same text, other database: its own row, not the same statement.
            sql_span_row(3, namespace="_internal", duration_ms=1.0),
            sql_span_row(4, sql=COUNT_SQL, duration_ms=2.0, status="ERROR"),
            # Callback work carries no SQL: listed under the callback's name,
            # or a third of a database's time would be invisible here.
            sql_span_row(5, callback="Database.table_columns.<locals>.<lambda>"),
        ],
    )
    rows = {(r["database"], r["query"]): r for r in (await listed(ds))["queries"]}
    plants = rows[("demo", PLANTS_SQL)]
    assert plants["run_count"] == 2
    assert plants["total_ms"] == pytest.approx(40.0)
    assert plants["max_rows"] == 101
    assert plants["operation"] == "SELECT"
    assert plants["callback"] is None
    assert rows[("_internal", PLANTS_SQL)]["run_count"] == 1
    assert rows[("demo", COUNT_SQL)]["error_count"] == 1

    callback = rows[("demo", "Database.table_columns.<locals>.<lambda>")]
    assert callback["callback"] == "Database.table_columns.<locals>.<lambda>"
    assert callback["operation"] is None

    # Ordered by the time each statement accounts for, not by its slowest run.
    assert (await listed(ds))["queries"][0]["query"] == PLANTS_SQL


@pytest.mark.asyncio
async def test_filters(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            sql_span_row(1, duration_ms=5.0),
            sql_span_row(2, duration_ms=500.0),
            sql_span_row(3, sql=COUNT_SQL, namespace="_internal"),
            sql_span_row(4, sql="delete from [plants]", operation="DELETE"),
            sql_span_row(5, callback="Database.table_columns.<locals>.<lambda>"),
        ],
    )

    async def runs(**body):
        return (await listed(ds, **body))["run_count"]

    assert await runs(sql="from [plants]") == 4
    # The needle matches callback names too, so filtering by a table name
    # doesn't hide the callbacks that touched it.
    assert await runs(sql="table_columns") == 1
    assert await runs(database="_internal") == 1
    assert await runs(operation="DELETE") == 1
    assert await runs(min_duration_ms=100) == 1
    assert await runs(sql="from [plants]", min_duration_ms=100) == 1
    # LIKE metacharacters are literal, not wildcards.
    assert await runs(sql="%") == 0

    facets = await listed(ds, database="demo")
    assert facets["operations"] == ["DELETE", "SELECT"]
    # A facet lists the options you could switch to: the database filter does
    # not narrow its own list.
    assert facets["databases"] == ["_internal", "demo"]


@pytest.mark.asyncio
async def test_read_write_split(make_ds):
    """Writes are spotted structurally, by the db.write.* child span
    Datasette gives them -- so a write callback with no SQL text counts, and
    a `with ... select` read is not mistaken for one by its keyword."""
    ds = await make_ds(public_viewer=True)
    write_sql = sql_span_row(1, sql="update [plants] set name = ?", operation="UPDATE")
    write_callback = sql_span_row(2, callback="apply_migrations.<locals>.fn")
    await store.insert_spans(
        ds,
        [
            write_sql,
            write_child_row(write_sql),
            write_callback,
            write_child_row(write_callback, name="db.write.queue_wait"),
            # A read whose first keyword is not SELECT, and a read callback.
            sql_span_row(
                3, sql="with rows as (select 1) select * from rows", operation="WITH"
            ),
            sql_span_row(4, callback="Database.table_columns.<locals>.<lambda>"),
            sql_span_row(5),
        ],
    )
    rows = {r["query"]: r for r in (await listed(ds))["queries"]}
    assert rows["update [plants] set name = ?"]["is_write"] is True
    assert rows["apply_migrations.<locals>.fn"]["is_write"] is True
    assert rows["with rows as (select 1) select * from rows"]["is_write"] is False
    assert rows["Database.table_columns.<locals>.<lambda>"]["is_write"] is False

    writes = await listed(ds, access="write")
    assert writes["run_count"] == 2
    assert {r["query"] for r in writes["queries"]} == {
        "update [plants] set name = ?",
        "apply_migrations.<locals>.fn",
    }
    reads = await listed(ds, access="read")
    # Every run is one or the other: a callback's NULL db.operation must not
    # fall out of both sides.
    assert reads["run_count"] + writes["run_count"] == (await listed(ds))["run_count"]
    assert "Database.table_columns.<locals>.<lambda>" in {
        r["query"] for r in reads["queries"]
    }

    bad = await ds.client.post("/-/otel/api/sql/queries", json={"access": "sideways"})
    assert bad.status_code == 400
    assert "access must be" in bad.json()["error"]


@pytest.mark.asyncio
async def test_page_renders_and_is_gated(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(ds, [sql_span_row(1, duration_ms=3.0)])
    response = await ds.client.get("/-/otel/sql?sql=plants&database=demo")
    assert response.status_code == 200
    assert "src/pages/sql_summary/index.ts" in response.text
    data = page_data(response.text)
    assert data["query"]["sql"] == "plants"
    assert data["queries"][0]["run_count"] == 1
    assert data["database"] == "otel"

    assert (await ds.client.get("/-/otel/sql?min_duration_ms=-1")).status_code == 400

    private = await make_ds()
    assert (await private.client.get("/-/otel/sql")).status_code == 403
    assert (
        await private.client.post("/-/otel/api/sql/queries", json={})
    ).status_code == 403
