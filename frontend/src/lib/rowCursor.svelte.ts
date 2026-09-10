/**
 * The highlighted row a table page moves with j and k. The page marks
 * its cursor row with `data-cursor="true"` and the `cursor` class (styled
 * in app.css); this only owns the index and keeps it on screen. Rows are
 * read through a getter so a reload that shrinks the table cannot leave
 * the cursor pointing past the end.
 */
export class RowCursor<T> {
  index = $state(-1);
  #rows: () => T[];

  constructor(rows: () => T[]) {
    this.#rows = rows;
  }

  get row(): T | null {
    const rows = this.#rows();
    return this.index >= 0 && this.index < rows.length
      ? rows[this.index]!
      : null;
  }

  /** Step by `delta`, clamped. With no cursor yet, the first step down
   * lands on the first row and the first step up on the last. */
  move(delta: number): void {
    const n = this.#rows().length;
    if (n === 0) {
      this.index = -1;
      return;
    }
    const from = this.index < 0 ? (delta > 0 ? -1 : n) : this.index;
    this.set(Math.min(n - 1, Math.max(0, from + delta)));
  }

  first(): void {
    if (this.#rows().length > 0) this.set(0);
  }

  last(): void {
    const n = this.#rows().length;
    if (n > 0) this.set(n - 1);
  }

  set(index: number): void {
    this.index = index;
    if (typeof document === "undefined") return;
    requestAnimationFrame(() => {
      document
        .querySelector('[data-cursor="true"]')
        ?.scrollIntoView({ block: "nearest" });
    });
  }
}
