"""Tests for datasette_otel_receiver.metrics_math. These vectors are mirrored
in the frontend's metricsMath.test.ts (ticket 07) so both implementations
agree."""

import pytest

from datasette_otel_receiver.metrics_math import (
    aggregate_numbers,
    bucket_start,
    counter_increment,
    histogram_delta,
    histogram_percentile,
    last_per_bucket,
    merge_histograms,
    rate,
    series_key,
)

NS = 1_000_000_000


def test_bucket_start_aligns_to_epoch_grid():
    step_ns = 60 * NS
    t_12_00_05 = 12 * 3600 * NS + 5 * NS
    t_12_00_59 = 12 * 3600 * NS + 59 * NS
    t_12_01_00 = 12 * 3600 * NS + 60 * NS
    expected_bucket = 12 * 3600 * NS
    assert bucket_start(t_12_00_05, step_ns) == expected_bucket
    assert bucket_start(t_12_00_59, step_ns) == expected_bucket
    assert bucket_start(t_12_01_00, step_ns) == expected_bucket + step_ns


def test_series_key_full_partial_none():
    attrs = {"a": 1, "b": 2}
    attrs_reordered = {"b": 2, "a": 1}

    full = series_key("svc", attrs, None)
    partial = series_key("svc", attrs, ["a"])
    merged = series_key("svc", attrs, [])

    assert full != partial
    assert partial != merged
    assert "b" in full and "a" in full
    assert "a" in partial and "b" not in partial
    assert (
        '"attrs": {}' in merged or merged.count('"a"') == 1
    )  # only the service key mentions "a"

    # Deterministic regardless of dict insertion order.
    assert series_key("svc", attrs, None) == series_key("svc", attrs_reordered, None)
    assert series_key("svc", attrs, ["a"]) == series_key("svc", attrs_reordered, ["a"])


def test_counter_increment():
    assert counter_increment(None, 5) == 5
    assert counter_increment(10, 15) == 5
    assert counter_increment(10, 3) == 3  # reset: cur < prev


def test_rate_basic_and_reset():
    points = [(0, 0), (60 * NS, 60), (120 * NS, 120), (180 * NS, 30)]
    result = rate(points)
    assert result == [
        (0, None),
        (60 * NS, 1.0),
        (120 * NS, 1.0),
        (180 * NS, 0.5),
    ]


def test_rate_zero_dt():
    points = [(0, 5), (0, 10)]
    result = rate(points)
    assert result[0] == (0, None)
    assert result[1] == (0, None)


def test_histogram_delta():
    prev = {
        "explicit_bounds": [1, 2],
        "count": 10,
        "bucket_counts": [5, 5, 0],
        "sum": 1.0,
    }
    cur = {
        "explicit_bounds": [1, 2],
        "count": 16,
        "bucket_counts": [7, 8, 1],
        "sum": 2.5,
    }
    delta = histogram_delta(prev, cur)
    assert delta["count"] == 6
    assert delta["bucket_counts"] == [2, 3, 1]
    assert delta["sum"] == pytest.approx(1.5)
    assert delta["min"] is None
    assert delta["max"] is None


def test_histogram_delta_reset_returns_cur():
    prev = {
        "explicit_bounds": [1, 2],
        "count": 20,
        "bucket_counts": [10, 10, 0],
        "sum": 5.0,
    }
    cur = {
        "explicit_bounds": [1, 2],
        "count": 5,
        "bucket_counts": [2, 2, 1],
        "sum": 1.0,
    }
    assert histogram_delta(prev, cur) == {**cur}


def test_histogram_delta_bounds_changed_returns_cur():
    prev = {
        "explicit_bounds": [1, 2],
        "count": 10,
        "bucket_counts": [5, 5, 0],
        "sum": 1.0,
    }
    cur = {
        "explicit_bounds": [1, 3],
        "count": 16,
        "bucket_counts": [7, 8, 1],
        "sum": 2.5,
    }
    assert histogram_delta(prev, cur) == {**cur}


