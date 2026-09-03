// Programmatic doc screenshots of datasette-otel-viewer → docs/screenshots/*.png.
//
// SELF-CONTAINED: boots its own throwaway datasette on a fixed port with a
// fresh span store, seeded at startup by scripts/shots_plugins/seed.py (loaded
// with --plugins-dir), drives Playwright, then tears the server down. One
// command, reproducible — the committed PNGs only change when the UI actually
// changes.
//
// Output is committed; NOTES.md embeds these, so re-run + commit when the UI
// look changes:  `just shots`  (or a subset, e.g. `just shots trace`).
//
// Follows the datasette-plugin-screenshots skill (../datasette-plugin-
// screenshot-skill), including its --plugins-dir seed plugin. Differences from
// its template: no actor cookies (the viewer is opened with public_viewer for
// the shots), and a frozen browser clock instead of text rewriting (the list
// renders relative times from Date.now()). The seed plugin is pinned to the
// same instant via SHOTS_NOW.
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { mkdir, rm } from "node:fs/promises";
import { spawn } from "node:child_process";

// A free high port unique to this plugin (others: paper 8486, sheets 8487,
// town 8489, kanban 8493).
const PORT = Number(process.env.SHOTS_PORT || 8494);
const BASE = `http://localhost:${PORT}`;
const TRACES_URL = `${BASE}/-/otel/traces`;
const LIST_API_URL = `${BASE}/-/otel/api/traces/list`;
// Fresh scratch store every run (never the repo-root otel.db `just dev` uses).
const OTEL_DB = join(tmpdir(), "datasette-otel-viewer-shots.db");

const HERE = dirname(fileURLToPath(import.meta.url)); // frontend/scripts
const OUT = resolve(HERE, "../../docs/screenshots");
const SEED_DIR = resolve(HERE, "../../scripts/shots_plugins");

const VIEWPORT = { width: 1200, height: 760 };
// The seeded spans sit just before this instant, so "Started" reads as
// seconds/minutes ago no matter when the shots are regenerated. Passed to the
// seed plugin as SHOTS_NOW and to the browser as a fixed clock.
const NOW = new Date("2026-09-01T12:00:00Z");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
async function reachable() {
  try {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 500);
    const r = await fetch(TRACES_URL, {
      redirect: "manual",
      signal: ac.signal,
    });
    clearTimeout(t);
    return r.status < 500;
  } catch {
    return false;
  }
}

async function startServer() {
  if (await reachable()) {
    throw new Error(
      `something is already serving on ${BASE}. Stop it (or set SHOTS_PORT) and retry.`,
    );
  }
  await rm(OTEL_DB, { force: true });
  // `detached: true` puts datasette in its own process group. datasette is a
  // grandchild of `uv run`, so stopServer kills the whole group.
  const child = spawn(
    "uv",
    [
      "run",
      "datasette",
      "--memory",
      "-p",
      String(PORT),
      // Deterministic traces + metrics, inserted through the store's own
      // insert functions in the seed plugin's startup hook.
      "--plugins-dir",
      SEED_DIR,
      "-s",
      "plugins.datasette-otel-viewer.db_path",
      OTEL_DB,
      "-s",
      "plugins.datasette-otel-viewer.public_viewer",
      "true",
      // The instance's own spans would differ run to run: seed rows only.
      "-s",
      "plugins.datasette-otel-viewer.self_traces",
      "false",
      // The metrics seed uses a fixed browser clock (NOW) but the store
      // prunes by real wall-clock time; keep the retention window huge so
      // seeded points survive no matter when the shots are regenerated.
      "-s",
      "plugins.datasette-otel-viewer.retention_hours",
      "876000",
    ],
    {
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
      env: {
        ...process.env,
        PYTHONHASHSEED: "0",
        SHOTS_NOW: NOW.toISOString(),
      },
    },
  );
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));

  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(
        `datasette exited early (code ${child.exitCode}):\n${log}`,
      );
    }
    if (await reachable()) return child;
    await sleep(250);
  }
  stopServer(child);
  throw new Error(`datasette never came up on ${BASE}:\n${log}`);
}

// Kill the server's whole process group (datasette is uv's child). Idempotent.
function stopServer(child) {
  if (!child || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch {
    try {
      child.kill("SIGKILL");
    } catch {
      // already gone
    }
  }
}

// ---------------------------------------------------------------------------
// The seed lives in scripts/shots_plugins/seed.py (loaded above with
// --plugins-dir): fixed span ids and timestamps relative to SHOTS_NOW, so the
// store — and every pixel — is identical run to run. These are the ids the
// trace shot deep-links to: trace A, and its slow `db.query` child.
const IDS = {
  datasetteTraceId: "a1".padStart(32, "0"),
  slowQuerySpanId: "1003".padStart(16, "0"),
};
const SEEDED_TRACES = 4;

// The startup hook finishes before the server accepts connections, so the
// rows are already there by the time reachable() succeeds — but poll anyway
// rather than screenshot an empty list if that ever stops being true.
async function waitForSeed() {
  const deadline = Date.now() + 30_000;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const r = await fetch(LIST_API_URL, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await r.text();
      if (r.ok) {
        const traces = JSON.parse(body).traces ?? [];
        if (traces.length >= SEEDED_TRACES) return;
        last = `only ${traces.length} trace(s)`;
      } else {
        last = `${r.status} ${body}`;
      }
    } catch (err) {
      last = String(err);
    }
    await sleep(250);
  }
  throw new Error(
    `seed plugin never produced ${SEEDED_TRACES} traces: ${last}`,
  );
}

