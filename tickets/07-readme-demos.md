# 07 — README + Justfile demos

Status: done

Justfile (sibling conventions, `--no-project --isolated --with-editable
~/projects/datasette` — spans only exist on the otel branches):

- `just demo` — self mode: serve demo.db, browse, open `/-/traces`.
- `just demo-receiver` + `just demo-sender` — the two-instance story:
  receiver on 8003 with a token, a second Datasette running
  datasette-otel-otlp pointed at it.
- `just test`, `just clean`.

README: quickstart (self mode first — install and go), the three roles, config
reference, schema table, **privacy headline** (`db.query.text` is user SQL;
private by default via `otel-view`; parameter values never recorded), the
foreign-provider limitation for self mode, the 1.0a39 lifecycle note, and the
debugger-replacement note.

Acceptance: fresh checkout → `just demo` → browse → `/-/traces` shows the
session. Receiver pair works with the sibling otlp plugin.
