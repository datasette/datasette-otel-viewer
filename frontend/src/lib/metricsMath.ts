/** Pure metric math for the metrics pages. Mirrors
 * datasette_otel_viewer/metrics_math.py (same test vectors) — keep in sync. */

export const NS = 1_000_000_000;

/** Time-range presets shown by the picker, in seconds. */
export const RANGE_PRESETS: { label: string; seconds: number }[] = [
  { label: "15m", seconds: 15 * 60 },
  { label: "1h", seconds: 3600 },
  { label: "6h", seconds: 6 * 3600 },
  { label: "24h", seconds: 24 * 3600 },
  { label: "72h", seconds: 72 * 3600 },
];

const STEP_CHOICES = [5, 15, 30, 60, 300, 900, 1800, 3600];

/** Bucket width for a range: the smallest step giving <= ~180 buckets. */
export function stepForRange(rangeSeconds: number, maxBuckets = 180): number {
  for (const s of STEP_CHOICES) {
    if (rangeSeconds / s <= maxBuckets) return s;
  }
  return STEP_CHOICES[STEP_CHOICES.length - 1]!;
}

/** since/until in ns for a preset, from a ms clock (Date.now() in prod,
 * a fixed value in tests). until is floored to the step so the newest
 * bucket is complete-ish and stable between refreshes. */
export function rangeBounds(
  rangeSeconds: number,
  nowMs: number,
): { since_ns: number; until_ns: number; step_s: number } {
  const step = stepForRange(rangeSeconds);
  const stepNs = BigInt(step) * BigInt(NS);
  const untilNs =
    ((BigInt(Math.floor(nowMs)) * 1_000_000n) / stepNs) * stepNs + stepNs;
  const sinceNs = untilNs - BigInt(rangeSeconds) * BigInt(NS);
  return { since_ns: Number(sinceNs), until_ns: Number(untilNs), step_s: step };
}

/** Start of the bucket containing timeNs, given a bucket width in ns. */
export function bucketStart(timeNs: number, stepNs: number): number {
  return Math.floor(timeNs / stepNs) * stepNs;
}

/** cur - prev, or cur after a reset (cur < prev) / with no prev. */
export function counterIncrement(prev: number | null, cur: number): number {
  return prev === null || cur < prev ? cur : cur - prev;
}

/** Per-second rate from cumulative (t ns, value) pairs sorted by t. First
 * point → null. Zero dt → null. */
export function rate(
  points: { t: number; value: number }[],
): { t: number; value: number | null }[] {
  const out: { t: number; value: number | null }[] = [];
  let prev: { t: number; value: number } | null = null;
  for (const p of points) {
    if (prev === null) out.push({ t: p.t, value: null });
    else {
      const dt = (p.t - prev.t) / NS;
      out.push({
        t: p.t,
        value: dt > 0 ? counterIncrement(prev.value, p.value) / dt : null,
      });
    }
    prev = p;
  }
  return out;
}

/** Fill the [since, until) grid with nulls where a series has no bucket, so
 * the line breaks instead of interpolating across gaps. */
export function fillGaps<T extends { t: number }>(
  points: T[],
  sinceNs: number,
  untilNs: number,
  stepS: number,
): (T | { t: number; value: null })[] {
  const stepNs = stepS * NS;
  const byT = new Map(points.map((p) => [p.t, p]));
  const out: (T | { t: number; value: null })[] = [];
  for (let t = bucketStart(sinceNs, stepNs); t < untilNs; t += stepNs) {
    out.push(byT.get(t) ?? { t, value: null });
  }
  return out;
}

/** Prometheus histogram_quantile rule over explicit buckets;
 * counts.length === bounds.length + 1 (the last count is the overflow
 * bucket, > bounds[bounds.length - 1]). Linear interpolation within the
 * bucket that crosses the target rank; `min`/`max` (when known, e.g. from
 * an exemplar or a fixed instrumentation range) refine the open-ended
 * first/last buckets instead of assuming the bound itself. */
export function histogramPercentile(
  bounds: number[],
  counts: number[],
  q: number,
  min: number | null = null,
  max: number | null = null,
): number | null {
  const total = counts.reduce((a, b) => a + b, 0);
  if (total === 0 || bounds.length === 0) return null;
  const target = q * total;
  let cum = 0;
  for (let i = 0; i < counts.length; i++) {
    const c = counts[i]!;
    const prevCum = cum;
    cum += c;
    if (cum >= target && c > 0) {
      if (i === bounds.length) return max ?? bounds[bounds.length - 1]!;
      const upper = bounds[i]!;
      const lower = i > 0 ? bounds[i - 1]! : min !== null && min > 0 ? min : 0;
      return lower + (upper - lower) * ((target - prevCum) / c);
    }
  }
  return max ?? bounds[bounds.length - 1]!;
}
