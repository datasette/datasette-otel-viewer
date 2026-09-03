"""Pure metric math shared by the query API and page routes. Mirrors
frontend/src/lib/metricsMath.ts (same test vectors) — keep them in sync."""

from __future__ import annotations

import json
from collections.abc import Iterable

NS = 1_000_000_000


def bucket_start(time_ns: int, step_ns: int) -> int:
    "Floor time_ns to the step grid anchored at the Unix epoch."
    return (time_ns // step_ns) * step_ns


def series_key(
    service_name: str | None, attributes: dict, group_by: list[str] | None
) -> str:
    """Stable series identity. group_by=None keeps the whole attribute set
    (native OTel series identity); a list keeps only those keys; [] merges
    everything into one series per service."""
    if group_by is None:
        keep = attributes
    else:
        keep = {k: attributes[k] for k in group_by if k in attributes}
    return json.dumps(
        {"service": service_name, "attrs": keep}, sort_keys=True, default=str
    )


def counter_increment(prev: float | None, cur: float) -> float:
    "cur - prev, or cur after a reset (cur < prev) or when there is no prev."
    if prev is None or cur < prev:
        return cur
    return cur - prev


def rate(points: list[tuple[int, float]]) -> list[tuple[int, float | None]]:
    """[(time_ns, cumulative_value)] sorted by time -> [(time_ns, per_second)].
    First point has no rate (None)."""
    out: list[tuple[int, float | None]] = []
    prev_t = prev_v = None
    for t, v in points:
        if prev_t is None:
            out.append((t, None))
        else:
            dt = (t - prev_t) / NS
            out.append((t, counter_increment(prev_v, v) / dt if dt > 0 else None))
        prev_t, prev_v = t, v
    return out


def histogram_delta(prev: dict | None, cur: dict) -> dict:
    """Per-interval histogram from two cumulative points with identical
    explicit_bounds. Counts use counter_increment element-wise; sum likewise.
    prev=None (first point, or a reset detected on `count`) returns cur as-is."""
    if (
        prev is None
        or cur["count"] < prev["count"]
        or prev["explicit_bounds"] != cur["explicit_bounds"]
    ):
        return {**cur}
    return {
        **cur,
        "count": cur["count"] - prev["count"],
        "sum": None
        if cur.get("sum") is None or prev.get("sum") is None
        else cur["sum"] - prev["sum"],
        "bucket_counts": [
            c - p for c, p in zip(cur["bucket_counts"], prev["bucket_counts"])
        ],
        "min": None,  # min/max are not differentiable across intervals
        "max": None,
    }


def merge_histograms(points: Iterable[dict]) -> dict | None:
    "Element-wise sum of per-interval histograms sharing explicit_bounds."
    acc: dict | None = None
    for p in points:
        if acc is None:
            acc = {**p, "bucket_counts": list(p["bucket_counts"])}
            continue
        if acc["explicit_bounds"] != p["explicit_bounds"]:
            raise ValueError("mixed explicit_bounds in one series")
        acc["count"] += p["count"]
        acc["sum"] = (
            None
            if acc.get("sum") is None or p.get("sum") is None
            else acc["sum"] + p["sum"]
        )
        acc["bucket_counts"] = [
            a + b for a, b in zip(acc["bucket_counts"], p["bucket_counts"])
        ]
    return acc


def histogram_percentile(
    bounds: list[float],
    counts: list[int],
    q: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Estimate the q-quantile (0 < q <= 1) from explicit buckets.
    len(counts) == len(bounds) + 1; bucket i covers (bounds[i-1], bounds[i]],
    the last bucket is (bounds[-1], +inf). Linear interpolation within the
    bucket that contains rank q*total (Prometheus histogram_quantile rule)."""
    total = sum(counts)
    if total == 0 or not bounds:
        return None
    target = q * total
    cum = 0
    for i, c in enumerate(counts):
        prev_cum = cum
        cum += c
        if cum >= target and c > 0:
            if i == len(bounds):  # overflow bucket
                return maximum if maximum is not None else bounds[-1]
            upper = bounds[i]
            lower = (
                bounds[i - 1]
                if i > 0
                else (minimum if minimum is not None and minimum > 0 else 0.0)
            )
            return lower + (upper - lower) * ((target - prev_cum) / c)
    return maximum if maximum is not None else bounds[-1]


def last_per_bucket(points: list[dict], step_ns: int) -> dict[int, dict]:
    "points sorted by time_ns -> {bucket_start_ns: last point in that bucket}."
    out: dict[int, dict] = {}
    for p in points:
        out[bucket_start(p["time_ns"], step_ns)] = p
    return out


def aggregate_numbers(values: list[float], how: str) -> float:
    "how: 'sum' | 'avg' | 'last' | 'max'."
    if how == "sum":
        return sum(values)
    if how == "avg":
        return sum(values) / len(values)
    if how == "max":
        return max(values)
    return values[-1]
