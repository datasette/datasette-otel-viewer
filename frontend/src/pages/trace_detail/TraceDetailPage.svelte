<script lang="ts">
  import {
    formatAbsoluteTimePrecise,
    formatDurationNs,
  } from "../../lib/time.ts";
  import {
    ancestorIds,
    buildTraceTree,
    flattenTree,
    traceBounds,
  } from "../../lib/traceTree.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type { TraceDetailPageData } from "../../page_data/TraceDetailPageData.types.ts";
  import WaterfallRow from "./WaterfallRow.svelte";

  // The server embeds the full trace (every span, attributes/resource
  // already JSON-decoded) as page data; see routes/pages.py. The same
  // payload is available as JSON at /-/api/traces/{trace_id}.
  const pageData = loadPageData<TraceDetailPageData>();

  const spans = pageData.spans;
  const tree = buildTraceTree(spans);
  const bounds = traceBounds(spans);
  const nodesById = flattenTree(tree);
  const ancestorsById = ancestorIds(tree);

  const minStartNs = bounds?.minStartNs ?? 0;
  const totalNs = bounds ? bounds.maxEndNs - bounds.minStartNs : 0;

  let collapsed = $state<Set<string>>(new Set());
  let selectedSpanId = $state<string | null>(null);

  const selectedNode = $derived(
    selectedSpanId ? (nodesById.get(selectedSpanId) ?? null) : null,
  );

  function toggle(spanId: string) {
    const next = new Set(collapsed);
    if (next.has(spanId)) {
      next.delete(spanId);
    } else {
      next.add(spanId);
    }
    collapsed = next;
  }

  function select(spanId: string) {
    selectedSpanId = spanId;
    history.replaceState(null, "", `#span-${spanId}`);
  }

  function closeInspector() {
    selectedSpanId = null;
  }

  function selectFromHash() {
    const match = /^#span-(.+)$/.exec(window.location.hash);
    if (!match) return;
    const spanId = match[1]!;
    if (!nodesById.has(spanId)) return;
    // Expand every collapsed ancestor so the deep-linked row is visible.
    const ancestors = ancestorsById.get(spanId) ?? [];
    if (ancestors.length > 0) {
      const next = new Set(collapsed);
      for (const id of ancestors) next.delete(id);
      collapsed = next;
    }
    selectedSpanId = spanId;
    requestAnimationFrame(() => {
      document
        .getElementById(`span-${spanId}`)
        ?.scrollIntoView({ block: "center" });
    });
  }

  $effect(() => {
    selectFromHash();
  });

  let copied = $state(false);
  async function copyTraceId() {
    try {
      await navigator.clipboard.writeText(pageData.trace_id);
      copied = true;
      setTimeout(() => {
        copied = false;
      }, 1500);
    } catch {
      // Clipboard can be unavailable (insecure context, permissions).
    }
  }

  function attributeEntries(obj: Record<string, unknown>): [string, string][] {
    return Object.entries(obj).map(([k, v]) => [
      k,
      typeof v === "string" ? v : JSON.stringify(v),
    ]);
  }
</script>

