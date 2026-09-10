import { describe, expect, it, vi } from "vitest";
import {
  formatKeys,
  globalShortcuts,
  navigate,
  SECTIONS,
  sectionShortcuts,
  tableShortcuts,
  type CursorLike,
} from "./shortcuts.ts";

describe("formatKeys", () => {
  it("spells the keys the way the help panel shows them", () => {
    expect(formatKeys("j")).toEqual(["j"]);
    expect(formatKeys("Shift+/")).toEqual(["?"]);
    expect(formatKeys("Shift+H")).toEqual(["⇧H"]);
    expect(formatKeys("Shift+Enter")).toEqual(["⇧Enter"]);
    expect(formatKeys("ArrowDown")).toEqual(["↓"]);
    expect(formatKeys("Escape")).toEqual(["Esc"]);
    expect(formatKeys("[")).toEqual(["["]);
  });

  it("gives a sequence one label per step", () => {
    expect(formatKeys(["g", "t"])).toEqual(["g", "t"]);
  });
});

describe("sectionShortcuts", () => {
  it("binds g then a letter for every section and marks the current one", () => {
    const group = sectionShortcuts("/-/otel/traces");
    expect(group.shortcuts.map((s) => s.keys)).toEqual(
      SECTIONS.map((s) => ["g", s.key]),
    );
    const traces = group.shortcuts.find((s) => s.label.startsWith("Traces"));
    expect(traces?.label).toBe("Traces (this page)");
  });

  it("uses distinct letters", () => {
    const letters = SECTIONS.map((s) => s.key);
    expect(new Set(letters).size).toBe(letters.length);
  });
});

describe("globalShortcuts", () => {
  it("only offers what the page has", () => {
    const bare = globalShortcuts({ filters: false });
    expect(bare.shortcuts.map((s) => s.keys)).toEqual([]);

    const full = globalShortcuts({
      refresh: () => {},
      up: { label: "Traces", href: "/-/otel/traces" },
    });
    expect(full.shortcuts.map((s) => s.keys)).toEqual([
      "r",
      "u",
      "f",
      "Escape",
    ]);
    expect(full.shortcuts[1]!.label).toBe("Up to Traces");
  });
});

function fakeCursor<T>(row: T | null): CursorLike<T> & {
  moves: number[];
} {
  const moves: number[] = [];
  return {
    row,
    moves,
    move: (d) => moves.push(d),
    first: () => moves.push(-Infinity),
    last: () => moves.push(Infinity),
  };
}

describe("tableShortcuts", () => {
  it("moves the cursor with j/k and the arrows", () => {
    const cursor = fakeCursor<string>(null);
    const group = tableShortcuts({ cursor, url: (r) => `/${r}` });
    const byKey = (k: string) => group.shortcuts.find((s) => s.keys === k)!;
    byKey("j").run();
    byKey("k").run();
    expect(cursor.moves).toEqual([1, -1]);
    expect(byKey("j").also).toEqual(["ArrowDown"]);
    expect(byKey("k").also).toEqual(["ArrowUp"]);
  });

  it("opens the cursor row, and does nothing without one", () => {
    const to = vi.spyOn(navigate, "to").mockImplementation(() => {});
    const newTab = vi.spyOn(navigate, "newTab").mockImplementation(() => {});
    const open = (cursor: CursorLike<string>) => {
      const group = tableShortcuts({ cursor, url: (r) => `/rows/${r}` });
      group.shortcuts.find((s) => s.keys === "Enter")!.run();
      group.shortcuts.find((s) => s.keys === "Shift+Enter")!.run();
    };
    open(fakeCursor(null));
    expect(to).not.toHaveBeenCalled();
    open(fakeCursor("a"));
    expect(to).toHaveBeenCalledWith("/rows/a");
    expect(newTab).toHaveBeenCalledWith("/rows/a");
    vi.restoreAllMocks();
  });

  it("binds a digit per sort column but lists them as one row", () => {
    const sort = vi.fn();
    const group = tableShortcuts({
      cursor: fakeCursor<string>(null),
      url: () => "/",
      sortKeys: ["name", "count", "p95"],
      sort,
    });
    const digits = group.shortcuts.filter((s) => /^\d$/.test(s.keys as string));
    expect(digits.map((s) => s.keys)).toEqual(["1", "2", "3"]);
    expect(digits.filter((s) => !s.hidden)).toHaveLength(1);
    expect(digits[0]!.display).toEqual(["1 – 3"]);
    digits[2]!.run();
    expect(sort).toHaveBeenCalledWith("p95");
  });

  it("caps sort digits at nine columns", () => {
    const group = tableShortcuts({
      cursor: fakeCursor<string>(null),
      url: () => "/",
      sortKeys: Array.from({ length: 12 }, (_, i) => `c${i}`),
      sort: () => {},
    });
    const digits = group.shortcuts.filter((s) => /^\d$/.test(s.keys as string));
    expect(digits).toHaveLength(9);
  });

  it("offers paging and the optional row links only when given", () => {
    const plain = tableShortcuts({
      cursor: fakeCursor<string>(null),
      url: () => "/",
    });
    const keys = (g: typeof plain) =>
      g.shortcuts.map((s) =>
        Array.isArray(s.keys) ? s.keys.join(" ") : s.keys,
      );
    expect(keys(plain)).toEqual([
      "j",
      "k",
      "g g",
      "Shift+G",
      "Enter",
      "Shift+Enter",
    ]);
    const full = tableShortcuts({
      cursor: fakeCursor<string>(null),
      url: () => "/",
      slowestUrl: () => null,
      rawUrl: () => null,
      previousPage: () => {},
      nextPage: () => {},
    });
    expect(keys(full).slice(6)).toEqual(["s", "x", "[", "]"]);
  });
});
