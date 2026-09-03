<script lang="ts">
  import type { SortState } from "../lib/sort.ts";

  /**
   * A clickable, sortable <th>. The owning table keeps the SortState and
   * passes an `onsort` callback (wired to lib/sort.ts's nextSort); this
   * just renders the button and the active-column arrow.
   */
  const {
    key,
    label,
    sort,
    onsort,
    numeric = false,
  }: {
    key: string;
    label: string;
    sort: SortState;
    onsort: (key: string) => void;
    numeric?: boolean;
  } = $props();

  const active = $derived(sort.key === key);
  const ariaSort = $derived(
    active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined,
  );
</script>

<th class:num={numeric} aria-sort={ariaSort}>
  <button type="button" onclick={() => onsort(key)}>
    {label}<span class="arrow"
      >{active ? (sort.dir === "asc" ? "▲" : "▼") : ""}</span
    >
  </button>
</th>

<style>
  th {
    text-align: left;
    padding: 0;
    border-bottom: 1px solid #e2e2e2;
  }
  th.num {
    text-align: right;
  }
  button {
    /* Inherit the th's typography; the whole header cell is the target. */
    all: unset;
    box-sizing: border-box;
    cursor: pointer;
    display: block;
    width: 100%;
    padding: 0.5rem 0.6rem;
    font-weight: 600;
    text-align: inherit;
    white-space: nowrap;
  }
  button:hover {
    background: #f6f8fa;
  }
  .arrow {
    display: inline-block;
    width: 1em;
    font-size: 0.7em;
    vertical-align: middle;
    margin-left: 0.15rem;
    color: #555;
  }
</style>
