<script lang="ts">
  import type { Row } from "../../lib/traceTree.ts";
  import { formatDurationNs } from "../../lib/time.ts";
  import { barClass } from "./barClass.ts";
  import Icon from "../../components/Icon.svelte";
  // Self-import: WaterfallRow renders itself recursively for nested
  // subtrees (replaces the deprecated <svelte:self>, per svelte-check's
  // svelte_self_deprecated guidance).
  import WaterfallRow from "./WaterfallRow.svelte";

  interface Props {
    /** A span, or a group of consecutive same-name siblings
     * (`traceTree.groupSiblings`) standing in for its members' rows. */
    row: Row;
    depth: number;
    minStartNs: number;
    totalNs: number;
    /** span_ids whose subtree is hidden. */
    collapsed: Set<string>;
    /** Group ids whose members are shown as rows of their own. */
    expandedGroups: Set<string>;
    selectedSpanId: string | null;
    /** parent span_id -> rows under it, precomputed for the whole trace
     * (`traceTree.buildRows`) so grouping is decided once, not per row. */
    rowsByParent: Map<string, Row[]>;
    onToggle: (spanId: string) => void;
    onToggleGroup: (groupId: string) => void;
    onSelect: (spanId: string) => void;
  }

  let {
    row,
    depth,
    minStartNs,
    totalNs,
    collapsed,
    expandedGroups,
    selectedSpanId,
    rowsByParent,
    onToggle,
    onToggleGroup,
    onSelect,
  }: Props = $props();

  const group = $derived(row.kind === "group" ? row : null);
  const node = $derived(row.kind === "span" ? row.node : null);
  const span = $derived(node ? node.span : group!.members[0]!.span);

  const childRows = $derived(
    node && node.children.length > 0
      ? (rowsByParent.get(node.span.span_id) ?? [])
      : [],
  );
  const hasChildren = $derived(childRows.length > 0);
  const isCollapsed = $derived(node !== null && collapsed.has(span.span_id));
  const isExpanded = $derived(group !== null && expandedGroups.has(group.id));
  const isSelected = $derived(node !== null && selectedSpanId === span.span_id);

  // Bar position/size as a percentage of [minStartNs, minStartNs + totalNs]
  // -- the whole trace's time range. A group's bar covers the union of
  // its members. Guard totalNs === 0 (a trace with a single zero-duration
  // span) to avoid a divide-by-zero producing NaN%.
  const startNs = $derived(group ? group.startNs : span.start_ns);
  const endNs = $derived(group ? group.endNs : span.end_ns);
  const leftPct = $derived(
    totalNs > 0 ? ((startNs - minStartNs) / totalNs) * 100 : 0,
  );
  const widthPct = $derived(
    totalNs > 0 ? Math.max(((endNs - startNs) / totalNs) * 100, 0.2) : 100,
  );
  /** A span's own duration; a group's summed work, which is what "how
   * much did these queries cost" means. */
  const durationNs = $derived(
    group ? group.sumDurationNs : span.end_ns - span.start_ns,
  );

  function activate() {
    if (group) {
      onToggleGroup(group.id);
    } else {
      onSelect(span.span_id);
    }
  }
</script>

<div
  class="row"
  class:row-selected={isSelected}
  class:row-group={group !== null}
  id={group ? group.id : `span-${span.span_id}`}
>
  <div
    class="row-main"
    role="button"
    tabindex="0"
    aria-expanded={group ? isExpanded : undefined}
    onclick={activate}
    onkeydown={(e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate();
      }
    }}
  >
    <span class="name-cell" style={`padding-left: ${depth * 1.25}rem`}>
      {#if group}
        <span class="toggle"
          ><Icon
            name={isExpanded ? "chevronDown" : "chevronRight"}
            size="0.7rem"
          /></span
        >
        <span class="span-name" title={group.name}>{group.name}</span>
        <span
          class="group-count"
          title={`${group.members.length} consecutive ${group.name} spans` +
            (group.spanCount > group.members.length
              ? ` (${group.spanCount} including their children)`
              : "")}>&times;{group.members.length}</span
        >
      {:else}
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
            <Icon
              name={isCollapsed ? "chevronRight" : "chevronDown"}
              size="0.7rem"
            />
          </button>
        {:else}
          <span class="toggle-spacer"></span>
        {/if}
        <span class="span-name" title={span.name}>{span.name}</span>
      {/if}
    </span>
    <span
      class="duration-label"
      title={group ? "Total across the grouped spans" : undefined}
      >{formatDurationNs(durationNs)}</span
    >
    <span class="bar-track">
      <span
        class={barClass(span, depth)}
        style={`left: ${leftPct}%; width: ${widthPct}%`}
      ></span>
    </span>
  </div>
</div>

{#if group && isExpanded}
  {#each group.members as member (member.span.span_id)}
    <WaterfallRow
      row={{ kind: "span", node: member }}
      {depth}
      {minStartNs}
      {totalNs}
      {collapsed}
      {expandedGroups}
      {selectedSpanId}
      {rowsByParent}
      {onToggle}
      {onToggleGroup}
      {onSelect}
    />
  {/each}
{:else if hasChildren && !isCollapsed}
  {#each childRows as child (child.kind === "group" ? child.id : child.node.span.span_id)}
    <WaterfallRow
      row={child}
      depth={depth + 1}
      {minStartNs}
      {totalNs}
      {collapsed}
      {expandedGroups}
      {selectedSpanId}
      {rowsByParent}
      {onToggle}
      {onToggleGroup}
      {onSelect}
    />
  {/each}
{/if}

<style>
  .row {
    border-bottom: 1px solid #f0f0f0;
  }
  .row-selected {
    background: #eef4ff;
  }
  .row-main {
    display: flex;
    align-items: center;
    width: 100%;
    padding: 0.15rem 0.6rem;
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
  .row-group .row-main {
    background: #fafbfc;
  }
  .row-group .row-main:hover {
    background: #f1f4f7;
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
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1rem;
    background: none;
    border: none;
    cursor: pointer;
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
  .group-count {
    flex-shrink: 0;
    font-size: 0.72rem;
    line-height: 1;
    padding: 0.12rem 0.35rem;
    border-radius: 0.6rem;
    background: #e5e7eb;
    color: #374151;
    font-variant-numeric: tabular-nums;
  }
  .bar-track {
    position: relative;
    flex: 1;
    height: 1rem;
    display: flex;
    align-items: center;
  }
  .bar {
    position: absolute;
    height: 0.65rem;
    border-radius: 2px;
    min-width: 2px;
  }
  .row-group .bar {
    opacity: 0.55;
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
