# 03 — Self source: provider, sampler, buffering exporter

Status: done

Productize `demos/otel/self_storage/plugins/self_traces.py` with the sibling
plugins' import-time pattern:

- **Import**: global provider untouched → install `TracerProvider` with
  `SuppressingSampler` + `BatchSpanProcessor(SelfStoreExporter,
  schedule_delay_millis=1000)`. Real SDK provider already present → mode
  `foreign`: self mode structurally off (suppression needs our sampler);
  loud stderr line at startup. Buffering-until-armed exporter captures the
  `datasette.startup` trace (bounded pending buffer, 8192 rows).
- **Startup hook**: `ensure_db`, then arm the exporter with (event loop,
  datasette) — depends on the 1.0a39 lifecycle guarantee that `datasette
  serve` runs startup and serving on ONE loop; on older lifecycles inserts
  fail with "Event loop is closed" (drop, don't crash — same guard as the
  demo). `self_traces: false` → disarm and discard instead; sampling stays on
  (attached sibling processors may still need spans; documented overhead).
- `span_to_row(ReadableSpan)` mirrors `otlp.request_to_rows`'s row shape so
  self and ingested spans are indistinguishable in the store; resource
  `service.name` from config (default `datasette`).
- `export()` schedules one insert coroutine per batch via
  `run_coroutine_threadsafe`; the coroutine wraps `insert_spans` +
  `maybe_prune` in `store.suppress()` — with `block=True`-style default
  writes, the context reaches core's write thread and the sampler drops the
  three insert spans. Gain < 1 by construction.

Acceptance: e2e — request lands as rows (request span + db.query children);
`datasette.startup` trace present; span count **stabilizes** after activity
stops (the anti-runaway assertion); `self_traces: false` stores nothing.
