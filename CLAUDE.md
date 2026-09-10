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
| `just dev` | Datasette on :8012 against `demo.db` with datasette-debug-gotham; only `clark` (and `--root`) holds `datasette-otel-viewer`; serves the built bundle |
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
├── __init__.py              # Hooks: startup, register_routes, extra_template_vars, menu_links
├── router.py                # Shared Router + check_viewer() decorator
├── page_data.py             # Pydantic models: page data + API request/response
├── queries.py               # Read-side SQL shared by pages and API
├── routes/pages.py          # GET /-/otel (index), /traces, /traces/{trace_id}, /http, /sql, /spans, /spans/list, /metrics, /metrics/{name} (HTML)
├── routes/api.py            # POST .../traces/list, http/endpoints, sql/queries, spans/groups, metrics/*; GET .../traces/{trace_id}
├── selfsource.py            # TracerProvider ownership + suppression sampler
├── selfmetrics.py           # MeterProvider ownership/attach + SDK metrics → rows
├── store.py                 # Schema, batch insert, retention
├── permissions.py           # datasette-otel-viewer action; raw tables private by default
├── templates/otel_viewer_base.html   # The single template (overrides Datasette's `crumbs` block)
├── static/gen/, manifest.json          # Built by Vite (gitignored)

frontend/src/
├── pages/index/             # /-/otel landing page (IndexPage.svelte): counts + links
├── pages/traces_list/       # List page (TracesListPage.svelte)
├── pages/trace_detail/      # Waterfall + inspector (TraceDetailPage, WaterfallRow)
├── pages/http_summary/      # /-/otel/http endpoints (HttpSummaryPage.svelte)
├── pages/sql_summary/       # /-/otel/sql statements (SqlSummaryPage.svelte)
├── pages/spans_summary/     # /-/otel/spans catalogue (SpansSummaryPage.svelte)
├── pages/spans_list/        # /-/otel/spans/list drill-through (SpansListPage.svelte)
├── pages/metrics_list/      # Metrics table
├── pages/metric_detail/     # SveltePlot charts (SeriesChart, HistogramHeatmap, PercentileChart)
├── lib/traceTree.ts         # Span forest assembly (unit-tested), time.ts, sort.ts
├── lib/metricsMath.ts       # TS twin of metrics_math.py (shared test vectors), metricsSeries.ts
├── components/SortHeader.svelte, Breadcrumbs.svelte
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

- `datasette-otel-viewer` (global action, same name as the plugin) gates the viewer pages, the JSON API and the
  raw `otel` tables. `public_viewer: true` opens pages + API only; the raw
  tables stay gated (`permissions.py` emits a database-scoped deny row).
- `check_viewer()` in `router.py` returns a plain-text 403, preserving the
  message the tests assert on.
- There is no write path over HTTP: the store is only written by this
  instance's own span and metric readers.

## Key conventions

- Breadcrumbs are built twice from one definition of the trail:
  `routes/pages.SECTIONS`/`_crumbs()` feeds Datasette's header (the
  `crumbs` block in `otel_viewer_base.html`, which calls core's
  `crumb_items()` for the "home" link), and `components/Breadcrumbs.svelte`
  renders the same trail above each page's `<h1>`. A new page needs both.
- Svelte 5 runes only (`$state`, `$derived`, `$props`); no `export let`.
- Generated files are gitignored: `api.d.ts`, `*_schema.json`, `*.types.ts`,
  `static/gen/`, `manifest.json`. Run `just types` after touching Pydantic
  models or route signatures; `just frontend` before `just dev`.
- `make_ds()` builds instances with `self_traces: false` unless a test passes
  `self_traces=True`. The BatchSpanProcessor flushes on its own timer, so an
  instance that records itself can drop spans into the store mid-test, which
  every row count then races -- that was a ~1-in-4 suite failure. Tests that
  want self-recorded spans opt in and `drain()`.
- Tests configure `plugins.datasette-vite.dev_paths` so page routes render
  without a build; `test_built_manifest_serves_hashed_assets` is the one test
  that needs `just frontend` first (CI does it).
- `scripts/shots_plugins/seed.py` is loaded only by `just shots`
  (`--plugins-dir`); it builds row dicts against `store.COLUMNS` /
  `METRIC_POINT_COLUMNS` by hand — update it when those change.
