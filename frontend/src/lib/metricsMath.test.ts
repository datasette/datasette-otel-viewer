import { describe, expect, it } from "vitest";
import {
  NS,
  bucketStart,
  fillGaps,
  histogramPercentile,
  rangeBounds,
  rate,
  stepForRange,
} from "./metricsMath.ts";

describe("stepForRange", () => {
  it.each([
    [900, 5],
    [3600, 30],
    [21600, 300], // 21600/60 = 360 > 180, 21600/300 = 72 <= 180
    [86400, 900],
    [259200, 1800],
  ])("stepForRange(%i) === %i", (rangeSeconds, expected) => {
    expect(stepForRange(rangeSeconds)).toBe(expected);
  });
});

describe("rangeBounds", () => {
  it("floors until to the step and derives since from the range", () => {
    const nowMs = Date.UTC(2026, 8, 1, 12, 0, 7);
    const { since_ns, until_ns, step_s } = rangeBounds(3600, nowMs);
    expect(step_s).toBe(30);
    const expectedUntilNs = Date.UTC(2026, 8, 1, 12, 0, 30) * 1e6;
    expect(until_ns).toBe(expectedUntilNs);
    expect(since_ns).toBe(expectedUntilNs - 3600 * NS);
  });
});

describe("bucketStart", () => {
  const stepNs = 60 * NS;
  it("floors to the enclosing 60s bucket", () => {
    const base = Date.UTC(2026, 8, 1, 12, 0, 0);
    expect(bucketStart(Date.UTC(2026, 8, 1, 12, 0, 5) * 1e6, stepNs)).toBe(
      base * 1e6,
    );
    expect(bucketStart(Date.UTC(2026, 8, 1, 12, 0, 59) * 1e6, stepNs)).toBe(
      base * 1e6,
    );
  });
  it("leaves an exact bucket start unchanged", () => {
    const exact = Date.UTC(2026, 8, 1, 12, 1, 0) * 1e6;
    expect(bucketStart(exact, stepNs)).toBe(exact);
  });
});

describe("rate", () => {
  it("computes per-second rate and handles a counter reset", () => {
    const points = [0, 60, 120, 180].map((s, i) => ({
      t: s * NS,
      value: [0, 60, 120, 30][i]!,
    }));
    const out = rate(points).map((p) => p.value);
    expect(out[0]).toBeNull();
    expect(out[1]).toBeCloseTo(1);
    expect(out[2]).toBeCloseTo(1);
    expect(out[3]).toBeCloseTo(0.5);
  });

  it("returns null for zero dt", () => {
    const out = rate([
      { t: 0, value: 5 },
      { t: 0, value: 10 },
    ]);
    expect(out[0]!.value).toBeNull();
    expect(out[1]!.value).toBeNull();
  });
});

describe("fillGaps", () => {
  it("fills a missing bucket with null and keeps present objects as-is", () => {
    const p0 = { t: 0, value: 5 };
    const p2 = { t: 120 * NS, value: 7 };
    const out = fillGaps([p0, p2], 0, 180 * NS, 60);
    expect(out).toHaveLength(3);
    expect(out[0]).toBe(p0);
    expect(out[1]).toEqual({ t: 60 * NS, value: null });
    expect(out[2]).toBe(p2);
  });
});

describe("histogramPercentile", () => {
  it("interpolates within a known distribution", () => {
    const bounds = [0.1, 0.5, 1];
    const counts = [10, 20, 10, 0];
    expect(histogramPercentile(bounds, counts, 0.5)).toBeCloseTo(0.3);
    expect(histogramPercentile(bounds, counts, 0.9)).toBeCloseTo(0.8);
    expect(histogramPercentile(bounds, counts, 0.99)).toBeCloseTo(0.98);
  });

  it("interpolates the first (0-origin) bucket, refined by min", () => {
    const bounds = [0.1, 0.5, 1];
    const counts = [10, 0, 0, 0];
    expect(histogramPercentile(bounds, counts, 0.5)).toBeCloseTo(0.05);
    expect(histogramPercentile(bounds, counts, 0.5, 0.02)).toBeCloseTo(0.06);
  });

  it("falls into the overflow bucket, refined by max", () => {
    const bounds = [0.1, 0.5, 1];
    const counts = [0, 0, 0, 10];
    expect(histogramPercentile(bounds, counts, 0.5)).toBe(1);
    expect(histogramPercentile(bounds, counts, 0.5, null, 4.2)).toBe(4.2);
  });

  it("returns null for an empty histogram", () => {
    expect(histogramPercentile([0.1, 0.5, 1], [0, 0, 0, 0], 0.5)).toBeNull();
  });

  it("matches Datasette's DURATION_BUCKETS", () => {
    // DURATION_BUCKETS from datasette.telemetry_registry (see
    // plans/metrics/notes.md); Python mirror (ticket 04) does not exist in
    // this tree yet, so this uses the bounds/counts from this ticket's own
    // table. NOTE: the ticket's table states p50 = 0.0075 (p99 ~= 0.0495),
    // but tracing the exact `histogramPercentile` algorithm above by hand
    // (and independently in Python) over these bounds/counts gives
    // p50 = 0.00625 and p99 = 0.048 -- asserted here instead. The bucket
    // crossed for p50 is [0.005, 0.01] with 4 of 10 samples already
    // accounted for below it: 0.005 + (0.01 - 0.005) * (5 - 4) / 4.
    const bounds = [
      0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10,
    ];
    const counts = [0, 0, 0, 4, 4, 2, 0, 0, 0, 0, 0, 0];
    expect(histogramPercentile(bounds, counts, 0.5)).toBeCloseTo(0.00625, 5);
    expect(histogramPercentile(bounds, counts, 0.99)).toBeCloseTo(0.048, 5);
  });
});