// ---------------------------------------------------------------------------
// Kill carets/transitions and hide dev-only widgets so a re-run with no UI
// change produces no binary diff.
const STABILITY_CSS = `*, *::before, *::after {
  caret-color: transparent !important;
  transition: none !important;
  animation: none !important;
}
#datasette-debug-bar { display: none !important; }`;

async function makeContext(browser, viewport = VIEWPORT) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    timezoneId: "UTC",
    colorScheme: "light",
  });
  await ctx.addInitScript((css) => {
    const inject = () => {
      if (document.getElementById("__shots_stability")) return;
      const s = document.createElement("style");
      s.id = "__shots_stability";
      s.textContent = css;
      (document.head || document.documentElement).appendChild(s);
    };
    inject();
    document.addEventListener("DOMContentLoaded", inject);
  }, STABILITY_CSS);
  return ctx;
}

async function newPage(browser) {
  const ctx = await makeContext(browser);
  const page = await ctx.newPage();
  // Relative "Started" cells derive from Date.now(); pin it.
  await page.clock.setFixedTime(NOW);
  return { ctx, page };
}

// ---------------------------------------------------------------------------
function buildShots(browser, ids) {
  const out = (n) => resolve(OUT, `${n}.png`);

  return {
    // The traces list: four traces from the one service, one errored.
    traces: async () => {
      const { ctx, page } = await newPage(browser);
      await page.goto(TRACES_URL);
      await page
        .locator("main.traces tbody tr.row-link")
        .first()
        .waitFor({ timeout: 15_000 });
      await page.screenshot({ path: out("traces") });
      await ctx.close();
    },

    // The waterfall for the Datasette request, with the slow query selected
    // so the inspector (attributes incl. db.query.text) is open.
    trace: async () => {
      const { ctx, page } = await newPage(browser);
      await page.goto(
        `${TRACES_URL}/${ids.datasetteTraceId}#span-${ids.slowQuerySpanId}`,
      );
      await page.locator("aside.inspector").waitFor({ timeout: 15_000 });
      await page.screenshot({ path: out("trace") });
      await ctx.close();
    },

    // The metrics list: a gauge, a counter and a histogram, all from the
    // one service.
    metrics: async () => {
      const { ctx, page } = await newPage(browser);
      await page.goto(`${BASE}/-/otel/metrics`);
      await page
        .locator("main.metrics tbody tr.row-link")
        .first()
        .waitFor({ timeout: 15_000 });
      await page.screenshot({ path: out("metrics") });
      await ctx.close();
    },

    // The histogram detail: heatmap + percentile chart for the last hour.
    // Each SveltePlot <Plot> renders its own legend as a small nested
    // svg (inside .plot-header .color-legend), so scope to the chart's own
    // top-level .plot-body > svg rather than matching both.
    metric: async () => {
      const { ctx, page } = await newPage(browser);
      await page.goto(`${BASE}/-/otel/metrics/db.client.operation.duration`);
      await page
        .locator(
          '[data-testid="histogram-heatmap"] > figure.svelteplot > div.plot-body > svg',
        )
        .waitFor({ timeout: 15_000 });
      await page
        .locator(
          '[data-testid="percentile-chart"] > figure.svelteplot > div.plot-body > svg',
        )
        .waitFor({ timeout: 15_000 });
      await page.screenshot({ path: out("metric"), fullPage: true });
      await ctx.close();
    },
  };
}

// ---------------------------------------------------------------------------
async function main() {
  const requested = new Set(process.argv.slice(2));

  await mkdir(OUT, { recursive: true });
  console.log(`booting datasette on ${BASE} …`);
  const server = await startServer();
  const onSignal = () => {
    stopServer(server);
    process.exit(130);
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  const browser = await chromium.launch();
  try {
    await waitForSeed();
    const shotsByName = buildShots(browser, IDS);
    const names = Object.keys(shotsByName);
    const unknown = [...requested].filter((n) => !names.includes(n));
    if (unknown.length) {
      throw new Error(
        `unknown shot(s): ${unknown.join(", ")} (have: ${names.join(", ")})`,
      );
    }
    const todo = requested.size ? names.filter((n) => requested.has(n)) : names;

    for (const name of todo) {
      await shotsByName[name]();
      console.log(`✓ ${name} → ${resolve(OUT, name + ".png")}`);
    }
  } finally {
    await browser.close();
    stopServer(server);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