- `/-/otel/sql` decides "was this a write?" with a correlated `exists` over
  `spans(parent_span_id, name)` (`queries._IS_WRITE_CHILD`,
  `store.idx_spans_parent_name`). The index is not optional: as a joined
  subquery, or as this `exists` without the index, that test is a scan per
  span and the page takes over a minute at `max_spans`.
- Every route is wrapped by `check_viewer()`, which turns a `QueryInterrupted`
  into a plain-text 503. The summary pages scan the whole store, so a big
  store on a slow box can outrun `sql_time_limit_ms`; that has to be an
  answer, not a 500. `prepare_connection` gives the otel database a 32MB page
  cache for the same reason.
- Store writes run inside `store.suppress()` so spans about storing spans are
  never recorded; see `selfsource.py` before touching the write path.
  `selfmetrics.py` drops metric points whose `db.namespace` is the otel
  database for the same reason.
- The traces list sorts and pages in SQL, under Datasette's own querystring
  names (`?_sort`/`?_sort_desc`/`?_size`/`?_next` on the page, the same
  fields on `TracesQuery` for the API). The sortable-column allowlist is
  `page_data.TRACE_SORT_COLUMNS`; the SQL behind each name is
  `queries.TRACE_ORDER_BY`, and a module-level assert keeps the two in sync.
  `lib/sort.ts`'s `sortRows` is now the metrics catalogue's only (it fits in
  one response); the traces page uses `nextSort` for header state alone.
- `page_data.TraceFilters` is the filter surface both trace views share
  (`service`, `path`, `route`, `method`, `status`, `min_duration_ms`);
  `queries._where()` turns one into SQL for the trace list, the root facet
  and the HTTP endpoint summary alike, so `/-/otel/http` can drill through to
  `/-/otel/traces` by handing over its own querystring. All three
  summaries share one shape, and it is load-bearing at the default
  `max_spans`: a materialized `grouped` CTE aggregates first, then
  `_percentile_ctes()` joins one row per group onto it. `grouped` also hands
  `pct` the group size (`n_timed`), which is what lets `ranked` get away with
  a single window and a single sort, and lets `pct` discard every row but the
  two percentile boundaries and the last. Do not reintroduce a `count(*)
  over` window for the size, and do not join `pct` to the rows and aggregate
  afterwards -- each costs a full extra pass over every span. The slowest
  span's ids ride along on `pct` (`rn = n_timed`, ties broken by `span_id
  desc`) rather than a second window pass.
  `sql_queries` aggregates spans rather than traces (its own
  `SqlFilters`/`_sql_where`, since path/status/method mean nothing there) and
  keys rows on `coalesce(db_query_text, datasette.callback)` so callback work
  is counted, not dropped. `span_groups` is the general case of both: any
  span, keyed on name + `scope_name` (the plugin that emitted it), with
  `split_by` breaking a row down by an attribute -- that key reaches a JSON
  path, so it is bound as `?1` *and* pattern-checked in
  `page_data.SpanFilters`. A summary row is a *group*, so opening one drills
  into `span_list()` (the spans behind it, paged like the trace list), never
  into a single span -- the slowest-span jump stays on the Max cell.
- `SpanListQuery.highlight` is one span_id to pin: the trace inspector's "All
  spans like this" link into `span_list()`. With no cursor of its own,
  `span_list()` ranks that span with one window over the matching spans under
  the query's own ordering and writes the page offset back onto `query.next`,
  so the frontend, the URL and Previous all see an ordinary offset. An
  explicit `_next` wins, which is what stops paging away from the pinned page
  snapping back to it; SpansListPage marks the row and says so on the chip
  when the pin is not on the page in front of you.
- The traces list's `?root=` filter buckets traces by root span --
  `queries.root_kinds` derives the buckets from the store (HTTP roots
  collapsed, everything else by span name and instrumentation scope), so
  nothing here enumerates Datasette's or a plugin's span names. `_where()`
  builds the filter for both the list and the facet; keep the HTTP test
  (`url.path` on the root span) in step with `http_label`.
- `metrics_math.py` and `frontend/src/lib/metricsMath.ts` must stay in sync;
  both test files assert the same vectors.
- The metrics planning package (research, decision log, tickets) is in
  `plans/metrics/`, untracked.
