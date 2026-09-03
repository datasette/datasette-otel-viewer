import { defineConfig } from "vitest/config";

// Pure-logic unit tests over the plain-TS modules in src/lib (no DOM, no
// component rendering), so the default node environment is enough.
export default defineConfig({
  test: {
    include: ["src/lib/*.test.ts"],
  },
});
