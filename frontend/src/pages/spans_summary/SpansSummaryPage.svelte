<script lang="ts">
  import { makeClient } from "../../api.ts";
  import Breadcrumbs from "../../components/Breadcrumbs.svelte";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, sortRows, type SortState } from "../../lib/sort.ts";
  import { formatMs, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    SpanGroupRow,
    SpansPageData,
    SpansQuery,
  } from "../../page_data/SpansPageData.types.ts";

  // The server answers the URL's filters and embeds the result (see
  // routes/pages.py); every control change re-runs it through the typed
  // client, same shape as the HTTP and SQL summaries.
  const initial = loadPageData<SpansPageData>();
  const client = makeClient();

  const TYPING_DELAY_MS = 350;

  let spans = $state<SpanGroupRow[]>(initial.spans);
  let query = $state<SpansQuery>({ ...initial.query });
  let spanCount = $state<number>(initial.span_count);
  let totalMs = $state<number | null>(initial.total_ms ?? null);
  let truncated = $state<boolean>(initial.truncated ?? false);
  let scopes = $state<string[]>(initial.scopes ?? []);
  let kinds = $state<string[]>(initial.kinds ?? []);
  let services = $state<string[]>(initial.services ?? []);
  let attributeKeys = $state<string[]>(initial.attribute_keys ?? []);
  let loading = $state(false);
  let error = $state<string | null>(null);

  let nameText = $state<string>(initial.query.name ?? "");
  let minMsText = $state<string>(
    initial.query.min_duration_ms == null
      ? ""
      : String(initial.query.min_duration_ms),
  );

  // The whole catalogue arrives in one response (SPAN_GROUP_LIMIT), so
  // sorting the loaded rows sorts everything -- see lib/sort.ts.
  const NUMERIC_COLUMNS = new Set([
    "span_count",
    "trace_count",
    "error_count",
    "total_ms",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "last_seen_ns",
  ]);
  let sort = $state<SortState>({ key: null, dir: "desc" });
  const sortedSpans = $derived(sortRows(spans, sort));

  function handleSort(key: string) {
    sort = nextSort(sort, key, NUMERIC_COLUMNS);
  }

  /** Every filter, straight off the query the server echoed back: this
   * page's model is filters and nothing else (SpansQuery), and the route
   * reads them under these same names. */
  function syncUrl() {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
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
      "/-/otel/api/spans/groups",
      { body: query },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      spans = data.spans;
      spanCount = data.span_count;
      totalMs = data.total_ms ?? null;
      truncated = data.truncated ?? false;
      scopes = data.scopes ?? [];
      kinds = data.kinds ?? [];
      services = data.services ?? [];
      attributeKeys = data.attribute_keys ?? [];
      query = { ...data.query };
      syncUrl();
    }
    loading = false;
  }

  function update(changes: Partial<SpansQuery>) {
    query = { ...query, ...changes };
    load();
  }

  let typingTimer: ReturnType<typeof setTimeout> | undefined;
  function updateAfterTyping(changes: () => Partial<SpansQuery> | null) {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      const changed = changes();
      if (changed) update(changed);
    }, TYPING_DELAY_MS);
  }

  function onNameInput() {
    updateAfterTyping(() => ({ name: nameText.trim() || null }));
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

  /** Straight to the slowest span of this kind, in its own waterfall. Kept
   * on the Max cell, where the number it belongs to is. */
  function slowestUrl(row: SpanGroupRow): string | null {
    if (!row.slowest_trace_id || !row.slowest_span_id) return null;
    return `/-/otel/traces/${row.slowest_trace_id}#span-${row.slowest_span_id}`;
  }

  /** Opening a row shows the spans it counted -- the row is a group, so the
   * drill-through is a list, carrying this row's identity plus whatever is
   * currently narrowing the catalogue. */
  function listUrl(row: SpanGroupRow): string {
    const params = new URLSearchParams({ name_exact: row.name });
    if (row.scope) params.set("scope", row.scope);
    if (query.split_by) {
      params.set("split_by", query.split_by);
      if (row.split_value !== null && row.split_value !== undefined) {
        params.set("split_value", row.split_value);
      }
    }
    if (query.kind) params.set("kind", query.kind);
    if (query.nesting) params.set("nesting", query.nesting);
    if (query.service) params.set("service", query.service);
    if (query.min_duration_ms != null) {
      params.set("min_duration_ms", String(query.min_duration_ms));
    }
    return `/-/otel/spans/list?${params}`;
  }

  function open(row: SpanGroupRow) {
    window.location.href = listUrl(row);
  }

  /** db.query spans have a page of their own, broken down by statement
   * rather than by span name. */
  function sqlUrl(row: SpanGroupRow): string | null {
    return row.name === "db.query" ? "/-/otel/sql" : null;
  }

  const fmt = new Intl.NumberFormat();
  const perTrace = (row: SpanGroupRow) =>
    row.trace_count === 0
      ? "—"
      : (row.span_count / row.trace_count).toFixed(
          row.span_count % row.trace_count === 0 ? 0 : 1,
        );
</script>

<main class="spans">
  <Breadcrumbs trail={[{ label: "Spans" }]} />
  <h1>
    Spans
    <a class="dim" href="/-/otel/traces">Traces &rarr;</a>
    <a class="dim" href="/-/otel/http">HTTP endpoints &rarr;</a>
    <a class="dim" href="/-/otel/sql">SQL &rarr;</a>
    <a class="dim" href="/-/otel/metrics">Metrics &rarr;</a>
  </h1>

  <p class="lede">
    Every kind of work this instance records, grouped by span name and by the
    instrumentation <strong>scope</strong> that emitted it — which is what keeps
    a plugin's spans (<code>datasette_cron.run</code>, scope
    <code>datasette_cron</code>) together and apart from Datasette's own. Most
    of these are nested inside a trace rather than starting one; use
    <em>Split by</em> to break a row down by one of its attributes, and open a row
    to land on its slowest span in the waterfall.
  </p>

  <div class="controls">
    <label class="grow">
      Name contains
      <input
        type="search"
        placeholder="db.query"
        bind:value={nameText}
        oninput={onNameInput}
      />
    </label>

    <label>
      Scope
      <select
        value={query.scope ?? ""}
        onchange={(e) => update({ scope: e.currentTarget.value || null })}
      >
        <option value="">All</option>
        {#each scopes as scope (scope)}
          <option value={scope}>{scope}</option>
        {/each}
      </select>
    </label>

    <label>
      Kind
      <select
        value={query.kind ?? ""}
        onchange={(e) => update({ kind: e.currentTarget.value || null })}
      >
        <option value="">Any</option>
        {#each kinds as kind (kind)}
          <option value={kind}>{kind}</option>
        {/each}
      </select>
    </label>

    <label>
      Nesting
      <select
        value={query.nesting ?? ""}
        onchange={(e) => update({ nesting: e.currentTarget.value || null })}
      >
        <option value="">All spans</option>
        <option value="nested">Nested only</option>
        <option value="root">Roots only</option>
      </select>
    </label>

    <label>
      Split by
      <select
        value={query.split_by ?? ""}
        onchange={(e) => update({ split_by: e.currentTarget.value || null })}
      >
        <option value="">Nothing</option>
        {#each attributeKeys as key (key)}
          <option value={key}>{key}</option>
        {/each}
      </select>
    </label>

    <label>
      Slower than
      <span class="suffixed">
        <input
          type="number"
          min="0"
          step="0.1"
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

  {#if error}
    <p class="error">Failed to load spans: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="name" label="Span" {sort} onsort={handleSort} />
        <SortHeader key="scope" label="Scope" {sort} onsort={handleSort} />
        {#if query.split_by}
          <SortHeader
            key="split_value"
            label={query.split_by}
            {sort}
            onsort={handleSort}
          />
        {/if}
        <SortHeader
          key="span_count"
          label="Spans"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="trace_count"
          label="Traces"
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
          key="total_ms"
          label="Total"
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
      {#each sortedSpans as row (`${row.scope} ${row.name} ${row.split_value}`)}
        <tr class="row-link" onclick={() => open(row)}>
          <td class="span-name">
            {#if row.kind && row.kind !== "INTERNAL"}
              <span class="tag">{row.kind}</span>
            {/if}
            <a href={listUrl(row)} onclick={(e) => e.stopPropagation()}
              ><code title={row.name}>{row.name}</code></a
            >
            {#if sqlUrl(row)}
              <a
                class="dim by-statement"
                href={sqlUrl(row)}
                onclick={(e) => e.stopPropagation()}>by statement &rarr;</a
              >
            {/if}
          </td>
          <td class="mono dim">{row.scope ?? "—"}</td>
          {#if query.split_by}
            <td class="mono">{row.split_value ?? "—"}</td>
          {/if}
          <td class="num">
            {fmt.format(row.span_count)}
            <span class="dim per-trace" title="Spans per trace"
              >&times;{perTrace(row)}</span
            >
          </td>
          <td class="num">{fmt.format(row.trace_count)}</td>
          <td class="num">
            {#if row.error_count > 0}
              <span class="status-error">{row.error_count}</span>
            {:else}
              0
            {/if}
          </td>
          <td class="num mono">{formatMs(row.total_ms ?? null)}</td>
          <td class="num mono">{formatMs(row.p50_ms ?? null)}</td>
          <td class="num mono">{formatMs(row.p95_ms ?? null)}</td>
          <td class="num mono">
            {#if slowestUrl(row)}
              <a
                href={slowestUrl(row)}
                title="Jump to this span in its trace"
                onclick={(e) => e.stopPropagation()}
                >{formatMs(row.max_ms ?? null)}</a
              >
            {:else}
              {formatMs(row.max_ms ?? null)}
            {/if}
          </td>
          <td class="dim">
            {row.last_seen_ns == null
              ? "—"
              : formatRelativeTime(row.last_seen_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="11" class="empty">
            {spanCount === 0 && !query.name && !query.scope
              ? "No spans recorded yet — make a request, then refresh."
              : "No spans match these filters."}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <p class="dim summary">
    {fmt.format(spanCount)} span{spanCount === 1 ? "" : "s"}
    {#if totalMs != null}
      &middot; {formatMs(totalMs)} ms of span time
    {/if}
    &middot; {fmt.format(spans.length)} row{spans.length === 1 ? "" : "s"}
    {#if truncated}
      (only the {fmt.format(spans.length)} costliest are listed)
    {/if}
    &middot; nested spans overlap their parents, so the total double-counts
  </p>

  <p class="dim raw-links">
    Raw table: <a href={`/${initial.database}/spans`}>spans</a>
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
    max-width: 78ch;
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
  .controls label.grow {
    flex: 1 1 14rem;
  }
  .controls input,
  .controls select,
  .controls button {
    font-size: 0.9rem;
    padding: 0.35rem 0.5rem;
  }
  .suffixed {
    display: flex;
    align-items: baseline;
    gap: 0.3rem;
  }
  .suffixed input {
    width: 5.5rem;
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
  td.span-name {
    white-space: normal;
    max-width: 32rem;
    word-break: break-word;
  }
  td.span-name code {
    font-size: 0.85em;
  }
  .by-statement {
    font-size: 0.75rem;
    margin-left: 0.4rem;
    white-space: nowrap;
  }
  .tag {
    display: inline-block;
    font-size: 0.7rem;
    letter-spacing: 0.03em;
    background: #eef2f6;
    border: 1px solid #d7dee6;
    border-radius: 0.25rem;
    padding: 0 0.3rem;
    margin-right: 0.35rem;
    color: #555;
    vertical-align: 1px;
  }
  .per-trace {
    font-size: 0.75rem;
    margin-left: 0.3rem;
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
  .raw-links {
    margin-top: 0.5rem;
    font-size: 0.85rem;
  }
</style>
