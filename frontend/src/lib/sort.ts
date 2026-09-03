/**
 * Client-side column sorting for the analytics/list tables. Pure functions
 * (no runes) so this stays unit-testable in plain vitest like traceTree;
 * the $state lives in each page component, and SortHeader.svelte renders
 * the clickable <th>s.
 *
 * Sorting is over the already-loaded rows only -- the traces list caps at
 * 500 rows and the analytics tables at 100 (api.py MAX_LIMIT /
 * DEFAULT_LIMIT), so there is no server-side ordering round trip.
 */

export type SortDir = "asc" | "desc";

export interface SortState {
  key: string | null;
  dir: SortDir;
}

/**
 * The next sort state after clicking `clicked`: clicking the active column
 * flips direction; clicking a new column sorts descending first when the
 * column is numeric (biggest/slowest first is what you want from a
 * metrics column) and ascending first otherwise.
 */
export function nextSort(
  current: SortState,
  clicked: string,
  numericKeys: ReadonlySet<string>,
): SortState {
  if (current.key === clicked) {
    return { key: clicked, dir: current.dir === "asc" ? "desc" : "asc" };
  }
  return { key: clicked, dir: numericKeys.has(clicked) ? "desc" : "asc" };
}

/**
 * A copy of `rows` sorted by `state.key`. Null/undefined cells always sort
 * last regardless of direction (a route with no HTTP status shouldn't
 * lead the ascending sort). Numbers compare numerically, everything else
 * case-insensitively as strings. `state.key: null` (initial state) returns
 * the rows untouched -- the server's own ordering.
 */
export function sortRows<T extends object>(rows: T[], state: SortState): T[] {
  const key = state.key;
  if (key === null) return rows;
  const sign = state.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    // `T extends object` rather than an index-signature constraint so the
    // row interfaces (TraceRow etc.) are accepted as-is.
    const av = (a as Record<string, unknown>)[key];
    const bv = (b as Record<string, unknown>)[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    const cmp =
      typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv), undefined, {
            sensitivity: "base",
          });
    return cmp * sign;
  });
}
