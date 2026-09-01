# 05 — Viewer: /-/traces list + waterfall

Status: done

Server-rendered, no build step (the debugger's Svelte frontend is reference
only). Port the demo plugin's two views, adapted to the richer schema:

- `/-/traces`: reads the derived `traces` summary table (no aggregation at
  request time) — root name, service, spans, errors, duration, started.
  Generic-first: columns are semconv-derived so non-Datasette senders render
  identically; `service_name` shown because multi-service is the receiver's
  point.
- `/-/traces/<trace_id>`: indented waterfall over a shared timeline,
  cycle-guarded depth walk, orphans root at top level, expandable attribute
  JSON, 5000-span cap, ERROR bars tinted.
- Both link to the raw tables (`/otel/spans`, `/otel/traces`) — facets and SQL
  come free from Datasette itself.

Gated by `otel-view` (ticket 06) unless `public_viewer: true`.

Acceptance: pages render for self-stored and ingested spans alike; a trace id
from either source round-trips list → waterfall.
