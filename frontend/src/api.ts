import createClient from "openapi-fetch";
import type { paths } from "../api.d.ts";

/** Typed client over the routes in datasette_otel_viewer/routes/api.py.
 * `paths` comes from `just types-routes` (router → OpenAPI →
 * openapi-typescript). */
export function makeClient() {
  return createClient<paths>({ baseUrl: "/" });
}

export type ApiClient = ReturnType<typeof makeClient>;
