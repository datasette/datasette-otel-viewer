<script lang="ts">
  import { loadPageData } from "../../page_data/load.ts";
  import type { OtelIndexPageData } from "../../page_data/OtelIndexPageData.types.ts";

  // Landing page for /-/otel: two cards pointing at the traces and metrics
  // viewers, with the store's row counts so an empty instance is obvious.
  const data = loadPageData<OtelIndexPageData>();

  const fmt = new Intl.NumberFormat();
  const empty = $derived(
    data.span_count === 0 && data.metric_point_count === 0,
  );
</script>

<main class="index">
  <h1>OpenTelemetry</h1>
  <p class="intro">
    The traces and metrics this Datasette instance records about itself, stored
    in the <a href={`/${data.database}`}>{data.database}</a> database.
  </p>

  <div class="cards">
    <a class="card" href="/-/otel/traces">
      <h2>Traces &rarr;</h2>
      <p>Recent requests and the spans inside each one, as a waterfall.</p>
      <dl>
        <dt>Traces</dt>
        <dd>{fmt.format(data.trace_count)}</dd>
        <dt>Spans</dt>
        <dd>{fmt.format(data.span_count)}</dd>
      </dl>
    </a>
    <a class="card" href="/-/otel/metrics">
      <h2>Metrics &rarr;</h2>
      <p>Counters, gauges and histograms charted over time.</p>
      <dl>
        <dt>Metrics</dt>
        <dd>{fmt.format(data.metric_count)}</dd>
        <dt>Points</dt>
        <dd>{fmt.format(data.metric_point_count)}</dd>
      </dl>
    </a>
  </div>

  {#if empty}
    <p class="dim">
      Nothing recorded yet — make a few requests to this instance, then refresh.
    </p>
  {:else if data.services.length}
    <p class="dim">
      Services: {data.services.join(", ")}
    </p>
  {/if}

  <p class="dim raw-links">
    Raw tables:
    <a href={`/${data.database}/traces`}>traces</a>
    &middot;
    <a href={`/${data.database}/spans`}>spans</a>
    &middot;
    <a href={`/${data.database}/metrics`}>metrics</a>
    &middot;
    <a href={`/${data.database}/metric_points`}>metric_points</a>
  </p>
</main>

<style>
  h1 {
    margin: 0 0 0.5rem;
  }
  .intro {
    margin: 0 0 1.5rem;
    color: #555;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
    gap: 1rem;
    max-width: 48rem;
    margin-bottom: 1.5rem;
  }
  .card {
    display: block;
    padding: 1rem 1.25rem;
    border: 1px solid #ddd;
    border-radius: 6px;
    color: inherit;
    text-decoration: none;
    background: #fff;
  }
  .card:hover {
    border-color: #0b62a4;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  }
  .card h2 {
    margin: 0 0 0.25rem;
    font-size: 1.1rem;
    color: #0b62a4;
  }
  .card p {
    margin: 0 0 0.75rem;
    color: #555;
    font-size: 0.9rem;
  }
  .card dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.75rem;
    margin: 0;
    font-size: 0.85rem;
  }
  .card dt {
    color: #777;
  }
  .card dd {
    margin: 0;
    color: #222;
    font-variant-numeric: tabular-nums;
  }
  .raw-links {
    font-size: 0.85rem;
  }
</style>
