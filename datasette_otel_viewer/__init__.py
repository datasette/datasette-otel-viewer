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

import asyncio

from datasette import hookimpl
from datasette_vite import vite_entry

from . import selfmetrics, selfsource, store
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
        await store.ensure_db(datasette)
        loop = asyncio.get_running_loop()
        selfsource.configure(datasette, loop)
        selfmetrics.configure(datasette, loop)

    return inner


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
