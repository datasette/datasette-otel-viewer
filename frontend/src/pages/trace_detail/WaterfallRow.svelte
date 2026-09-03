<script lang="ts">
  import type { SpanNode } from "../../lib/traceTree.ts";
  import { formatDurationNs } from "../../lib/time.ts";
  // Self-import: WaterfallRow renders itself recursively for nested
  // subtrees (replaces the deprecated <svelte:self>, per svelte-check's
  // svelte_self_deprecated guidance).
  import WaterfallRow from "./WaterfallRow.svelte";

  interface Props {
    node: SpanNode;
    depth: number;
    minStartNs: number;
    totalNs: number;
    collapsed: Set<string>;
    selectedSpanId: string | null;
    onToggle: (spanId: string) => void;
    onSelect: (spanId: string) => void;
  }

  let {
    node,
    depth,
    minStartNs,
    totalNs,
    collapsed,
    selectedSpanId,
    onToggle,
    onSelect,
  }: Props = $props();

  const span = $derived(node.span);
  const hasChildren = $derived(node.children.length > 0);
  const isCollapsed = $derived(collapsed.has(span.span_id));
  const isSelected = $derived(selectedSpanId === span.span_id);

  // Bar position/size as a percentage of [minStartNs, minStartNs + totalNs]
  // -- the whole trace's time range. Guard totalNs === 0
  // (a trace with a single zero-duration span) to avoid a divide-by-zero
  // producing NaN%.
  const leftPct = $derived(
    totalNs > 0 ? ((span.start_ns - minStartNs) / totalNs) * 100 : 0,
  );
  const widthPct = $derived(
    totalNs > 0
      ? Math.max(((span.end_ns - span.start_ns) / totalNs) * 100, 0.2)
      : 100,
  );
  const durationNs = $derived(span.end_ns - span.start_ns);

  /** Color by span family: the root of the tree, `db.query`, `db.write.*`,
   * else "other" -- a small fixed palette. ERROR
   * status overrides to red regardless of family. */
  function barClass(): string {
    if (span.status === "ERROR") return "bar bar-error";
    if (depth === 0) return "bar bar-root";
    if (span.name === "db.query") return "bar bar-db-query";
    if (span.name.startsWith("db.write.")) return "bar bar-db-write";
    return "bar bar-other";
  }
</script>

<div class="row" class:row-selected={isSelected} id={`span-${span.span_id}`}>
  <div
    class="row-main"
    role="button"
    tabindex="0"
    onclick={() => onSelect(span.span_id)}
    onkeydown={(e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(span.span_id);
      }
    }}
  >
    <span class="name-cell" style={`padding-left: ${depth * 1.25}rem`}>
      {#if hasChildren}
        <button
          type="button"
          class="toggle"
          aria-label={isCollapsed ? "Expand subtree" : "Collapse subtree"}
          onclick={(e) => {
            e.stopPropagation();
            onToggle(span.span_id);
          }}
        >
          {isCollapsed ? "▶" : "▼"}
        </button>
      {:else}
        <span class="toggle-spacer"></span>
      {/if}
      <span class="span-name" title={span.name}>{span.name}</span>
    </span>
    <span class="duration-label">{formatDurationNs(durationNs)}</span>
    <span class="bar-track">
      <span class={barClass()} style={`left: ${leftPct}%; width: ${widthPct}%`}
      ></span>
    </span>
  </div>
</div>

{#if hasChildren && !isCollapsed}
  {#each node.children as child (child.span.span_id)}
    <WaterfallRow
      node={child}
      depth={depth + 1}
      {minStartNs}
      {totalNs}
      {collapsed}
      {selectedSpanId}
      {onToggle}
      {onSelect}
    />
  {/each}
{/if}

<style>
  .row {
    border-bottom: 1px solid #eee;
  }
  .row-selected {
    background: #eef4ff;
  }
  .row-main {
    display: flex;
    align-items: center;
    width: 100%;
    padding: 0.35rem 0.6rem;
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }
  .row-main:hover {
    background: #f6f8fa;
  }
  .row-selected .row-main:hover {
    background: #e4edfe;
  }
  .name-cell {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    width: 32%;
    min-width: 180px;
    flex-shrink: 0;
    overflow: hidden;
  }
  .toggle {
    flex-shrink: 0;
    width: 1rem;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.65rem;
    color: #666;
    padding: 0;
  }
  .toggle-spacer {
    display: inline-block;
    width: 1rem;
    flex-shrink: 0;
  }
  .span-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bar-track {
    position: relative;
    flex: 1;
    height: 1.1rem;
    display: flex;
    align-items: center;
  }
  .bar {
    position: absolute;
    height: 0.7rem;
    border-radius: 2px;
    min-width: 2px;
  }
  .bar-root {
    background: #6b7280;
  }
  .bar-db-query {
    background: #2563eb;
  }
  .bar-db-write {
    background: #7c3aed;
  }
  .bar-other {
    background: #10b981;
  }
  .bar-error {
    background: #dc2626;
  }
  .duration-label {
    flex-shrink: 0;
    width: 4.5rem;
    margin-right: 0.6rem;
    font-size: 0.75rem;
    color: #555;
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
</style>
