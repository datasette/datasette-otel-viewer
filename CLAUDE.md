# datasette-otel-viewer

Browse the OpenTelemetry traces and metrics a Datasette instance emits, from
inside that instance: a self-recording span and metric store plus the
`/-/otel` viewer (`/traces`, `/metrics`). Nothing else can write into the
store over HTTP. Product notes, privacy posture and config reference live in
`NOTES.md`; this file is the map for working on the code.

## Architecture

- **Backend:** Python, Datasette (otel branch, see `[tool.uv.sources]`),
  datasette-plugin-router, Pydantic, opentelemetry-sdk/proto
- **Frontend:** Svelte 5 (runes), TypeScript, Vite, openapi-fetch, SveltePlot
  (metric charts only), served through datasette-vite
- **Store:** `otel.db` (`traces` + `spans`, `metrics` + `metric_points`,
  `store.py`), no migration framework — `SCHEMA_SQL` is `IF NOT EXISTS` and
  reruns on every startup
- **Build:** Just (Justfile), uv (Python), npm (frontend)

## Commands

| Command | What it does |
|---------|-------------|
| `just dev` | Datasette on :8012 against `demo.db` (`--root`, viewer public); serves the built bundle |
| `just dev-with-hmr` | `dev` + Vite HMR via datasette-vite `dev_paths`; restarts on .py/.html changes |
| `just frontend-dev` | Vite dev server on :5186 (pair with `dev-with-hmr`) |
| `just frontend` | Production build → `datasette_otel_viewer/static/gen/` + `manifest.json` |
| `just types` | Regenerate `frontend/api.d.ts` + `frontend/src/page_data/*.types.ts` from Python |
| `just types-watch` | Watch .py files, auto-regenerate types |
| `just format` / `just format-check` | ruff (backend) + prettier (frontend) |
| `just check` | ruff check + svelte-check/tsc |
| `just test` | pytest (page routes run in Vite dev mode, no build needed) |
| `just test-frontend` | vitest over `frontend/src/lib/*.test.ts` |
| `just shots` | Regenerate `docs/screenshots/*.png` via Playwright (not in CI; re-run and check `git status`) |
| `just demo` | The NOTES.md self-mode demo on :8003 |

CI: `.github/workflows/test.yml` builds the bundle before pytest, and
type-checks/tests/builds the frontend in a second job.

## Project structure

```
datasette_otel_viewer/
├── __init__.py              # Hooks: startup, register_routes, extra_template_vars
├── router.py                # Shared Router + check_viewer() decorator
├── page_data.py             # Pydantic models: page data + API request/response
├── queries.py               # Read-side SQL shared by pages and API
├── routes/pages.py          # GET /-/otel (index), /traces, /traces/{trace_id}, /metrics, /metrics/{name} (HTML)
├── routes/api.py            # POST /-/otel/api/traces/list, GET /-/otel/api/traces/{trace_id}
├── selfsource.py            # TracerProvider ownership + suppression sampler
├── selfmetrics.py           # MeterProvider ownership/attach + SDK metrics → rows
├── store.py                 # Schema, batch insert, retention
├── permissions.py           # otel-view action; raw tables private by default
├── templates/otel_viewer_base.html   # The single template
├── static/gen/, manifest.json          # Built by Vite (gitignored)

frontend/src/
├── pages/index/             # /-/otel landing page (IndexPage.svelte): counts + links
├── pages/traces_list/       # List page (TracesListPage.svelte)
├── pages/trace_detail/      # Waterfall + inspector (TraceDetailPage, WaterfallRow)
├── pages/metrics_list/      # Metrics table
├── pages/metric_detail/     # SveltePlot charts (SeriesChart, HistogramHeatmap, PercentileChart)
├── lib/traceTree.ts         # Span forest assembly (unit-tested), time.ts, sort.ts
├── lib/metricsMath.ts       # TS twin of metrics_math.py (shared test vectors), metricsSeries.ts
├── components/SortHeader.svelte
├── page_data/load.ts        # loadPageData<T>()
├── api.ts                   # openapi-fetch client over api.d.ts
└── app.css

frontend/scripts/screenshots.mjs   # `just shots` harness
scripts/typegen-pagedata.py        # Pydantic → JSON Schema
```

## Page data flow

1. A route in `routes/pages.py` builds a Pydantic model from `queries.py`
2. Passes `page_data.model_dump()` (a dict) to `otel_viewer_base.html`
3. The template embeds it as `<script id="pageData">{{ page_data | tojson }}</script>`
   and the Vite entrypoint via `datasette_otel_viewer_vite_entry(entrypoint)`
4. The Svelte page calls `loadPageData<TracesListPageData>()`
5. Types come from `just types-pagedata` (models listed in `page_data.__exports__`)

## API type safety

1. `routes/api.py` declares `output=Model` and `body: Annotated[Model, Body()]`
2. `just types-routes` → `router.openapi_document_json()` → openapi-typescript → `frontend/api.d.ts`
3. The frontend calls `client.POST("/-/otel/api/traces/list", { body })` via `src/api.ts`

The list fetch is a POST with a body (not GET + query string) because the
router only types path params and bodies. Do **not** add
`from __future__ import annotations` to route modules: the router inspects
the real `Annotated[..., Body()]` objects at decoration time.

## Permissions

- `otel-view` (global action) gates the viewer pages, the JSON API and the
  raw `otel` tables. `public_viewer: true` opens pages + API only; the raw
  tables stay gated (`permissions.py` emits a database-scoped deny row).
- `check_viewer()` in `router.py` returns a plain-text 403, preserving the
  message the tests assert on.
- There is no write path over HTTP: the store is only written by this
  instance's own span and metric readers.

## Key conventions

- Svelte 5 runes only (`$state`, `$derived`, `$props`); no `export let`.
- Generated files are gitignored: `api.d.ts`, `*_schema.json`, `*.types.ts`,
  `static/gen/`, `manifest.json`. Run `just types` after touching Pydantic
  models or route signatures; `just frontend` before `just dev`.
- Tests configure `plugins.datasette-vite.dev_paths` so page routes render
  without a build; `test_built_manifest_serves_hashed_assets` is the one test
  that needs `just frontend` first (CI does it).
- `scripts/shots_plugins/seed.py` is loaded only by `just shots`
  (`--plugins-dir`); it builds row dicts against `store.COLUMNS` /
  `METRIC_POINT_COLUMNS` by hand — update it when those change.
- Store writes run inside `store.suppress()` so spans about storing spans are
  never recorded; see `selfsource.py` before touching the write path.
  `selfmetrics.py` drops metric points whose `db.namespace` is the otel
  database for the same reason.
- `metrics_math.py` and `frontend/src/lib/metricsMath.ts` must stay in sync;
  both test files assert the same vectors.
- The metrics planning package (research, decision log, tickets) is in
  `plans/metrics/`, untracked.
