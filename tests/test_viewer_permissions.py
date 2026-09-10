"""Viewer pages and JSON API: render with typed page data; private by
default; public_viewer opens only the viewer, never the raw tables."""

import time

import pytest
from conftest import drain, page_data

from datasette_otel_viewer import store
from datasette_otel_viewer.permissions import VIEW_ACTION_NAME

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
START_NS = time.time_ns() - 1_000_000_000
END_NS = START_NS + 5_000_000
ROOT_NAME = "datasette.startup"


def span_row(**overrides):
    "A store.COLUMNS row dict, the shape selfsource.span_to_row produces."
    row = {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "parent_span_id": None,
        "name": ROOT_NAME,
        "kind": "INTERNAL",
        "start_ns": START_NS,
        "end_ns": END_NS,
        "duration_ms": (END_NS - START_NS) / 1e6,
        "status": "UNSET",
        "service_name": "datasette",
        "attributes": "{}",
        "resource": '{"service.name": "datasette"}',
    }
    row.update(overrides)
    return row


async def seed(ds):
    await store.insert_spans(ds, [span_row()])


@pytest.mark.asyncio
async def test_viewer_list_and_waterfall(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    listing = await ds.client.get("/-/otel/traces")
    assert listing.status_code == 200
    # The page is a Vite entrypoint plus an embedded page-data blob.
    assert "src/pages/traces_list/index.ts" in listing.text
    data = page_data(listing.text)
    assert data["database"] == "otel"
    assert data["query"]["size"] == 100
    assert data["total"] == 1
    assert data["next"] is None
    assert "datasette" in data["services"]
    trace = next(t for t in data["traces"] if t["trace_id"] == TRACE_ID)
    assert trace["service_name"] == "datasette"
    assert trace["span_count"] >= 1

    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID}")
    assert waterfall.status_code == 200
    assert "src/pages/trace_detail/index.ts" in waterfall.text
    detail = page_data(waterfall.text)
    assert detail["trace_id"] == TRACE_ID
    assert ROOT_NAME in {s["name"] for s in detail["spans"]}
    # attributes/resource arrive JSON-decoded, ready for the inspector
    assert all(isinstance(s["attributes"], dict) for s in detail["spans"])


