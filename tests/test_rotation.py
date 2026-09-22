"""Unit tests for the rotation-scheduling conflict scan and dose estimates.

Run:  PYTHONPATH=. python3 tests/test_rotation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, rotation  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def series(case_id: int, name: str, lux: float, hours: int = 24 * 40) -> audit.CaseSeries:
    rows = [(T0 + timedelta(hours=i), lux) for i in range(hours + 1)]
    return audit.make_case_series(case_id, name, rows, 60)


def draft(did: int, exhibit: int, case: int, start_h: float, end_h,
          status: str = "pending") -> rotation.Draft:
    return rotation.Draft(
        id=did,
        exhibit_id=exhibit,
        exhibit_name=f"ex{exhibit}",
        case_id=case,
        case_name=f"case{case}",
        start=T0 + timedelta(hours=start_h),
        end=None if end_h is None else T0 + timedelta(hours=end_h),
        status=status,
    )


def analyze(drafts, caps=None, series_map=None, limits=None, committed=None):
    return rotation.analyze(drafts, caps or {}, series_map or {},
                            limits or {}, committed or {})


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def test_half_open_touching_no_conflict():
    # [0,10) and [10,20) share only an endpoint -> not an overlap
    r = analyze([draft(1, 1, 1, 0, 10), draft(2, 2, 1, 10, 20)])
    assert r["conflicts"] == [], r["conflicts"]
    assert r["conflict_chains"] == []
    print("ok test_half_open_touching_no_conflict")


def test_same_case_overlap_window():
    r = analyze([draft(1, 1, 1, 0, 10), draft(2, 2, 1, 5, 15)])
    assert len(r["conflicts"]) == 1
    c = r["conflicts"][0]
    assert c["type"] == "case_overlap"
    assert c["start_ts"] == audit.iso(T0 + timedelta(hours=5))
    assert c["end_ts"] == audit.iso(T0 + timedelta(hours=10))
    assert c["draft_ids"] == [1, 2]
    assert c["capacity"] == 1 and c["peak_occupants"] == 2
    assert len(r["conflict_chains"]) == 1
    assert r["conflict_chains"][0]["draft_ids"] == [1, 2]
    print("ok test_same_case_overlap_window")


def test_case_capacity_respected():
    # capacity 2: only the window where all three overlap is a conflict
    ds = [draft(1, 1, 1, 0, 10), draft(2, 2, 1, 5, 15), draft(3, 3, 1, 8, 20)]
    r = analyze(ds, caps={1: 2})
    assert len(r["conflicts"]) == 1
    c = r["conflicts"][0]
    assert c["start_ts"] == audit.iso(T0 + timedelta(hours=8))
    assert c["end_ts"] == audit.iso(T0 + timedelta(hours=10))
    assert c["draft_ids"] == [1, 2, 3] and c["peak_occupants"] == 3
    # with capacity 3 there is no conflict at all
    r2 = analyze(ds, caps={1: 3})
    assert r2["conflicts"] == []
    print("ok test_case_capacity_respected")


def test_exhibit_double_booking_across_cases():
    r = analyze([draft(1, 1, 1, 0, 10), draft(2, 1, 2, 5, 15)])
    types = {c["type"] for c in r["conflicts"]}
    assert types == {"exhibit_double_booking"}, types
    c = r["conflicts"][0]
    assert c["exhibit_id"] == 1 and c["peak_cases"] == 2
    assert c["start_ts"] == audit.iso(T0 + timedelta(hours=5))
    print("ok test_exhibit_double_booking_across_cases")


def test_same_exhibit_same_case_is_case_overlap_only():
    # duplicate drafts of one exhibit in ONE case: capacity conflict, not 多柜
    r = analyze([draft(1, 1, 1, 0, 10), draft(2, 1, 1, 5, 15)])
    types = {c["type"] for c in r["conflicts"]}
    assert types == {"case_overlap"}, types
    print("ok test_same_exhibit_same_case_is_case_overlap_only")


def test_open_ended_conflict_has_open_window():
    r = analyze([draft(1, 1, 1, 0, None), draft(2, 2, 1, 5, None)])
    c = r["conflicts"][0]
    assert c["end_ts"] is None
    assert c["start_ts"] == audit.iso(T0 + timedelta(hours=5))
    print("ok test_open_ended_conflict_has_open_window")


def test_revoked_drafts_excluded():
    r = analyze([draft(1, 1, 1, 0, 10), draft(2, 2, 1, 5, 15, status="revoked")])
    assert r["conflicts"] == []
    assert r["summary"]["revoked_draft_count"] == 1
    assert r["summary"]["active_draft_count"] == 1
    # revoked draft still listed, without an estimate
    d2 = [d for d in r["drafts"] if d["id"] == 2][0]
    assert d2["status"] == "revoked" and d2["annual_estimate"] is None
    print("ok test_revoked_drafts_excluded")


def test_conflict_chains_connected_and_sorted():
    # chain 1: drafts 1-2-3 linked transitively; chain 2: drafts 4-5 later
    ds = [
        draft(1, 1, 1, 0, 10),
        draft(2, 2, 1, 5, 15),
        draft(3, 3, 1, 12, 20),
        draft(4, 4, 2, 100, 110),
        draft(5, 5, 2, 105, 115),
    ]
    r = analyze(ds)
    assert len(r["conflict_chains"]) == 2
    ch1, ch2 = r["conflict_chains"]
    assert ch1["draft_ids"] == [1, 2, 3]
    assert ch2["draft_ids"] == [4, 5]
    assert ch1["start_ts"] < ch2["start_ts"]
    assert [e["name"] for e in ch1["exhibits"]] == ["ex1", "ex2", "ex3"]
    assert ch1["chain_id"] == 1 and ch2["chain_id"] == 2
    print("ok test_conflict_chains_connected_and_sorted")


def test_chain_order_stable_on_equal_start():
    # two independent chains with identical start times keep submission order
    ds = [
        draft(1, 1, 1, 0, 10), draft(2, 2, 1, 0, 10),
        draft(3, 3, 2, 0, 10), draft(4, 4, 2, 0, 10),
    ]
    r = analyze(ds)
    assert len(r["conflict_chains"]) == 2
    assert r["conflict_chains"][0]["draft_ids"] == [1, 2]
    assert r["conflict_chains"][1]["draft_ids"] == [3, 4]
    print("ok test_chain_order_stable_on_equal_start")


def test_dose_estimate_fully_covered():
    sm = {1: series(1, "case1", 50.0)}
    r = analyze([draft(1, 1, 1, 0, 24)], series_map=sm, limits={1: 100000.0})
    est = r["drafts"][0]["annual_estimate"]
    assert est["estimable"] and approx(est["estimated_lux_hours"], 1200.0), est
    assert approx(est["measured_lux_hours"], 1200.0)
    assert approx(est["uncovered_hours"], 0.0)
    print("ok test_dose_estimate_fully_covered")


def test_dose_estimate_extrapolates_beyond_coverage():
    sm = {1: series(1, "case1", 50.0, hours=24)}  # readings cover only 24 h
    r = analyze([draft(1, 1, 1, 0, 48)], series_map=sm, limits={1: 100000.0})
    est = r["drafts"][0]["annual_estimate"]
    # 24 h measured (1200) + 24 h extrapolated at the 50 lux mean (1200)
    assert approx(est["measured_lux_hours"], 1200.0)
    assert approx(est["uncovered_hours"], 24.0)
    assert approx(est["extrapolated_lux_hours"], 1200.0)
    assert approx(est["estimated_lux_hours"], 2400.0)
    print("ok test_dose_estimate_extrapolates_beyond_coverage")


def test_dose_unestimable_without_readings():
    r = analyze([draft(1, 1, 9, 0, 24)], series_map={}, limits={1: 100000.0})
    est = r["drafts"][0]["annual_estimate"]
    assert est["estimable"] is False and est["estimated_lux_hours"] is None
    tot = r["annual_totals"][0]
    assert tot["has_unestimable_segments"] is True
    print("ok test_dose_unestimable_without_readings")


def test_exceeding_plan_includes_committed_dose():
    sm = {1: series(1, "case1", 50.0)}
    # draft alone: 1200 lux*h; committed occupancy adds another 1200
    committed = {1: [(T0, T0 + timedelta(hours=24), 1)]}
    r = analyze([draft(1, 1, 1, 24, 48)], series_map=sm,
                limits={1: 2000.0}, committed=committed)
    assert len(r["exceeding_plans"]) == 1
    p = r["exceeding_plans"][0]
    assert p["exhibit_id"] == 1 and p["year"] == 2026
    assert approx(p["committed_lux_hours"], 1200.0)
    assert approx(p["planned_lux_hours"], 1200.0)
    assert approx(p["projected_total_lux_hours"], 2400.0)
    assert approx(p["over_by_lux_hours"], 400.0)
    # with a higher limit nothing is flagged
    r2 = analyze([draft(1, 1, 1, 24, 48)], series_map=sm,
                 limits={1: 5000.0}, committed=committed)
    assert r2["exceeding_plans"] == []
    assert r2["annual_totals"][0]["exceeds_limit"] is False
    print("ok test_exceeding_plan_includes_committed_dose")


def test_open_ended_dose_clipped_to_calendar_year():
    # open-ended draft starting 2026-12-01: dose window ends 2027-01-01
    sm = {1: series(1, "case1", 50.0, hours=24 * 40)}
    start = datetime(2026, 12, 1, tzinfo=UTC)
    d = rotation.Draft(id=1, exhibit_id=1, exhibit_name="ex1", case_id=1,
                       case_name="case1", start=start, end=None)
    r = analyze([d], series_map=sm, limits={1: 10 ** 9})
    est = r["drafts"][0]["annual_estimate"]
    assert est["year"] == 2026
    assert est["window"]["end_ts"] == audit.iso(datetime(2027, 1, 1, tzinfo=UTC))
    assert est["clipped_to_calendar_year"] is True
    # 31 days * 24 h * 50 lux (readings cover only 40 d, rest extrapolated)
    assert approx(est["estimated_lux_hours"], 31 * 24 * 50.0), est
    print("ok test_open_ended_dose_clipped_to_calendar_year")


def test_drafts_sorted_by_start_stably():
    ds = [draft(1, 1, 1, 50, 60), draft(2, 2, 1, 10, 20), draft(3, 3, 2, 10, 30)]
    r = analyze(ds)
    assert [d["id"] for d in r["drafts"]] == [2, 3, 1]
    print("ok test_drafts_sorted_by_start_stably")


if __name__ == "__main__":
    test_half_open_touching_no_conflict()
    test_same_case_overlap_window()
    test_case_capacity_respected()
    test_exhibit_double_booking_across_cases()
    test_same_exhibit_same_case_is_case_overlap_only()
    test_open_ended_conflict_has_open_window()
    test_revoked_drafts_excluded()
    test_conflict_chains_connected_and_sorted()
    test_chain_order_stable_on_equal_start()
    test_dose_estimate_fully_covered()
    test_dose_estimate_extrapolates_beyond_coverage()
    test_dose_unestimable_without_readings()
    test_exceeding_plan_includes_committed_dose()
    test_open_ended_dose_clipped_to_calendar_year()
    test_drafts_sorted_by_start_stably()
    print("\nAll rotation scheduling tests passed.")
