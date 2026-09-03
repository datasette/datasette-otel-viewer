"""Shared datasette-plugin-router instance plus the viewer gate.

Every page and JSON route in ``routes/`` registers on this one ``router``;
``__init__.register_routes`` hands ``router.routes()`` to Datasette, and
``just types-routes`` feeds ``router.openapi_document_json()`` to
openapi-typescript for the frontend's typed client.
"""

from __future__ import annotations

from functools import wraps

from datasette import Response
from datasette_plugin_router import Router

from . import store
from .permissions import VIEW_ACTION_NAME

router = Router(title="datasette-otel-viewer", version="0.1.0")


async def viewer_allowed(datasette, actor) -> bool:
    """True when the viewer pages/API may be shown to ``actor``: either the
    operator opened the viewer with ``public_viewer: true`` or the actor
    holds ``otel-view``. The raw tables stay gated regardless (see
    permissions.py)."""
    config = datasette.plugin_config(store.PLUGIN_NAME) or {}
    if config.get("public_viewer") is True:
        return True
    return await datasette.allowed(action=VIEW_ACTION_NAME, actor=actor)


FORBIDDEN_TEXT = (
    "Forbidden: viewing traces requires the otel-view permission "
    "(or public_viewer: true in this plugin's config)"
)


def check_viewer():
    "Route decorator: 403 (plain text) unless ``viewer_allowed``."

    def decorator(func):
        @wraps(func)
        async def wrapper(datasette, request, **kwargs):
            if not await viewer_allowed(datasette, request.actor):
                return Response.text(FORBIDDEN_TEXT, status=403)
            return await func(datasette=datasette, request=request, **kwargs)

        return wrapper

    return decorator
