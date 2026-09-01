# datasette-otel-receiver

One plugin, three roles, one SQLite store:

1. **self** (default on) — store the spans *this* Datasette emits, browsable on the
   same instance. The productized `demos/otel/self_storage` workaround.
2. **receiver** (off until configured) — an OTLP/HTTP ingest endpoint (protobuf +
   JSON, bearer auth), so *any* OTel source — another Datasette running
   datasette-otel-otlp, Flask, Deno, a browser SDK — can export to this instance.
3. **viewer** — server-rendered `/-/traces` (recent traces) and
   `/-/traces/<trace_id>` (waterfall), plus the raw tables served like any other
   Datasette database.

Together they replace **datasette-otel-debugger**: the receiver-only two-instance
architecture survives as roles 2+3, and the "self-observe mode" its plan rejected
is now safe to ship because the feedback loop was solved by measurement
(`demos/otel/self_storage/README.md`): a custom context key + `SuppressingSampler`
+ `block=True` writes is the one workaround that holds.

## The one hard rule

**Self-storage requires owning the provider's sampler.** The suppression that
stops the store's own writes from re-entering the store lives in the sampler; a
provider some other plugin installed doesn't have it, and self-storing without it
is the measured runaway (~10–14k spans/s). So:

- At import, if the global provider is untouched, this plugin installs its own
  `TracerProvider` with the `SuppressingSampler` (+ a 1s `BatchSpanProcessor`
  around a buffering exporter, so the `datasette.startup` trace is captured, same
  two-phase trick as the sibling plugins).
- If a real SDK provider already exists at import (agent, or otlp/parquet imported
  first), **self mode is structurally disabled** — one loud stderr line at
  startup; receiver + viewer still work. Never attach-and-self-store.
- The sibling plugins (post-coexistence-fix) attach to *our* provider happily, so
  receiver+otlp+parquet all compose when this plugin imports first.

Every store write (self exporter *and* OTLP ingest) runs inside the suppression
context, so ingest under a live provider records one request span per POST and
zero spans about the inserts themselves — gain < 1 by construction.

## Store schema v1 (public contract)

The debugger's hybrid schema, ported verbatim (it was designed for exactly this):
a wide `spans` table (promoted semconv columns — `service_name`, `http_route`,
`http_status`, `db_namespace`, `db_operation`, `db_query_text` — plus
full-fidelity `attributes`/`resource` JSON) and a **derived** `traces` summary
table recomputed per touched trace_id after every insert. `span_id` primary key
makes retried OTLP batches idempotent; FKs are declared-not-enforced so orphan and
remote-parent spans keep inserting. Database name `otel` (file `otel.db`),
configurable. Generic views key off semconv columns so non-Datasette senders get
them free; `datasette.*` niceties stay a thin layer on top.

## Retention is required, not stretch

In-process, trace-granular ring buffer: after inserts (at most once per 60s),
delete whole traces older than `retention_hours` (default 72) and, if the store
still exceeds `max_spans` (default 100,000), the oldest whole traces beyond it.
Trace-granular means summary rows never drift. Runs inside the suppression
context like every other store write.

## Config

```yaml
plugins:
  datasette-otel-receiver:
    self_traces: true            # default true; false = viewer/receiver only
    ingest_token: $OTEL_TOKEN    # setting this enables POST /v1/traces
    # allow_unauthenticated_ingest: true   # dev escape hatch, enables it too
    public_viewer: false         # default false: viewer + tables need otel-view
    retention_hours: 72
    max_spans: 100000
    db_name: otel
    db_path: otel.db
    service_name: datasette      # resource attr for self-emitted spans
```

Private by default: the `otel-view` Action gates the viewer routes AND (via a
scoped `permission_resources_sql` deny) the raw database — `db.query.text` is
user-supplied SQL on public instances. `public_viewer: true` opens the `/-/traces`
pages only; the tables stay gated.

## Tickets

| # | Ticket | Status |
|---|--------|--------|
| 01 | [Scaffold](tickets/01-scaffold.md) | done |
| 02 | [Store: schema port + in-process retention](tickets/02-store.md) | done |
| 03 | [Self source: provider, sampler, buffering exporter](tickets/03-self-source.md) | done |
| 04 | [OTLP receiver: decode + ingest routes](tickets/04-receiver.md) | done |
| 05 | [Viewer: /-/traces list + waterfall](tickets/05-viewer.md) | done |
| 06 | [Permissions: otel-view, private by default](tickets/06-permissions.md) | done |
| 07 | [README + Justfile demos](tickets/07-readme-demos.md) | done |

## Reference material

- `~/projects/datasette/demos/otel/self_storage/` — the measured feedback loop and
  the working demo plugin this productizes (incl. the 1.0a39 single-event-loop
  lifecycle note the exporter depends on).
- `~/work/simonw/datasette-otel-debugger` — donor for `otlp.py` (OTLP decode),
  `store.py` (schema), `ingest.py` (routes/auth), `permissions.py` (Action +
  scoped deny). Its Svelte frontend is reference-only; the viewer here is
  server-rendered. NOTE: that repo has uncommitted work in flight (frontend
  sort/routes/sql pages, api/pages changes) — left untouched there.
- `~/work/simonw/datasette-otel-otlp`, `~/work/simonw/datasette-otel-parquet` —
  house style, the import-time provider pattern, coexistence semantics, test
  bootstrap for OTel's set-once globals.
