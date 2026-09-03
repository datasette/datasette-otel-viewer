"""Storage: attach the ``otel`` database, hold its schema, batch-write
decoded spans, and enforce retention.

Schema ported from datasette-otel-debugger (see its plan.md "Storage"): one
wide ``spans`` table for all senders (promoted semconv columns for
facets/sorts + full-fidelity JSON), plus a derived ``traces`` summary table
recomputed per touched trace_id after every insert.

Unlike the debugger — which installed no TracerProvider, so its writes could
never be traced — this instance MAY have a live provider (self mode, or a
sibling plugin's). Every write path here therefore runs inside
``suppress()``: a context key the self source's SuppressingSampler checks, so
spans about storing spans are dropped at creation. With Datasette's default
``block=True`` writes the enqueueing context reaches the write thread, which
is what carries the key to the ``db.query`` / ``db.write.*`` spans (measured
in demos/otel/self_storage/README.md — the one workaround that holds).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from datasette.database import Database
from opentelemetry import context as otel_context

PLUGIN_NAME = "datasette-otel-receiver"
DEFAULT_DB_NAME = "otel"
DEFAULT_DB_PATH = "otel.db"
DEFAULT_RETENTION_HOURS = 72
DEFAULT_MAX_SPANS = 100_000
PRUNE_INTERVAL_SECONDS = 60

SUPPRESS_KEY = otel_context.create_key("datasette_otel_receiver.suppress")


@contextlib.contextmanager
def suppress():
    "Mark everything started in here as store plumbing: sampler drops it."
    token = otel_context.attach(otel_context.set_value(SUPPRESS_KEY, True))
    try:
        yield
    finally:
        otel_context.detach(token)


# The row dict contract shared by both writers (otlp.request_to_rows for
# ingested spans, selfsource.span_to_row for this instance's own). Promoted
# columns may be None; attributes/resource are already-serialized JSON.
COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "start_ns",
    "end_ns",
    "duration_ms",
    "status",
    "status_description",
    "service_name",
    "http_route",
    "http_status",
    "db_namespace",
    "db_operation",
    "db_query_text",
    "attributes",
    "resource",
    "scope_name",
    "scope_version",
    "schema_url",
)

# span_id PRIMARY KEY: dedupes retried OTLP batches (with INSERT_SQL's
# `or replace`) and gives stable row URLs. All FKs declared-but-unenforced on
# purpose: orphan spans whose parent never arrives (remote traceparent
# parents) and batches inserted before their traces summary row exists are
# design requirements. IF NOT EXISTS everywhere — no migration framework in
# v1, ensure_db() reruns this on every startup.
SCHEMA_SQL = """
create table if not exists traces (
  trace_id text primary key,
  start_ns integer, end_ns integer, duration_ms real,
  span_count integer not null default 0,
  error_count integer not null default 0,
  root_span_id text references spans(span_id),
  name text, service_name text,
  http_route text, http_status integer,
  status text
);
create index if not exists idx_traces_start_ns on traces (start_ns);
create index if not exists idx_traces_service_start
  on traces (service_name, start_ns);

create table if not exists spans (
  trace_id text references traces(trace_id),
  span_id text primary key,
  parent_span_id text references spans(span_id),
  name text, kind text,
  start_ns integer, end_ns integer, duration_ms real,
  status text, status_description text,
  -- promoted at insert time (null when absent):
  service_name text,
  http_route text, http_status integer,
  db_namespace text, db_operation text, db_query_text text,
  -- full fidelity:
  attributes text,   -- JSON, json_extract() for anything not promoted
  resource text,     -- JSON
  scope_name text, scope_version text, schema_url text
);
create index if not exists idx_spans_name_start_ns on spans (name, start_ns);
create index if not exists idx_spans_trace_id on spans (trace_id);
create index if not exists idx_spans_service_name_start_ns
  on spans (service_name, start_ns);
"""

INSERT_SQL = "insert or replace into spans ({}) values ({})".format(
    ", ".join(COLUMNS), ", ".join("?" for _ in COLUMNS)
)

# Recomputes exactly one traces row (:trace_id) entirely from spans — never
# increments, so duplicate/retried batches can't double-count. Root = the
# trace's earliest span whose parent is null or absent from the trace
# (orphan-as-root), ties broken by span_id.
TRACES_UPSERT_SQL = """
insert or replace into traces
select
  s.trace_id,
  min(s.start_ns) as start_ns,
  max(s.end_ns) as end_ns,
  (max(s.end_ns) - min(s.start_ns)) / 1e6 as duration_ms,
  count(*) as span_count,
  sum(s.status = 'ERROR') as error_count,
  root.span_id as root_span_id,
  root.name as name,
  root.service_name as service_name,
  root.http_route as http_route,
  root.http_status as http_status,
  root.status as status
