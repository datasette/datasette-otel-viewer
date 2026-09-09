<script lang="ts">
  import { makeClient } from "../../api.ts";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, sortRows, type SortState } from "../../lib/sort.ts";
  import { formatMs, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    EndpointRow,
    HttpSummaryPageData,
    EndpointsQuery,
  } from "../../page_data/HttpSummaryPageData.types.ts";

  // The server answers the URL's filters and embeds the result (see
  // routes/pages.py), so a shared link renders filtered with no round trip.
  // Every control change re-runs the same query through the typed client.
  const initial = loadPageData<HttpSummaryPageData>();
  const client = makeClient();

  // Same as the server's: an exact code or a class. Checked here so a typo
  // shows a hint instead of firing a request that comes back 400.
  const STATUS_PATTERN = /^(?:[1-5][0-9][0-9]|[1-5]xx)$/;
  // Long enough that typing a path doesn't fire a request per keystroke.
  const TYPING_DELAY_MS = 350;

  let endpoints = $state<EndpointRow[]>(initial.endpoints);
  let query = $state<EndpointsQuery>({ ...initial.query });
  let requestCount = $state<number>(initial.request_count);
  let truncated = $state<boolean>(initial.truncated ?? false);
  let methods = $state<string[]>(initial.methods ?? []);
  let services = $state<string[]>(initial.services ?? []);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Text inputs are bound to their own state and only reach `query` once
  // typing settles, so the table doesn't thrash mid-word.
  let pathText = $state<string>(initial.query.path ?? "");
  let statusText = $state<string>(initial.query.status ?? "");
  let minMsText = $state<string>(
    initial.query.min_duration_ms == null
      ? ""
      : String(initial.query.min_duration_ms),
  );
  const statusInvalid = $derived(
    statusText !== "" && !STATUS_PATTERN.test(statusText),
  );

  // The whole catalogue arrives in one response (ENDPOINT_LIMIT), so
  // sorting the loaded rows sorts everything -- see lib/sort.ts.
  const NUMERIC_COLUMNS = new Set([
    "request_count",
    "error_count",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "last_seen_ns",
  ]);
  let sort = $state<SortState>({ key: null, dir: "desc" });
  const sortedEndpoints = $derived(sortRows(endpoints, sort));

  function handleSort(key: string) {
    sort = nextSort(sort, key, NUMERIC_COLUMNS);
  }

  function syncUrl() {
    const params = new URLSearchParams();
    for (const [key, value] of [
      ["service", query.service],
      ["path", query.path],
      ["route", query.route],
      ["method", query.method],
      ["status", query.status],
      ["min_duration_ms", query.min_duration_ms],
    ] as [string, string | number | null | undefined][]) {
      if (value !== null && value !== undefined && value !== "") {
        params.set(key, String(value));
      }
    }
    const search = params.toString();
    history.replaceState(null, "", search ? `?${search}` : location.pathname);
  }

  async function load() {
    loading = true;
    error = null;
    const { data, error: apiError } = await client.POST(
      "/-/otel/api/http/endpoints",
      { body: query },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      endpoints = data.endpoints;
      requestCount = data.request_count;
      truncated = data.truncated ?? false;
      methods = data.methods ?? [];
      services = data.services ?? [];
      query = { ...data.query };
      syncUrl();
    }
    loading = false;
  }

  function update(changes: Partial<EndpointsQuery>) {
    query = { ...query, ...changes };
    load();
  }

  let typingTimer: ReturnType<typeof setTimeout> | undefined;
  function updateAfterTyping(changes: () => Partial<EndpointsQuery> | null) {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      const changed = changes();
      if (changed) update(changed);
    }, TYPING_DELAY_MS);
  }

  function onPathInput() {
    updateAfterTyping(() => ({ path: pathText.trim() || null }));
  }

  function onStatusInput() {
    updateAfterTyping(() =>
      statusInvalid ? null : { status: statusText.trim() || null },
    );
  }

  function onMinMsInput() {
    updateAfterTyping(() => {
      const value = Number(minMsText);
      if (minMsText !== "" && (!Number.isFinite(value) || value < 0)) {
        return null;
      }
      return { min_duration_ms: minMsText === "" ? null : value };
    });
  }

  /** An endpoint row drills through to the traces behind it: its own
   * method/route plus whatever else is currently narrowing the summary, so
   * the trace list shows the same requests this row counted. */
  function tracesUrl(endpoint: EndpointRow): string {
    const params = new URLSearchParams({ root: "http" });
    if (endpoint.method) params.set("method", endpoint.method);
    params.set("route", endpoint.route ?? "none");
    if (query.service) params.set("service", query.service);
    if (query.path) params.set("path", query.path);
    if (query.status) params.set("status", query.status);
    if (query.min_duration_ms != null) {
      params.set("min_duration_ms", String(query.min_duration_ms));
    }
    return `/-/otel/traces?${params}`;
  }

  function errorLabel(endpoint: EndpointRow): string {
    if (endpoint.error_count === 0) return "0";
    const pct = (100 * endpoint.error_count) / endpoint.request_count;
    return `${endpoint.error_count} (${pct < 1 ? "<1" : pct.toFixed(0)}%)`;
  }

  const fmt = new Intl.NumberFormat();
