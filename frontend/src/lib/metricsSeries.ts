/** Shape API series into rows for SveltePlot marks. */
import { fillGaps, rate } from "./metricsMath.ts";

export interface ApiSeries {
  key: string;
  service_name: string | null;
  attributes: Record<string, unknown>;
  points: {
    t: number;
    value?: number | null;
    count?: number | null;
    bucket_counts?: number[] | null;
  }[];
}

/** "svc · k=v, k2=v2" (or just "svc", or "all") for legends. */
export function seriesLabel(s: {
  service_name: string | null;
  attributes: Record<string, unknown>;
}): string {
  const attrs = Object.entries(s.attributes)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
  return [s.service_name ?? "", attrs].filter(Boolean).join(" · ") || "all";
}

export interface LineRow {
  t: Date;
  value: number | null;
  series: string;
  seg: string;
}

/** Rows for <Line>: one per bucket per series; gaps become null values.
 * `seg` changes after every gap so a `z` channel can split the path if the
 * mark does not break on null itself. With rateMode the cumulative values
 * are converted to per-second rates first. */
export function toLineRows(
  series: ApiSeries[],
  sinceNs: number,
  untilNs: number,
  stepS: number,
  rateMode: boolean,
): LineRow[] {
  const rows: LineRow[] = [];
  for (const s of series) {
    const label = seriesLabel(s);
    let pts = s.points.map((p) => ({ t: p.t, value: p.value ?? null }));
    if (rateMode) {
      const r = rate(
        pts.filter((p): p is { t: number; value: number } => p.value !== null),
      );
      pts = r;
    }
    let seg = 0;
    let prevNull = true;
    for (const p of fillGaps(pts, sinceNs, untilNs, stepS)) {
      if (p.value === null) {
        prevNull = true;
      } else if (prevNull) {
        seg++;
        prevNull = false;
      }
      rows.push({
        t: new Date(p.t / 1e6),
        value: p.value,
        series: label,
        seg: `${label}#${seg}`,
      });
    }
  }
  return rows;
}

export interface HeatCell {
  t: string;
  bucket: string;
  n: number;
}

/** Bucket label for bounds[i]: "≤0.005", the overflow bucket ">10". */
export function bucketLabel(bounds: number[], i: number): string {
  return i < bounds.length ? `≤${bounds[i]}` : `>${bounds[bounds.length - 1]}`;
}

/** Heatmap cells (band x = bucket start label, band y = bucket label) with
 * per-interval counts summed across the given series. */
export function toHeatCells(
  series: ApiSeries[],
  bounds: number[],
  formatT: (tNs: number) => string,
): HeatCell[] {
  const acc = new Map<string, HeatCell>();
  for (const s of series) {
    for (const p of s.points) {
      if (!p.bucket_counts) continue;
      const t = formatT(p.t);
      p.bucket_counts.forEach((n, i) => {
        const key = `${t}|${i}`;
        const cell = acc.get(key) ?? {
          t,
          bucket: bucketLabel(bounds, i),
          n: 0,
        };
        cell.n += n;
        acc.set(key, cell);
      });
    }
  }
  return [...acc.values()];
}
