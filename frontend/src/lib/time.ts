/**
 * Relative/absolute time formatting for `start_ns` (nanoseconds since the
 * Unix epoch, OTLP's native unit -- see `types.ts`).
 */

const UNITS: [limit: number, divisor: number, suffix: string][] = [
  [60, 1, "s"],
  [60 * 60, 60, "m"],
  [60 * 60 * 24, 60 * 60, "h"],
  [Infinity, 60 * 60 * 24, "d"],
];

/** "3m ago" style relative time. Clamped to "just now" for <5s (including
 * slightly-in-the-future timestamps from clock skew between the browser
 * clock and this instance). */
export function formatRelativeTime(
  startNs: number,
  now: number = Date.now(),
): string {
  const startMs = startNs / 1e6;
  const diffSec = Math.max(0, Math.round((now - startMs) / 1000));
  if (diffSec < 5) return "just now";
  for (const [limit, divisor, suffix] of UNITS) {
    if (diffSec < limit) return `${Math.floor(diffSec / divisor)}${suffix} ago`;
  }
  return `${Math.floor(diffSec / (60 * 60 * 24))}d ago`;
}

/** Absolute local timestamp for a tooltip, e.g. "2026-08-03 14:05:12". */
export function formatAbsoluteTime(startNs: number): string {
  const date = new Date(startNs / 1e6);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** Absolute local timestamp with milliseconds, e.g.
 * "2026-08-03 14:05:12.345" -- the waterfall inspector's "exact times"
 * (ticket 08), where sub-second precision matters. */
export function formatAbsoluteTimePrecise(startNs: number): string {
  const startMs = startNs / 1e6;
  const millis = String(Math.floor(startMs) % 1000).padStart(3, "0");
  return `${formatAbsoluteTime(startNs)}.${millis}`;
}

/** A `duration_ms` value (real, from SQL aggregates -- may be null when
 * e.g. every span in a group lacked timing) for the analytics tables:
 * one decimal place, "—" for null. */
export function formatMs(ms: number | null): string {
  return ms == null ? "—" : ms.toFixed(1);
}

/** Duration label for a waterfall bar/row: microseconds below 1ms,
 * milliseconds otherwise (ticket 08). Takes nanoseconds so sub-millisecond
 * spans -- which round away to "0.0ms" if computed from the rounded
 * `duration_ms` column -- stay readable. */
export function formatDurationNs(durationNs: number): string {
  if (durationNs < 0) return "—";
  if (durationNs < 1_000_000) {
    return `${(durationNs / 1_000).toFixed(1)}µs`;
  }
  const ms = durationNs / 1e6;
  return `${ms.toFixed(ms < 10 ? 2 : 1)}ms`;
}
