"""Shared datasette-plugin-router instance plus the viewer gate.

Every page and JSON route in ``routes/`` registers on this one ``router``;
``__init__.register_routes`` hands ``router.routes()`` to Datasette, and
``just types-routes`` feeds ``router.openapi_document_json()`` to
openapi-typescript for the frontend's typed client.
"""

from __future__ import annotations

from functools import wraps

from datasette import Response
from datasette.database import QueryInterrupted
from datasette_plugin_router import Router

from .config import get_config
from .permissions import VIEW_ACTION_NAME

router = Router(title="datasette-otel-viewer", version="0.1.0")


async def viewer_allowed(datasette, actor) -> bool:
    """True when the viewer pages/API may be shown to ``actor``: either the
    operator opened the viewer with ``public_viewer: true`` or the actor
    holds ``datasette-otel-viewer``. The raw tables stay gated regardless (see
    permissions.py)."""
    if get_config(datasette).public_viewer:
        return True
    return await datasette.allowed(action=VIEW_ACTION_NAME, actor=actor)


FORBIDDEN_TEXT = (
    f"Forbidden: viewing traces requires the {VIEW_ACTION_NAME} permission "
    "(or public_viewer: true in this plugin's config)"
)


# The summary pages read every stored span, so their cost rises with the
# store. They fit inside Datasette's default sql_time_limit_ms at the default
# max_spans on the hardware this was measured on -- but "the store is big and
# this box is slow" is a real state, and left alone it surfaces as an
# unhandled QueryInterrupted: a 500 and a traceback in the log. Answer it with
# the two dials that actually fix it instead.
TOO_SLOW_TEXT = (
    "This view timed out reading the trace store.\n\n"
    "It summarises every stored span, so its cost rises with the store's "
    "size. Either narrow the view with a filter, or give it more room:\n\n"
    "  - raise Datasette's sql_time_limit_ms setting, or\n"
    "  - lower this plugin's max_spans / retention_hours so the store holds "
    "less.\n"
)


def check_viewer():
    """Route decorator: 403 (plain text) unless ``viewer_allowed``, and a
    plain-text 503 rather than a 500 when the store outgrows the SQL time
    limit. Both are text so the message survives whether the caller wanted
    the HTML page or the JSON API."""

    def decorator(func):
        @wraps(func)
        async def wrapper(datasette, request, **kwargs):
            if not await viewer_allowed(datasette, request.actor):
                return Response.text(FORBIDDEN_TEXT, status=403)
            try:
                return await func(datasette=datasette, request=request, **kwargs)
            except QueryInterrupted:
                return Response.text(TOO_SLOW_TEXT, status=503)

        return wrapper

    return decorator
