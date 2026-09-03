<script lang="ts">
  import type { components } from "../../../api.d.ts";
  import { makeClient } from "../../api.ts";
  import {
    RANGE_PRESETS,
    fillGaps,
    rangeBounds,
  } from "../../lib/metricsMath.ts";
  import {
    bucketLabel,
    toHeatCells,
    toLineRows,
  } from "../../lib/metricsSeries.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import type { MetricDetailPageData } from "../../page_data/MetricDetailPageData.types.ts";
  import HistogramHeatmap from "./HistogramHeatmap.svelte";
  import PercentileChart from "./PercentileChart.svelte";
  import SeriesChart from "./SeriesChart.svelte";

  type ApiSeries = components["schemas"]["Series"];
  type QueryResult = {
    since_ns: number;
    until_ns: number;
    step_s: number;
    explicit_bounds: number[] | null;
    series: ApiSeries[];
    truncated: boolean;
  };

  // The server embeds only the metric definition and the pickers' choices;
  // the series themselves are fetched here so the browser's clock decides the
  // time range (see routes/pages.py).
  const page = loadPageData<MetricDetailPageData>();
  const client = makeClient();
  const metric = page.metric;

  const isHistogram = metric.type === "histogram";
  const isCumulativeSum =
    metric.type === "sum" && metric.temporality === "cumulative";
  const isLineMetric = metric.type === "gauge" || metric.type === "sum";

  const initialService =
    new URLSearchParams(window.location.search).get("service") ?? "";

  let rangeSeconds = $state(3600);
  let service = $state(initialService);
  // splitAll → group_by: null, i.e. the stored series stay split by every
  // attribute; otherwise only the checked keys survive the merge.
  let splitAll = $state(true);
  let groupBy = $state<string[]>([]);
  let rateMode = $state(isCumulativeSum);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let result = $state<QueryResult | null>(null);

  let requestId = 0;

  async function refresh() {
    const id = ++requestId;
    loading = true;
    error = null;
    const bounds = rangeBounds(rangeSeconds, Date.now());
    const { data, error: apiError } = await client.POST(
      "/-/otel/api/metrics/query",
      {
        body: {
          name: metric.name,
          ...bounds,
          group_by: splitAll ? null : groupBy,
          service: service || null,
        },
      },
    );
    // A slower earlier request must not overwrite a newer answer.
    if (id !== requestId) return;
    if (apiError || !data) {
      error = apiError ? JSON.stringify(apiError) : "Request failed";
      result = null;
    } else {
      result = data;
    }
    loading = false;
  }

  // Spreading groupBy registers its contents (not just the binding) as a
  // dependency, so checking an attribute refetches. rateMode is deliberately
  // absent: it is applied client-side.
  const queryKey = $derived(
    JSON.stringify([rangeSeconds, service, splitAll, [...groupBy]]),
  );
  $effect(() => {
    void queryKey;
    void refresh();
  });

  function toggleKey(key: string) {
    groupBy = groupBy.includes(key)
      ? groupBy.filter((k) => k !== key)
      : [...groupBy, key];
  }

  /** UTC bucket label for the heatmap's band scale: seconds when the step is
   * sub-minute, a date prefix once the range spans more than one day, so the
   * labels stay unique and sort chronologically. */
  function bucketTimeLabel(tNs: number, stepS: number, range: number): string {
    const iso = new Date(tNs / 1e6).toISOString();
    const time = stepS < 60 ? iso.slice(11, 19) : iso.slice(11, 16);
    return range > 12 * 3600 ? `${iso.slice(5, 10)} ${time}` : time;
  }

  const lineRows = $derived(
    result && !isHistogram
      ? toLineRows(
          result.series,
          result.since_ns,
          result.until_ns,
          result.step_s,
          rateMode,
        )
      : [],
  );

  const heat = $derived.by(() => {
    const res = result;
    const bounds = res?.explicit_bounds;
    if (!res || !isHistogram || !bounds) return null;
    const label = (tNs: number) =>
      bucketTimeLabel(tNs, res.step_s, rangeSeconds);
    const buckets: string[] = [];
    for (let i = 0; i <= bounds.length; i++)
      buckets.push(bucketLabel(bounds, i));
    return {
      bounds,
      cells: toHeatCells(res.series, bounds, label),
      // The band domain comes from the full bucket grid (fillGaps over an
      // empty series) rather than from sorting the labels, which would only
      // order correctly within a single day.
      times: fillGaps<{ t: number }>(
        [],
        res.since_ns,
        res.until_ns,
        res.step_s,
      ).map((p) => label(p.t)),
      buckets: buckets.reverse(),
    };
  });

  const unit = $derived(metric.unit ?? "");
  const yLabel = $derived(rateMode && !isHistogram ? `${unit}/s` : unit);
  const hasSeries = $derived((result?.series.length ?? 0) > 0);
  const rawPointsUrl = $derived(
    `/${page.database}/metric_points?metric_name=${encodeURIComponent(metric.name)}&_sort_desc=time_ns`,
  );
