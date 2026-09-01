# 02 — Store: schema port + in-process retention

Status: done

Port the debugger's `store.py` (schema, COLUMNS row contract, idempotent
`insert or replace`, derived-`traces` upsert) with the plugin name changed.
This schema is the public contract — changes after 0.1 need a versioning story.

Add what the debugger deferred to an external cron:

- `SUPPRESS_KEY` + `suppress()` context manager (otel context key) — every
  store write path wraps itself in it; the self source's sampler drops spans
  started under it. Lives here because both writers (self exporter, ingest)
  need it without importing each other.
- `maybe_prune(datasette)`: throttled (>=60s between runs, monkeypatchable),
  trace-granular — delete whole traces past `retention_hours`, then whole
  oldest traces beyond `max_spans` (window-function cumulative span_count over
  `traces` ordered newest-first). Delete `spans` by trace_id then the `traces`
  rows; no summary recompute needed since deletion is whole-trace.

Acceptance: unit tests seed doctored `start_ns` values and verify age prune,
size prune, orphan-free `traces`, and throttling.
