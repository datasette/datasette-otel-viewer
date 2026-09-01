# 01 — Scaffold

Status: done

House style from datasette-otel-otlp: setuptools, `Framework :: Datasette`,
Apache-2.0, uv. Package `datasette_otel_receiver`, entry point `otel_receiver`.
Deps: `datasette>=1a37`, `opentelemetry-sdk>=1.37`, `opentelemetry-proto`
(receiver decode). Dev: pytest, pytest-asyncio (strict), httpx.

Acceptance: plugin listed by `/-/plugins`; `just test` runs.
