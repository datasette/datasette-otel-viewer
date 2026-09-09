"""Breadcrumbs: every viewer page hangs off ``/-/otel`` in Datasette's own
header, and the Svelte pages render the same trail next to their <h1>."""

import re

import pytest
from conftest import drain


def header_crumbs(html):
    "``[(href, label), ...]`` from the crumbs Datasette renders in its header."
    block = re.search(r'<p class="crumbs">(.*?)</p>', html, re.DOTALL)
    assert block, "no crumbs in the page header"
    return re.findall(r'<a href="([^"]+)">\s*([^<]+?)\s*</a>', block.group(1))


@pytest.mark.asyncio
async def test_every_section_sits_under_the_viewer_root(make_ds):
    ds = await make_ds(public_viewer=True)
    sections = {
        "/-/otel": [],
        "/-/otel/traces": [("/-/otel/traces", "Traces")],
        "/-/otel/http": [("/-/otel/http", "HTTP endpoints")],
        "/-/otel/sql": [("/-/otel/sql", "SQL queries")],
        "/-/otel/metrics": [("/-/otel/metrics", "Metrics")],
    }
    for path, tail in sections.items():
        response = await ds.client.get(path)
        assert response.status_code == 200, path
        assert header_crumbs(response.text) == [
            ("/", "home"),
            ("/-/otel", "OpenTelemetry"),
            *tail,
        ], path


@pytest.mark.asyncio
async def test_a_detail_page_keeps_its_section(make_ds):
    "A trace hangs off Traces, and names itself as the leaf."
    ds = await make_ds(public_viewer=True, self_traces=True)
    await ds.client.get("/-/versions.json")
    await drain()
    listed = await ds.client.post("/-/otel/api/traces/list", json={"root": "http"})
    trace = listed.json()["traces"][0]

    crumbs = header_crumbs(
        (await ds.client.get(f"/-/otel/traces/{trace['trace_id']}")).text
    )
    assert crumbs == [
        ("/", "home"),
        ("/-/otel", "OpenTelemetry"),
        ("/-/otel/traces", "Traces"),
        (f"/-/otel/traces/{trace['trace_id']}", trace["label"]),
    ]