@pytest.mark.asyncio
async def test_json_api_matches_page_data(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    listed = await ds.client.post("/-/otel/api/traces/list", json={"size": 10})
    assert listed.status_code == 200
    rows = listed.json()["traces"]
    assert [t["trace_id"] for t in rows] == [
        t["trace_id"]
        for t in page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    ]

    detail = await ds.client.get(f"/-/otel/api/traces/{TRACE_ID}")
    assert detail.status_code == 200
    assert detail.json() == page_data(
        (await ds.client.get(f"/-/otel/traces/{TRACE_ID}")).text
    )


@pytest.mark.asyncio
async def test_json_api_filters_and_validates(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    hit = await ds.client.post("/-/otel/api/traces/list", json={"service": "datasette"})
    assert [t["trace_id"] for t in hit.json()["traces"]] == [TRACE_ID]
    miss = await ds.client.post("/-/otel/api/traces/list", json={"service": "nope"})
    assert miss.json()["traces"] == []
    assert miss.json()["total"] == 0
    # Pydantic validation: size is capped, the sort column is an allowlist,
    # malformed bodies are 400s.
    too_big = await ds.client.post("/-/otel/api/traces/list", json={"size": 10_000})
    assert too_big.status_code == 400
    assert "size" in too_big.json()["error"]
    bad_sort = await ds.client.post(
        "/-/otel/api/traces/list", json={"sort": "db_query_text"}
    )
    assert bad_sort.status_code == 400
    assert "cannot sort traces by db_query_text" in bad_sort.json()["error"]
    both = await ds.client.post(
        "/-/otel/api/traces/list", json={"sort": "start_ns", "sort_desc": "start_ns"}
    )
    assert both.status_code == 400
    missing = await ds.client.get("/-/otel/api/traces/" + "0" * 32)
    assert missing.status_code == 404


async def seed_many(ds, count, service="datasette"):
    """`count` single-span traces, each slower and newer than the last, so
    every sortable column has a distinct known order."""
    rows = []
    for i in range(count):
        start = START_NS + i * 1_000_000_000
        rows.append(
            span_row(
                trace_id=f"{i:032x}",
                span_id=f"{i:016x}",
                name=f"span-{i}",
                service_name=service,
                start_ns=start,
                end_ns=start + (i + 1) * 1_000_000,
                duration_ms=(i + 1) * 1.0,
            )
        )
    await store.insert_spans(ds, rows)


@pytest.mark.asyncio
async def test_sorting_happens_in_sql_over_the_whole_table(make_ds):
    """The point of server-side ordering: the slowest trace overall leads
    the duration sort even though it is not in the newest-first first page."""
    ds = await make_ds(public_viewer=True)
    await seed_many(ds, 12)

    async def listed(**body):
        response = await ds.client.post("/-/otel/api/traces/list", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    newest = await listed(size=5)
    assert [t["label"] for t in newest["traces"]] == [
        f"span-{i}" for i in (11, 10, 9, 8, 7)
    ]
    # Default ordering is start_ns desc, echoed back for the column headers.
    assert newest["query"]["sort_desc"] == "start_ns"
    assert newest["total"] == 12

    slowest = await listed(size=5, sort_desc="duration_ms")
    assert [t["duration_ms"] for t in slowest["traces"]] == [12.0, 11.0, 10.0, 9.0, 8.0]
    fastest = await listed(size=5, sort="duration_ms")
    assert [t["duration_ms"] for t in fastest["traces"]] == [1.0, 2.0, 3.0, 4.0, 5.0]
    by_label = await listed(size=3, sort="label")
    assert [t["label"] for t in by_label["traces"]] == ["span-0", "span-1", "span-10"]


@pytest.mark.asyncio
async def test_paging_walks_every_trace_once(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed_many(ds, 12)
    seen = []
    cursor = None
    for _ in range(10):  # guard against a cursor that never terminates
        page = (
            await ds.client.post(
                "/-/otel/api/traces/list",
                json={"size": 5, "sort": "duration_ms", "next": cursor},
            )
        ).json()
        assert page["total"] == 12
        seen.extend(t["trace_id"] for t in page["traces"])
        cursor = page["next"]
        if cursor is None:
            break
    assert cursor is None
    assert len(seen) == len(set(seen)) == 12
    # A cursor is a page-boundary offset, not an arbitrary string.
    bad = await ds.client.post("/-/otel/api/traces/list", json={"next": "abc"})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_page_url_carries_the_query(make_ds):
    """`/-/otel/traces?_sort_desc=...` renders that page server-side: the
    embedded blob is the query's answer, so a shared URL loads sorted with
    no client round trip."""
    ds = await make_ds(public_viewer=True)
    await seed_many(ds, 12)
    data = page_data(
        (await ds.client.get("/-/otel/traces?_sort=duration_ms&_size=4")).text
    )
    assert [t["duration_ms"] for t in data["traces"]] == [1.0, 2.0, 3.0, 4.0]
    assert {k: data["query"][k] for k in ("size", "sort", "sort_desc", "next")} == {
        "size": 4,
        "sort": "duration_ms",
        "sort_desc": None,
        "next": None,
    }
    assert data["next"] == "4"
    assert data["total"] == 12

    second = page_data(
        (await ds.client.get("/-/otel/traces?_sort=duration_ms&_size=4&_next=4")).text
    )
    assert [t["duration_ms"] for t in second["traces"]] == [5.0, 6.0, 7.0, 8.0]

    filtered = page_data((await ds.client.get("/-/otel/traces?service=nope")).text)
    assert filtered["traces"] == []
    assert filtered["query"]["service"] == "nope"

    bad = await ds.client.get("/-/otel/traces?_sort=nope")
    assert bad.status_code == 400
    assert "cannot sort traces by nope" in bad.text


@pytest.mark.asyncio
async def test_root_kinds_bucket_traces_by_what_started_them(make_ds):
    """The root filter: HTTP traces in one bucket, every other root under its
    own span name, tagged with the scope that emitted it -- which is how a
    plugin's roots (datasette_cron.run and friends) stay separate from
    Datasette's own without this plugin knowing they exist."""
    ds = await make_ds(public_viewer=True, self_traces=True)
    await ds.client.get("/-/versions.json")
    await drain()

    async def listed(**body):
        response = await ds.client.post("/-/otel/api/traces/list", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    kinds = {k["key"]: k for k in (await listed())["root_kinds"]}
    # Real Datasette startup and request spans, no seeding: one HTTP bucket
    # (route names are per-endpoint, so they collapse) and datasette.startup
    # under Datasette's own instrumentation scope.
    assert kinds["http"]["label"] == "HTTP requests"
    assert kinds["http"]["scope"] is None
    assert kinds["datasette.startup"] == {
        "key": "datasette.startup",
        "label": "datasette.startup",
        "scope": "datasette",
        "count": 1,
    }

    http = await listed(root="http")
    assert http["total"] == kinds["http"]["count"]
    assert all(t["label"].startswith("GET /") for t in http["traces"])

    startup = await listed(root="datasette.startup")
    assert [t["label"] for t in startup["traces"]] == ["datasette.startup"]
    assert startup["total"] == 1
    # A facet counts the buckets you could switch to, so it ignores the root
    # filter currently applied (but not the service one).
    assert {k["key"] for k in startup["root_kinds"]} == set(kinds)
    assert {k["key"] for k in (await listed(service="nope"))["root_kinds"]} == set()

    # An unknown bucket is empty rather than an error: the keys are data, not
    # an allowlist, and the store is a ring buffer.
    assert (await listed(root="datasette_cron.run"))["traces"] == []


@pytest.mark.asyncio
async def test_root_filter_from_the_page_url(make_ds):
    ds = await make_ds(public_viewer=True, self_traces=True)
    await ds.client.get("/-/versions.json")
    await drain()
    data = page_data((await ds.client.get("/-/otel/traces?root=http")).text)
    assert data["query"]["root"] == "http"
    assert all(t["label"].startswith("GET /") for t in data["traces"])
    assert "datasette.startup" not in [t["label"] for t in data["traces"]]


@pytest.mark.asyncio
async def test_self_stored_trace_round_trips(make_ds):
    ds = await make_ds(public_viewer=True, self_traces=True)
    await ds.client.get("/")
    await drain()
    listing = await ds.client.get("/-/otel/traces")
    assert listing.status_code == 200
    labels = [t["label"] for t in page_data(listing.text)["traces"]]
    assert any(label.startswith("GET ") for label in labels)


@pytest.mark.asyncio
async def test_http_roots_show_path_not_route_pattern(make_ds):
    """Root spans are named after the low-cardinality route *pattern*
    (semconv); the UI shows the concrete "<method> <url.path>" instead,
    keeping the pattern as `name` (list tooltip) / `route` (waterfall)."""
    ds = await make_ds(public_viewer=True, self_traces=True)
    await ds.client.get("/-/versions.json")
    await drain()

    rows = page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    row = next(t for t in rows if t["label"] == "GET /-/versions.json")
    route_pattern = row["name"]
    assert route_pattern != "GET /-/versions.json"

    waterfall = await ds.client.get(f"/-/otel/traces/{row['trace_id']}")
    assert "<title>GET /-/versions.json</title>" in waterfall.text
    detail = page_data(waterfall.text)
    assert detail["title"] == "GET /-/versions.json"
    assert detail["route"] == route_pattern


@pytest.mark.asyncio
async def test_non_http_roots_fall_back_to_span_name(make_ds):
    """Non-HTTP roots (startup, background work) keep their span name as the
    label."""
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    rows = page_data((await ds.client.get("/-/otel/traces")).text)["traces"]
    row = next(t for t in rows if t["trace_id"] == TRACE_ID)
    assert row["label"] == ROOT_NAME
    waterfall = await ds.client.get(f"/-/otel/traces/{TRACE_ID}")
    assert f"<title>{ROOT_NAME}</title>" in waterfall.text
    assert page_data(waterfall.text)["route"] is None


@pytest.mark.asyncio
async def test_viewer_private_by_default(make_ds):
    ds = await make_ds()
    await seed(ds)
    assert (await ds.client.get("/-/otel/traces")).status_code == 403
    assert (await ds.client.get(f"/-/otel/traces/{TRACE_ID}")).status_code == 403
    assert (await ds.client.post("/-/otel/api/traces/list", json={})).status_code == 403
    assert (await ds.client.get(f"/-/otel/api/traces/{TRACE_ID}")).status_code == 403


@pytest.mark.asyncio
async def test_menu_link_follows_the_viewer_permission(make_ds):
    """The one entry this plugin adds to Datasette's own menu. It is gated
    like the pages: an actor who would only get the 403 is never shown it."""
    from datasette_otel_viewer import menu_links

    async def links(ds, actor=None):
        return await menu_links(datasette=ds, actor=actor)()

    private = await make_ds()
    assert await links(private) == []
    assert await links(private, {"id": "lois"}) == []

    public = await make_ds(public_viewer=True)
    assert await links(public) == [{"href": "/-/otel", "label": "OpenTelemetry"}]

    granted = await make_ds(permissions={"datasette-otel-viewer": {"id": "clark"}})
    assert await links(granted, {"id": "clark"})
    assert await links(granted, {"id": "lois"}) == []

    # ...and it reaches the rendered page, not just the hook.
    page = await public.client.get("/")
    assert '<a href="/-/otel">OpenTelemetry</a>' in page.text
    assert "OpenTelemetry" not in (await private.client.get("/")).text


@pytest.mark.asyncio
async def test_raw_tables_denied_anonymously_even_with_public_viewer(make_ds):
    ds = await make_ds(public_viewer=True)
    await seed(ds)
    response = await ds.client.get("/otel/spans.json")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_config_grant_by_actor_id(make_ds):
    """The `just dev` setup: `permissions.datasette-otel-viewer.id clark`
    lets clark (a datasette-debug-gotham actor) into the pages, the API and
    the raw tables; every other actor and anonymous get the 403."""
    assert VIEW_ACTION_NAME == "datasette-otel-viewer"
    ds = await make_ds(permissions={"datasette-otel-viewer": {"id": "clark"}})
    await seed(ds)
    clark = {"ds_actor": ds.client.actor_cookie({"id": "clark"})}
    lois = {"ds_actor": ds.client.actor_cookie({"id": "lois"})}
    for path in ("/-/otel", "/-/otel/traces", "/-/otel/metrics", "/otel/spans.json"):
        assert (await ds.client.get(path, cookies=clark)).status_code == 200, path
        assert (await ds.client.get(path, cookies=lois)).status_code == 403, path
        assert (await ds.client.get(path)).status_code == 403, path
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=clark)
    ).status_code == 200
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=lois)
    ).status_code == 403


@pytest.mark.asyncio
async def test_root_actor_sees_everything(make_ds):
    ds = await make_ds()
    ds.root_enabled = True
    await seed(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    assert (await ds.client.get("/-/otel/traces", cookies=cookies)).status_code == 200
    assert (
        await ds.client.post("/-/otel/api/traces/list", json={}, cookies=cookies)
    ).status_code == 200
    assert (await ds.client.get("/otel/spans.json", cookies=cookies)).status_code == 200


@pytest.mark.asyncio
async def test_metric_tables_denied_anonymously_even_with_public_viewer(make_ds):
    ds = await make_ds(public_viewer=True)
    await store.insert_metrics(
        ds,
        [{"name": "cpu", "type": "gauge"}],
        [
            {
                "metric_name": "cpu",
                "service_name": "datasette",
                "time_ns": START_NS,
                "attributes": "{}",
                "value_double": 0.5,
            }
        ],
    )
    assert (await ds.client.get("/otel/metric_points.json")).status_code == 403
    assert (await ds.client.get("/otel/metrics.json")).status_code == 403


@pytest.mark.asyncio
async def test_root_sees_metric_tables(make_ds):
    ds = await make_ds()
    ds.root_enabled = True
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    assert (
        await ds.client.get("/otel/metric_points.json", cookies=cookies)
    ).status_code == 200


@pytest.mark.asyncio
async def test_other_databases_unaffected(make_ds):
    "The permission deny row is scoped to the otel database only."
    ds = await make_ds()
    response = await ds.client.get("/_memory.json")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_ingest_routes(make_ds):
    """The OTLP receiver is gone: /v1/* is Datasette's 404, not a plugin
    response. A regression here would reopen the write-over-HTTP surface."""
    ds = await make_ds(public_viewer=True)
    for path in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        assert (await ds.client.get(path)).status_code == 404, path
        assert (await ds.client.post(path, content=b"")).status_code == 404, path


@pytest.mark.asyncio
async def test_built_manifest_serves_hashed_assets(make_ds, tmp_path):
    """Without a dev path configured, vite_entry resolves the built
    manifest (present after `just frontend`, which CI runs first)."""
    from datasette.app import Datasette

    ds = Datasette(
        [],
        memory=True,
        config={
            "plugins": {
                "datasette-otel-viewer": {
                    "public_viewer": True,
                    "db_path": str(tmp_path / "otel.db"),
                }
            }
        },
    )
    await ds.invoke_startup()
    response = await ds.client.get("/-/otel/traces")
    assert response.status_code == 200
    assert "/-/static-plugins/datasette_otel_viewer/gen/" in response.text
    assert page_data(response.text)["database"] == store.db_name(ds)


@pytest.mark.asyncio
async def test_trace_rows_carry_the_root_span_identity(make_ds):
    """A trace row's label is a *category* of work, so the list links it to
    the catalogue row for that category -- which is keyed on span name plus
    the scope that emitted it. Both have to reach the row for the link to
    mean the same thing /-/otel/spans does."""
    ds = await make_ds(public_viewer=True)
    await store.insert_spans(
        ds,
        [
            span_row(
                trace_id="c" * 32,
                span_id="c" * 16,
                name="datasette_cron.run",
                scope_name="datasette_cron",
            )
        ],
    )
    data = page_data((await ds.client.get("/-/otel/traces")).text)
    row = next(t for t in data["traces"] if t["trace_id"] == "c" * 32)
    assert (row["name"], row["scope"]) == ("datasette_cron.run", "datasette_cron")

    # The spans behind that label: the same pair, as the link sends them.
    listed = await ds.client.post(
        "/-/otel/api/spans/list",
        json={"name_exact": row["name"], "scope": row["scope"], "nesting": "root"},
    )
    assert [s["span_id"] for s in listed.json()["spans"]] == ["c" * 16]
