import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync, writeFileSync } from "fs";
import { compile } from "json-schema-to-typescript";
import { glob } from "glob";

// Unique per plugin so several can run side by side (see the sibling
// plugins' Justfiles for the ports already taken).
const DEV_PORT = 5186;

export default defineConfig({
  server: {
    port: DEV_PORT,
    strictPort: true,
    cors: true,
    // No hmr host override: the client derives ws host/port from the URL the
    // /@vite/client script was loaded from (datasette-vite's dev_paths).
  },
  plugins: [
    svelte(),
    {
      // Compile the JSON Schemas that scripts/typegen-pagedata.py writes
      // into src/page_data/*.types.ts, on build and on change.
      name: "page-data-types",
      async buildStart() {
        const files = glob.sync("src/page_data/*_schema.json");
        for (const file of files) {
          const schema = JSON.parse(readFileSync(file, "utf-8"));
          const ts = await compile(schema, "");
          const outFile = file.replace("_schema.json", ".types.ts");
          writeFileSync(outFile, ts);
        }
      },
      async handleHotUpdate({ file, server }) {
        if (!file.endsWith("_schema.json")) return;
        const schema = JSON.parse(readFileSync(file, "utf-8"));
        const ts = await compile(schema, "");
        const outFile = file.replace("_schema.json", ".types.ts");
        writeFileSync(outFile, ts);
        const mod = server.moduleGraph.getModuleById(outFile);
        if (mod) {
          server.moduleGraph.invalidateModule(mod);
          return [mod];
        }
      },
    },
  ],
  build: {
    manifest: "manifest.json",
    outDir: "../datasette_otel_receiver",
    assetsDir: "static/gen",
    // The package dir also holds the Python sources: never wipe it.
    emptyOutDir: false,
    rollupOptions: {
      input: {
        traces_list: "src/pages/traces_list/index.ts",
        trace_detail: "src/pages/trace_detail/index.ts",
      },
    },
  },
});
