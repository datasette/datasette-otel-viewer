/**
 * Pure span-tree assembly for the trace waterfall . No Svelte,
 * no DOM -- everything here is a plain function over `Span[]` so it's
 * unit-testable in isolation (see `traceTree.test.ts`).
 */

import type { SpanRow as Span } from "../page_data/TraceDetailPageData.types.ts";

export interface SpanNode {
  span: Span;
  children: SpanNode[];
}

function byStartNs(a: Span, b: Span): number {
  return a.start_ns - b.start_ns;
}

/**
 * Assemble a flat list of spans (as returned by
 * `GET /-/otel/api/traces/{trace_id}`) into a forest of `SpanNode` trees.
 *
 * Root rule (orphan/remote-parent tolerance: an
 * explicit requirement): a span is a ROOT if its `parent_span_id` is
 * `null`, OR it references a `span_id` that isn't present anywhere in this
 * trace -- e.g. a remote parent from a proxy's `traceparent` header whose
 * own span was never recorded by this instance. Every span in the input
 * ends up in exactly one tree; nothing is ever dropped for having an
 * unresolvable parent.
 *
 * Ordering: children of every node, and the top-level roots themselves,
 * are sorted by `start_ns` ascending, so the waterfall reads top-to-bottom
 * in time order.
 *
 * Cycle guard: malformed or duplicate data (e.g. a duplicated span_id
 * recorded twice with different parent linkages) could in principle wire
 * up a loop reachable from a real root. Recursion tracks a `visited` set
 * of span_ids along the current root-to-node path; if a span_id reappears
 * on its own ancestor path, that branch's children are cut off there
 * instead of recursing forever.
 */
export function buildTraceTree(spans: Span[]): SpanNode[] {
  const idsInTrace = new Set(spans.map((s) => s.span_id));
  const childrenByParent = new Map<string, Span[]>();
  const roots: Span[] = [];

  for (const span of spans) {
    const parentId = span.parent_span_id;
    const isRoot = parentId == null || !idsInTrace.has(parentId);
    if (isRoot) {
      roots.push(span);
      continue;
    }
    const siblings = childrenByParent.get(parentId);
    if (siblings) {
      siblings.push(span);
    } else {
      childrenByParent.set(parentId, [span]);
    }
  }

  function build(span: Span, visited: ReadonlySet<string>): SpanNode {
    if (visited.has(span.span_id)) {
      // Cycle: this span_id is its own ancestor on this path. Stop here
      // rather than recursing forever.
      return { span, children: [] };
    }
    const nextVisited = new Set(visited);
    nextVisited.add(span.span_id);
    const children = (childrenByParent.get(span.span_id) ?? [])
      .slice()
      .sort(byStartNs)
      .map((child) => build(child, nextVisited));
    return { span, children };
  }

  return roots
    .slice()
    .sort(byStartNs)
    .map((root) => build(root, new Set<string>()));
}

/**
 * `[min(start_ns), max(end_ns)]` across every span in the trace -- the
 * waterfall's bar-positioning basis (plan.md "UI": bars are percentages of
 * the whole trace's time range). Returns `null` for an empty span list.
 */
export function traceBounds(
  spans: Span[],
): { minStartNs: number; maxEndNs: number } | null {
  if (spans.length === 0) return null;
  let minStartNs = spans[0]!.start_ns;
  let maxEndNs = spans[0]!.end_ns;
  for (const span of spans) {
    if (span.start_ns < minStartNs) minStartNs = span.start_ns;
    if (span.end_ns > maxEndNs) maxEndNs = span.end_ns;
  }
  return { minStartNs, maxEndNs };
}

/**
 * Flatten a forest of `SpanNode` into a `span_id -> SpanNode` map, for
 * O(1) lookup by the inspector panel and the `#span-{id}` deep-link
 * handler (both need to find a node by id without re-walking the tree).
 */
export function flattenTree(nodes: SpanNode[]): Map<string, SpanNode> {
  const map = new Map<string, SpanNode>();
  function walk(node: SpanNode) {
    map.set(node.span.span_id, node);
    for (const child of node.children) walk(child);
  }
  for (const node of nodes) walk(node);
  return map;
}

/** span_id -> ancestor span_ids (immediate parent first), for expanding
 * collapsed ancestors when deep-linking to a nested span. */
export function ancestorIds(nodes: SpanNode[]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  function walk(node: SpanNode, ancestors: string[]) {
    map.set(node.span.span_id, ancestors);
    for (const child of node.children) {
      walk(child, [node.span.span_id, ...ancestors]);
    }
  }
  for (const node of nodes) walk(node, []);
  return map;
}

/**
 * A span's own time: its duration minus the part of it covered by its
 * children (their union, clipped to the span, so overlapping or
 * over-running children are not counted twice). Ranking by this finds
 * the spans where the time actually went: a 15s `invoke_agent` that
 * spends 14.9s inside three `chat` calls is a wrapper, and the chats
 * are the answer.
 */
export function selfTimeNs(node: SpanNode): number {
  const start = node.span.start_ns;
  const end = node.span.end_ns;
  let covered = 0;
  let cursor = start;
  // Children are already in start order (buildTraceTree sorts them).
  for (const child of node.children) {
    const childStart = Math.max(child.span.start_ns, cursor);
    const childEnd = Math.min(child.span.end_ns, end);
    if (childEnd > childStart) {
      covered += childEnd - childStart;
      cursor = childEnd;
    }
  }
  return Math.max(end - start - covered, 0);
}

