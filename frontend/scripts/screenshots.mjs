// Programmatic doc screenshots of datasette-otel-receiver → docs/screenshots/*.png.
//
// SELF-CONTAINED: boots its own throwaway datasette on a fixed port with a
// fresh span store, seeds deterministic traces over the plugin's own OTLP/JSON
// ingest endpoint, drives Playwright, then tears the server down. One command,
// reproducible — the committed PNGs only change when the UI actually changes.
//
// Output is committed; NOTES.md embeds these, so re-run + commit when the UI
// look changes:  `just shots`  (or a subset, e.g. `just shots trace`).
//
// Follows the datasette-plugin-screenshots skill (../datasette-plugin-
// screenshot-skill). Differences from its template, all because this plugin
// is simpler than the acl-backed ones: no actor cookies (the viewer is opened
// with public_viewer for the shots), no --plugins-dir seed plugin (seeding is
// a POST to /v1/traces), and a frozen browser clock instead of text rewriting
// (the list renders relative times from Date.now()).
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
const INGEST_TOKEN = "shots-token";
// Fresh scratch store every run (never the repo-root otel.db `just dev` uses).
const OTEL_DB = join(tmpdir(), "datasette-otel-receiver-shots.db");

const HERE = dirname(fileURLToPath(import.meta.url)); // frontend/scripts
const OUT = resolve(HERE, "../../docs/screenshots");

