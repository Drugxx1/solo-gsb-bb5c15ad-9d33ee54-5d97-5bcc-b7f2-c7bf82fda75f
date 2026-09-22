"""Persistence helpers for rotation scheduling.

Kept free of web-framework imports so the whole DB workflow (batch creation,
status transitions, version history) is unit-testable against a temporary
SQLite database. HTTP translation (404/409) lives in ``app.main``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from . import audit, db, rotation


class DraftNotFound(LookupError):
    def __init__(self, draft_id: int):
        super().__init__(f"rotation_drafts id={draft_id} 不存在")
        self.draft_id = draft_id


class StatusConflict(RuntimeError):
    def __init__(self, draft_id: int, status: str):
        super().__init__(f"草案 #{draft_id} 已处于 {status} 状态")
        self.draft_id = draft_id
        self.status = status


def create_batch(
    conn: sqlite3.Connection,
    name: str,
    note: str,
    capacities: list[tuple[int, int]],
    drafts: list[tuple[int, int, datetime, Optional[datetime], str]],
) -> int:
    """Insert one batch with capacities and pending drafts.

    ``capacities``: ``(case_id, capacity)`` pairs.
    ``drafts``: ``(exhibit_id, case_id, start, end_or_None, note)`` tuples.
    Every draft gets its initial ``submit`` version event.
    """
    now = db.utc_now_iso()
    cur = conn.execute(
        "INSERT INTO rotation_batches(name, note, created_at) VALUES (?,?,?)",
        (name, note, now))
    batch_id = cur.lastrowid
    for case_id, capacity in capacities:
        conn.execute(
            "INSERT INTO rotation_capacities(batch_id, case_id, capacity) VALUES (?,?,?)",
            (batch_id, case_id, capacity))
    for exhibit_id, case_id, start, end, dnote in drafts:
        cur = conn.execute(
            "INSERT INTO rotation_drafts(batch_id, exhibit_id, case_id, start_ts, "
            "end_ts, note, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?)",
            (batch_id, exhibit_id, case_id, audit.iso(start),
             audit.iso(end) if end else None, dnote, now, now))
        conn.execute(
            "INSERT INTO rotation_draft_events(draft_id, seq, action, note, created_at) "
            "VALUES (?,?,?,?,?)",
            (cur.lastrowid, 1, "submit", "批次提交", now))
    return batch_id


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.*, "
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id=b.id) AS draft_count, "
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id=b.id "
        "   AND d.status='accepted') AS accepted_count, "
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id=b.id "
        "   AND d.status='revoked') AS revoked_count "
        "FROM rotation_batches b ORDER BY b.id DESC").fetchall()
    return [dict(r) for r in rows]


def load_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM rotation_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def load_capacities(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT rc.case_id, rc.capacity, c.name AS case_name "
        "FROM rotation_capacities rc JOIN cases c ON c.id = rc.case_id "
        "WHERE rc.batch_id = ? ORDER BY c.name", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def load_drafts(conn: sqlite3.Connection, batch_id: int) -> list[rotation.Draft]:
    """Drafts in submission order (ascending id) for stable sort tie-breaks."""
    rows = conn.execute(
        "SELECT d.*, e.name AS exhibit_name, c.name AS case_name "
        "FROM rotation_drafts d "
        "JOIN exhibits e ON e.id = d.exhibit_id "
        "JOIN cases c ON c.id = d.case_id "
        "WHERE d.batch_id = ? ORDER BY d.id", (batch_id,)).fetchall()
    return [
        rotation.Draft(
            id=r["id"], exhibit_id=r["exhibit_id"], exhibit_name=r["exhibit_name"],
            case_id=r["case_id"], case_name=r["case_name"],
            start=audit.parse_ts(r["start_ts"]),
            end=audit.parse_ts(r["end_ts"]) if r["end_ts"] else None,
            status=r["status"], note=r["note"],
        )
        for r in rows
    ]


def set_draft_status(
    conn: sqlite3.Connection, draft_id: int, action: str, note: str
) -> dict:
    """Apply accept/revoke and append the version event (append-only)."""
    new_status = {"accept": "accepted", "revoke": "revoked"}[action]
    row = conn.execute(
        "SELECT * FROM rotation_drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise DraftNotFound(draft_id)
    if row["status"] == new_status:
        raise StatusConflict(draft_id, new_status)
    now = db.utc_now_iso()
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM rotation_draft_events "
        "WHERE draft_id=?", (draft_id,)).fetchone()["s"]
    conn.execute(
        "UPDATE rotation_drafts SET status=?, updated_at=? WHERE id=?",
        (new_status, now, draft_id))
    conn.execute(
        "INSERT INTO rotation_draft_events(draft_id, seq, action, note, created_at) "
        "VALUES (?,?,?,?,?)", (draft_id, seq, action, note, now))
    return {
        "id": draft_id,
        "batch_id": row["batch_id"],
        "status": new_status,
        "history": _events(conn, draft_id),
    }


def get_draft_history(conn: sqlite3.Connection, draft_id: int) -> dict:
    row = conn.execute(
        "SELECT d.*, e.name AS exhibit_name, c.name AS case_name "
        "FROM rotation_drafts d "
        "JOIN exhibits e ON e.id = d.exhibit_id "
        "JOIN cases c ON c.id = d.case_id WHERE d.id=?", (draft_id,)).fetchone()
    if row is None:
        raise DraftNotFound(draft_id)
    return {"draft": dict(row), "events": _events(conn, draft_id)}


def _events(conn: sqlite3.Connection, draft_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT seq, action, note, created_at FROM rotation_draft_events "
        "WHERE draft_id=? ORDER BY seq", (draft_id,))]
