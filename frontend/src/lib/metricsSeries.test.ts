import { describe, expect, it } from "vitest";
import { NS } from "./metricsMath.ts";
import {
  bucketLabel,
  seriesLabel,
  toHeatCells,
  toLineRows,
  type ApiSeries,
} from "./metricsSeries.ts";

describe("seriesLabel", () => {
  it("joins service and attributes", () => {
    expect(seriesLabel({ service_name: "svc", attributes: { a: 1 } })).toBe(
      "svc · a=1",
    );
  });
  it('falls back to "all" with no service and no attributes', () => {
    expect(seriesLabel({ service_name: null, attributes: {} })).toBe("all");
  });
});

function series(points: ApiSeries["points"]): ApiSeries {
  return { key: "s1", service_name: "svc", attributes: {}, points };
}

describe("toLineRows", () => {
  it("fills gaps and starts a new seg after a gap", () => {
    const rows = toLineRows(
      [
        series([
          { t: 0, value: 1 },
          { t: 120 * NS, value: 2 },
        ]),
      ],
      0,
      180 * NS,
      60,
      false,
    );
    expect(rows).toHaveLength(3);
    expect(rows[1]!.value).toBeNull();
    expect(rows[0]!.value).toBe(1);
    expect(rows[2]!.value).toBe(2);
    expect(rows[0]!.seg).not.toBe(rows[2]!.seg);
  });

  it("converts cumulative values to a per-second rate in rate mode", () => {
    const rows = toLineRows(
      [
        series([
          { t: 0, value: 0 },
          { t: 60 * NS, value: 60 },
          { t: 120 * NS, value: 120 },
        ]),
      ],
      0,
      180 * NS,
      60,
      true,
    );
    expect(rows.map((r) => r.value)).toEqual([null, 1, 1]);
  });
});

describe("bucketLabel", () => {
  const bounds = [0.1, 0.5];
  it("labels an in-range bucket with its upper bound", () => {
    expect(bucketLabel(bounds, 0)).toBe("≤0.1");
  });
  it("labels the overflow bucket with the last bound", () => {
    expect(bucketLabel(bounds, 2)).toBe(">0.5");
  });
});

describe("toHeatCells", () => {
  it("sums bucket_counts across series for the same interval", () => {
    const bounds = [0.1, 0.5];
    const s1 = series([{ t: 0, bucket_counts: [1, 2, 0] }]);
    const s2 = series([{ t: 0, bucket_counts: [1, 0, 1] }]);
    const cells = toHeatCells([s1, s2], bounds, (t) => `t${t}`);
    const byBucket = Object.fromEntries(cells.map((c) => [c.bucket, c.n]));
    expect(byBucket["≤0.1"]).toBe(2);
    expect(byBucket["≤0.5"]).toBe(2);
    expect(byBucket[">0.5"]).toBe(1);
    expect(cells.every((c) => c.t === "t0")).toBe(true);
  });
});
