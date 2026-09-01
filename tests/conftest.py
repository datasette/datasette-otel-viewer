"""Fixtures: OTel set-once reset + a Datasette factory + drain helper.

Importing this conftest imports datasette_otel_receiver, which installs the
plugin's provider (owner mode) before any test imports datasette.app - so
every datasette span in the whole pytest run flows to that one provider,
same approach as the sibling plugins' test suites. Between tests we rewind
the mutable pieces (exporter arm state, module _state) rather than fighting
the once-only global.

Requires the editable datasette checkout on an otel branch (run via
`just test`).
"""

import asyncio
import sqlite3

import pytest
import pytest_asyncio
from opentelemetry import trace

import datasette_otel_receiver  # noqa: F401  (installs the provider)
from datasette_otel_receiver import selfsource, store

_SNAPSHOT = dict(selfsource._state)


def reset_tracer_state():
    "Unwind OpenTelemetry's set-once provider global (provider-game tests)."
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


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

    store._last_prune = 0.0
    yield
    exporter.disable()


@pytest_asyncio.fixture
async def make_ds(tmp_path):
    "Factory: a started Datasette with the plugin configured into tmp_path."
    made = []

    async def _make(**plugin_config):
        from datasette.app import Datasette

        plugin_config.setdefault("db_path", str(tmp_path / "otel.db"))
        ds = Datasette(
            [], memory=True,
            config={"plugins": {"datasette-otel-receiver": plugin_config}},
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
