# datasette resolves from the asg017/otel-phase1-6-plugin-kit branch of
# simonw/datasette via [tool.uv.sources] in pyproject.toml - no released
# datasette emits these spans yet.

DEV_PORT := "5186"
DEV_HTTP_PORT := "8012"

default:
    @just --list --unsorted

# === Type generation ===

# TypeScript types for the JSON API, from the router's OpenAPI document
types-routes:
    uv run python -c 'from datasette_otel_receiver.router import router; import json; print(json.dumps(router.openapi_document_json()))' \
      | npx --prefix frontend openapi-typescript > frontend/api.d.ts

# TypeScript types for the embedded page data, from the Pydantic models
types-pagedata:
    uv run scripts/typegen-pagedata.py
    for f in frontend/src/page_data/*_schema.json; do \
      npx --prefix frontend json2ts "$f" > "${f%_schema.json}.types.ts"; \
    done

# Regenerate all TypeScript types from Python
types:
    just types-routes
    just types-pagedata

# Watch Python files and regenerate types on change
types-watch:
    watchexec -e py --clear -- just types

# === Frontend ===

# Build the frontend into datasette_otel_receiver/static/gen + manifest.json
frontend *flags:
    npm run build --prefix frontend {{flags}}

# Vite dev server (HMR) on port 5186
frontend-dev *flags:
    npm run dev --prefix frontend -- --port {{DEV_PORT}} {{flags}}

# === Formatting / checks ===

format-backend *flags:
    uv run ruff format {{flags}}

format-backend-check *flags:
    uv run ruff format --check {{flags}}

format-frontend *flags:
    npm run format --prefix frontend {{flags}}

format-frontend-check *flags:
    npm run format:check --prefix frontend {{flags}}

format:
    just format-backend
    just format-frontend

format-check:
    just format-backend-check
    just format-frontend-check

check-backend:
    uv run ruff check

check-frontend:
    npm run check --prefix frontend

check:
    just check-backend
    just check-frontend

# === Tests ===

# Python tests (page routes run in vite dev mode, so no frontend build needed)
test *options:
    uv run pytest {{ options }}

test-frontend *flags:
    npm test --prefix frontend {{flags}}

test-all:
    just test
    just test-frontend

# === Dev servers ===

# Generate demo.db (200-row table) if missing
demo-db:
    @[ -e demo.db ] || sqlite3 demo.db "create table plants(id integer primary key, name text, height_cm real); with recursive n(i) as (select 1 union all select i + 1 from n where i < 200) insert into plants select i, 'plant ' || i, abs(random() % 300) from n;"

# Self mode against demo.db (--root; viewer public). Serves the built bundle:
# run `just frontend` first, or use dev-with-hmr.
dev *options: demo-db
    DATASETTE_SECRET=abc123 uv run datasette demo.db --root \
        -s plugins.datasette-otel-receiver.public_viewer true \
        -p {{DEV_HTTP_PORT}} {{ options }}

# `dev` + Vite HMR: page routes load assets from `just frontend-dev` (other
# terminal) and watchexec restarts Datasette on .py/.html changes.
dev-with-hmr *options:
    watchexec \
        --stop-signal SIGKILL \
        -e py,html \
        --ignore '*.db' \
        --restart \
        --clear -- \
        just dev -s plugins.datasette-vite.dev_paths.datasette_otel_receiver "http://localhost:{{DEV_PORT}}/" {{ options }}

# Self mode demo: browse the instance, then open http://localhost:8003/-/traces
demo *options: demo-db frontend
    uv run datasette demo.db --root \
        -s plugins.datasette-otel-receiver.public_viewer true \
        -p 8003 {{ options }}

# Two-instance story, terminal 1: the receiver (viewer public for the demo)
demo-receiver *options: frontend
    uv run datasette --memory \
        -s plugins.datasette-otel-receiver.ingest_token demo-token \
        -s plugins.datasette-otel-receiver.public_viewer true \
        -s plugins.datasette-otel-receiver.self_traces false \
        -p 8003 {{ options }}

# Two-instance story, terminal 2: an observed Datasette exporting to it via
# the sibling datasette-otel-otlp plugin. This project's own receiver plugin
# is in the venv too, so its self-tracing is switched off here to keep the
# sender a pure observed instance.
demo-sender *options: demo-db
    uv run --with "datasette-otel-otlp @ git+https://github.com/datasette/datasette-otel-otlp" \
      datasette demo.db \
        -s plugins.datasette-otel-receiver.self_traces false \
        -s plugins.datasette-otel-otlp.endpoint http://localhost:8003 \
        -s plugins.datasette-otel-otlp.headers.authorization 'Bearer demo-token' \
        -s plugins.datasette-otel-otlp.service_name observed-datasette \
        -p 8004 {{ options }}

# Delete demo output
clean:
    rm -f otel.db demo.db

# === Screenshots ===

# Regenerate the committed doc screenshots in docs/screenshots/. Self-contained:
# boots a throwaway datasette, seeds traces over /v1/traces, drives Playwright,
# tears down. Builds the bundle first so shots reflect the current frontend.
# Not run in CI — re-run and confirm `git status` is clean. Subset: `just shots trace`.
shots *names:
    just frontend
    npm --prefix frontend exec -- playwright install chromium
    node frontend/scripts/screenshots.mjs {{names}}
