<script lang="ts">
  import { makeClient } from "../../api.ts";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, type SortState } from "../../lib/sort.ts";
  import { formatAbsoluteTime, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    TraceRow,
    TracesListPageData,
    TracesQuery,
  } from "../../page_data/TracesListPageData.types.ts";

  // The server answers the URL's query itself (routes/pages.py reads
  // ?_sort/?_sort_desc/?_size/?_next) and embeds the result, so a shared
  // link renders sorted and paged with no round trip. Every control change
  // from here re-runs that same query through the typed API client.
  const initial = loadPageData<TracesListPageData>();
  const client = makeClient();

  const SIZE_PRESETS = [25, 50, 100, 250, 500];
  // Descending first for these when a new column is clicked: slowest,
  // biggest, newest is what you want from a metrics column.
  const NUMERIC_COLUMNS = new Set([
    "start_ns",
    "http_status",
    "duration_ms",
    "span_count",
    "error_count",
  ]);

  let traces = $state<TraceRow[]>(initial.traces);
  let query = $state<TracesQuery>({ ...initial.query });
  let total = $state<number>(initial.total);
  let nextCursor = $state<string | null>(initial.next ?? null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Ordering is the server's (SQL over the whole table); this is just which
  // header shows an arrow. The server always echoes an explicit sort, so
  // the default (start_ns desc) lights up "Started" like any other.
  const sort = $derived<SortState>({
    key: query.sort ?? query.sort_desc ?? null,
    dir: query.sort ? "asc" : "desc",
  });

  const size = $derived(query.size ?? 100);
  // A hand-written ?_size= (or a link from elsewhere) is a legitimate page
  // size; offer it alongside the presets rather than showing a blank select.
  const sizeOptions = $derived(
    [...new Set([...SIZE_PRESETS, size])].sort((a, b) => a - b),
  );
  const offset = $derived(Number(query.next ?? 0));
  const hasPrevious = $derived(offset > 0);
  // Null rather than "0" for the first page, so paging back to it leaves the
  // same URL you started with instead of a redundant ?_next=0.
  const previousCursor = $derived(
    offset - size > 0 ? String(offset - size) : null,
  );

  // Service choices: the server's distinct list, plus anything only seen in
  // the loaded rows (a brand-new service arriving between requests).
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

  /** Datasette's own querystring vocabulary, so the list page URL reads
   * like one of its table pages -- and so it survives a reload or a share. */
  function syncUrl() {
    const params = new URLSearchParams();
    if (query.service) params.set("service", query.service);
    if (query.sort) params.set("_sort", query.sort);
    else if (query.sort_desc) params.set("_sort_desc", query.sort_desc);
    if (size !== 100) params.set("_size", String(size));
    if (query.next) params.set("_next", query.next);
    const search = params.toString();
    history.replaceState(null, "", search ? `?${search}` : location.pathname);
  }

  async function load() {
    loading = true;
    error = null;
    const { data, error: apiError } = await client.POST(
      "/-/otel/api/traces/list",
      { body: query },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      traces = data.traces;
      total = data.total;
      nextCursor = data.next ?? null;
      // The server normalises the query (an explicit default sort); take its
      // word for it so the headers and the URL agree with the rows.
      query = { ...data.query };
      syncUrl();
    }
    loading = false;
  }

  /** Any change other than paging invalidates the cursor: a different sort
   * or filter makes "row 100 onwards" meaningless. */
  function update(changes: Partial<TracesQuery>) {
    query = { ...query, next: null, ...changes };
    load();
  }

  function handleSort(key: string) {
    const next = nextSort(sort, key, NUMERIC_COLUMNS);
    update(
      next.dir === "asc"
        ? { sort: next.key, sort_desc: null }
        : { sort: null, sort_desc: next.key },
    );
  }

  function traceUrl(traceId: string): string {
    return `/-/otel/traces/${traceId}`;
  }

  function goToTrace(traceId: string) {
    window.location.href = traceUrl(traceId);
  }

  /** The spans table filtered to one trace: the raw rows behind a trace,
   * one click away from facets/CSV/SQL (gated by datasette-otel-viewer). */
  function rawSpansUrl(traceId: string): string {
    return `/${initial.database}/spans?trace_id=${traceId}`;
  }

  const rangeLabel = $derived(
    total === 0
      ? "No traces"
      : `Traces ${offset + 1}–${offset + traces.length} of ${total.toLocaleString()}`,
  );
</script>

<main class="traces">
  <h1>Traces <a class="dim" href="/-/otel/metrics">Metrics &rarr;</a></h1>

  <p class="lede">
    One row per trace, labelled by its <strong>root span</strong> — the request or
    task that started it. Spans, errors and duration cover the whole trace; open a
    row for the span waterfall.
  </p>

  <div class="controls">
    <label>
      Service
      <select
        value={query.service ?? ""}
        onchange={(e) => update({ service: e.currentTarget.value || null })}
      >
        <option value="">All services</option>
        {#each services as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label>
      Page size
      <select
        value={size}
        onchange={(e) => update({ size: Number(e.currentTarget.value) })}
      >
        {#each sizeOptions as n (n)}
          <option value={n}>{n}</option>
        {/each}
      </select>
    </label>

    <button type="button" onclick={load} disabled={loading}>
      {loading ? "Refreshing…" : "Refresh"}
    </button>
  </div>

  {#if error}
    <p class="error">Failed to load traces: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="label" label="Root span" {sort} onsort={handleSort} />
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
      {#each traces as trace (trace.trace_id)}
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
            {query.service
              ? `No traces from ${query.service} yet.`
              : "No traces yet — make a request, then refresh."}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <div class="pager">
    <span class="dim">{rangeLabel}</span>
    <span class="pages">
      <button
        type="button"
        disabled={loading || !hasPrevious}
        onclick={() => {
          query = { ...query, next: previousCursor };
          load();
        }}>&larr; Previous</button
      >
      <button
        type="button"
        disabled={loading || nextCursor === null}
        onclick={() => {
          query = { ...query, next: nextCursor };
          load();
        }}>Next &rarr;</button
      >
    </span>
  </div>

  <p class="dim raw-links">
    Raw tables:
    <a href={`/${initial.database}/traces`}>traces</a>
    &middot;
    <a href={`/${initial.database}/spans`}>spans</a>
  </p>
</main>

<style>
  h1 {
    margin: 0 0 0.5rem;
  }
  h1 a {
    font-size: 0.8rem;
    font-weight: normal;
    margin-left: 1rem;
  }
  .lede {
    margin: 0 0 1rem;
    max-width: 62ch;
    font-size: 0.9rem;
    color: #555;
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
  .pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 0.75rem;
    font-size: 0.85rem;
    flex-wrap: wrap;
  }
  .pager button {
    font-size: 0.85rem;
    padding: 0.3rem 0.6rem;
    margin-left: 0.4rem;
  }
  .pager button:disabled {
    color: #999;
    cursor: default;
  }
  .raw-links {
    margin-top: 1rem;
    font-size: 0.85rem;
  }
</style>
