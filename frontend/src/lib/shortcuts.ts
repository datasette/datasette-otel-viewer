/**
 * Keyboard shortcut definitions: the one list each page hands to
 * `components/Shortcuts.svelte`, which both binds the keys (through
 * TanStack Hotkeys) and draws the `?` help panel from it. One source, so
 * the help can never describe a key the page does not bind.
 *
 * Key spelling follows @tanstack/hotkeys: a shifted letter is `Shift+H`,
 * and `?` has to be written `Shift+/` (the library matches Shift plus the
 * physical slash key; `?` on its own is not a key name it accepts). A
 * sequence is an array: `["g", "t"]` is g then t, within a second.
 *
 * Nothing here touches the DOM at import time, so `formatKeys` and the
 * group builders are unit-testable.
 */

/** One hotkey (`"j"`, `"Shift+H"`) or a sequence (`["g", "t"]`). */
export type Keys = string | string[];

export interface Shortcut {
  keys: Keys;
  /** Other keys that do the same thing (arrows beside j/k); bound too,
   * and shown after the primary key. */
  also?: string[];
  /** What the help panel says. Short, verb first. */
  label: string;
  run: () => void;
  /** Bound but not listed: the `2`..`9` behind one "1 – 9" help row. */
  hidden?: boolean;
  /** What the help panel shows instead of {@link keys}, as kbd labels. */
  display?: string[];
}

export interface ShortcutGroup {
  title: string;
  shortcuts: Shortcut[];
}

/** kbd labels for a hotkey: `"Shift+/"` is `?`, `"Shift+H"` is `⇧H`,
 * `"ArrowDown"` is `↓`, a sequence is one label per step. */
export function formatKeys(keys: Keys): string[] {
  const steps = Array.isArray(keys) ? keys : [keys];
  return steps.map(formatKey);
}

const KEY_LABELS: Record<string, string> = {
  "Shift+/": "?",
  ArrowDown: "↓",
  ArrowUp: "↑",
  ArrowLeft: "←",
  ArrowRight: "→",
  Escape: "Esc",
  Enter: "Enter",
  " ": "Space",
};

function formatKey(key: string): string {
  const known = KEY_LABELS[key];
  if (known) return known;
  const parts = key.split("+");
  const base = parts.pop() ?? key;
  const mods = parts
    .map((m) =>
      m === "Shift" ? "⇧" : m === "Alt" ? "⌥" : m === "Mod" ? "⌘" : m,
    )
    .join("");
  return mods + (KEY_LABELS[base] ?? base);
}

/** The `/-/otel` sections the `g` chords jump to, in nav order. */
export const SECTIONS: { key: string; label: string; href: string }[] = [
  { key: "o", label: "Overview", href: "/-/otel" },
  { key: "t", label: "Traces", href: "/-/otel/traces" },
  { key: "h", label: "HTTP endpoints", href: "/-/otel/http" },
  { key: "q", label: "SQL queries", href: "/-/otel/sql" },
  { key: "s", label: "Spans", href: "/-/otel/spans" },
  { key: "m", label: "Metrics", href: "/-/otel/metrics" },
];

/** Where a page goes. Swappable so tests can watch it. */
export const navigate = {
  to(href: string) {
    window.location.href = href;
  },
  newTab(href: string) {
    window.open(href, "_blank", "noopener");
  },
};

/** The first text-ish control on the page: what `f` focuses. */
function focusFilter() {
  const scope = document.querySelector("main") ?? document;
  const el = scope.querySelector<HTMLElement>(
    'input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]), select, textarea',
  );
  el?.focus();
  if (el instanceof HTMLInputElement) el.select();
}

export interface GlobalOptions {
  /** The page's Refresh button, for `r`. */
  refresh?: () => void;
  /** The crumb above this page, for `u`. */
  up?: { label: string; href: string };
  /** Whether the page has anything for `f` to focus. Default true. */
  filters?: boolean;
}

/** The group every page carries: section jumps, refresh, up, filter,
 * Escape. `Shortcuts.svelte` binds Escape itself (help panel first, then
 * blur a field, then any Escape entries the page listed), so the entry
 * here is for the help panel only. */
