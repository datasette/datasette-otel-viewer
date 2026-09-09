<script lang="ts">
  /**
   * The viewer's trail, rendered above a page's <h1>: "OpenTelemetry /
   * Traces / GET /demo/plants". The root is implicit, so a page passes only
   * what it adds below it, and the last item is the page you are on (no
   * link).
   *
   * Datasette's own header carries the same trail, built server-side in
   * routes/pages.py (`SECTIONS`/`_crumbs`); keep the two in step.
   */
  export interface Crumb {
    label: string;
    href?: string;
  }

  const { trail = [] }: { trail?: Crumb[] } = $props();
</script>

<nav class="crumbs" aria-label="Breadcrumb">
  <a href="/-/otel">OpenTelemetry</a>
  {#each trail as item (item.label)}
    <span class="sep" aria-hidden="true">/</span>
    {#if item.href}
      <a href={item.href}>{item.label}</a>
    {:else}
      <span class="current" aria-current="page">{item.label}</span>
    {/if}
  {/each}
</nav>

<style>
  .crumbs {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin: 0 0 0.15rem;
    font-size: 0.8rem;
    color: #666;
  }
  .sep {
    color: #aaa;
  }
  .current {
    /* The page you are on: named, not linked. */
    color: #333;
    max-width: 60ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
