import { describe, expect, it } from "vitest";
import {
  ancestorIds,
  buildTraceTree,
  flattenTree,
  traceBounds,
} from "./traceTree.ts";
import type { SpanRow as Span } from "../page_data/TraceDetailPageData.types.ts";

/** Minimal Span factory -- only the fields traceTree.ts actually reads
 * (span_id, parent_span_id, start_ns/end_ns) vary per test; everything
 * else is a fixed, valid placeholder. */
function makeSpan(overrides: Partial<Span> & { span_id: string }): Span {
  return {
    trace_id: "trace-1",
    parent_span_id: null,
    name: "span",
    kind: "INTERNAL",
    start_ns: 0,
    end_ns: 1_000_000,
    duration_ms: 1,
    status: "OK",
    status_description: null,
    service_name: "svc",
    http_route: null,
    http_status: null,
    db_namespace: null,
    db_operation: null,
    db_query_text: null,
    attributes: {},
    resource: {},
    scope_name: null,
    scope_version: null,
    schema_url: null,
    ...overrides,
  };
}

describe("buildTraceTree: simple nesting", () => {
  it("nests children under their parent, sorted by start_ns", () => {
    const root = makeSpan({ span_id: "root", start_ns: 0, end_ns: 100 });
    const childB = makeSpan({
      span_id: "child-b",
      parent_span_id: "root",
      start_ns: 20,
      end_ns: 40,
    });
    const childA = makeSpan({
      span_id: "child-a",
      parent_span_id: "root",
      start_ns: 10,
      end_ns: 30,
    });
    const grandchild = makeSpan({
      span_id: "grandchild",
      parent_span_id: "child-a",
      start_ns: 15,
      end_ns: 25,
    });

    const tree = buildTraceTree([root, childB, childA, grandchild]);

    expect(tree).toHaveLength(1);
    expect(tree[0]!.span.span_id).toBe("root");
    // Children sorted by start_ns ascending, regardless of input order.
    expect(tree[0]!.children.map((n) => n.span.span_id)).toEqual([
      "child-a",
      "child-b",
    ]);
    expect(tree[0]!.children[0]!.children.map((n) => n.span.span_id)).toEqual([
      "grandchild",
    ]);
    expect(tree[0]!.children[1]!.children).toEqual([]);
  });

  it("sorts multiple top-level roots by start_ns", () => {
    const later = makeSpan({ span_id: "later", start_ns: 100, end_ns: 200 });
    const earlier = makeSpan({ span_id: "earlier", start_ns: 0, end_ns: 50 });

    const tree = buildTraceTree([later, earlier]);

    expect(tree.map((n) => n.span.span_id)).toEqual(["earlier", "later"]);
  });
});

describe("buildTraceTree: orphan/remote-parent tolerance", () => {
  it("roots a span whose parent_span_id is null", () => {
    const span = makeSpan({ span_id: "a", parent_span_id: null });
    const tree = buildTraceTree([span]);
    expect(tree.map((n) => n.span.span_id)).toEqual(["a"]);
  });

  it("roots a span whose parent_span_id references a span not in the trace", () => {
    // e.g. a remote parent from a proxy's traceparent header that this
    // instance never ingested a span for (plan.md's orphan tolerance
    // requirement).
    const orphan = makeSpan({
      span_id: "orphan",
      parent_span_id: "never-ingested",
      start_ns: 5,
      end_ns: 10,
    });
    const normalRoot = makeSpan({ span_id: "root", start_ns: 0, end_ns: 100 });
    const normalChild = makeSpan({
      span_id: "child",
      parent_span_id: "root",
      start_ns: 1,
      end_ns: 2,
    });

    const tree = buildTraceTree([normalRoot, normalChild, orphan]);

    // Both the true root and the orphan surface as top-level roots -- the
    // orphan is never dropped for having an unresolvable parent.
    expect(tree.map((n) => n.span.span_id).sort()).toEqual(["orphan", "root"]);
    const orphanNode = tree.find((n) => n.span.span_id === "orphan");
    expect(orphanNode?.children).toEqual([]);
  });
});

