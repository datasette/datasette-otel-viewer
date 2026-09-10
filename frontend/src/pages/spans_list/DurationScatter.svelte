<script lang="ts">
  import { Dot, Plot } from "svelteplot";
  import { formatAbsoluteTimePrecise, formatMs } from "../../lib/time.ts";
  import type { SpanChartPoint } from "../../page_data/SpansListPageData.types.ts";

  // Every matching span as a dot: when it ran against how long it took. The
  // table below is one page of this, sorted; the cloud is what says whether
  // the span you are looking at is ordinary, and whether "slow" is a spike at
  // one moment or the way this work always behaves.
  const {
    points,
    highlight,
    stride,
  }: {
    points: SpanChartPoint[];
    highlight?: string | null;
    stride: number;
  } = $props();

  // SveltePlot marks want `Record<string | symbol, RawValue>`; an interface
  // never has the implicit index signature that satisfies it, so rows go
  // through a type alias (as in SeriesChart).
  type PlotRow = {
    t: Date;
    ms: number;
    span_id: string;
    trace_id: string;
    label: string;
  };

  let logScale = $state(false);

  const rows = $derived(
    points.map((p): PlotRow => ({
      t: new Date(p.start_ns / 1e6),
      ms: p.duration_ms,
      span_id: p.span_id,
      trace_id: p.trace_id,
      label: `${formatMs(p.duration_ms)} — ${formatAbsoluteTimePrecise(p.start_ns)}${
        p.status === "ERROR" ? " — error" : ""
      }\nClick to open this span in its trace`,
    })),
  );
  // A log axis has no room for a zero-duration span; say how many dots that
  // costs rather than quietly drawing fewer of them.
  const plottable = $derived(logScale ? rows.filter((r) => r.ms > 0) : rows);
  const dropped = $derived(rows.length - plottable.length);

  const errored = $derived(
    new Set(points.filter((p) => p.status === "ERROR").map((p) => p.span_id)),
  );
  const ok = $derived(plottable.filter((r) => !errored.has(r.span_id)));
  const bad = $derived(plottable.filter((r) => errored.has(r.span_id)));
  // The span the trace view sent us to, drawn last and bigger: finding it in
  // the cloud is the whole point of arriving with one pinned.
  const pinned = $derived(
    highlight ? plottable.filter((r) => r.span_id === highlight) : [],
  );

  function open(_event: Event, row: PlotRow) {
    window.location.href = `/-/otel/traces/${row.trace_id}#span-${row.span_id}`;
  }
</script>

<figure class="scatter" data-testid="duration-scatter">
  <figcaption>
    <span>
      {stride > 1
        ? `Duration over time — one span in ${stride}, ${rows.length.toLocaleString()} dots`
        : `Duration over time — every matching span`}
      {#if bad.length}<span class="key error">● error</span>{/if}
      {#if dropped}<span class="dim"
          >({dropped.toLocaleString()} at 0 ms hidden by the log axis)</span
        >{/if}
    </span>
    <label>
      <input type="checkbox" bind:checked={logScale} />
      Log scale
    </label>
  </figcaption>
  <Plot
    height={220}
    marginLeft={56}
    x={{ type: "utc", grid: true, label: false }}
    y={{
      type: logScale ? "log" : "linear",
      grid: true,
      label: "ms",
      nice: true,
      zero: !logScale,
    }}
  >
    <Dot
      data={ok}
      x="t"
      y="ms"
      r={2.5}
      fill="#3f7fbf"
      fillOpacity={0.55}
      title="label"
      onclick={open}
    />
    <Dot
      data={bad}
      x="t"
      y="ms"
      r={3}
      fill="#d13438"
      fillOpacity={0.8}
      title="label"
      onclick={open}
    />
    <Dot
      data={pinned}
      x="t"
      y="ms"
      r={6}
      fill="#fff8e1"
      stroke="#e0a800"
      strokeWidth={2}
      title="label"
      onclick={open}
    />
  </Plot>
</figure>

<style>
  .scatter {
    margin: 0 0 1rem;
  }
  figcaption {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    font-size: 0.8rem;
    color: #666;
    margin-bottom: 0.25rem;
  }
  figcaption label {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    white-space: nowrap;
  }
  .key.error {
    color: #d13438;
    margin-left: 0.5rem;
  }
  .dim {
    margin-left: 0.5rem;
  }
  /* The dots are links in all but name. */
  .scatter :global(circle),
  .scatter :global(path) {
    cursor: pointer;
  }
</style>