def test_histogram_delta_first_point_returns_cur():
    cur = {
        "explicit_bounds": [1, 2],
        "count": 16,
        "bucket_counts": [7, 8, 1],
        "sum": 2.5,
    }
    assert histogram_delta(None, cur) == {**cur}


def test_merge_histograms():
    h1 = {
        "explicit_bounds": [1, 2],
        "count": 10,
        "bucket_counts": [5, 5, 0],
        "sum": 1.0,
    }
    h2 = {"explicit_bounds": [1, 2], "count": 6, "bucket_counts": [1, 2, 3], "sum": 0.5}
    merged = merge_histograms([h1, h2])
    assert merged["bucket_counts"] == [6, 7, 3]
    assert merged["count"] == 16
    assert merged["sum"] == pytest.approx(1.5)

    h3 = {"explicit_bounds": [1, 3], "count": 4, "bucket_counts": [1, 1, 2], "sum": 0.2}
    with pytest.raises(ValueError):
        merge_histograms([h1, h3])


def test_merge_histograms_empty():
    assert merge_histograms([]) is None


def test_percentile_known_distribution():
    bounds = [0.1, 0.5, 1]
    counts = [10, 20, 10, 0]
    assert histogram_percentile(bounds, counts, 0.5) == pytest.approx(0.3)
    assert histogram_percentile(bounds, counts, 0.9) == pytest.approx(0.8)
    assert histogram_percentile(bounds, counts, 0.99) == pytest.approx(0.98)


def test_percentile_first_bucket_uses_zero_or_min():
    bounds = [0.1, 0.5, 1]
    counts = [10, 0, 0, 0]
    assert histogram_percentile(bounds, counts, 0.5) == pytest.approx(0.05)
    assert histogram_percentile(bounds, counts, 0.5, minimum=0.02) == pytest.approx(
        0.06
    )


def test_percentile_overflow_bucket():
    bounds = [0.1, 0.5, 1]
    counts = [0, 0, 0, 10]
    assert histogram_percentile(bounds, counts, 0.5) == pytest.approx(1.0)
    assert histogram_percentile(bounds, counts, 0.5, maximum=4.2) == pytest.approx(4.2)


def test_percentile_empty():
    bounds = [0.1, 0.5, 1]
    counts = [0, 0, 0, 0]
    assert histogram_percentile(bounds, counts, 0.5) is None


def test_percentile_datasette_duration_buckets():
    from datasette.telemetry_registry import DURATION_BUCKETS

    bounds = list(DURATION_BUCKETS)
    counts = [0, 0, 0, 4, 4, 2, 0, 0, 0, 0, 0, 0]
    assert len(counts) == len(bounds) + 1

    # p50 lands in the (0.005, 0.01] bucket: 0.005 + (0.01-0.005) * (5-4)/4
    assert histogram_percentile(bounds, counts, 0.5) == pytest.approx(0.00625)
    # p99 lands in the (0.01, 0.05] bucket: 0.01 + (0.05-0.01) * (9.9-8)/2
    assert histogram_percentile(bounds, counts, 0.99) == pytest.approx(0.048)


def test_last_per_bucket():
    step_ns = 60 * NS
    p1 = {"time_ns": 5 * NS, "value": 1}
    p2 = {"time_ns": 50 * NS, "value": 2}  # same bucket as p1, later -> wins
    p3 = {"time_ns": 70 * NS, "value": 3}  # next bucket
    result = last_per_bucket([p1, p2, p3], step_ns)
    assert result == {0: p2, step_ns: p3}


def test_aggregate_numbers():
    assert aggregate_numbers([1.0, 2.0, 3.0], "sum") == pytest.approx(6.0)
    assert aggregate_numbers([1.0, 2.0, 3.0], "avg") == pytest.approx(2.0)
    assert aggregate_numbers([1.0, 2.0, 3.0], "max") == pytest.approx(3.0)
    assert aggregate_numbers([1.0, 2.0, 3.0], "last") == pytest.approx(3.0)
