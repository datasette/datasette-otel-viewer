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
