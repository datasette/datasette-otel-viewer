import type { SpanRow as Span } from "../../page_data/TraceDetailPageData.types.ts";

/** Bar colour by span family: the root of the tree, `db.query`,
 * `db.write.*`, else "other" -- a small fixed palette. ERROR status
 * overrides to red regardless of family. A group row takes the class of
 * its first member (a group never contains an ERROR span, see
 * `traceTree.groupSiblings`). */
export function barClass(span: Span, depth: number): string {
  if (span.status === "ERROR") return "bar bar-error";
  if (depth === 0) return "bar bar-root";
  if (span.name === "db.query") return "bar bar-db-query";
  if (span.name.startsWith("db.write.")) return "bar bar-db-write";
  return "bar bar-other";
}
