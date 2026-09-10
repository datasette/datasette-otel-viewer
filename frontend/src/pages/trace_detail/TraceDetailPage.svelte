<script lang="ts">
  import { untrack } from "svelte";
  import Breadcrumbs from "../../components/Breadcrumbs.svelte";
  import {
    formatAbsoluteTimePrecise,
    formatDurationNs,
  } from "../../lib/time.ts";
  import {
    ancestorIds,
    buildRows,
    buildTraceTree,
    flattenTree,
    GROUP_MAX_FRACTION,
    GROUP_MIN_RUN,
    ROOT_KEY,
    selfTimeNs,
    traceBounds,
  } from "../../lib/traceTree.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type { TraceDetailPageData } from "../../page_data/TraceDetailPageData.types.ts";
  import WaterfallRow from "./WaterfallRow.svelte";

  // The server embeds the full trace (every span, attributes/resource
  // already JSON-decoded) as page data; see routes/pages.py. The same
  // payload is available as JSON at /-/otel/api/traces/{trace_id}.
  const pageData = loadPageData<TraceDetailPageData>();

  const spans = pageData.spans;
  const tree = buildTraceTree(spans);
  const bounds = traceBounds(spans);
  const nodesById = flattenTree(tree);
  const ancestorsById = ancestorIds(tree);

  const minStartNs = bounds?.minStartNs ?? 0;
  const totalNs = bounds ? bounds.maxEndNs - bounds.minStartNs : 0;

  /** Fold runs of consecutive same-name siblings into one row each (see
   * `traceTree.groupSiblings`): a dozen sub-millisecond `db.query` rows
   * between two five-second spans is the shape of every agent trace, and
   * the long bars are what you came for. Off shows every span. */
  let grouping = $state(true);
  const rowModel = $derived(
    buildRows(tree, {
      minRun: grouping ? GROUP_MIN_RUN : Infinity,
      maxDurationNs: totalNs * GROUP_MAX_FRACTION,
    }),
  );
  const rootRows = $derived(rowModel.rowsByParent.get(ROOT_KEY) ?? []);
  const groupIds = $derived(
    Array.from(rowModel.rowsByParent.values())
      .flat()
      .filter((row) => row.kind === "group")
      .map((row) => row.id),
  );
  const groupCount = $derived(groupIds.length);

  let collapsed = $state<Set<string>>(new Set());
  /** Groups opened to show their members. Groups start closed: that is
   * the whole point of them. */
  let expandedGroups = $state<Set<string>>(new Set());
  let selectedSpanId = $state<string | null>(null);

  /** The spans where the time went, for jumping straight to them without
   * scrolling. Ranked by self time (`traceTree.selfTimeNs`), not
   * duration: by duration the root and every wrapper around the slow
   * call would fill the strip, each as long as the trace. */
  const SLOWEST_COUNT = 5;
  const slowest = Array.from(nodesById.values())
    .map((node) => ({ span: node.span, selfNs: selfTimeNs(node) }))
    .filter((entry) => entry.selfNs > 0)
    .sort((a, b) => b.selfNs - a.selfNs)
    .slice(0, SLOWEST_COUNT);

  function toggleGroup(groupId: string) {
    const next = new Set(expandedGroups);
    if (next.has(groupId)) {
      next.delete(groupId);
    } else {
      next.add(groupId);
    }
    expandedGroups = next;
  }

  function collapseAll() {
    const next = new Set<string>();
    for (const [id, node] of nodesById) {
      if (node.children.length > 0) next.add(id);
    }
    collapsed = next;
    expandedGroups = new Set();
  }

  function expandAll() {
    collapsed = new Set();
    expandedGroups = new Set(groupIds);
  }

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

  /** Select a span and make sure its row is on screen: expand every
   * collapsed ancestor, and open any group folding it or an ancestor
   * away. State is read untracked, and only rewritten when something is
   * actually hidden: `selectFromHash` runs inside an $effect, and a plain
   * read + reassign made that effect invalidate itself every run
   * (effect_update_depth_exceeded, on any nested #span- deep link). */
  function reveal(spanId: string) {
    const ancestors = ancestorsById.get(spanId) ?? [];
    const hidden = untrack(() => ancestors.filter((id) => collapsed.has(id)));
    if (hidden.length > 0) {
      const next = new Set(untrack(() => collapsed));
      for (const id of hidden) next.delete(id);
      collapsed = next;
    }
    const folded = untrack(() =>
      [spanId, ...ancestors]
        .map((id) => rowModel.groupOfSpan.get(id))
        .filter((g): g is string => g !== undefined && !expandedGroups.has(g)),
    );
    if (folded.length > 0) {
      const next = new Set(untrack(() => expandedGroups));
      for (const id of folded) next.add(id);
      expandedGroups = next;
    }
    selectedSpanId = spanId;
    requestAnimationFrame(() => {
      document
        .getElementById(`span-${spanId}`)
        ?.scrollIntoView({ block: "center" });
    });
  }

  function jumpTo(spanId: string) {
    history.replaceState(null, "", `#span-${spanId}`);
    reveal(spanId);
  }

  function selectFromHash() {
    const match = /^#span-(.+)$/.exec(window.location.hash);
    if (!match) return;
    const spanId = match[1]!;
    if (!nodesById.has(spanId)) return;
    reveal(spanId);
  }

  // Runs once (selectFromHash tracks nothing), then on every hash change --
  // back/forward through deep links, or a hash typed by hand.
  $effect(() => {
    selectFromHash();
    window.addEventListener("hashchange", selectFromHash);
    return () => window.removeEventListener("hashchange", selectFromHash);
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

  /** The span catalogue, narrowed to this kind of work: same name, same
   * instrumentation scope (which is what /-/otel/spans keys a row on), with
   * this span pinned so the list opens on its page and marks its row. One
   * span in one trace tells you nothing about whether 40ms is normal; this
   * is the jump from "this span" to "every span like it". */
  function spanListUrl(span: TraceDetailPageData["spans"][number]): string {
    const params = new URLSearchParams({ name_exact: span.name });
    if (span.scope_name) params.set("scope", span.scope_name);
    params.set("highlight", span.span_id);
    return `/-/otel/spans/list?${params}`;
  }

  /** An attribute key, cut into the pieces it may wrap between. The keys are
   * dotted paths and the inspector column is narrow, so most of them wrap;
   * offering the breaks after the separators splits `gen_ai.usage.` rather
   * than mid-word, and keeps the key one copyable string. */
  function keySegments(key: string): string[] {
    return key.match(/[^._/-]*[._/-]*/g)?.filter(Boolean) ?? [key];
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
    <Breadcrumbs
      trail={[
        { label: "Traces", href: "/-/otel/traces" },
        { label: pageData.title },
      ]}
    />
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
    <div class="toolbar">
      {#if slowest.length > 1}
        <div
          class="slowest"
          title="Spans ranked by self time: their duration minus the time spent in their children"
        >
          <span class="dim">Slowest:</span>
          {#each slowest as { span: s, selfNs } (s.span_id)}
            <button
              type="button"
              class="chip"
              class:chip-selected={selectedSpanId === s.span_id}
              title={`${s.name}: ${formatDurationNs(selfNs)} self time of ${formatDurationNs(s.end_ns - s.start_ns)}`}
              onclick={() => jumpTo(s.span_id)}
            >
              <span class="chip-name">{s.name}</span>
              <span class="chip-duration">{formatDurationNs(selfNs)}</span>
            </button>
          {/each}
        </div>
      {/if}
      <div class="tree-controls">
        <label class="group-toggle">
          <input type="checkbox" bind:checked={grouping} />
          Group repeated spans{#if grouping && groupCount > 0}
            <span class="dim"
              >&nbsp;({groupCount} group{groupCount === 1 ? "" : "s"})</span
            >{/if}
        </label>
        <button type="button" class="tree-btn" onclick={collapseAll}
          >Collapse all</button
        >
        <button type="button" class="tree-btn" onclick={expandAll}
          >Expand all</button
        >
      </div>
    </div>
    <div class="body">
      <section class="waterfall">
        {#each rootRows as row (row.kind === "group" ? row.id : row.node.span.span_id)}
          <WaterfallRow
            {row}
            depth={0}
            {minStartNs}
            {totalNs}
            {collapsed}
            {expandedGroups}
            {selectedSpanId}
            rowsByParent={rowModel.rowsByParent}
            onToggle={toggle}
            onToggleGroup={toggleGroup}
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

          <div class="inspector-actions">
            <a
              class="action"
              href={spanListUrl(s)}
              title="Every span with this name and scope, this one highlighted"
              >All spans like this &rarr;</a
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
                    <th
                      >{#each keySegments(key) as seg, i}{#if i > 0}<wbr
                          />{/if}{seg}{/each}</th
                    >
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
                    <th
                      >{#each keySegments(key) as seg, i}{#if i > 0}<wbr
                          />{/if}{seg}{/each}</th
                    >
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
  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem 1.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.6rem;
    font-size: 0.82rem;
  }
  .slowest {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
    min-width: 0;
  }
  .chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35rem;
    max-width: 16rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid #d7dee6;
    border-radius: 1rem;
    background: #f6f8fa;
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .chip:hover {
    background: #eef2f6;
  }
  .chip-selected {
    background: #eef4ff;
    border-color: #a9c1f5;
  }
  .chip-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chip-duration {
    flex-shrink: 0;
    color: #555;
    font-variant-numeric: tabular-nums;
  }
  .tree-controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-shrink: 0;
  }
  .group-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
    white-space: nowrap;
  }
  .group-toggle input {
    margin: 0;
  }
  .tree-btn {
    font: inherit;
    font-size: 0.78rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid #d7dee6;
    border-radius: 0.25rem;
    background: #f6f8fa;
    cursor: pointer;
  }
  .tree-btn:hover {
    background: #eef2f6;
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
  .inspector-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin: 0.5rem 0 0.75rem;
  }
  .inspector-actions .action {
    display: inline-block;
    font-size: 0.8rem;
    padding: 0.2rem 0.5rem;
    border: 1px solid #d7dee6;
    border-radius: 0.25rem;
    background: #f6f8fa;
    text-decoration: none;
  }
  .inspector-actions .action:hover {
    background: #eef2f6;
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
    /* Datasette core's app.css sets `th { white-space: nowrap }` on every
       table. In this fixed-layout one that means a long key never wraps: it
       runs straight over its own value. */
    white-space: normal;
    overflow-wrap: anywhere;
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