const VIEWPORT = { width: 1200, height: 760 };
// The seeded spans sit just before this instant, so "Started" reads as
// seconds/minutes ago no matter when the shots are regenerated.
const NOW = new Date("2026-09-01T12:00:00Z");
const NOW_NS = BigInt(NOW.getTime()) * 1_000_000n;
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
      "-s",
      "plugins.datasette-otel-receiver.db_path",
      OTEL_DB,
      "-s",
      "plugins.datasette-otel-receiver.ingest_token",
      INGEST_TOKEN,
      "-s",
      "plugins.datasette-otel-receiver.public_viewer",
      "true",
      // The instance's own spans would differ run to run: receiver-only.
      "-s",
      "plugins.datasette-otel-receiver.self_traces",
      "false",
    ],
    {
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
      env: { ...process.env, PYTHONHASHSEED: "0" },
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
// Seed: three traces from three services, as one OTLP/JSON export. Ids and
// timestamps are fixed so the store — and every pixel — is identical run to
// run. Times are nanoseconds relative to NOW.
const hex = (n, width) => n.toString(16).padStart(width, "0");
let nextSpan = 0x1000;

function span({
  trace,
  parent,
  name,
  kind = 1,
  start,
  dur,
  attrs = {},
  error,
}) {
  const id = hex(nextSpan++, 16);
  const startNs = NOW_NS - BigInt(Math.round(start * 1e6));
  const endNs = startNs + BigInt(Math.round(dur * 1e6));
  const attributes = Object.entries(attrs).map(([key, value]) => ({
    key,
    value:
      typeof value === "number"
        ? { intValue: String(value) }
        : { stringValue: value },
  }));
  return {
    id,
    body: {
      traceId: trace,
      spanId: id,
      ...(parent ? { parentSpanId: parent } : {}),
      name,
      kind,
      startTimeUnixNano: String(startNs),
      endTimeUnixNano: String(endNs),
      attributes,
      status: error ? { code: 2, message: error } : { code: 0 },
    },
  };
}

// `start` is "ms before NOW" (so bigger = earlier), `dur` is ms.
function datasetteTrace() {
  const trace = hex(0xa1, 32);
  const root = span({
    trace,
    name: "GET /{database}/{table}",
    kind: 2,
    start: 45_000,
    dur: 38.4,
    attrs: {
      "http.request.method": "GET",
      "url.path": "/demo/plants",
      "http.route": "/{database}/{table}",
      "http.response.status_code": 200,
    },
  });
  const view = span({
    trace,
    parent: root.id,
    name: "datasette.view.table",
    start: 44_998.8,
    dur: 35.1,
    attrs: { "datasette.database": "demo", "datasette.table": "plants" },
  });
  const q1 = span({
    trace,
    parent: view.id,
    name: "db.query",
    kind: 3,
    start: 44_996,
    dur: 4.2,
    attrs: {
      "db.system.name": "sqlite",
      "db.namespace": "demo",
      "db.operation.name": "SELECT",
      "db.query.text": "select count(*) from [plants]",
    },
  });
  const q2 = span({
    trace,
    parent: view.id,
    name: "db.query",
    kind: 3,
    start: 44_991,
    dur: 21.7,
    attrs: {
      "db.system.name": "sqlite",
      "db.namespace": "demo",
      "db.operation.name": "SELECT",
      "db.query.text":
        "select id, name, height_cm from [plants] order by id limit 101",
    },
  });
  const facet = span({
    trace,
    parent: view.id,
    name: "db.query",
    kind: 3,
    start: 44_968,
    dur: 6.3,
    attrs: {
      "db.system.name": "sqlite",
      "db.namespace": "demo",
      "db.operation.name": "SELECT",
      "db.query.text":
        "select height_cm as value, count(*) as count from [plants] group by height_cm order by count desc limit 31",
    },
  });
  const render = span({
    trace,
    parent: root.id,
    name: "datasette.render_template",
    start: 44_963,
    dur: 2.9,
    attrs: { "datasette.template": "table.html" },
  });
  return [root, view, q1, q2, facet, render];
}

function flaskTrace() {
  const trace = hex(0xb2, 32);
  const root = span({
    trace,
    name: "POST /api/orders",
    kind: 2,
    start: 190_000,
    dur: 212.6,
    attrs: {
      "http.request.method": "POST",
      "url.path": "/api/orders",
      "http.route": "/api/orders",
      "http.response.status_code": 500,
    },
    error: "IntegrityError: UNIQUE constraint failed: orders.sku",
  });
  const validate = span({
    trace,
    parent: root.id,
    name: "validate_order",
    start: 189_999,
    dur: 1.4,
  });
  const insert = span({
    trace,
    parent: root.id,
    name: "INSERT orders",
    kind: 3,
    start: 189_997,
    dur: 208.9,
    attrs: {
      "db.system.name": "sqlite",
      "db.operation.name": "INSERT",
      "db.query.text": "INSERT INTO orders (sku, qty) VALUES (?, ?)",
    },
    error: "UNIQUE constraint failed: orders.sku",
  });
  return [root, validate, insert];
}

function denoTrace() {
  const trace = hex(0xc3, 32);
  const root = span({
    trace,
    name: "GET /healthz",
    kind: 2,
    start: 610_000,
    dur: 0.8,
    attrs: {
      "http.request.method": "GET",
      "url.path": "/healthz",
      "http.response.status_code": 200,
    },
  });
  return [root];
}

function resourceSpans(service, spans, extra = {}) {
  return {
    resource: {
      attributes: Object.entries({ "service.name": service, ...extra }).map(
        ([key, value]) => ({ key, value: { stringValue: value } }),
      ),
    },
    scopeSpans: [
      {
        scope: { name: "screenshots", version: "0" },
        spans: spans.map((s) => s.body),
      },
    ],
  };
}

async function seed() {
  const datasette = datasetteTrace();
  const body = {
    resourceSpans: [
      resourceSpans("datasette", datasette, { "service.version": "1.0a39" }),
      resourceSpans("flask-app", flaskTrace()),
      resourceSpans("deno-service", denoTrace()),
    ],
  };
  const r = await fetch(`${BASE}/v1/traces`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${INGEST_TOKEN}`,
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`seed failed: ${r.status} ${await r.text()}`);
  return {
    datasetteTraceId: datasette[0].body.traceId,
    slowQuerySpanId: datasette[3].id,
  };
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
    // The traces list: three services, one errored trace.
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
    const ids = await seed();
    const shotsByName = buildShots(browser, ids);
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