</script>

<main class="http">
  <h1>
    HTTP endpoints
    <a class="dim" href="/-/otel/traces">Traces &rarr;</a>
    <a class="dim" href="/-/otel/metrics">Metrics &rarr;</a>
  </h1>

  <p class="lede">
    One row per endpoint — the method plus the route Datasette matched — over
    every HTTP trace in the store. Percentiles are exact, read off the stored
    durations rather than estimated from buckets. Open a row for the traces
    behind it.
  </p>

  <div class="controls">
    <label>
      Endpoint contains
      <input
        type="search"
        placeholder="/demo/"
        bind:value={pathText}
        oninput={onPathInput}
      />
    </label>

    <label>
      Status
      <input
        type="search"
        class:invalid={statusInvalid}
        placeholder="500 or 5xx"
        size="10"
        bind:value={statusText}
        oninput={onStatusInput}
      />
    </label>

    <label>
      Method
      <select
        value={query.method ?? ""}
        onchange={(e) => update({ method: e.currentTarget.value || null })}
      >
        <option value="">Any</option>
        {#each methods as m (m)}
          <option value={m}>{m}</option>
        {/each}
      </select>
    </label>

    <label>
      Slower than
      <span class="suffixed">
        <input
          type="number"
          min="0"
          step="1"
          size="6"
          bind:value={minMsText}
          oninput={onMinMsInput}
        /><span class="dim">ms</span>
      </span>
    </label>

    {#if services.length > 1}
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
    {/if}

    <button type="button" onclick={load} disabled={loading}>
      {loading ? "Refreshing…" : "Refresh"}
    </button>
  </div>

  {#if statusInvalid}
    <p class="hint">
      Status filter wants a code like <code>404</code> or a class like
      <code>4xx</code>.
    </p>
  {/if}
  {#if error}
    <p class="error">Failed to load endpoints: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="label" label="Endpoint" {sort} onsort={handleSort} />
        <SortHeader
          key="request_count"
          label="Requests"
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
          key="p50_ms"
          label="p50"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="p95_ms"
          label="p95"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="max_ms"
          label="Max"
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
      {#each sortedEndpoints as endpoint (endpoint.label)}
        <tr
          class="row-link"
          onclick={() => (window.location.href = tracesUrl(endpoint))}
        >
          <td class="endpoint">
            <a
              href={tracesUrl(endpoint)}
              title={endpoint.route ?? "matched no route"}
              onclick={(e) => e.stopPropagation()}>{endpoint.label}</a
            >
          </td>
          <td class="num">{fmt.format(endpoint.request_count)}</td>
          <td class="num">
            {#if endpoint.error_count > 0}
              <span class="status-error">{errorLabel(endpoint)}</span>
            {:else}
              0
            {/if}
          </td>
          <td class="num mono">{formatMs(endpoint.p50_ms ?? null)}</td>
          <td class="num mono">{formatMs(endpoint.p95_ms ?? null)}</td>
          <td class="num mono">{formatMs(endpoint.max_ms ?? null)}</td>
          <td class="dim">
            {endpoint.last_seen_ns == null
              ? "—"
              : formatRelativeTime(endpoint.last_seen_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="7" class="empty">
            {requestCount === 0 && !query.path && !query.status
              ? "No HTTP traces yet — make a request, then refresh."
              : "No endpoints match these filters."}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <p class="dim summary">
    {fmt.format(requestCount)} request{requestCount === 1 ? "" : "s"} across
    {fmt.format(endpoints.length)} endpoint{endpoints.length === 1 ? "" : "s"}
    {#if truncated}
      (only the {fmt.format(endpoints.length)} busiest are listed)
    {/if}
    · <span class="mono">ms</span>, percentiles nearest-rank
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
    max-width: 66ch;
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
  .controls input,
  .controls select,
  .controls button {
    font-size: 0.9rem;
    padding: 0.35rem 0.5rem;
  }
  .controls input.invalid {
    border-color: #b00020;
  }
  .suffixed {
    display: flex;
    align-items: baseline;
    gap: 0.3rem;
  }
  .suffixed input {
    width: 6rem;
  }
  .hint {
    font-size: 0.85rem;
    color: #b00020;
    margin: 0 0 0.5rem;
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
  td.endpoint {
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
  .summary {
    margin-top: 0.75rem;
    font-size: 0.85rem;
  }
</style>
