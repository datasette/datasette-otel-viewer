"""datasette-otel-receiver: span store + /-/otel/traces viewer + OTLP ingest.

Three roles, one SQLite store (see PLAN.md):

- self (default on): store the spans this instance emits — requires owning
  the TracerProvider, installed at import time below (see selfsource.py for
  why both the ownership and the timing are load-bearing).
- receiver (opt-in): POST /v1/traces OTLP/HTTP ingest, bearer auth.
- viewer: /-/otel/traces list + waterfall (Svelte, built with Vite and served
  through datasette-vite), gated by the otel-view action.

Replaces datasette-otel-debugger.
"""

import asyncio

from datasette import hookimpl
from datasette_vite import vite_entry

from . import ingest, selfsource, store
from .permissions import (  # noqa: F401  (re-exported for pluggy's scan)
    permission_resources_sql,
    register_actions,
)
from .router import router

# Importing the route modules registers their handlers on the shared router.
from .routes import api, pages

_ = (api, pages)

# Provider install must happen at import: plugins load before
# invoke_startup(), and the datasette.startup span starts before any hook.
selfsource.install()


@hookimpl
def startup(datasette):
    async def inner():
        await store.ensure_db(datasette)
        selfsource.configure(datasette, asyncio.get_running_loop())

    return inner


@hookimpl
def register_routes():
    return router.routes() + [
        # Top-level /v1/* because stock OTLP exporters append these paths to
        # the base endpoint. They 503 until ingest is configured. Plain
        # tuples, not router routes: protobuf bodies, not JSON contracts.
        (r"^/v1/traces$", ingest.traces_view),
        (r"^/v1/metrics$", ingest.stub_view),
        (r"^/v1/logs$", ingest.stub_view),
    ]


@hookimpl
def extra_template_vars(datasette):
    return {
        "datasette_otel_receiver_vite_entry": vite_entry(
            datasette=datasette, plugin_package="datasette_otel_receiver"
        )
    }