from spans s
left join (
  select span_id, name, service_name, http_route, http_status, status
  from (
    select r.span_id, r.name, r.service_name, r.http_route, r.http_status,
           r.status,
           row_number() over (order by r.start_ns, r.span_id) as rn
    from spans r
    where r.trace_id = :trace_id
      and (r.parent_span_id is null
           or not exists (
             select 1 from spans p
             where p.trace_id = r.trace_id and p.span_id = r.parent_span_id
           ))
  )
  where rn = 1
) root on 1 = 1
where s.trace_id = :trace_id
group by s.trace_id
"""

# Retention is trace-granular: whole traces are deleted, never individual
# spans, so the derived summary rows stay exact with no recompute.
PRUNE_AGE_SQL = """
delete from spans where trace_id in
  (select trace_id from traces where start_ns < :cutoff_ns);
delete from traces where start_ns < :cutoff_ns;
"""

# Oldest whole traces beyond max_spans, by cumulative span_count newest-first.
PRUNE_SIZE_SELECT_SQL = """
select trace_id from (
  select trace_id,
         sum(span_count) over (
           order by start_ns desc, trace_id
           rows between unbounded preceding and current row
         ) as cumulative
  from traces
) where cumulative > :max_spans
"""


def _plugin_config(datasette) -> dict:
    return datasette.plugin_config(PLUGIN_NAME) or {}


def db_name(datasette) -> str:
    return _plugin_config(datasette).get("db_name") or DEFAULT_DB_NAME


def _db_path(datasette) -> str:
    return _plugin_config(datasette).get("db_path") or DEFAULT_DB_PATH


async def ensure_db(datasette) -> Database:
    """Attach the otel database (creating file/connection if needed) and
    ensure the schema. Idempotent; called from the startup hook."""
    name = db_name(datasette)
    if name not in datasette.databases:
        db = datasette.add_database(
            Database(datasette, path=_db_path(datasette), is_mutable=True),
            name=name,
        )
    else:
        db = datasette.databases[name]
    with suppress():
        await db.execute_write_script(SCHEMA_SQL)
    return db


def _row_values(row: dict[str, Any]) -> tuple:
    duration_ms = row.get("duration_ms")
    if duration_ms is None:
        start_ns, end_ns = row.get("start_ns"), row.get("end_ns")
        if start_ns is not None and end_ns is not None:
            duration_ms = (end_ns - start_ns) / 1e6
    return tuple(
        duration_ms if column == "duration_ms" else row.get(column)
        for column in COLUMNS
    )


async def insert_spans(datasette, rows: list[dict]) -> int:
    """Batch-insert decoded row dicts, then recompute the traces summary for
    every distinct trace_id touched. Runs suppressed — see module docstring."""
    if not rows:
        return 0
    db = datasette.databases[db_name(datasette)]
    with suppress():
        await db.execute_write_many(INSERT_SQL, [_row_values(r) for r in rows])
        trace_ids = sorted(
            {r.get("trace_id") for r in rows if r.get("trace_id") is not None}
        )
        if trace_ids:
            await db.execute_write_many(
                TRACES_UPSERT_SQL, [{"trace_id": t} for t in trace_ids]
            )
    return len(rows)


_last_prune = 0.0


async def maybe_prune(datasette, force: bool = False) -> None:
    """Trace-granular ring buffer, throttled to once per PRUNE_INTERVAL_SECONDS.

    Age first (retention_hours), then size (max_spans): both delete whole
    traces so `traces` never drifts from `spans`."""
    global _last_prune
    now = time.monotonic()
    if not force and now - _last_prune < PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = now

    config = _plugin_config(datasette)
    retention_hours = float(config.get("retention_hours", DEFAULT_RETENTION_HOURS))
    max_spans = int(config.get("max_spans", DEFAULT_MAX_SPANS))
    cutoff_ns = int((time.time() - retention_hours * 3600) * 1e9)

    db = datasette.databases[db_name(datasette)]
    with suppress():

        def prune(conn):
            conn.execute(
                "delete from spans where trace_id in "
                "(select trace_id from traces where start_ns < :cutoff_ns)",
                {"cutoff_ns": cutoff_ns},
            )
            conn.execute(
                "delete from traces where start_ns < :cutoff_ns",
                {"cutoff_ns": cutoff_ns},
            )
            doomed = [
                row[0]
                for row in conn.execute(PRUNE_SIZE_SELECT_SQL, {"max_spans": max_spans})
            ]
            if doomed:
                placeholders = ", ".join("?" for _ in doomed)
                conn.execute(
                    f"delete from spans where trace_id in ({placeholders})",
                    doomed,
                )
                conn.execute(
                    f"delete from traces where trace_id in ({placeholders})",
                    doomed,
                )
            conn.commit()

        await db.execute_write_fn(prune)
