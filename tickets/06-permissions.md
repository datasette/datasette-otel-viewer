# 06 — Permissions: otel-view, private by default

Status: done

Port the debugger's `permissions.py` wholesale — it already solved the hard
part (a `permission_resources_sql` deny row **scoped to the otel database**
via `parent = db_name`, never global, so other databases are untouched; the
actor escape via `await datasette.allowed(action="otel-view")` covers config
grants and `--root` with zero extra code). Registered action: `otel-view`.

Additions: the viewer routes (ticket 05) check the same action directly,
bypassed by `public_viewer: true` (which deliberately does NOT open the raw
tables). Ingest auth stays the separate static bearer token.

Acceptance: anonymous `/otel/spans.json` and `/-/traces` are denied by
default; `public_viewer: true` opens only the viewer; a granted actor (or
root) sees everything.
