<script lang="ts">
  import { Line, Plot } from "svelteplot";
  import type { components } from "../../../api.d.ts";
  import { fillGaps, histogramPercentile } from "../../lib/metricsMath.ts";
  import { seriesLabel } from "../../lib/metricsSeries.ts";

  type ApiSeries = components["schemas"]["Series"];

  const {
    series,
    bounds,
    sinceNs,
    untilNs,
    stepS,
    unit,
  }: {
    series: ApiSeries[];
    bounds: number[];
    sinceNs: number;
    untilNs: number;
    stepS: number;
    unit: string;
  } = $props();

  const DEFAULT_QUANTILES = ["0.5", "0.9", "0.99"];

  // The API returns per-bucket percentiles keyed by str(q); read the keys off
  // the data rather than assuming, and fall back to computing them from the
  // bucket counts when a response carries none.
  const quantiles = $derived.by(() => {
    for (const s of series) {
      for (const p of s.points) {
        const keys = Object.keys(p.percentiles ?? {});
        if (keys.length > 0) {
          return keys.sort((a, b) => Number(a) - Number(b));
        }
      }
    }
    return DEFAULT_QUANTILES;
  });

  function valueAt(
    point: ApiSeries["points"][number],
    q: string,
  ): number | null {
    const fromApi = point.percentiles?.[q];
    if (fromApi != null) return fromApi;
    if (!point.bucket_counts) return null;
    return histogramPercentile(bounds, point.bucket_counts, Number(q));
  }

  const rows = $derived(
    series.flatMap((s) => {
      const label = seriesLabel(s);
      const prefix = series.length > 1 ? `${label} ` : "";
      return quantiles.flatMap((q) => {
        const line = `${prefix}p${Math.round(Number(q) * 100)}`;
        const points = s.points.map((p) => ({ t: p.t, value: valueAt(p, q) }));
        return fillGaps(points, sinceNs, untilNs, stepS).map((p) => ({
          t: new Date(p.t / 1e6),
          value: p.value,
          line,
        }));
      });
    }),
  );

  const hasData = $derived(rows.some((r) => r.value !== null));
</script>

{#if hasData}
  <div class="chart" data-testid="percentile-chart">
    <Plot
      height={220}
      x={{ type: "utc", grid: true, label: false }}
      y={{ grid: true, label: unit || false, zero: true, nice: true }}
      color={{ legend: true }}
    >
      <Line data={rows} x="t" y="value" stroke="line" />
    </Plot>
  </div>
{:else}
  <p class="dim">No percentiles in this range.</p>
{/if}

<style>
  .chart {
    margin: 0 0 1.5rem;
  }
</style>