<main class="trace" class:with-inspector={selectedNode !== null}>
  <header class="trace-header">
    <a class="back-link" href="/-/traces">&larr; all traces</a>
    <div class="header-row">
      <h1>{pageData.title}</h1>
      <div class="trace-id-row">
        <span class="trace-id mono" title={pageData.trace_id}
          >{pageData.trace_id}</span
        >
        <button type="button" class="copy-btn" onclick={copyTraceId}>
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
    </div>
    <div class="stats">
      <span><strong>{formatDurationNs(totalNs)}</strong> total</span>
      <span
        ><strong>{spans.length}</strong> span{spans.length === 1
          ? ""
          : "s"}</span
      >
      {#if pageData.route}
        <span class="dim">route: <code>{pageData.route}</code></span>
      {/if}
      {#if pageData.service_name}
        <span class="dim">service: <code>{pageData.service_name}</code></span>
      {/if}
      <a
        href={`/${pageData.database}/spans?trace_id=${pageData.trace_id}`}
        title="Raw span rows in Datasette (facets, CSV, SQL)"
        >Raw spans &rarr;</a
      >
    </div>
    {#if pageData.truncated}
      <p class="dim">Showing the first {spans.length} spans.</p>
    {/if}
  </header>

  {#if spans.length === 0}
    <p class="empty">No spans found for this trace.</p>
  {:else}
    <div class="body">
      <section class="waterfall">
        {#each tree as node (node.span.span_id)}
          <WaterfallRow
            {node}
            depth={0}
            {minStartNs}
            {totalNs}
            {collapsed}
            {selectedSpanId}
            onToggle={toggle}
            onSelect={select}
          />
        {/each}
      </section>

      {#if selectedNode}
        {@const s = selectedNode.span}
        <aside class="inspector">
          <div class="inspector-head">
            <h2 title={s.name}>{s.name}</h2>
            <button type="button" class="close-btn" onclick={closeInspector}
              >&times;</button
            >
          </div>

          <dl class="kv">
            <dt>Kind</dt>
            <dd>{s.kind ?? "—"}</dd>
            <dt>Status</dt>
            <dd>
              <span class:status-error={s.status === "ERROR"}
                >{s.status ?? "UNSET"}</span
              >
              {#if s.status_description}<span class="dim">
                  — {s.status_description}</span
                >{/if}
            </dd>
            <dt>Start</dt>
            <dd>{formatAbsoluteTimePrecise(s.start_ns)}</dd>
            <dt>End</dt>
            <dd>{formatAbsoluteTimePrecise(s.end_ns)}</dd>
            <dt>Duration</dt>
            <dd>{formatDurationNs(s.end_ns - s.start_ns)}</dd>
            {#if s.service_name}
              <dt>Service</dt>
              <dd>{s.service_name}</dd>
            {/if}
            <dt>Raw row</dt>
            <dd>
              <a
                href={`/${pageData.database}/spans/${s.span_id}`}
                title="This span's row in Datasette"
                >{pageData.database}/spans/{s.span_id}</a
              >
            </dd>
          </dl>

          <h3>Attributes</h3>
          {#if attributeEntries(s.attributes ?? {}).length === 0}
            <p class="dim">None.</p>
          {:else}
            <table class="attrs">
              <tbody>
                {#each attributeEntries(s.attributes ?? {}) as [key, value] (key)}
                  <tr>
                    <th>{key}</th>
                    <td>
                      {#if key === "db.query.text"}
                        <pre class="query-text">{value}</pre>
                      {:else}
                        {value}
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}

          {#if s.db_query_text && !("db.query.text" in (s.attributes ?? {}))}
            <h3>Query text</h3>
            <pre class="query-text">{s.db_query_text}</pre>
          {/if}

          <h3>Resource</h3>
          {#if attributeEntries(s.resource ?? {}).length === 0}
            <p class="dim">None.</p>
          {:else}
            <table class="attrs">
              <tbody>
                {#each attributeEntries(s.resource ?? {}) as [key, value] (key)}
                  <tr>
                    <th>{key}</th>
                    <td>{value}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}

          <h3>Scope</h3>
          <dl class="kv">
            <dt>Name</dt>
            <dd>{s.scope_name ?? "—"}</dd>
            <dt>Version</dt>
            <dd>{s.scope_version ?? "—"}</dd>
            <dt>Schema URL</dt>
            <dd class="break-all">{s.schema_url ?? "—"}</dd>
          </dl>
        </aside>
      {/if}
    </div>
  {/if}
</main>

<style>
  .trace-header {
    margin-bottom: 1rem;
  }
  .back-link {
    color: #555;
    font-size: 0.85rem;
    text-decoration: none;
  }
  .back-link:hover {
    text-decoration: underline;
  }
  .header-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-top: 0.35rem;
  }
  h1 {
    margin: 0;
    font-size: 1.3rem;
    word-break: break-word;
  }
  .trace-id-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .trace-id {
    font-size: 0.85rem;
    color: #555;
    word-break: break-all;
  }
  .copy-btn {
    font-size: 0.75rem;
    padding: 0.15rem 0.45rem;
    cursor: pointer;
  }
  .stats {
    display: flex;
    gap: 1.25rem;
    margin-top: 0.4rem;
    font-size: 0.85rem;
    color: #444;
    flex-wrap: wrap;
  }
  .empty {
    color: #666;
    padding: 2rem 0;
    text-align: center;
  }
  .body {
    display: flex;
    gap: 1.25rem;
    align-items: flex-start;
  }
  .waterfall {
    flex: 1;
    min-width: 0;
    border: 1px solid #e2e2e2;
    border-radius: 4px;
    overflow: hidden;
  }
  .inspector {
    width: 380px;
    flex-shrink: 0;
    border: 1px solid #e2e2e2;
    border-radius: 4px;
    padding: 0.9rem 1rem;
    position: sticky;
    top: 1rem;
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
  }
  .inspector-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .inspector h2 {
    margin: 0;
    font-size: 1rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    word-break: break-word;
  }
  .inspector h3 {
    margin: 1rem 0 0.35rem;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: #666;
  }
  .close-btn {
    background: none;
    border: none;
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    color: #666;
    flex-shrink: 0;
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.75rem;
    margin: 0.5rem 0 0;
    font-size: 0.82rem;
  }
  .kv dt {
    color: #666;
  }
  .kv dd {
    margin: 0;
    word-break: break-word;
  }
  .break-all {
    word-break: break-all;
  }
  table.attrs {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
    table-layout: fixed;
  }
  table.attrs th {
    text-align: left;
    color: #666;
    font-weight: 600;
    vertical-align: top;
    padding: 0.2rem 0.4rem 0.2rem 0;
    width: 38%;
    word-break: break-word;
  }
  table.attrs td {
    padding: 0.2rem 0;
    word-break: break-word;
  }
  .query-text {
    white-space: pre-wrap;
    word-break: break-word;
    background: #f6f8fa;
    border: 1px solid #e2e2e2;
    border-radius: 3px;
    padding: 0.4rem 0.5rem;
    font-size: 0.78rem;
    margin: 0.2rem 0 0;
  }

  @media (max-width: 900px) {
    .body {
      flex-direction: column;
    }
    .inspector {
      width: 100%;
      position: static;
      max-height: none;
    }
  }
</style>