// ---------------------------------------------------------------------------
// Sibling grouping: the waterfall's answer to a dozen sub-millisecond
// `db.query` rows sitting between the two five-second spans you came to see.

/** Fewest consecutive matching siblings that fold into one row. */
export const GROUP_MIN_RUN = 3;

/**
 * A sibling longer than this fraction of the whole trace never joins a
 * group, however many neighbours match it: a span that long is the kind
 * the waterfall exists to show. Proportional so it self-tunes: 2% of a
 * 15s agent trace is 300ms and folds every query away; 2% of a 30ms
 * request is 0.6ms and leaves the 1ms queries, whose bars are legible at
 * that scale, alone.
 */
export const GROUP_MAX_FRACTION = 0.02;

export interface GroupOptions {
  /** {@link GROUP_MIN_RUN}; `Infinity` disables grouping. */
  minRun: number;
  /** Absolute cap in ns, normally `traceDuration * GROUP_MAX_FRACTION`. */
  maxDurationNs: number;
}

/** A run of consecutive siblings with the same name and scope, folded into
 * one waterfall row. Members keep their own subtrees; they are rendered
 * as ordinary rows when the group is expanded. */
export interface SpanGroup {
  kind: "group";
  /** `group-` + the first member's span_id: stable across re-grouping. */
  id: string;
  name: string;
  scopeName: string | null;
  /** In start order, as the siblings were. */
  members: SpanNode[];
  startNs: number;
  endNs: number;
  /** Sum of member durations: work, not wall time (members may overlap). */
  sumDurationNs: number;
  /** Members plus every descendant: how many rows the group stands in for. */
  spanCount: number;
}

export type Row = { kind: "span"; node: SpanNode } | SpanGroup;

/** Key under which two siblings may share a group, or `null` when this
 * span must always have its own row. ERROR spans never group: the red
 * bar is the point. */
function groupKey(node: SpanNode, opts: GroupOptions): string | null {
  const span = node.span;
  if (span.status === "ERROR") return null;
  if (span.end_ns - span.start_ns > opts.maxDurationNs) return null;
  return `${span.name} ${span.scope_name ?? ""}`;
}

function subtreeSize(node: SpanNode): number {
  let n = 1;
  for (const child of node.children) n += subtreeSize(child);
  return n;
}

function makeGroup(members: SpanNode[]): SpanGroup {
  const first = members[0]!.span;
  let startNs = first.start_ns;
  let endNs = first.end_ns;
  let sumDurationNs = 0;
  let spanCount = 0;
  for (const member of members) {
    const s = member.span;
    if (s.start_ns < startNs) startNs = s.start_ns;
    if (s.end_ns > endNs) endNs = s.end_ns;
    sumDurationNs += s.end_ns - s.start_ns;
    spanCount += subtreeSize(member);
  }
  return {
    kind: "group",
    id: `group-${first.span_id}`,
    name: first.name,
    scopeName: first.scope_name ?? null,
    members,
    startNs,
    endNs,
    sumDurationNs,
    spanCount,
  };
}

/**
 * One node's children (already in start order) as the rows the waterfall
 * draws: a run of at least `minRun` consecutive siblings sharing a
 * {@link groupKey} becomes one {@link SpanGroup}; everything else is a
 * span row. Only *consecutive* siblings fold, so a query, a 5s LLM call
 * and another query stay three rows: the order of work is preserved,
 * which is what a waterfall is for.
 */
export function groupSiblings(siblings: SpanNode[], opts: GroupOptions): Row[] {
  const rows: Row[] = [];
  let i = 0;
  while (i < siblings.length) {
    const key = groupKey(siblings[i]!, opts);
    let j = i + 1;
    if (key !== null) {
      while (j < siblings.length && groupKey(siblings[j]!, opts) === key) j++;
    }
    if (key !== null && j - i >= opts.minRun) {
      rows.push(makeGroup(siblings.slice(i, j)));
    } else {
      for (let k = i; k < j; k++) {
        rows.push({ kind: "span", node: siblings[k]! });
      }
    }
    i = j;
  }
  return rows;
}

/** Key in {@link RowModel.rowsByParent} for the trace's root rows. */
export const ROOT_KEY = "";

export interface RowModel {
  /** parent span_id (or {@link ROOT_KEY}) -> the rows drawn under it. */
  rowsByParent: Map<string, Row[]>;
  /** member span_id -> id of the group that hides it while collapsed. */
  groupOfSpan: Map<string, string>;
}

/** {@link groupSiblings} applied at every level of the forest, computed
 * once so the rows and the deep-link handler (which must expand the group
 * around a `#span-` target) agree on where the groups are. */
export function buildRows(nodes: SpanNode[], opts: GroupOptions): RowModel {
  const rowsByParent = new Map<string, Row[]>();
  const groupOfSpan = new Map<string, string>();
  function place(key: string, siblings: SpanNode[]) {
    const rows = groupSiblings(siblings, opts);
    rowsByParent.set(key, rows);
    for (const row of rows) {
      if (row.kind !== "group") continue;
      for (const member of row.members) {
        groupOfSpan.set(member.span.span_id, row.id);
      }
    }
    for (const sibling of siblings) {
      if (sibling.children.length > 0) {
        place(sibling.span.span_id, sibling.children);
      }
    }
  }
  place(ROOT_KEY, nodes);
  return { rowsByParent, groupOfSpan };
}
