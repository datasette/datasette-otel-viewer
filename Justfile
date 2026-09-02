# datasette resolves from the asg017/otel-phase1-6-plugin-kit branch of
# simonw/datasette via [tool.uv.sources] in pyproject.toml - no released
# datasette emits these spans yet.

default:
    @just --list --unsorted

# Run the test suite.
test *options:
    uv run pytest {{ options }}

# Generate demo.db (200-row table) if missing
demo-db:
    @[ -e demo.db ] || sqlite3 demo.db "create table plants(id integer primary key, name text, height_cm real); with recursive n(i) as (select 1 union all select i + 1 from n where i < 200) insert into plants select i, 'plant ' || i, abs(random() % 300) from n;"

# Self mode: browse the instance, then open http://localhost:8003/-/traces
demo *options: demo-db
    uv run datasette demo.db --root \
        -s plugins.datasette-otel-receiver.public_viewer true \
        -p 8003 {{ options }}

# Two-instance story, terminal 1: the receiver (viewer public for the demo)
demo-receiver *options:
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
