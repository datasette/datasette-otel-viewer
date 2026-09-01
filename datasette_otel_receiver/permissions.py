"""Register the ``otel-view`` Action and make the ``otel`` database private
by default.

Per ``plan.md`` "Read side: permissions": the spans database holds
``db.query.text`` (user SQL with embedded literals), URL paths, and user
agents, so it must not be world-readable just because it is attached to the
instance. This module wires two hooks (re-exported from ``__init__.py`` so
pluggy's plugin scan -- which only inspects the top-level package object
returned by the ``datasette`` entry point, not its submodules -- can find
them; see ``datasette-places``/``datasette-paper`` for the same split):

``register_actions()``
    Registers a single new-style ``otel-view`` Action. ``Action`` (imported
    from ``datasette.permissions``, as instructed) is a frozen, kw_only
    dataclass on the installed ``datasette==1.0a37``
    (``datasette/permissions.py:161``) with fields ``name``, ``description``,
    ``abbr=None``, ``resource_class=None``, ``also_requires=None``.
    ``otel-view`` is a plain *global* action (no ``resource_class``): a
    single grant covers the whole ``otel`` database, there's no need to
    grant view access table-by-table.

``permission_resources_sql(datasette, actor, action)``
    Intercedes only for the read actions that expose the spans database's
    contents. **Verified against ``datasette/default_actions.py`` on the
    installed 1.0a37** (not the unreleased branch referenced in the ticket,
    which has since diverged -- see the divergence notes below): the real
    action list is ``view-database``, ``view-database-download``,
    ``execute-sql`` (all ``resource_class=DatabaseResource``) and
    ``view-table`` (``resource_class=TableResource``). There is no
    ``view-row``/row-level action on this release -- row-level access to
    the ``spans`` table is already fully covered by ``view-table``.

    For each of those actions, if the actor already holds ``otel-view``
    (checked via ``await datasette.allowed(action="otel-view",
    actor=actor)`` -- see the divergence note below), this hook returns
    ``None`` (no opinion; normal rules apply). Otherwise it returns a single
    ``PermissionSQL`` row denying access, **scoped to the configured otel
    database** by setting ``parent = db_name`` -- never ``NULL``/global.
    This matters: per
    ``datasette/utils/permissions.py:resolve_permissions_from_catalog``,
    rules are resolved child-beats-parent-beats-global by *specificity of
    the rule itself* (``r.parent``/``r.child``), and only *then* does
    deny-beat-allow apply within the same specificity level. A global deny
    row (``parent=NULL``) would be a database-agnostic rule that wins
    against every database's default per-action allow (also global, depth
    0) via tie-breaking, silently locking out every other attached
    database. A row scoped to ``parent=db_name`` only outranks (at
    "parent" depth 1) the unscoped "default allow for this action" rule
    (depth 0, from ``datasette.default_permissions.defaults
    .default_action_permissions_sql``) for that one database; every other
    database is untouched.

Divergences from the ticket text (ticket 05 was written against
``~/projects/datasette`` branch ``asg017/otel-phase1-3-remove-tracer``; per
the ground truth for this implementation, the installed
``datasette==1.0a37`` from PyPI wins wherever the two differ -- ticket 04
already documented such a split):

- The permission-check method is ``datasette.allowed(action=..., actor=...,
  resource=...)`` (``datasette/app.py``), not
  ``datasette.permission_allowed(actor, action)`` as the ticket guessed.
  ``permission_allowed`` does not exist anywhere in the installed package.
- ``PermissionSQL`` (``datasette/permissions.py``) is a plain dataclass
  with ``sql``/``params``/``source``/``restriction_sql`` fields -- rows are
  arbitrary SQL selecting ``(parent, child, allow, reason)``, not a
  constructor taking a resource directly. Its ``PermissionSQL.deny(reason)``
  classmethod produces a *global* deny row (see above for why that's unsafe
  here), so this module hand-writes the ``sql``/``params`` instead of using
  that helper.
- ``--root``/root actors need **no special-casing** in this module. Core's
  ``default_permissions.root_user_permissions_sql`` grants the root actor a
  blanket allow for *any* action it's asked about -- it doesn't even
  inspect the ``action`` argument -- so root already satisfies our internal
  ``otel-view`` check above and this hook steps out of the way for root on
  its own. Tested in ``tests/test_permissions.py`` by setting
  ``ds.root_enabled = True`` directly (what the ``--root`` CLI flag does at
  runtime, per ``datasette/cli.py``) with an actor id of ``"root"``.
- ``/-/actions.json`` **does exist** on the installed package
  (``datasette/app.py``, route ``r"/-/actions(\\.(?P<format>json))?$"``) and
  does list registered actions (populated into ``datasette.actions`` at
  startup from the ``register_actions`` hook) -- but it is itself gated
  behind the ``permissions-debug`` action, which is *not* in
  ``DEFAULT_ALLOW_ACTIONS``. So it 403s for ordinary actors and only
  succeeds for root (or an actor explicitly granted
  ``permissions-debug``). Tests exercise both: a direct check of
  ``datasette.actions["otel-view"]`` (always available after startup) and
  an HTTP round-trip against ``/-/actions.json`` as root.

Operator grant story (also exercised in tests/test_permissions.py):

- Standard config grant, e.g. CLI ``-s permissions.otel-view '{"id":
  "admin"}'`` (which Datasette's ``pairs_to_nested_config`` turns into a
  top-level ``config["permissions"]["otel-view"] = {"id": "admin"}``,
  identical to a ``permissions: {otel-view: {id: admin}}`` block in
  datasette.yaml) -- handled entirely by core's
  ``default_permissions.config_permissions_sql``; no plugin code needed
  beyond registering the Action.
- ``--root``: also works with zero extra plugin code, as described above.
- Ingest (``POST /v1/traces`` et al, ticket 04) is untouched by design:
  this hook only intercedes for the four read actions listed above, never
  for the ingest routes, which use the separate static bearer-token check
  in ``ingest.py`` (``_check_auth``). A future ``otel-ingest`` Action
  mapping tokens to actors is explicitly deferred (see plan.md "Deferred /
  future").
"""

