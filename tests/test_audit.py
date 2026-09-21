"""Unit tests for the light-dose audit math.

Run:  PYTHONPATH=. python3 tests/test_audit.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def series(case_id: int, name: str, lux: float, step_min=60, hours=48,
           gap: tuple[int, int] | None = None, threshold=60) -> audit.CaseSeries:
    rows = []
    for i in range(hours + 1):
        if gap and gap[0] < i < gap[1]:
            continue
        rows.append((T0 + timedelta(minutes=step_min * i), lux))
    return audit.make_case_series(case_id, name, rows, threshold)


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def test_constant_lux_basic_dose():
    cs = series(1, "c", 100.0)
    win = (T0, T0 + timedelta(hours=48))
    r = audit.integrate_window({1: cs}, [(T0, None, 1)], win)
    assert approx(r.total_lux_hours, 100.0 * 48), r.total_lux_hours
    assert r.breakpoint_evidence == []
    assert r.coverage_gaps == []
    curve = audit.cumulative_curve({1: cs}, [(T0, None, 1)], win, step_hours=3)
    assert approx(curve[-1]["cumulative_lux_hours"], 4800.0), curve[-1]
    print("ok test_constant_lux_basic_dose")


def test_trapezoid_on_ramp():
    # 0 -> 100 lux linearly over 10 h = triangle area 500 lux*h
    rows = [(T0 + timedelta(hours=i), float(i * 10)) for i in range(11)]
    cs = audit.make_case_series(1, "ramp", rows, 60)
    r = audit.integrate_window({1: cs}, [(T0, None, 1)],
                               (T0, T0 + timedelta(hours=10)))
    assert approx(r.total_lux_hours, 500.0), r.total_lux_hours
    print("ok test_trapezoid_on_ramp")


def test_breakpoint_flagged_and_excludable():
    cs = series(1, "c", 100.0, gap=(10, 20))  # 10 h outage
    win = (T0, T0 + timedelta(hours=48))
    r = audit.integrate_window({1: cs}, [(T0, None, 1)], win)
    assert len(r.breakpoint_evidence) == 1
    ev = r.breakpoint_evidence[0]
    assert approx(ev["gap_hours"], 10.0)
    # the flagged trapezoid spans the whole 10 h at 100 lux constant
    assert approx(ev["lux_hours_in_occupancy"], 1000.0)
    assert approx(r.total_lux_hours, 4800.0)
    assert approx(r.audited_lux_hours, 4800.0 - 1000.0)
    assert approx(r.breakpoint_lux_hours, 1000.0)
    print("ok test_breakpoint_flagged_and_excludable")


def test_dose_only_during_occupancy_and_clipping():
    # occupied only hours [12, 36): 24 h * 100 = 2400
    cs = series(1, "c", 100.0)
    occ = [(T0 + timedelta(hours=12), T0 + timedelta(hours=36), 1)]
    win = (T0, T0 + timedelta(hours=48))
    r = audit.integrate_window({1: cs}, occ, win)
    assert approx(r.total_lux_hours, 2400.0), r.total_lux_hours
    assert approx(r.occupied_hours, 24.0)
    # clipping inside a segment: occupancy [12.5, 13.5] -> 100 lux*h
    occ2 = [(T0 + timedelta(minutes=750), T0 + timedelta(minutes=810), 1)]
    r2 = audit.integrate_window({1: cs}, occ2, win)
    assert approx(r2.total_lux_hours, 100.0), r2.total_lux_hours
    print("ok test_dose_only_during_occupancy_and_clipping")


def test_coverage_gap_when_no_readings():
    cs = series(1, "c", 100.0, hours=24)
    # occupied beyond coverage -> coverage gap reported
    occ = [(T0, T0 + timedelta(hours=48), 1)]
    win = (T0, T0 + timedelta(hours=48))
    r = audit.integrate_window({1: cs}, occ, win)
    assert approx(r.total_lux_hours, 2400.0)
    assert len(r.coverage_gaps) == 1
    assert approx(r.coverage_gaps[0]["hours"], 24.0)
    print("ok test_coverage_gap_when_no_readings")


def test_two_case_rotation_rates():
    a = series(1, "A", 50.0, hours=24*30)
    b = series(2, "B", 10.0, hours=24*30)
    series_map = {1: a, 2: b}
    annual_start = T0
    annual_end = T0 + timedelta(days=365)
    as_of = T0 + timedelta(days=30)
    occ_stay = [(T0, None, 1)]
    occ_move = [(T0, as_of, 1), (as_of, None, 2)]
    rates = audit.ambient_rates(series_map, (annual_start, as_of))
    stay = audit.simulate_scenario("stay", series_map, occ_stay,
                                   (annual_start, annual_end), 20000.0, rates, as_of)
    moved = audit.simulate_scenario("move", series_map, occ_move,
                                    (annual_start, annual_end), 20000.0, rates, as_of)
    # 30 d in A at 50 lux = 36000 already over the 20k limit
    assert stay.projected_limit_ts == audit.iso(as_of)
    # use a bigger limit: 100000
    stay = audit.simulate_scenario("stay", series_map, occ_stay,
                                   (annual_start, annual_end), 100000.0, rates, as_of)
    moved = audit.simulate_scenario("move", series_map, occ_move,
                                    (annual_start, annual_end), 100000.0, rates, as_of)
    # ytd = 50 lux * 720 h = 36000; remaining 64000
    assert approx(stay.annual_dose_ytd, 36000.0)
    # stay: 64000/50 = 1280 h after as_of
    assert stay.projected_limit_ts == audit.iso(as_of + timedelta(hours=1280))
    # move: 64000/10 = 6400 h
    assert moved.projected_limit_ts == audit.iso(as_of + timedelta(hours=6400))
    assert moved.rate_segments[0].case_id == 2
    assert approx(moved.rate_segments[0].mean_lux, 10.0)
    print("ok test_two_case_rotation_rates")


def test_projection_crosses_annual_reset():
    a = series(1, "A", 1.0, hours=24)  # very dim
    annual_start = T0
    annual_end = T0 + timedelta(days=365)
    as_of = T0 + timedelta(days=1)
    rates = audit.ambient_rates({1: a}, (annual_start, as_of))
    sc = audit.simulate_scenario("stay", {1: a}, [(T0, None, 1)],
                                 (annual_start, annual_end), 100000.0, rates, as_of)
    # 1 lux: 100k hours needed, impossible within year
    assert sc.projected_limit_ts is None
    assert sc.projected_limit_crosses_reset is True
    assert "reset" in sc.note
    print("ok test_projection_crosses_annual_reset")


def test_lux_at_interpolation_and_bounds():
    cs = series(1, "c", 50.0, step_min=30, hours=4)
    assert cs.lux_at(T0 - timedelta(hours=1)) is None
    assert cs.lux_at(T0 + timedelta(hours=5)) is None
    v = cs.lux_at(T0 + timedelta(minutes=15))
    assert v is not None and approx(v[0], 50.0)
    # ramp interpolation mid-segment
    rows = [(T0, 0.0), (T0 + timedelta(hours=1), 100.0)]
    cs2 = audit.make_case_series(2, "ramp", rows, 60)
    v2 = cs2.lux_at(T0 + timedelta(minutes=30))
    assert v2 is not None and approx(v2[0], 50.0), v2
    print("ok test_lux_at_interpolation_and_bounds")


def test_scheduled_multi_segment_move_then_return():
    a = series(1, "A", 40.0, hours=24*60)
    b = series(2, "B", 10.0, hours=24*60)
    sm = {1: a, 2: b}
    annual_start, annual_end = T0, T0 + timedelta(days=365)
    as_of = T0 + timedelta(days=10)
    # move to B for 30 d, then back to A (open ended)
    sched = [(T0, as_of, 1),
             (as_of, as_of + timedelta(days=30), 2),
             (as_of + timedelta(days=30), None, 1)]
    rates = audit.ambient_rates(sm, (annual_start, as_of))
    sc = audit.simulate_scenario("roam", sm, sched, (annual_start, annual_end),
                                 50000.0, rates, as_of)
    # ytd = 40*240 = 9600; remaining 40400
    # 30 d in B at 10 = 7200; then 33200 at 40 = 830 h into A
    expected = as_of + timedelta(days=30) + timedelta(hours=830)
    assert sc.projected_limit_ts == audit.iso(expected), sc.projected_limit_ts
    assert [rs.case_id for rs in sc.rate_segments] == [2, 1]
    print("ok test_scheduled_multi_segment_move_then_return")


def test_naive_timestamp_assumed_utc():
    dt = audit.parse_ts("2026-01-01T00:00:00")
    assert dt.tzinfo == UTC and dt == T0
    dt2 = audit.parse_ts("2026-01-01T02:00:00+02:00")
    assert dt2 == T0
    print("ok test_naive_timestamp_assumed_utc")


if __name__ == "__main__":
    test_constant_lux_basic_dose()
    test_trapezoid_on_ramp()
    test_breakpoint_flagged_and_excludable()
    test_dose_only_during_occupancy_and_clipping()
    test_coverage_gap_when_no_readings()
    test_two_case_rotation_rates()
    test_projection_crosses_annual_reset()
    test_lux_at_interpolation_and_bounds()
    test_scheduled_multi_segment_move_then_return()
    test_naive_timestamp_assumed_utc()
    print("\nAll audit math tests passed.")
