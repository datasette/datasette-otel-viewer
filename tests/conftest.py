"""Fixtures: OTel set-once reset + a Datasette factory + drain helper.

Importing this conftest imports datasette_otel_viewer, which installs the
plugin's provider (owner mode) before any test imports datasette.app - so
every datasette span in the whole pytest run flows to that one provider,
same approach as the sibling plugins' test suites. Between tests we rewind
the mutable pieces (exporter arm state, module _state) rather than fighting
the once-only global.

Requires the editable datasette checkout on an otel branch (run via
`just test`).
"""

import asyncio
import json
import re
import sqlite3

import opentelemetry.metrics._internal as _metrics_internal
import pytest
import pytest_asyncio
from opentelemetry import trace

import datasette_otel_viewer  # noqa: F401  (installs the providers)
from datasette_otel_viewer import selfmetrics, selfsource, store

_SNAPSHOT = dict(selfsource._state)
_METRICS_SNAPSHOT = dict(selfmetrics._state)


def reset_tracer_state():
    "Unwind OpenTelemetry's set-once provider global (provider-game tests)."
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


def reset_meter_state():
    "Same, for the set-once meter provider global."
    _metrics_internal._METER_PROVIDER = None
    _metrics_internal._METER_PROVIDER_SET_ONCE._done = False


@pytest.fixture(autouse=True)
def reset_otel():
    # Point the world back at the plugin's own provider in case a previous
    # test replaced the global, and restore the module state snapshot.
    trace._TRACER_PROVIDER = _SNAPSHOT["provider"]
    trace._TRACER_PROVIDER_SET_ONCE._done = True
    selfsource._state.clear()
    selfsource._state.update(_SNAPSHOT)

    exporter = _SNAPSHOT["exporter"]
    # Drain anything a previous test left queued into a disabled exporter.
    exporter.disable()
    _SNAPSHOT["provider"].force_flush()
    exporter.futures.clear()
    with exporter._lock:
        exporter._pending = []
        exporter._disabled = False

    _metrics_internal._METER_PROVIDER = _METRICS_SNAPSHOT["provider"]
    _metrics_internal._METER_PROVIDER_SET_ONCE._done = True
    # Datasette's instruments are proxies bound at the first set_meter_provider;
    # a provider-game test rebinds them to its own provider, so point them back
    # (the SDK caches instruments by name, so re-binding is idempotent).
    _metrics_internal._PROXY_METER_PROVIDER.on_set_meter_provider(
        _METRICS_SNAPSHOT["provider"]
    )
    selfmetrics._state.clear()
    selfmetrics._state.update(_METRICS_SNAPSHOT)

    metrics_exporter = _METRICS_SNAPSHOT["exporter"]
    metrics_exporter.disable()
    metrics_exporter.futures.clear()
    with metrics_exporter._lock:
        metrics_exporter._pending = []
        metrics_exporter._disabled = False

    store._last_prune = 0.0
    yield
    exporter.disable()
    metrics_exporter.disable()


@pytest_asyncio.fixture
async def make_ds(tmp_path):
    "Factory: a started Datasette with the plugin configured into tmp_path."
    made = []

    async def _make(permissions=None, **plugin_config):
        "``permissions`` is Datasette's top-level ``permissions:`` config block."
        from datasette.app import Datasette

        plugin_config.setdefault("db_path", str(tmp_path / "otel.db"))
        ds = Datasette(
            [],
            memory=True,
            config={
                "permissions": permissions or {},
                "plugins": {
                    "datasette-otel-viewer": plugin_config,
                    # Vite dev mode: page routes emit dev-server script tags
                    # instead of resolving the built manifest, so the suite
                    # runs without `just frontend`.
                    "datasette-vite": {
                        "dev_paths": {"datasette_otel_viewer": "http://localhost:5186/"}
                    },
                },
            },
        )
        await ds.invoke_startup()
        made.append(ds)
        return ds

    yield _make


async def drain(exporter=None):
    """Push finished spans through: flush the BatchSpanProcessor, then await
    every insert coroutine it scheduled onto this loop."""
    exporter = exporter or selfsource._state["exporter"]
    selfsource._state["provider"].force_flush()
    futures, exporter.futures = list(exporter.futures), []
    for future in futures:
        await asyncio.wrap_future(future)


async def drain_metrics():
    """Run one collection cycle - which is also what invokes the observable
    gauge callbacks - and await the inserts it scheduled onto this loop."""
    exporter = selfmetrics._state["exporter"]
    selfmetrics._state["reader"].collect()
    futures, exporter.futures = list(exporter.futures), []
    for future in futures:
        await asyncio.wrap_future(future)


def page_data(html):
    "The JSON blob the base template embeds for the Svelte page."
    match = re.search(
        r'<script type="application/json" id="pageData">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "no #pageData script in response"
    return json.loads(match.group(1))


def raw_span_rows(db_path):
    """Read the store through plain sqlite3 - NOT through Datasette, whose
    instrumented read path would mint new spans while we count."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "select trace_id, name, attributes from spans order by start_ns"
        ).fetchall()
    finally:
        conn.close()


def raw_metric_rows(db_path, name=None):
    "Plain-sqlite3 point rows, like raw_span_rows: reading must not measure."
    conn = sqlite3.connect(db_path)
    try:
        sql = "select metric_name, attributes, value_int, value_double, count "
        sql += "from metric_points"
        params = []
        if name is not None:
            sql += " where metric_name = ?"
            params.append(name)
        return conn.execute(sql + " order by id", params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
