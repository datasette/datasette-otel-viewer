"""datasette-otel-viewer: span store + /-/otel/traces viewer.

Two roles, one SQLite store (see PLAN.md):

- self (default on): store the spans this instance emits — requires owning
  the TracerProvider, installed at import time below (see selfsource.py for
  why both the ownership and the timing are load-bearing). Metrics ride the
  same ownership rule in selfmetrics.py.
- viewer: /-/otel/traces list + waterfall (Svelte, built with Vite and served
  through datasette-vite), gated by the datasette-otel-viewer action.

Replaces datasette-otel-debugger.
"""

from datasette import hookimpl
from datasette.utils import StartupError
from datasette_vite import vite_entry

from . import selfmetrics, selfsource, store
from .config import get_config
from .permissions import (  # noqa: F401  (re-exported for pluggy's scan)
    permission_resources_sql,
    register_actions,
)
from .router import router, viewer_allowed

# Importing the route modules registers their handlers on the shared router.
from .routes import api, pages

_ = (api, pages)

# Provider install must happen at import: plugins load before
# invoke_startup(), and the datasette.startup span starts before any hook.
selfsource.install()
selfmetrics.install()


@hookimpl
def startup(datasette):
    async def inner():
        get_config(datasette)  # fail startup on a bad config block
        await store.ensure_db(datasette)
        selfsource.configure(datasette)
        selfmetrics.configure(datasette)

    return inner


@hookimpl
def prepare_connection(conn, database, datasette):
    """Give the otel database a page cache big enough to summarise itself.

    The summary pages (/-/otel/spans, /-/otel/sql) aggregate every stored
    span, and they reach the rows through an index, so the table access is
    random rather than sequential. Against SQLite's 2MB default cache and a
    spans table that is tens of MB -- most of it the attributes/resource JSON
    -- that means re-reading pages the same query already touched. 32MB holds
    the working set: measured ~1120ms -> ~870ms on /-/otel/spans at the
    100_000-span cap, on top of what the query shape saves.

    Negative means KiB rather than pages, so the ceiling is the same whatever
    the page size. Only this plugin's own database is touched -- the rest of
    the instance keeps Datasette's defaults.

    A bad config block is left for the startup hook to report: the CLI's
    check_databases() opens connections before it catches StartupError, so
    raising here would print a traceback instead of the message.
    """
    try:
        name = store.db_name(datasette)
    except StartupError:
        return
    if database == name:
        conn.execute("pragma cache_size = -32000")


@hookimpl
def register_routes():
    return router.routes()


@hookimpl
def menu_links(datasette, actor):
    """One entry in Datasette's own menu, pointing at the ``/-/otel`` landing
    page -- without it the viewer is reachable only by typing the URL.

    Gated on the same check the pages use, so an actor who would only get the
    403 is not shown the link at all. Async because that check is: pluggy
    takes the returned coroutine function via ``await_me_maybe``."""

    async def inner():
        if not await viewer_allowed(datasette, actor):
            return []
        return [
            {
                "href": datasette.urls.path("/-/otel"),
                "label": "OpenTelemetry",
            }
        ]

    return inner


@hookimpl
def extra_template_vars(datasette):
    return {
        "datasette_otel_viewer_vite_entry": vite_entry(
            datasette=datasette, plugin_package="datasette_otel_viewer"
        )
    }