export function globalShortcuts(opts: GlobalOptions = {}): ShortcutGroup {
  const shortcuts: Shortcut[] = [];
  if (opts.refresh) {
    shortcuts.push({ keys: "r", label: "Refresh", run: opts.refresh });
  }
  if (opts.up) {
    const { label, href } = opts.up;
    shortcuts.push({
      keys: "u",
      label: `Up to ${label}`,
      run: () => navigate.to(href),
    });
  }
  if (opts.filters !== false) {
    shortcuts.push({ keys: "f", label: "Focus the filters", run: focusFilter });
    shortcuts.push({
      keys: "Escape",
      label: "Leave a field, close a panel",
      run: () => {},
    });
  }
  return { title: "Everywhere", shortcuts };
}

/** `g` then a letter: jump between sections. Its own group so the help
 * panel can show the six destinations as a block. */
export function sectionShortcuts(current?: string): ShortcutGroup {
  return {
    title: "Go to",
    shortcuts: SECTIONS.map((s) => ({
      keys: ["g", s.key],
      label: s.label + (s.href === current ? " (this page)" : ""),
      run: () => navigate.to(s.href),
    })),
  };
}

/** The cursor a table page moves with j/k. `lib/rowCursor.svelte.ts` is
 * the implementation; this is the slice the bindings need. */
export interface CursorLike<T> {
  readonly row: T | null;
  move(delta: number): void;
  first(): void;
  last(): void;
}

export interface TableOptions<T> {
  cursor: CursorLike<T>;
  /** What the row opens on Enter (what clicking it does). */
  url: (row: T) => string;
  /** The row's Max cell, when it links into a trace: `s`. */
  slowestUrl?: (row: T) => string | null;
  /** The row's raw rows in Datasette: `x`. */
  rawUrl?: (row: T) => string | null;
  /** Sort keys in column order, for `1`..`9`. */
  sortKeys?: string[];
  sort?: (key: string) => void;
  previousPage?: () => void;
  nextPage?: () => void;
}

/** j/k over the rows, Enter to open, brackets to page, digits to sort. */
export function tableShortcuts<T>(opts: TableOptions<T>): ShortcutGroup {
  const { cursor } = opts;
  const withRow = (fn: (row: T) => void) => () => {
    const row = cursor.row;
    if (row) fn(row);
  };
  const shortcuts: Shortcut[] = [
    {
      keys: "j",
      also: ["ArrowDown"],
      label: "Next row",
      run: () => cursor.move(1),
    },
    {
      keys: "k",
      also: ["ArrowUp"],
      label: "Previous row",
      run: () => cursor.move(-1),
    },
    { keys: ["g", "g"], label: "First row", run: () => cursor.first() },
    { keys: "Shift+G", label: "Last row", run: () => cursor.last() },
    {
      keys: "Enter",
      also: ["o"],
      label: "Open the row",
      run: withRow((row) => navigate.to(opts.url(row))),
    },
    {
      keys: "Shift+Enter",
      label: "Open the row in a new tab",
      run: withRow((row) => navigate.newTab(opts.url(row))),
    },
  ];
  if (opts.slowestUrl) {
    const slowestUrl = opts.slowestUrl;
    shortcuts.push({
      keys: "s",
      label: "Jump to the row's slowest span",
      run: withRow((row) => {
        const href = slowestUrl(row);
        if (href) navigate.to(href);
      }),
    });
  }
  if (opts.rawUrl) {
    const rawUrl = opts.rawUrl;
    shortcuts.push({
      keys: "x",
      label: "Raw rows in Datasette",
      run: withRow((row) => {
        const href = rawUrl(row);
        if (href) navigate.to(href);
      }),
    });
  }
  if (opts.previousPage && opts.nextPage) {
    shortcuts.push(
      { keys: "[", label: "Previous page", run: opts.previousPage },
      { keys: "]", label: "Next page", run: opts.nextPage },
    );
  }
  if (opts.sortKeys && opts.sort) {
    const keys = opts.sortKeys.slice(0, 9);
    const sort = opts.sort;
    keys.forEach((key, i) => {
      shortcuts.push({
        keys: String(i + 1),
        label: "Sort by that column, again to flip",
        display: [`1 – ${keys.length}`],
        hidden: i > 0,
        run: () => sort(key),
      });
    });
  }
  return { title: "Rows", shortcuts };
}
