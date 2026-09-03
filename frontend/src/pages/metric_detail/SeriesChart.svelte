<script lang="ts">
  import { Dot, Line, Plot } from "svelteplot";
  import type { LineRow } from "../../lib/metricsSeries.ts";

  // SveltePlot marks require `Record<string | symbol, RawValue>`, and a TS
  // interface never gets the implicit index signature that satisfies it — so
  // restate LineRow as a type alias and pass the rows through it.
  type PlotRow = { t: Date; value: number | null; series: string; seg: string };

  const { rows, yLabel }: { rows: LineRow[]; yLabel: string } = $props();

  const data = $derived(rows as PlotRow[]);
  // Dots mark the real observations; the null rows only exist so the line
  // breaks over gaps instead of interpolating across them.
  const present = $derived(data.filter((r) => r.value !== null));
</script>

<div class="chart" data-testid="series-chart">
  <Plot
    height={280}
    x={{ type: "utc", grid: true, label: false }}
    y={{ grid: true, label: yLabel || false, zero: true, nice: true }}
    color={{ legend: true }}
  >
    <!-- `z` splits the path per contiguous run (LineRow.seg) so a gap breaks
         the line even if the mark does not break on null itself; `stroke`
         still colours one shade per series. -->
    <Line {data} x="t" y="value" stroke="series" z="seg" />
    <Dot data={present} x="t" y="value" stroke="series" r={2} />
  </Plot>
</div>

<style>
  .chart {
    margin: 0 0 1.5rem;
  }
</style>