from __future__ import annotations

from datasette import hookimpl
from datasette.permissions import Action, PermissionSQL

from . import store

PLUGIN_NAME = "datasette-otel-receiver"

VIEW_ACTION_NAME = "otel-view"

# The read actions that expose the otel database's contents, verified
# against datasette/default_actions.py on the installed datasette==1.0a37
# (see module docstring for the divergence from the ticket's unreleased
# reference branch). Each is a real, already-registered core Action:
# view-database/view-database-download/execute-sql (resource_class=
# DatabaseResource) and view-table (resource_class=TableResource). No
# view-row/row-level action exists on this release.
#
# `view-query` (resource_class=QueryResource, parent=database, child=query
# name) was added here during ticket 11 (docs-justfile): canned queries on
# the otel database (see dev/datasette.yaml) are gated by `view-query`, not
# `view-database`/`view-table` -- confirmed by reading
# `datasette/views/database.py`'s `QueryView.get`, which checks only
# `action="view-query"` via `datasette.check_visibility`. `view-query` is
# also in `default_permissions.defaults.DEFAULT_ALLOW_ACTIONS`, so without
# this addition every canned query -- including ones surfacing
# `db_query_text` -- would be publicly readable by default regardless of
# the `otel-view` grant, contradicting plan.md's "private by default"
# requirement. The existing deny row (child=NULL, scoped to `parent =
# db_name`) already covers this correctly: resolution is by the *rule's*
# parent/child specificity, not the resource's own shape (this is the same
# row that already blanket-denies every table under view-table), so no
# other change is needed -- just adding the action name to this set.
READ_ACTIONS = frozenset(
    {
        "view-database",
        "view-table",
        "view-database-download",
        "execute-sql",
        "view-query",
    }
)

DENY_REASON = "otel spans are private by default"


def _plugin_config(datasette) -> dict:
    return datasette.plugin_config(PLUGIN_NAME) or {}


def _db_name(datasette) -> str:
    return store.db_name(datasette)


@hookimpl
def register_actions():
    return [
        Action(
            name=VIEW_ACTION_NAME,
            description="View captured OpenTelemetry spans",
        )
    ]


@hookimpl
async def permission_resources_sql(datasette, actor, action):
    if action not in READ_ACTIONS:
        # Not one of the read actions that exposes the otel database --
        # no opinion, and in particular do not touch the ingest routes
        # (ticket 04's static bearer-token auth is separate by design).
        return None

    if await datasette.allowed(action=VIEW_ACTION_NAME, actor=actor):
        # Actor already holds otel-view (via config grant, root, or any
        # other plugin) -- no opinion, let normal rules decide.
        return None

    # Deny, scoped to the configured otel database only (parent=db_name,
    # never NULL/global -- see module docstring for why a global deny row
    # would be unsafe here).
    return PermissionSQL(
        sql=(
            "SELECT :otel_db_name AS parent, NULL AS child, 0 AS allow, "
            ":otel_deny_reason AS reason"
        ),
        params={"otel_db_name": _db_name(datasette), "otel_deny_reason": DENY_REASON},
    )
