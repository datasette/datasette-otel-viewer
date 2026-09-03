<script lang="ts">
  import { makeClient } from "../../api.ts";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, sortRows, type SortState } from "../../lib/sort.ts";
  import { formatAbsoluteTime, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    MetricSummaryRow,
    MetricsListPageData,
  } from "../../page_data/MetricsListPageData.types.ts";

  // The server embeds the unfiltered catalogue (see routes/pages.py) so
  // this renders without a second request; the service filter re-fetches
  // through the typed API client, same pattern as TracesListPage.
  const initial = loadPageData<MetricsListPageData>();
  const client = makeClient();

  // ?service= deep link: fetch the filtered list on mount, same as
  // TracesListPage's initialService handling.
  const initialService =
    new URLSearchParams(window.location.search).get("service") ?? "";

  let metrics = $state<MetricSummaryRow[]>(initial.metrics);
  let service = $state<string>(initialService);
  let loading = $state(false);
  let error = $state<string | null>(null);

  const NUMERIC_COLUMNS = new Set(["point_count", "last_seen_ns"]);
  let sort = $state<SortState>({ key: null, dir: "asc" });
  const sortedMetrics = $derived(sortRows(metrics, sort));

  function handleSort(key: string) {
    sort = nextSort(sort, key, NUMERIC_COLUMNS);
  }

  async function refresh() {
    loading = true;
    error = null;
    const { data, error: apiError } = await client.POST(
      "/-/otel/api/metrics/list",
      {
        body: { service: service || null },
      },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      metrics = data.metrics;
    }
    loading = false;
  }

  if (initialService) {
    refresh();
  }

  /** A sum reports "counter (delta)"/"counter (cumulative)" when
   * monotonic, "sum (…)" otherwise; a histogram reports its temporality
   * too; anything else (e.g. "gauge") is just its type. */
  function typeLabel(m: MetricSummaryRow): string {
    if (m.type === "sum") {
      return `${m.monotonic ? "counter" : "sum"} (${m.temporality ?? "?"})`;
    }
    if (m.type === "histogram") {
      return `histogram (${m.temporality ?? "?"})`;
    }
    return m.type.replace("_", " ");
  }

  function detailUrl(name: string): string {
    return `/-/otel/metrics/${encodeURIComponent(name)}`;
  }

  function goToMetric(name: string) {
    window.location.href = detailUrl(name);
  }

  /** The metric_points table filtered to one metric: the raw rows behind
   * a metric, one click away from facets/CSV/SQL (gated by otel-view). */
  function rawPointsUrl(name: string): string {
    return `/${initial.database}/metric_points?metric_name=${encodeURIComponent(name)}`;
  }
</script>

<main class="metrics">
  <h1>Metrics <a class="dim" href="/-/otel/traces">Traces &rarr;</a></h1>

  <div class="controls">
    <label>
      Service
      <select bind:value={service} onchange={refresh}>
        <option value="">All services</option>
        {#each initial.services as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <button type="button" onclick={refresh} disabled={loading}>
      {loading ? "Refreshing…" : "Refresh"}
    </button>
  </div>

  {#if error}
    <p class="error">Failed to load metrics: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="name" label="Metric" {sort} onsort={handleSort} />
        <SortHeader key="type" label="Type" {sort} onsort={handleSort} />
        <SortHeader key="unit" label="Unit" {sort} onsort={handleSort} />
        <th>Services</th>
        <SortHeader
          key="point_count"
          label="Points"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="last_seen_ns"
          label="Last seen"
          {sort}
          onsort={handleSort}
        />
      </tr>
    </thead>
    <tbody>
      {#each sortedMetrics as m (m.name)}
        <tr class="row-link" onclick={() => goToMetric(m.name)}>
          <td class="name">
            <a
              href={detailUrl(m.name)}
              title={m.description ?? undefined}
              onclick={(e) => e.stopPropagation()}>{m.name}</a
            >
          </td>
          <td>{typeLabel(m)}</td>
          <td class="mono">{m.unit ?? "—"}</td>
          <td>{m.services?.join(", ") || "—"}</td>
          <td class="num">
            <a
              href={rawPointsUrl(m.name)}
              title="Raw metric_points rows in Datasette"
              onclick={(e) => e.stopPropagation()}>{m.point_count}</a
            >
          </td>
          <td
            class="dim"
            title={m.last_seen_ns == null
              ? undefined
              : formatAbsoluteTime(m.last_seen_ns)}
          >
            {m.last_seen_ns == null ? "—" : formatRelativeTime(m.last_seen_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="6" class="empty">
            No metrics yet — point an OTLP metrics exporter at /v1/metrics, then
            refresh.
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <p class="dim raw-links">
    Raw tables:
    <a href={`/${initial.database}/metrics`}>metrics</a>
    &middot;
    <a href={`/${initial.database}/metric_points`}>metric_points</a>
  </p>
</main>

<style>
  h1 {
    margin: 0 0 1rem;
  }
  h1 a {
    font-size: 0.8rem;
    font-weight: normal;
    margin-left: 1rem;
  }
  .controls {
    display: flex;
    align-items: flex-end;
    gap: 1rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }
  .controls label {
    display: flex;
    flex-direction: column;
    font-size: 0.8rem;
    color: #555;
    gap: 0.25rem;
  }
  .controls select,
  .controls button {
    font-size: 0.9rem;
    padding: 0.35rem 0.5rem;
  }
  .error {
    color: #b00020;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  th {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid #e2e2e2;
  }
  td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid #e2e2e2;
    white-space: nowrap;
  }
  td.name {
    white-space: normal;
    word-break: break-all;
  }
  td.num {
    text-align: right;
  }
  tbody tr.row-link {
    cursor: pointer;
  }
  tbody tr.row-link:hover {
    background: #f6f8fa;
  }
  td.empty {
    text-align: center;
    color: #666;
    padding: 2rem 0;
    white-space: normal;
  }
  .raw-links {
    margin-top: 1rem;
    font-size: 0.85rem;
  }
</style>
