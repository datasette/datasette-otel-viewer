<script lang="ts">
  import { Cell, Plot } from "svelteplot";
  import type { HeatCell } from "../../lib/metricsSeries.ts";

  // SveltePlot marks require `Record<string | symbol, RawValue>`, and a TS
  // interface never gets the implicit index signature that satisfies it — so
  // restate HeatCell as a type alias and pass the cells through it.
  type PlotCell = { t: string; bucket: string; n: number };

  const {
    cells,
    times,
    buckets,
    unit,
  }: {
    cells: HeatCell[];
    /** x band domain: every bucket start in the range, in order. */
    times: string[];
    /** y band domain: bucket labels, largest first (band y draws the first
     * domain entry at the top). */
    buckets: string[];
    unit: string;
  } = $props();

  // With ~180 columns every tick would overlap, so thin them out to a
  // readable handful and let the band scale keep the cells aligned.
  const tickEvery = $derived(Math.max(1, Math.ceil(times.length / 12)));
  const ticks = $derived(times.filter((_, i) => i % tickEvery === 0));
  const data = $derived(cells as PlotCell[]);
</script>

<div class="chart" data-testid="histogram-heatmap">
  <Plot
    height={320}
    marginLeft={90}
    marginBottom={60}
    padding={0}
    x={{ type: "band", domain: times, ticks, label: false, tickRotate: -45 }}
    y={{ type: "band", domain: buckets, label: unit || "bucket" }}
    color={{ type: "linear", scheme: "blues", legend: true, label: "count" }}
  >
    <Cell {data} x="t" y="bucket" fill="n" inset={0.5} />
  </Plot>
</div>

<style>
  .chart {
    margin: 0 0 1.5rem;
  }
</style>
