<script lang="ts">
  import { makeClient } from "../../api.ts";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, sortRows, type SortState } from "../../lib/sort.ts";
  import { formatAbsoluteTime, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    TraceRow,
    TracesListPageData,
  } from "../../page_data/TracesListPageData.types.ts";

  // The server embeds the first page (see routes/pages.py) so this renders
  // without a second request; filter/limit changes re-fetch through the
  // typed API client.
  const initial = loadPageData<TracesListPageData>();
  const client = makeClient();

  const LIMIT_OPTIONS = [25, 50, 100, 250, 500];

  // ?service= deep link: the embedded first page is unfiltered, so when
  // the param is present the filtered list is fetched on mount.
  const initialService =
    new URLSearchParams(window.location.search).get("service") ?? "";

  let traces = $state<TraceRow[]>(initial.traces);
  let limit = $state<number>(initial.limit);
  let service = $state<string>(initialService);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Client-side column sort over the loaded rows; key: null keeps the
  // server's newest-first order until a header is clicked.
  const NUMERIC_COLUMNS = new Set([
    "start_ns",
    "http_status",
    "duration_ms",
    "span_count",
    "error_count",
  ]);
  let sort = $state<SortState>({ key: null, dir: "desc" });
  const sortedTraces = $derived(sortRows(traces, sort));

  function handleSort(key: string) {
    sort = nextSort(sort, key, NUMERIC_COLUMNS);
  }

  // Service choices: the server's distinct list, plus anything only seen
  // in the loaded rows (a brand-new service arriving between requests).
  const services = $derived(
    Array.from(
      new Set([
        ...initial.services,
        ...traces
          .map((t) => t.service_name)
          .filter((name): name is string => Boolean(name)),
      ]),
    ).sort(),
  );

  async function refresh() {
    loading = true;
    error = null;
    const { data, error: apiError } = await client.POST("/-/api/traces/list", {
      body: { limit, service: service || null },
    });
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      traces = data.traces;
    }
    loading = false;
  }

  if (initialService) {
    refresh();
  }

  function traceUrl(traceId: string): string {
    return `/-/traces/${traceId}`;
  }

  function goToTrace(traceId: string) {
    window.location.href = traceUrl(traceId);
  }

  /** The spans table filtered to one trace: the raw rows behind a trace,
   * one click away from facets/CSV/SQL (gated by otel-view). */
  function rawSpansUrl(traceId: string): string {
    return `/${initial.database}/spans?trace_id=${traceId}`;
  }
</script>

<main class="traces">
  <h1>Traces</h1>

  <div class="controls">
    <label>
      Service
      <select bind:value={service} onchange={refresh}>
        <option value="">All services</option>
        {#each services as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label>
      Limit
      <select bind:value={limit} onchange={refresh}>
        {#each LIMIT_OPTIONS as n (n)}
          <option value={n}>{n}</option>
        {/each}
      </select>
    </label>

    <button type="button" onclick={refresh} disabled={loading}>
      {loading ? "Refreshing…" : "Refresh"}
    </button>
  </div>

  {#if error}
    <p class="error">Failed to load traces: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="label" label="Root" {sort} onsort={handleSort} />
        <SortHeader
          key="service_name"
          label="Service"
          {sort}
          onsort={handleSort}
        />
        <SortHeader key="http_status" label="HTTP" {sort} onsort={handleSort} />
        <SortHeader
          key="span_count"
          label="Spans"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="error_count"
          label="Errors"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="duration_ms"
          label="Duration"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader key="start_ns" label="Started" {sort} onsort={handleSort} />
      </tr>
    </thead>
    <tbody>
      {#each sortedTraces as trace (trace.trace_id)}
        <tr class="row-link" onclick={() => goToTrace(trace.trace_id)}>
          <td class="root">
            <a
              href={traceUrl(trace.trace_id)}
              title={trace.name ?? undefined}
              onclick={(e) => e.stopPropagation()}>{trace.label}</a
            >
          </td>
          <td>{trace.service_name ?? "—"}</td>
          <td>{trace.http_status ?? "—"}</td>
          <td class="num">
            <a
              href={rawSpansUrl(trace.trace_id)}
              title="Raw span rows in Datasette"
              onclick={(e) => e.stopPropagation()}>{trace.span_count}</a
            >
          </td>
          <td class="num">
            {#if trace.error_count > 0}
              <span class="status-error">{trace.error_count}</span>
            {:else}
              0
            {/if}
          </td>
          <td class="num mono">
            {trace.duration_ms == null
              ? "—"
              : `${trace.duration_ms.toFixed(1)} ms`}
          </td>
          <td
            class="dim"
            title={trace.start_ns == null
              ? undefined
              : formatAbsoluteTime(trace.start_ns)}
          >
            {trace.start_ns == null ? "—" : formatRelativeTime(trace.start_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="7" class="empty">
            No traces yet - make a request (or send some), then refresh.
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <p class="dim raw-links">
    Raw tables:
    <a href={`/${initial.database}/traces`}>traces</a>
    &middot;
    <a href={`/${initial.database}/spans`}>spans</a>
  </p>
</main>

<style>
  h1 {
    margin: 0 0 1rem;
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
  td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid #e2e2e2;
    white-space: nowrap;
  }
  td.root {
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