describe("buildTraceTree: cycle guard", () => {
  it("cuts off recursion instead of looping when a duplicated span_id creates a cycle", () => {
    // Malformed/duplicate data: span_id "a" appears twice with different
    // parent linkages, wiring up a genuine cycle reachable from a real
    // root: root -> a(#1) -> b -> a(#2) -> (would loop back to b forever
    // without the visited-set guard).
    const root = makeSpan({
      span_id: "root",
      parent_span_id: null,
      start_ns: 0,
    });
    const aFirst = makeSpan({
      span_id: "a",
      parent_span_id: "root",
      start_ns: 1,
    });
    const b = makeSpan({ span_id: "b", parent_span_id: "a", start_ns: 2 });
    const aSecond = makeSpan({
      span_id: "a",
      parent_span_id: "b",
      start_ns: 3,
    });

    const tree = buildTraceTree([root, aFirst, b, aSecond]);

    // Must terminate (this call itself not hanging/stack-overflowing is
    // most of the assertion) and produce a finite, sane structure: root ->
    // a -> b -> a(cut off, no further children).
    expect(tree).toHaveLength(1);
    expect(tree[0]!.span.span_id).toBe("root");
    const aNode = tree[0]!.children[0]!;
    expect(aNode.span.span_id).toBe("a");
    const bNode = aNode.children[0]!;
    expect(bNode.span.span_id).toBe("b");
    const cutOffNode = bNode.children[0]!;
    expect(cutOffNode.span.span_id).toBe("a");
    expect(cutOffNode.children).toEqual([]);
  });

  it("drops a self-contained cycle with no root entry point (never rendered, never hangs)", () => {
    // Two spans that are mutual parents of each other and have no other
    // root reaching them: neither is a root (each has a parent present in
    // the set), so this pathological pair is simply excluded rather than
    // rendered or infinitely recursed into.
    const x = makeSpan({ span_id: "x", parent_span_id: "y" });
    const y = makeSpan({ span_id: "y", parent_span_id: "x" });

    const tree = buildTraceTree([x, y]);

    expect(tree).toEqual([]);
  });
});

describe("traceBounds", () => {
  it("returns [min(start_ns), max(end_ns)] across all spans", () => {
    const spans = [
      makeSpan({ span_id: "a", start_ns: 10, end_ns: 20 }),
      makeSpan({ span_id: "b", start_ns: 5, end_ns: 15 }),
      makeSpan({ span_id: "c", start_ns: 8, end_ns: 100 }),
    ];
    expect(traceBounds(spans)).toEqual({ minStartNs: 5, maxEndNs: 100 });
  });

  it("returns null for an empty list", () => {
    expect(traceBounds([])).toBeNull();
  });
});

describe("flattenTree / ancestorIds", () => {
  it("flattenTree indexes every node by span_id", () => {
    const root = makeSpan({ span_id: "root" });
    const child = makeSpan({ span_id: "child", parent_span_id: "root" });
    const tree = buildTraceTree([root, child]);

    const map = flattenTree(tree);

    expect(Array.from(map.keys()).sort()).toEqual(["child", "root"]);
    expect(map.get("child")?.span.span_id).toBe("child");
  });

  it("ancestorIds lists immediate parent first", () => {
    const root = makeSpan({ span_id: "root" });
    const child = makeSpan({ span_id: "child", parent_span_id: "root" });
    const grandchild = makeSpan({
      span_id: "grandchild",
      parent_span_id: "child",
    });
    const tree = buildTraceTree([root, child, grandchild]);

    const map = ancestorIds(tree);

    expect(map.get("root")).toEqual([]);
    expect(map.get("child")).toEqual(["root"]);
    expect(map.get("grandchild")).toEqual(["child", "root"]);
  });
});
