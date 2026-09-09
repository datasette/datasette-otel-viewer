<script lang="ts">
  import { makeClient } from "../../api.ts";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, sortRows, type SortState } from "../../lib/sort.ts";
  import { formatMs, formatRelativeTime } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    SqlQueriesQuery,
    SqlQueryRow,
    SqlSummaryPageData,
  } from "../../page_data/SqlSummaryPageData.types.ts";

  // The server answers the URL's filters and embeds the result (see
  // routes/pages.py); every control change re-runs it through the typed
  // client, same shape as the HTTP endpoint page.
  const initial = loadPageData<SqlSummaryPageData>();
  const client = makeClient();

  const TYPING_DELAY_MS = 350;

  let queries = $state<SqlQueryRow[]>(initial.queries);
  let query = $state<SqlQueriesQuery>({ ...initial.query });
  let runCount = $state<number>(initial.run_count);
  let totalMs = $state<number | null>(initial.total_ms ?? null);
  let truncated = $state<boolean>(initial.truncated ?? false);
  let databases = $state<string[]>(initial.databases ?? []);
  let operations = $state<string[]>(initial.operations ?? []);
  let services = $state<string[]>(initial.services ?? []);
  let loading = $state(false);
  let error = $state<string | null>(null);

  let sqlText = $state<string>(initial.query.sql ?? "");
  let minMsText = $state<string>(
    initial.query.min_duration_ms == null
      ? ""
      : String(initial.query.min_duration_ms),
  );

  // The whole list arrives in one response (SQL_QUERY_LIMIT), so sorting the
  // loaded rows sorts everything -- see lib/sort.ts.
  const NUMERIC_COLUMNS = new Set([
    "run_count",
    "error_count",
    "total_ms",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "max_rows",
    "last_seen_ns",
  ]);
  let sort = $state<SortState>({ key: null, dir: "desc" });
  const sortedQueries = $derived(sortRows(queries, sort));

  function handleSort(key: string) {
    sort = nextSort(sort, key, NUMERIC_COLUMNS);
  }

  function syncUrl() {
    const params = new URLSearchParams();
    for (const [key, value] of [
      ["service", query.service],
      ["sql", query.sql],
      ["database", query.database],
      ["operation", query.operation],
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
      "/-/otel/api/sql/queries",
      { body: query },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      queries = data.queries;
      runCount = data.run_count;
      totalMs = data.total_ms ?? null;
      truncated = data.truncated ?? false;
      databases = data.databases ?? [];
      operations = data.operations ?? [];
      services = data.services ?? [];
      query = { ...data.query };
      syncUrl();
    }
    loading = false;
  }

  function update(changes: Partial<SqlQueriesQuery>) {
    query = { ...query, ...changes };
    load();
  }

  let typingTimer: ReturnType<typeof setTimeout> | undefined;
  function updateAfterTyping(changes: () => Partial<SqlQueriesQuery> | null) {
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      const changed = changes();
      if (changed) update(changed);
    }, TYPING_DELAY_MS);
  }

  function onSqlInput() {
    updateAfterTyping(() => ({ sql: sqlText.trim() || null }));
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

  /** Straight to the slowest run of this statement, in its own waterfall:
   * the span anchor opens the inspector on it (see TraceDetailPage). */
  function slowestUrl(row: SqlQueryRow): string | null {
    if (!row.slowest_trace_id || !row.slowest_span_id) return null;
    return `/-/otel/traces/${row.slowest_trace_id}#span-${row.slowest_span_id}`;
  }

  function open(row: SqlQueryRow) {
    const url = slowestUrl(row);
    if (url) window.location.href = url;
  }

  const fmt = new Intl.NumberFormat();
</script>

<main class="sql">
  <h1>
    SQL queries
    <a class="dim" href="/-/otel/traces">Traces &rarr;</a>
    <a class="dim" href="/-/otel/http">HTTP endpoints &rarr;</a>
    <a class="dim" href="/-/otel/metrics">Metrics &rarr;</a>
  </h1>

  <p class="lede">
    One row per statement, over every time this instance ran it, ordered by the
    total time it accounts for: a fast query run thousands of times outweighs a
    slow one run twice. Callback-style reads (<code>execute_fn</code>) carry no
    SQL text and are listed under the callback's name. Open a row to land on its
    slowest run in the waterfall.
  </p>

  <div class="controls">
    <label class="grow">
      SQL contains
      <input
        type="search"
        placeholder="from plants"
        bind:value={sqlText}
        oninput={onSqlInput}
      />
    </label>

    <label>
      Reads/writes
      <select
        value={query.access ?? ""}
        onchange={(e) => update({ access: e.currentTarget.value || null })}
      >
        <option value="">All</option>
        <option value="read">Reads</option>
        <option value="write">Writes</option>
      </select>
    </label>

    <label>
      Database
      <select
        value={query.database ?? ""}
        onchange={(e) => update({ database: e.currentTarget.value || null })}
      >
        <option value="">All</option>
        {#each databases as d (d)}
          <option value={d}>{d}</option>
        {/each}
      </select>
    </label>

    <label>
      Operation
      <select
        value={query.operation ?? ""}
        onchange={(e) => update({ operation: e.currentTarget.value || null })}
      >
        <option value="">Any</option>
        {#each operations as op (op)}
          <option value={op}>{op}</option>
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
    <p class="error">Failed to load queries: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="query" label="Statement" {sort} onsort={handleSort} />
        <SortHeader
          key="database"
          label="Database"
          {sort}
          onsort={handleSort}
        />
        <SortHeader
          key="run_count"
          label="Runs"
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
          key="max_rows"
          label="Rows"
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
      {#each sortedQueries as row (`${row.database} ${row.query}`)}
        <tr class="row-link" onclick={() => open(row)}>
          <td class="statement">
            {#if row.is_write}
              <span class="tag write" title="Ran through the write path"
                >write</span
              >
            {/if}
            {#if row.callback}
              <span class="tag" title="A callback, not a SQL string"
                >callback</span
              >
            {:else if row.operation}
              <span class="tag">{row.operation}</span>
            {/if}
            <code title={row.query}
              >{row.query}{row.text_truncated ? "…" : ""}</code
            >
          </td>
          <td>{row.database ?? "—"}</td>
          <td class="num">{fmt.format(row.run_count)}</td>
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
                title="Jump to this run in its trace"
                onclick={(e) => e.stopPropagation()}
                >{formatMs(row.max_ms ?? null)}</a
              >
            {:else}
              {formatMs(row.max_ms ?? null)}
            {/if}
          </td>
          <td class="num">{row.max_rows ?? "—"}</td>
          <td class="dim">
            {row.last_seen_ns == null
              ? "—"
              : formatRelativeTime(row.last_seen_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="10" class="empty">
            {runCount === 0 && !query.sql && !query.database
              ? "No SQL recorded yet — browse a few pages, then refresh."
              : "No statements match these filters."}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <p class="dim summary">
    {fmt.format(runCount)} run{runCount === 1 ? "" : "s"}
    {#if totalMs != null}
      &middot; {formatMs(totalMs)} ms of database time
    {/if}
    &middot; {fmt.format(queries.length)} statement{queries.length === 1
      ? ""
      : "s"}
    {#if truncated}
      (only the {fmt.format(queries.length)} costliest are listed)
    {/if}
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
    max-width: 72ch;
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
    flex: 1 1 18rem;
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
    width: 6rem;
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
  td.statement {
    white-space: normal;
    max-width: 44rem;
  }
  td.statement code {
    /* Two lines of SQL, the rest on hover: the store keeps the full text. */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: 0.85em;
    word-break: break-word;
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
  .tag.write {
    background: #fdf1e3;
    border-color: #e8d5b7;
    color: #8a5a1b;
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
