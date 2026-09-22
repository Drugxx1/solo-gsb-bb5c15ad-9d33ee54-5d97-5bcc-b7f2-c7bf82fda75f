"""SQLite persistence layer.

All timestamps are stored as UTC ISO-8601 strings ("2026-08-01T08:00:00+00:00").
The database file lives at the path given by the LIGHT_AUDIT_DB environment
variable (default: ./data/light_audit.db).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = os.environ.get(
    "LIGHT_AUDIT_DB", str(Path(__file__).resolve().parent.parent / "data" / "light_audit.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    gap_threshold_minutes INTEGER NOT NULL DEFAULT 60,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exhibits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    material    TEXT NOT NULL DEFAULT '',
    annual_limit_lux_hours REAL NOT NULL,
    created_at  TEXT NOT NULL
);

-- Timestamped lux readings, one series per case.
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    lux         REAL NOT NULL,
    UNIQUE(case_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_case_ts ON readings(case_id, ts);

-- Periods during which the exhibit physically resides in a case.
CREATE TABLE IF NOT EXISTS occupancies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibit_id  INTEGER NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT,  -- NULL means "still in the case / open ended"
    note        TEXT NOT NULL DEFAULT '',
    CHECK (end_ts IS NULL OR end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS idx_occ_exhibit ON occupancies(exhibit_id, start_ts);

-- Rotation scheduling: one batch = one submission of draft in-case plans
-- for several exhibits plus per-case capacities.
CREATE TABLE IF NOT EXISTS rotation_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- Max simultaneous occupants per case within a batch (cases not listed
-- default to capacity 1 at scan time).
CREATE TABLE IF NOT EXISTS rotation_capacities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES rotation_batches(id) ON DELETE CASCADE,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    capacity    INTEGER NOT NULL CHECK (capacity >= 1),
    UNIQUE(batch_id, case_id)
);

-- Draft in-case plans. Status lifecycle: pending -> accepted / revoked
-- (accepted <-> revoked allowed); every transition is versioned below.
CREATE TABLE IF NOT EXISTS rotation_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES rotation_batches(id) ON DELETE CASCADE,
    exhibit_id  INTEGER NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT,  -- NULL means "planned open ended"
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','revoked')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (end_ts IS NULL OR end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS idx_rotation_drafts_batch ON rotation_drafts(batch_id, start_ts);

-- Append-only version trail for each draft; rows are never rewritten.
CREATE TABLE IF NOT EXISTS rotation_draft_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES rotation_drafts(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('submit','accept','revoke')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(draft_id, seq)
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
