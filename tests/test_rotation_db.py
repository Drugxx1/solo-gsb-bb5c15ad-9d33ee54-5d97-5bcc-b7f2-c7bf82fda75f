"""Integration smoke test: rotation scheduling against a real SQLite DB.

Covers what the HTTP layer delegates to ``app.rotation_store`` plus the
analysis composition, without needing FastAPI installed.

Run:  PYTHONPATH=. python3 tests/test_rotation_db.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="rotation_db_test_")
os.environ["LIGHT_AUDIT_DB"] = str(Path(_TMP) / "test.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, db, rotation, rotation_store  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def setup_data(conn):
    for name in ("case-a", "case-b"):
        conn.execute(
            "INSERT INTO cases(name, description, gap_threshold_minutes, created_at) "
            "VALUES (?,?,?,?)", (name, "", 60, db.utc_now_iso()))
    for name, limit in (("silk", 50000.0), ("scroll", 30000.0)):
        conn.execute(
            "INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at) "
            "VALUES (?,?,?,?)", (name, "", limit, db.utc_now_iso()))
    # case-a: 50 lux constant for 60 days; case-b: 10 lux
    for cid, lux in ((1, 50.0), (2, 10.0)):
        for i in range(24 * 60 + 1):
            ts = audit.iso(T0 + timedelta(hours=i))
            conn.execute("INSERT INTO readings(case_id, ts, lux) VALUES (?,?,?)",
                         (cid, ts, lux))
    # committed occupancy: exhibit 1 in case-a for the first 24 h
    conn.execute(
        "INSERT INTO occupancies(exhibit_id, case_id, start_ts, end_ts, note) "
        "VALUES (?,?,?,?,?)",
        (1, 1, audit.iso(T0), audit.iso(T0 + timedelta(hours=24)), ""))


def load_series(conn):
    out = {}
    for c in conn.execute("SELECT * FROM cases").fetchall():
        rows = [(audit.parse_ts(r["ts"]), r["lux"]) for r in conn.execute(
            "SELECT ts, lux FROM readings WHERE case_id=? ORDER BY ts", (c["id"],))]
        if rows:
            out[c["id"]] = audit.make_case_series(
                c["id"], c["name"], rows, c["gap_threshold_minutes"])
    return out


def test_full_db_workflow():
    db.init_db()
    with db.get_conn() as conn:
        setup_data(conn)

        # --- create a batch: capacities + drafts in one submission ---------
        drafts = [
            # exhibit 1: back into case-a [24h, 72h)
            (1, 1, T0 + timedelta(hours=24), T0 + timedelta(hours=72), "回展"),
            # exhibit 2: case-a overlaps the above [48h, 96h)
            (2, 1, T0 + timedelta(hours=48), T0 + timedelta(hours=96), ""),
            # exhibit 2: also booked into case-b at the same time -> 多柜
            (2, 2, T0 + timedelta(hours=60), T0 + timedelta(hours=80), ""),
        ]
        batch_id = rotation_store.create_batch(
            conn, "2026 春季轮换", "冒烟测试", [(1, 1)], drafts)
        assert batch_id == 1

        batches = rotation_store.list_batches(conn)
        assert len(batches) == 1 and batches[0]["draft_count"] == 3
        assert batches[0]["accepted_count"] == 0

        caps = rotation_store.load_capacities(conn, batch_id)
        assert caps == [{"case_id": 1, "capacity": 1, "case_name": "case-a"}]

        loaded = rotation_store.load_drafts(conn, batch_id)
        assert [d.id for d in loaded] == [1, 2, 3]
        assert all(d.status == "pending" for d in loaded)
        assert loaded[0].exhibit_name == "silk" and loaded[0].case_name == "case-a"

        # --- analysis composition (same as main._rotation_analysis) --------
        series = load_series(conn)
        limits = {r["id"]: r["annual_limit_lux_hours"] for r in conn.execute(
            "SELECT id, annual_limit_lux_hours FROM exhibits")}
        committed = {1: [(T0, T0 + timedelta(hours=24), 1)]}
        result = rotation.analyze(loaded, {1: 1}, series, limits, committed)

        types = sorted(c["type"] for c in result["conflicts"])
        assert types == ["case_overlap", "exhibit_double_booking"], types
        # drafts 1,2,3 all linked into one chain
        assert len(result["conflict_chains"]) == 1
        assert result["conflict_chains"][0]["draft_ids"] == [1, 2, 3]
        # dose estimates: exhibit 1 draft = 48 h * 50 lux = 2400
        est1 = result["drafts"][0]["annual_estimate"]
        assert abs(est1["estimated_lux_hours"] - 2400.0) < 1e-6
        # annual totals: exhibit 1 = committed 1200 + planned 2400 = 3600
        tot1 = [t for t in result["annual_totals"] if t["exhibit_id"] == 1][0]
        assert abs(tot1["committed_lux_hours"] - 1200.0) < 1e-6
        assert abs(tot1["projected_total_lux_hours"] - 3600.0) < 1e-6
        assert result["exceeding_plans"] == []  # 3600 << 50000

        # --- accept / revoke with version trail -----------------------------
        r = rotation_store.set_draft_status(conn, 1, "accept", "同意回展")
        assert r["status"] == "accepted"
        assert [e["action"] for e in r["history"]] == ["submit", "accept"]

        r = rotation_store.set_draft_status(conn, 1, "revoke", "暂缓")
        assert r["status"] == "revoked"
        r = rotation_store.set_draft_status(conn, 1, "accept", "")
        hist = rotation_store.get_draft_history(conn, 1)
        assert [e["seq"] for e in hist["events"]] == [1, 2, 3, 4]
        assert [e["action"] for e in hist["events"]] == [
            "submit", "accept", "revoke", "accept"]
        assert hist["events"][1]["note"] == "同意回展"
        assert hist["draft"]["status"] == "accepted"

        # revoked drafts drop out of the conflict scan
        rotation_store.set_draft_status(conn, 2, "revoke", "")
        loaded = rotation_store.load_drafts(conn, batch_id)
        result2 = rotation.analyze(loaded, {1: 1}, series, limits, committed)
        assert result2["summary"]["active_draft_count"] == 2
        assert result2["conflicts"] == []  # only drafts 1+3 remain, no overlap

        # idempotency guard + not found
        try:
            rotation_store.set_draft_status(conn, 1, "accept", "")
            raise AssertionError("expected StatusConflict")
        except rotation_store.StatusConflict:
            pass
        try:
            rotation_store.set_draft_status(conn, 999, "accept", "")
            raise AssertionError("expected DraftNotFound")
        except rotation_store.DraftNotFound:
            pass
        try:
            rotation_store.get_draft_history(conn, 999)
            raise AssertionError("expected DraftNotFound")
        except rotation_store.DraftNotFound:
            pass

        # batch list reflects status counts (draft1 accepted, draft2 revoked,
        # draft3 still pending)
        b = rotation_store.list_batches(conn)[0]
        assert (b["accepted_count"], b["revoked_count"]) == (1, 1)
    print("ok test_full_db_workflow")


def test_schema_fresh_install_has_all_tables():
    # init_db() on a previously nonexistent file is the docker-compose first
    # boot path: every table (old + new) must exist afterwards.
    with db.get_conn() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"cases", "exhibits", "readings", "occupancies",
                "rotation_batches", "rotation_capacities",
                "rotation_drafts", "rotation_draft_events"}
    assert expected <= names, names
    print("ok test_schema_fresh_install_has_all_tables")


if __name__ == "__main__":
    test_full_db_workflow()
    test_schema_fresh_install_has_all_tables()
    print("\nAll rotation DB integration tests passed.")
