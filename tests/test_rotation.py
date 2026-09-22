"""Tests for the rotation scheduling module (app/rotation.py).

Covers half-open interval scanning, conflict chains, per-segment annual dose
estimation, over-limit plans, and the accept/revoke version lifecycle against
a real (temporary) SQLite database.

Run:  PYTHONPATH=. python3 tests/test_rotation.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LIGHT_AUDIT_DB"] = str(
    Path(tempfile.mkdtemp(prefix="rot_test_")) / "test_rotation.db")

from app import audit, db, rotation  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
WIN = (T0, T0 + timedelta(days=365))

EXH = {
    1: {"name": "ex-1", "annual_limit_lux_hours": 50000.0},
    2: {"name": "ex-2", "annual_limit_lux_hours": 50000.0},
    3: {"name": "ex-3", "annual_limit_lux_hours": 50000.0},
}
CASES = {1: {"name": "case-a"}, 2: {"name": "case-b"}}


def series(case_id: int, name: str, lux: float, hours: int = 48,
           start: datetime = T0) -> audit.CaseSeries:
    rows = [(start + timedelta(hours=i), lux) for i in range(hours + 1)]
    return audit.make_case_series(case_id, name, rows, 60)


def draft(did: int, ex: int, case: int, start_h: float, end_h: float,
          status: str = "draft", open_ended: bool = False) -> rotation.DraftInterval:
    return rotation.DraftInterval(
        draft_id=did, exhibit_id=ex, case_id=case,
        start=T0 + timedelta(hours=start_h),
        end=T0 + timedelta(hours=end_h),
        open_ended=open_ended, status=status)


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Half-open interval scanning
# ---------------------------------------------------------------------------

def test_half_open_adjacency_no_conflict():
    # [0,10) and [10,20) touch but do not overlap -> no conflict
    drafts = [draft(1, 1, 1, 0, 10), draft(2, 1, 2, 10, 20)]
    res = rotation.analyze(drafts, {1: 1, 2: 1}, {}, WIN, EXH, CASES)
    assert res["conflict_chains"] == []
    assert res["conflict_period_counts"]["exhibit_double_booked"] == 0
    assert res["conflict_period_counts"]["case_over_capacity"] == 0
    print("ok test_half_open_adjacency_no_conflict")


def test_same_exhibit_multi_case_conflict():
    drafts = [draft(1, 1, 1, 0, 10), draft(2, 1, 2, 5, 15)]
    res = rotation.analyze(drafts, {1: 1, 2: 1}, {}, WIN, EXH, CASES)
    assert len(res["conflict_chains"]) == 1
    ch = res["conflict_chains"][0]
    assert ch["chain_no"] == 1
    assert ch["types"] == ["exhibit_double_booked"]
    assert ch["start_ts"] == audit.iso(T0)  # chain starts at earliest draft
    p = ch["periods"][0]
    assert p["type"] == "exhibit_double_booked"
    assert p["exhibit_id"] == 1 and p["exhibit_name"] == "ex-1"
    assert p["start_ts"] == audit.iso(T0 + timedelta(hours=5))
    assert p["end_ts"] == audit.iso(T0 + timedelta(hours=10))
    assert p["active"] == 2
    # drafts inside the chain are sorted by start time
    assert [d["draft_id"] for d in ch["drafts"]] == [1, 2]
    assert [e["id"] for e in res["involved_exhibits"]] == [1]
    assert res["involved_exhibits"][0]["chains"] == [1]
    print("ok test_same_exhibit_multi_case_conflict")


def test_case_over_capacity_respects_capacity():
    # capacity 2: two overlapping drafts in one case are fine
    drafts = [draft(1, 1, 1, 0, 10), draft(2, 2, 1, 5, 15)]
    res = rotation.analyze(drafts, {1: 2}, {}, WIN, EXH, CASES)
    assert res["conflict_chains"] == []
    # a third simultaneous draft pushes the case over capacity
    drafts.append(draft(3, 3, 1, 6, 8))
    res = rotation.analyze(drafts, {1: 2}, {}, WIN, EXH, CASES)
    assert len(res["conflict_chains"]) == 1
    ch = res["conflict_chains"][0]
    assert ch["types"] == ["case_over_capacity"]
    p = ch["periods"][0]
    assert p["active"] == 3 and p["capacity"] == 2
    assert p["start_ts"] == audit.iso(T0 + timedelta(hours=6))
    assert p["end_ts"] == audit.iso(T0 + timedelta(hours=8))
    assert sorted(p["draft_ids"]) == [1, 2, 3]
    # involved exhibits sorted by first conflict appearance
    assert [e["id"] for e in res["involved_exhibits"]] == [1, 2, 3]
    print("ok test_case_over_capacity_respects_capacity")


def test_conflict_chain_merges_and_sorts_by_start():
    # D1/D2 share exhibit 1 (overlap 5-10); D2/D3 overflow case 2 (cap 1) at 6-7
    drafts = [draft(1, 1, 1, 0, 10), draft(2, 1, 2, 5, 15), draft(3, 2, 2, 6, 7)]
    res = rotation.analyze(drafts, {1: 1, 2: 1}, {}, WIN, EXH, CASES)
    assert len(res["conflict_chains"]) == 1
    ch = res["conflict_chains"][0]
    assert [d["draft_id"] for d in ch["drafts"]] == [1, 2, 3]
    assert ch["types"] == ["case_over_capacity", "exhibit_double_booked"]
    assert [e["id"] for e in ch["exhibits"]] == [1, 2]
    # an independent later chain sorts after the earlier one (stable by start)
    drafts2 = drafts + [draft(4, 3, 1, 100, 110), draft(5, 3, 2, 105, 120)]
    res2 = rotation.analyze(drafts2, {1: 1, 2: 1}, {}, WIN, EXH, CASES)
    assert len(res2["conflict_chains"]) == 2
    assert res2["conflict_chains"][0]["start_ts"] < res2["conflict_chains"][1]["start_ts"]
    assert [d["draft_id"] for d in res2["conflict_chains"][1]["drafts"]] == [4, 5]
    print("ok test_conflict_chain_merges_and_sorts_by_start")


def test_draft_outside_annual_window_reported():
    drafts = [draft(1, 1, 1, 0, 10), draft(2, 1, 1, 9000, 9010)]  # 9000h > 365d
    res = rotation.analyze(drafts, {1: 1}, {}, WIN, EXH, CASES)
    assert [d["draft_id"] for d in res["outside_annual_window"]] == [2]
    assert res["conflict_chains"] == []
    # only the in-window draft gets a dose segment
    assert len(res["exhibit_plans"]) == 1
    assert len(res["exhibit_plans"][0]["segments"]) == 1
    print("ok test_draft_outside_annual_window_reported")


# ---------------------------------------------------------------------------
# Dose estimation
# ---------------------------------------------------------------------------

def test_segment_dose_measured_plus_extrapolated():
    # case-a: constant 50 lux, readings cover [0, 48h]
    sm = {1: series(1, "case-a", 50.0, hours=48)}
    drafts = [draft(1, 1, 1, 12, 100)]
    res = rotation.analyze(drafts, {1: 1}, sm, WIN,
                           {1: {"name": "ex-1", "annual_limit_lux_hours": 1e9}},
                           {1: {"name": "case-a"}})
    seg = res["exhibit_plans"][0]["segments"][0]
    assert approx(seg["measured_lux_hours"], 50.0 * 36)     # hours 12..48 covered
    assert approx(seg["estimated_lux_hours"], 50.0 * 52)    # hours 48..100 extrapolated
    assert approx(seg["dose_lux_hours"], 50.0 * 88)
    assert seg["estimate_note"] == "measured+extrapolated"
    print("ok test_segment_dose_measured_plus_extrapolated")


def test_open_ended_draft_clamped_to_window():
    sm = {1: series(1, "case-a", 10.0, hours=48)}
    d = rotation.DraftInterval(1, 1, 1, T0 + timedelta(hours=10),
                               T0 + timedelta(days=365), True, "draft")
    res = rotation.analyze([d], {1: 1}, sm, WIN,
                           {1: {"name": "ex-1", "annual_limit_lux_hours": 1e9}},
                           {1: {"name": "case-a"}})
    seg = res["exhibit_plans"][0]["segments"][0]
    assert seg["open_ended"] is True
    assert seg["end_ts"] == audit.iso(T0 + timedelta(days=365))
    assert approx(seg["measured_lux_hours"], 10.0 * 38)     # hours 10..48
    assert seg["estimated_lux_hours"] > 0
    print("ok test_open_ended_draft_clamped_to_window")


def test_over_limit_plan_flagged_with_baseline():
    sm = {1: series(1, "case-a", 50.0, hours=200)}
    drafts = [draft(1, 1, 1, 0, 100)]
    exhibits = {1: {"name": "silk", "annual_limit_lux_hours": 5000.0}}
    baseline = {1: [(T0 + timedelta(hours=100), T0 + timedelta(hours=140), 1)]}
    res = rotation.analyze(drafts, {1: 1}, sm, WIN, exhibits, {1: {"name": "case-a"}},
                           baseline)
    plan = res["exhibit_plans"][0]
    # planned 100h*50 = 5000, baseline 40h*50 = 2000 -> total 7000 > 5000
    assert approx(plan["planned_lux_hours"], 5000.0)
    assert approx(plan["baseline_lux_hours"], 2000.0)
    assert approx(plan["total_lux_hours"], 7000.0)
    assert plan["exceeds_limit"] is True
    assert approx(plan["excess_lux_hours"], 2000.0)
    assert res["over_limit_plans"][0]["exhibit_id"] == 1
    assert res["over_limit_plans"][0]["first_segment_start_ts"] == audit.iso(T0)
    print("ok test_over_limit_plan_flagged_with_baseline")


# ---------------------------------------------------------------------------
# DB lifecycle: batches, accept/revoke, versions
# ---------------------------------------------------------------------------

def test_schema_tables_created():
    db.init_db()
    with db.get_conn() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("rotation_batches", "rotation_case_capacities",
              "rotation_drafts", "rotation_draft_versions"):
        assert t in names, t
    print("ok test_schema_tables_created")


def test_db_batch_lifecycle_and_versions():
    db.init_db()
    with db.get_conn() as conn:
        now = db.utc_now_iso()
        conn.execute("INSERT INTO cases(name, description, gap_threshold_minutes, created_at)"
                     " VALUES ('case-a','',60,?)", (now,))
        conn.execute("INSERT INTO cases(name, description, gap_threshold_minutes, created_at)"
                     " VALUES ('case-b','',60,?)", (now,))
        conn.execute("INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at)"
                     " VALUES ('ex-1','',50000,?)", (now,))
        conn.execute("INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at)"
                     " VALUES ('ex-2','',50000,?)", (now,))
        for i in range(73):  # case-a and case-b: 50 lux constant for 72 h
            ts = audit.iso(T0 + timedelta(hours=i))
            conn.execute("INSERT INTO readings(case_id, ts, lux) VALUES (1, ?, 50.0)", (ts,))
            conn.execute("INSERT INTO readings(case_id, ts, lux) VALUES (2, ?, 50.0)", (ts,))
        batch_id = rotation.create_batch(
            conn, name="2026Q4 轮换", note="", annual_start=T0,
            capacities=[{"case_id": 1, "capacity": 1}, {"case_id": 2, "capacity": 1}],
            drafts=[
                {"exhibit_id": 1, "case_id": 1, "start": T0,
                 "end": T0 + timedelta(hours=10), "note": ""},
                {"exhibit_id": 1, "case_id": 2, "start": T0 + timedelta(hours=5),
                 "end": T0 + timedelta(hours=15), "note": ""},
                {"exhibit_id": 2, "case_id": 1, "start": T0 + timedelta(hours=20),
                 "end": None, "note": "开放段"},
            ])
        payload = rotation.batch_payload(conn, batch_id)
        assert payload["batch"]["name"] == "2026Q4 轮换"
        assert len(payload["drafts"]) == 3
        assert all(d["status"] == "draft" and d["version"] == 1 for d in payload["drafts"])
        assert {c["case_id"]: c["capacity"] for c in payload["capacities"]} == {1: 1, 2: 1}

        a = payload["analysis"]
        assert a["annual_window"]["start_ts"] == audit.iso(T0)
        # drafts 1 & 2 conflict (same exhibit, two cases); draft 3 is clean
        assert len(a["conflict_chains"]) == 1
        assert [d["draft_id"] for d in a["conflict_chains"][0]["drafts"]] == [1, 2]
        assert [e["name"] for e in a["involved_exhibits"]] == ["ex-1"]
        # open-ended draft 3 clamped to the annual window end
        plans = {p["exhibit_name"]: p for p in a["exhibit_plans"]}
        seg3 = plans["ex-2"]["segments"][0]
        assert seg3["open_ended"] is True
        assert seg3["end_ts"] == audit.iso(T0 + timedelta(days=365))
        # ex-2 sits in case-a (50 lux) for the rest of the year -> way over 50k
        assert plans["ex-2"]["exceeds_limit"] is True
        assert [p["exhibit_name"] for p in a["over_limit_plans"]] == ["ex-2"]
        # ex-1: 10h + 10h at 50 lux = 1000 lux*h, well under the limit
        assert approx(plans["ex-1"]["planned_lux_hours"], 1000.0)
        assert plans["ex-1"]["exceeds_limit"] is False

        # accept draft 1 -> version 2; revoke -> version 3; history preserved
        r = rotation.set_draft_status(conn, 1, "accepted", "策展同意")
        assert r["version"] == 2 and r["status"] == "accepted"
        r = rotation.set_draft_status(conn, 1, "revoked", "改期")
        assert r["version"] == 3 and r["status"] == "revoked"
        versions = rotation.list_draft_versions(conn, 1)
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert [v["status"] for v in versions] == ["draft", "accepted", "revoked"]
        assert versions[1]["note"] == "策展同意"
        # same-status transition rejected
        try:
            rotation.set_draft_status(conn, 1, "revoked")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        # revoked draft 1 leaves the plan -> the conflict chain disappears
        a2 = rotation.batch_payload(conn, batch_id)["analysis"]
        assert a2["conflict_chains"] == []
        assert a2["involved_exhibits"] == []
        # batch list reflects status counts
        batches = rotation.list_batches(conn)
        assert batches[0]["draft_count"] == 3
        assert batches[0]["accepted_count"] == 0
        assert batches[0]["revoked_count"] == 1
        # unknown ids raise LookupError (mapped to 404 by the API layer)
        for fn in (lambda: rotation.batch_payload(conn, 999),
                   lambda: rotation.set_draft_status(conn, 999, "accepted"),
                   lambda: rotation.list_draft_versions(conn, 999)):
            try:
                fn()
                raise AssertionError("expected LookupError")
            except LookupError:
                pass
    print("ok test_db_batch_lifecycle_and_versions")


def test_default_annual_start_from_earliest_draft():
    db.init_db()
    with db.get_conn() as conn:
        now = db.utc_now_iso()
        cur = conn.execute("INSERT INTO cases(name, description, gap_threshold_minutes, created_at)"
                           " VALUES ('case-c','',60,?)", (now,))
        case_c = cur.lastrowid
        cur = conn.execute("INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at)"
                           " VALUES ('ex-9','',50000,?)", (now,))
        ex_9 = cur.lastrowid
        batch_id = rotation.create_batch(
            conn, name="无年度起点", note="", annual_start=None,
            capacities=[],
            drafts=[{"exhibit_id": ex_9, "case_id": case_c,
                     "start": datetime(2027, 3, 1, tzinfo=UTC),
                     "end": datetime(2027, 4, 1, tzinfo=UTC), "note": ""}])
        a = rotation.batch_payload(conn, batch_id)["analysis"]
        # annual window defaults to Jan 1 of the earliest draft's year
        assert a["annual_window"]["start_ts"] == audit.iso(datetime(2027, 1, 1, tzinfo=UTC))
        assert a["annual_window"]["end_ts"] == audit.iso(datetime(2028, 1, 1, tzinfo=UTC))
        # case-c has no readings -> segment reported, dose not estimable
        seg = a["exhibit_plans"][0]["segments"][0]
        assert seg["estimate_note"] == "no_readings"
        assert seg["dose_lux_hours"] == 0.0
    print("ok test_default_annual_start_from_earliest_draft")


if __name__ == "__main__":
    test_half_open_adjacency_no_conflict()
    test_same_exhibit_multi_case_conflict()
    test_case_over_capacity_respects_capacity()
    test_conflict_chain_merges_and_sorts_by_start()
    test_draft_outside_annual_window_reported()
    test_segment_dose_measured_plus_extrapolated()
    test_open_ended_draft_clamped_to_window()
    test_over_limit_plan_flagged_with_baseline()
    test_schema_tables_created()
    test_db_batch_lifecycle_and_versions()
    test_default_annual_start_from_earliest_draft()
    print("\nAll rotation scheduling tests passed.")
