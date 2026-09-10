<script lang="ts">
  import { makeClient } from "../../api.ts";
  import Breadcrumbs from "../../components/Breadcrumbs.svelte";
  import SortHeader from "../../components/SortHeader.svelte";
  import { nextSort, type SortState } from "../../lib/sort.ts";
  import {
    formatAbsoluteTime,
    formatMs,
    formatRelativeTime,
  } from "../../lib/time.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type {
    SpanListQuery,
    SpanListRow,
    SpansListPageData,
  } from "../../page_data/SpansListPageData.types.ts";

  // The spans behind one row of /-/otel/spans (or of /-/otel/sql): the server
  // answers the URL's filters and embeds the result, and ordering and paging
  // happen in SQL, like the trace list.
  const initial = loadPageData<SpansListPageData>();
  const client = makeClient();

  const SIZE_PRESETS = [25, 50, 100, 250, 500];
  // page_data.DEFAULT_SPAN_SORT_DESC: you opened a row to see where the time
  // went, so the server resolves an unsorted query to slowest-first.
  const DEFAULT_SORT_DESC = "duration_ms";
  const NUMERIC_COLUMNS = new Set(["duration_ms", "start_ns"]);

  let spans = $state<SpanListRow[]>(initial.spans);
  let query = $state<SpanListQuery>({ ...initial.query });
  let total = $state<number>(initial.total);
  let nextCursor = $state<string | null>(initial.next ?? null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Ordering is the server's; this is which header shows an arrow.
  const sort = $derived<SortState>({
    key: query.sort ?? query.sort_desc ?? null,
    dir: query.sort ? "asc" : "desc",
  });
  const size = $derived(query.size ?? 100);
  const sizeOptions = $derived(
    [...new Set([...SIZE_PRESETS, size])].sort((a, b) => a - b),
  );
  const offset = $derived(Number(query.next ?? 0));
  const hasPrevious = $derived(offset > 0);
  const previousCursor = $derived(
    offset - size > 0 ? String(offset - size) : null,
  );

  /** The span the trace view sent us to, if it is on the page in front of
   * us. The server opens the list on the page holding it, so normally it is
   * -- but paging on from there, or dropping a filter, can leave it behind,
   * and a pin that quietly points at nothing is worse than one that says so. */
  const highlightShown = $derived(
    query.highlight != null &&
      spans.some((row) => row.span_id === query.highlight),
  );

  // Bring the pinned row into view whenever it lands on the page: it can be
  // row 80 of 100, and a highlight below the fold is not a highlight.
  $effect(() => {
    if (!highlightShown) return;
    const id = query.highlight;
    requestAnimationFrame(() => {
      document
        .getElementById(`span-row-${id}`)
        ?.scrollIntoView({ block: "center" });
    });
  });

  /** What this list is showing, as removable chips: the catalogue row that
   * was opened, plus any filter it was carrying at the time. */
  const filterChips = $derived(
    (
      [
        ["name_exact", query.name_exact && `Span ${query.name_exact}`],
        ["statement", query.statement && `Statement ${query.statement}`],
        ["scope", query.scope && `Scope ${query.scope}`],
        [
          "split_value",
          query.split_by &&
            query.split_value != null &&
            `${query.split_by} = ${query.split_value}`,
        ],
        ["name", query.name && `Name contains “${query.name}”`],
        ["kind", query.kind && `Kind ${query.kind}`],
        [
          "nesting",
          query.nesting && (query.nesting === "root" ? "Roots" : "Nested"),
        ],
        [
          "min_duration_ms",
          query.min_duration_ms != null &&
            `Slower than ${query.min_duration_ms} ms`,
        ],
        ["service", query.service && `Service ${query.service}`],
        [
          "highlight",
          query.highlight &&
            (highlightShown
              ? "Span you came from"
              : "Span you came from (not on this page)"),
        ],
      ] as [keyof SpanListQuery, string | false | null | undefined][]
    )
      .filter(([, label]) => Boolean(label))
      .map(([key, label]) => ({ key, label: label as string })),
  );

  function syncUrl() {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined || value === "") continue;
      if (key === "size") {
        if (value !== 100) params.set("_size", String(value));
      } else if (key === "sort") {
        params.set("_sort", String(value));
      } else if (key === "sort_desc") {
        // The default ordering is the server's; spelling it out is noise.
        if (value !== DEFAULT_SORT_DESC)
          params.set("_sort_desc", String(value));
      } else if (key === "next") {
        params.set("_next", String(value));
      } else {
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
      "/-/otel/api/spans/list",
      { body: query },
    );
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
    } else {
      spans = data.spans;
      total = data.total;
      nextCursor = data.next ?? null;
      query = { ...data.query };
      syncUrl();
    }
    loading = false;
  }

  /** Any change other than paging invalidates the cursor. */
  function update(changes: Partial<SpanListQuery>) {
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

  function spanUrl(row: SpanListRow): string {
    return `/-/otel/traces/${row.trace_id}#span-${row.span_id}`;
  }

  const fmt = new Intl.NumberFormat();
  const rangeLabel = $derived(
    total === 0
      ? "No spans"
      : `Spans ${offset + 1}–${offset + spans.length} of ${fmt.format(total)}`,
  );
  const heading = $derived(
    query.name_exact ?? query.statement ?? "Matching spans",
  );
</script>

<main class="span-list">
  <Breadcrumbs
    trail={[{ label: "Spans", href: "/-/otel/spans" }, { label: heading }]}
  />
  <h1><code>{heading}</code></h1>

  <p class="lede">
    Every span behind that row, slowest first. Open one to land on it in its own
    trace; the crumb above goes back to the catalogue.
  </p>

  {#if filterChips.length}
    <div class="chips">
      {#each filterChips as chip (chip.key)}
        <span class="chip">
          {chip.label}
          <button
            type="button"
            title="Remove this filter"
            onclick={() => update({ [chip.key]: null })}>&times;</button
          >
        </span>
      {/each}
    </div>
  {/if}

  <div class="controls">
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
    <p class="error">Failed to load spans: {error}</p>
  {/if}

  <table>
    <thead>
      <tr>
        <SortHeader key="name" label="Span" {sort} onsort={handleSort} />
        {#if query.split_by}
          <th class="split">{query.split_by}</th>
        {/if}
        <th>In trace</th>
        <SortHeader key="status" label="Status" {sort} onsort={handleSort} />
        <SortHeader
          key="duration_ms"
          label="Duration (ms)"
          numeric
          {sort}
          onsort={handleSort}
        />
        <SortHeader key="start_ns" label="Started" {sort} onsort={handleSort} />
      </tr>
    </thead>
    <tbody>
      {#each spans as row (row.span_id)}
        <tr
          id={`span-row-${row.span_id}`}
          class="row-link"
          class:highlighted={row.span_id === query.highlight}
          onclick={() => (window.location.href = spanUrl(row))}
        >
          <td class="span-name">
            <a href={spanUrl(row)} onclick={(e) => e.stopPropagation()}
              ><code>{row.name}</code></a
            >
            {#if row.kind && row.kind !== "INTERNAL"}
              <span class="tag">{row.kind}</span>
            {/if}
            {#if !row.parent_span_id}
              <span class="tag" title="Starts its own trace">root</span>
            {/if}
          </td>
          {#if query.split_by}
            <td class="mono">{row.split_value ?? "—"}</td>
          {/if}
          <td class="dim trace">
            <a
              href={`/-/otel/traces/${row.trace_id}`}
              onclick={(e) => e.stopPropagation()}>{row.trace_label ?? "—"}</a
            >
          </td>
          <td>
            {#if row.status === "ERROR"}
              <span class="status-error">ERROR</span>
            {:else}
              <span class="dim">{row.status ?? "—"}</span>
            {/if}
          </td>
          <td class="num mono">{formatMs(row.duration_ms ?? null)}</td>
          <td
            class="dim"
            title={row.start_ns == null
              ? undefined
              : formatAbsoluteTime(row.start_ns)}
          >
            {row.start_ns == null ? "—" : formatRelativeTime(row.start_ns)}
          </td>
        </tr>
      {:else}
        <tr>
          <td colspan="6" class="empty">No spans match these filters.</td>
        </tr>
      {/each}
    </tbody>
  </table>

  <div class="pager">
    <span class="dim">{rangeLabel}</span>
    <span>
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
</main>

<style>
  h1 {
    margin: 0 0 0.5rem;
    font-size: 1.4rem;
    word-break: break-word;
  }
  .lede {
    margin: 0 0 1rem;
    max-width: 70ch;
    font-size: 0.9rem;
    color: #555;
  }
  .chips {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
    margin: 0 0 1rem;
    font-size: 0.85rem;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    background: #eef2f6;
    border: 1px solid #d7dee6;
    border-radius: 1rem;
    padding: 0.15rem 0.3rem 0.15rem 0.6rem;
    max-width: 46rem;
  }
  .chip button {
    all: unset;
    cursor: pointer;
    padding: 0 0.35rem;
    color: #555;
    line-height: 1;
  }
  .chip button:hover {
    color: #b00020;
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
  th.split {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid #e2e2e2;
    font-weight: 600;
    white-space: nowrap;
  }
  td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid #e2e2e2;
    white-space: nowrap;
  }
  td.span-name,
  td.trace {
    white-space: normal;
    word-break: break-word;
  }
  td.num {
    text-align: right;
  }
  .tag {
    display: inline-block;
    font-size: 0.7rem;
    letter-spacing: 0.03em;
    background: #eef2f6;
    border: 1px solid #d7dee6;
    border-radius: 0.25rem;
    padding: 0 0.3rem;
    margin-left: 0.35rem;
    color: #555;
    vertical-align: 1px;
  }
  tbody tr.row-link {
    cursor: pointer;
  }
  tbody tr.row-link:hover {
    background: #f6f8fa;
  }
  tbody tr.highlighted > td {
    background: #fff8e1;
  }
  tbody tr.highlighted > td:first-child {
    box-shadow: inset 3px 0 0 #e0a800;
  }
  tbody tr.highlighted:hover > td {
    background: #fdf1cd;
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
</style>
