# 04 — OTLP receiver: decode + ingest routes

Status: done

Port the debugger's `otlp.py` (protobuf + OTLP/JSON decode with the hex-id
rewrite, AnyValue flattening, rows matching store.COLUMNS) and `ingest.py`
(top-level `POST /v1/traces` because stock exporters append the path;
`/v1/metrics` + `/v1/logs` accept-and-discard stubs; gzip; bearer auth with
constant-time compare; 503 when unconfigured).

Changes from the donor: plugin name; ingest wraps `insert_spans` in
`store.suppress()` (this instance may have a live provider now — the debugger
never did); enabled iff `ingest_token` set or `allow_unauthenticated_ingest:
true`.

Acceptance: protobuf and JSON bodies land as rows; bad token 401; unconfigured
503; stubs 200; a datasette-otel-otlp sender pointed at this instance works
(manual, Justfile).