</script>

<main class="metric">
  <a class="back-link" href="/-/otel/metrics">&larr; all metrics</a>
  <h1 class="mono">{metric.name}</h1>
  <p class="dim meta">
    {[metric.type, metric.temporality, unit].filter(Boolean).join(" \u00b7 ")}
  </p>
  {#if metric.description}
    <p class="dim">{metric.description}</p>
  {/if}

  <div class="controls">
    <div class="range" role="group" aria-label="Time range">
      {#each RANGE_PRESETS as preset (preset.seconds)}
        <button
          type="button"
          class:active={rangeSeconds === preset.seconds}
          onclick={() => (rangeSeconds = preset.seconds)}
        >
          {preset.label}
        </button>
      {/each}
    </div>

    <label>
      Service
      <select bind:value={service}>
        <option value="">All services</option>
        {#each page.services as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    {#if page.attribute_keys.length > 0}
      <fieldset class="group-by">
        <legend>Split by</legend>
        <label>
          <input type="checkbox" bind:checked={splitAll} /> every attribute
        </label>
        {#each page.attribute_keys as key (key)}
          <label>
            <input
              type="checkbox"
              checked={groupBy.includes(key)}
              disabled={splitAll}
              onchange={() => toggleKey(key)}
            />
            {key}
          </label>
        {/each}
      </fieldset>
    {/if}

    {#if isCumulativeSum}
      <label class="inline">
        <input type="checkbox" bind:checked={rateMode} /> per-second rate
      </label>
    {/if}

    <button type="button" onclick={refresh} disabled={loading}>
      {loading ? "Loading…" : "Refresh"}
    </button>
  </div>

  {#if error}
    <p class="error">Failed to load series: {error}</p>
  {/if}
  {#if result?.truncated}
    <p class="dim">
      Result truncated — narrow the time range or split by fewer attributes.
    </p>
  {/if}

  {#if loading && !result}
    <p class="empty">Loading…</p>
  {/if}

  {#if result}
    {#if !hasSeries}
      <p class="empty">No points for this metric in the selected range.</p>
    {:else if isHistogram}
      {#if heat}
        <HistogramHeatmap
          cells={heat.cells}
          times={heat.times}
          buckets={heat.buckets}
          {unit}
        />
        <PercentileChart
          series={result.series}
          bounds={heat.bounds}
          sinceNs={result.since_ns}
          untilNs={result.until_ns}
          stepS={result.step_s}
          {unit}
        />
      {:else}
        <p class="empty">
          These series use different bucket boundaries, so they cannot share a
          heatmap — filter to one service or split by fewer attributes.
        </p>
      {/if}
    {:else if isLineMetric}
      <SeriesChart rows={lineRows} {yLabel} />
    {:else}
      <p class="empty">
        {metric.type} metrics are stored but not charted yet — see the raw rows.
      </p>
    {/if}
  {/if}

  <p class="dim raw-links">
    <a href={rawPointsUrl}>Raw points &rarr;</a>
  </p>
</main>

<style>
  h1 {
    margin: 0.25rem 0 0.25rem;
    font-size: 1.4rem;
    word-break: break-all;
  }
  .back-link {
    color: #555;
    font-size: 0.85rem;
    text-decoration: none;
  }
  .back-link:hover {
    text-decoration: underline;
  }
  .meta {
    font-size: 0.85rem;
    margin: 0 0 0.5rem;
  }
  .controls {
    display: flex;
    align-items: flex-end;
    gap: 1rem;
    margin: 1rem 0;
    flex-wrap: wrap;
  }
  .controls label {
    display: flex;
    flex-direction: column;
    font-size: 0.8rem;
    color: #555;
    gap: 0.25rem;
  }
  .controls label.inline,
  .group-by label {
    flex-direction: row;
    align-items: center;
    gap: 0.35rem;
  }
  .controls select,
  .controls button {
    font-size: 0.9rem;
    padding: 0.35rem 0.5rem;
  }
  .range {
    display: flex;
    gap: 0.25rem;
  }
  .range button.active {
    background: #0b62a4;
    border-color: #0b62a4;
    color: #fff;
  }
  .group-by {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 0.25rem 0.6rem 0.5rem;
    margin: 0;
  }
  .group-by legend {
    font-size: 0.8rem;
    color: #555;
  }
  .error {
    color: #b00020;
  }
  .empty {
    color: #666;
    padding: 2rem 0;
  }
  .raw-links {
    margin-top: 1rem;
    font-size: 0.85rem;
  }
</style>
