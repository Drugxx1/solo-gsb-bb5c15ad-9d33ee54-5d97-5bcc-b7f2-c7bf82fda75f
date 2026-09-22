"""FastAPI application: museum exhibit light-dose audit.

Run locally:  uvicorn app.main:app --reload
Run in Docker: docker compose up --build  (http://localhost:8000)
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit, db, rotation, rotation_store, seed as seed_mod


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="博物馆展品光照剂量审计", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    gap_threshold_minutes: int = Field(default=60, ge=1, le=10080)


class ExhibitIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    material: str = ""
    annual_limit_lux_hours: float = Field(gt=0)


class OccupancyIn(BaseModel):
    exhibit_id: int
    case_id: int
    start_ts: str
    end_ts: Optional[str] = None
    note: str = ""


class AuditQuery(BaseModel):
    exhibit_id: int
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    step_hours: float = Field(default=1.0, gt=0, le=24)


class SimulateIn(BaseModel):
    exhibit_id: int
    target_case_id: int
    move_ts: Optional[str] = None
    annual_start: Optional[str] = None  # default: Jan 1 of the as_of year
    as_of: Optional[str] = None        # default: latest reading timestamp


class RotationCapacityIn(BaseModel):
    case_id: int
    capacity: int = Field(ge=1, le=99)


class RotationDraftIn(BaseModel):
    exhibit_id: int
    case_id: int
    start_ts: str
    end_ts: Optional[str] = None
    note: str = ""


class RotationBatchIn(BaseModel):
    name: str = Field(default="", max_length=120)
    note: str = ""
    capacities: list[RotationCapacityIn] = Field(default_factory=list)
    drafts: list[RotationDraftIn] = Field(min_length=1)


class DraftActionIn(BaseModel):
    note: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_or_404(conn, table: str, row_id: int):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"{table} id={row_id} 不存在")
    return row


def _parse_ts_or_400(value: Optional[str], field: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        return audit.parse_ts(value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{field} 时间戳无效: {value!r} ({exc})")


def _load_series(conn) -> dict[int, audit.CaseSeries]:
    out: dict[int, audit.CaseSeries] = {}
    for c in conn.execute("SELECT * FROM cases").fetchall():
        rows = [
            (audit.parse_ts(r["ts"]), r["lux"])
            for r in conn.execute(
                "SELECT ts, lux FROM readings WHERE case_id = ? ORDER BY ts", (c["id"],)
            )
        ]
        if rows:
            out[c["id"]] = audit.make_case_series(
                c["id"], c["name"], rows, c["gap_threshold_minutes"]
            )
    return out


def _case_map(conn) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM cases").fetchall()}


def _exhibit_map(conn) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM exhibits").fetchall()}


def _load_occupancies(conn, exhibit_id: int):
    rows = conn.execute(
        "SELECT * FROM occupancies WHERE exhibit_id = ? ORDER BY start_ts", (exhibit_id,)
    ).fetchall()
    return [
        (audit.parse_ts(r["start_ts"]),
         audit.parse_ts(r["end_ts"]) if r["end_ts"] else None,
         r["case_id"])
        for r in rows
    ]


def _occupancy_rows(conn, exhibit_id: int):
    return [
        {
            "id": r["id"],
            "case_id": r["case_id"],
            "start_ts": r["start_ts"],
            "end_ts": r["end_ts"],
            "note": r["note"],
        }
        for r in conn.execute(
            "SELECT * FROM occupancies WHERE exhibit_id = ? ORDER BY start_ts", (exhibit_id,)
        ).fetchall()
    ]


def _latest_ts(conn) -> datetime:
    r = conn.execute("SELECT MAX(ts) AS m FROM readings").fetchone()
    if not r["m"]:
        raise HTTPException(400, "系统中还没有任何照度读数，请先上传 JSONL 或载入示例数据")
    return audit.parse_ts(r["m"])


def _insert_readings(conn, case_id: int, records: list[dict]) -> int:
    n = 0
    for rec in records:
        if "ts" not in rec or "lux" not in rec:
            raise HTTPException(400, "每条读数需要 ts 与 lux 字段")
        ts = _parse_ts_or_400(str(rec["ts"]), "ts")
        lux = float(rec["lux"])
        if lux < 0:
            raise HTTPException(400, f"照度不能为负: {lux}")
        conn.execute(
            "INSERT INTO readings(case_id, ts, lux) VALUES (?,?,?) "
            "ON CONFLICT(case_id, ts) DO UPDATE SET lux=excluded.lux",
            (case_id, audit.iso(ts), lux),
        )
        n += 1
    return n


def _parse_jsonl(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, f"文件不是 UTF-8 文本: {exc}")
    records: list[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"第 {i} 行不是合法 JSON: {exc}")
    return records


# ---------------------------------------------------------------------------
# Metadata endpoints
# ---------------------------------------------------------------------------

@app.post("/api/cases", status_code=201)
def create_case(body: CaseIn):
    with db.get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO cases(name, description, gap_threshold_minutes, created_at) "
                "VALUES (?,?,?,?)",
                (body.name, body.description, body.gap_threshold_minutes, db.utc_now_iso()),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"展柜名称已存在: {body.name}") from exc
        return {"id": cur.lastrowid}


@app.get("/api/cases")
def list_cases():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM cases ORDER BY name").fetchall()
        out = []
        for r in rows:
            n = conn.execute(
                "SELECT COUNT(*) c, MIN(ts) mn, MAX(ts) mx FROM readings WHERE case_id=?",
                (r["id"],),
            ).fetchone()
            out.append({
                "id": r["id"], "name": r["name"], "description": r["description"],
                "gap_threshold_minutes": r["gap_threshold_minutes"],
                "reading_count": n["c"], "first_ts": n["mn"], "last_ts": n["mx"],
            })
        return out


@app.post("/api/exhibits", status_code=201)
def create_exhibit(body: ExhibitIn):
    with db.get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at) "
                "VALUES (?,?,?,?)",
                (body.name, body.material, body.annual_limit_lux_hours, db.utc_now_iso()),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"展品名称已存在: {body.name}") from exc
        return {"id": cur.lastrowid}


@app.get("/api/exhibits")
def list_exhibits():
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM exhibits ORDER BY name").fetchall()]


@app.post("/api/occupancies", status_code=201)
def add_occupancy(body: OccupancyIn):
    start = _parse_ts_or_400(body.start_ts, "start_ts")
    end = _parse_ts_or_400(body.end_ts, "end_ts")
    if end and end <= start:
        raise HTTPException(400, "end_ts 必须晚于 start_ts")
    with db.get_conn() as conn:
        _row_or_404(conn, "exhibits", body.exhibit_id)
        _row_or_404(conn, "cases", body.case_id)
        cur = conn.execute(
            "INSERT INTO occupancies(exhibit_id, case_id, start_ts, end_ts, note) "
            "VALUES (?,?,?,?,?)",
            (body.exhibit_id, body.case_id, audit.iso(start),
             audit.iso(end) if end else None, body.note),
        )
        return {"id": cur.lastrowid}


@app.get("/api/occupancies")
def get_occupancies(exhibit_id: int):
    with db.get_conn() as conn:
        _row_or_404(conn, "exhibits", exhibit_id)
        return _occupancy_rows(conn, exhibit_id)


@app.delete("/api/occupancies/{occ_id}", status_code=204)
def delete_occupancy(occ_id: int):
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM occupancies WHERE id=?", (occ_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"occupancy id={occ_id} 不存在")


# ---------------------------------------------------------------------------
# JSONL upload endpoints
# ---------------------------------------------------------------------------

@app.post("/api/cases/{case_id}/readings/upload")
async def upload_readings(case_id: int, file: UploadFile = File(...)):
    with db.get_conn() as conn:
        _row_or_404(conn, "cases", case_id)
        raw = await file.read()
    records = _parse_jsonl(raw)
    if not records:
        raise HTTPException(400, "JSONL 文件为空")
    with db.get_conn() as conn:
        n = _insert_readings(conn, case_id, records)
    return {"inserted_or_updated": n, "case_id": case_id, "filename": file.filename}


@app.post("/api/readings/import")
async def import_readings_jsonl(
    file: UploadFile = File(...),
    default_case: Optional[str] = Form(default=None),
):
    """Import a multi-case readings JSONL.

    Per-line object: {"ts": "...", "lux": 120.0, "case": "case-a"}.
    Lines without a ``case`` field go to ``default_case`` (form field).
    Unknown case names are auto-created with the default 60-minute threshold.
    """
    records = _parse_jsonl(await file.read())
    if not records:
        raise HTTPException(400, "JSONL 文件为空")

    created: dict[str, int] = {}
    counts: dict[str, int] = {}
    with db.get_conn() as conn:
        def case_id_for(name: Optional[str]) -> int:
            cname = name or default_case
            if not cname:
                raise HTTPException(400, "存在没有 case 字段的行，且未提供 default_case")
            cmap = _case_map(conn)
            if cname not in cmap:
                cur = conn.execute(
                    "INSERT INTO cases(name, description, gap_threshold_minutes, created_at) "
                    "VALUES (?,?,?,?)",
                    (cname, "JSONL 导入时自动创建", 60, db.utc_now_iso()),
                )
                cmap[cname] = cur.lastrowid
                created[cname] = cur.lastrowid
            return cmap[cname]

        grouped: dict[int, list[dict]] = {}
        for rec in records:
            cid = case_id_for(rec.get("case"))
            grouped.setdefault(cid, []).append(rec)
        for cid, recs in grouped.items():
            counts[str(cid)] = _insert_readings(conn, cid, recs)

    return {
        "inserted_or_updated": sum(counts.values()),
        "per_case": counts,
        "cases_created": created,
        "filename": file.filename,
    }


@app.post("/api/occupancies/import")
async def import_occupancies_jsonl(
    file: UploadFile = File(...),
    auto_create: bool = Form(default=True),
):
    """Import occupancy JSONL.

    Per line: {"exhibit": "silk-scroll", "case": "case-a",
               "start_ts": "...", "end_ts": "..." | null, "note": ""}.
    Names are resolved against existing cases/exhibits and auto-created when
    ``auto_create`` is set (new exhibits get a 50000 lux*h/year default limit;
    adjust via the UI afterwards).
    """
    records = _parse_jsonl(await file.read())
    if not records:
        raise HTTPException(400, "JSONL 文件为空")

    inserted: list[dict] = []
    with db.get_conn() as conn:
        for i, rec in enumerate(records, 1):
            ename, cname = rec.get("exhibit"), rec.get("case")
            if not ename or not cname:
                raise HTTPException(400, f"第 {i} 行缺少 exhibit/case 字段")
            start = _parse_ts_or_400(str(rec.get("start_ts", "")), "start_ts")
            end = _parse_ts_or_400(rec.get("end_ts"), "end_ts")
            if start is None:
                raise HTTPException(400, f"第 {i} 行缺少 start_ts")
            if end and end <= start:
                raise HTTPException(400, f"第 {i} 行 end_ts 早于 start_ts")

            emap, cmap = _exhibit_map(conn), _case_map(conn)
            if cname not in cmap:
                if not auto_create:
                    raise HTTPException(400, f"第 {i} 行展柜 {cname!r} 不存在")
                cur = conn.execute(
                    "INSERT INTO cases(name, description, gap_threshold_minutes, created_at) "
                    "VALUES (?,?,?,?)", (cname, "JSONL 导入时自动创建", 60, db.utc_now_iso()))
                cmap[cname] = cur.lastrowid
            if ename not in emap:
                if not auto_create:
                    raise HTTPException(400, f"第 {i} 行展品 {ename!r} 不存在")
                cur = conn.execute(
                    "INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at) "
                    "VALUES (?,?,?,?)",
                    (ename, "JSONL 导入时自动创建", 50000.0, db.utc_now_iso()))
                emap[ename] = cur.lastrowid

            cur = conn.execute(
                "INSERT INTO occupancies(exhibit_id, case_id, start_ts, end_ts, note) "
                "VALUES (?,?,?,?,?)",
                (emap[ename], cmap[cname], audit.iso(start),
                 audit.iso(end) if end else None, str(rec.get("note", ""))),
            )
            inserted.append({"id": cur.lastrowid, "exhibit": ename, "case": cname})

    return {"inserted": len(inserted), "occupancies": inserted, "filename": file.filename}


# ---------------------------------------------------------------------------
# Audit + simulation
# ---------------------------------------------------------------------------

def _build_audit(exhibit_id: int, start: Optional[datetime], end: Optional[datetime],
                 step_hours: float) -> dict:
    with db.get_conn() as conn:
        exhibit = _row_or_404(conn, "exhibits", exhibit_id)
        occ = _load_occupancies(conn, exhibit_id)
        series = _load_series(conn)
        cases = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM cases").fetchall()}
        occ_detail = _occupancy_rows(conn, exhibit_id)
    if not occ:
        raise HTTPException(400, "该展品还没有任何在柜时间段（occupancy）记录")

    if start is None:
        start = min(s for s, _, _ in occ)
    if end is None:
        ends = [e for _, e, _ in occ if e]
        series_end = max((cs.coverage_end for cs in series.values() if cs.coverage_end),
                         default=start)
        end = max(ends + [series_end, start])
    if end <= start:
        raise HTTPException(400, "审计结束时间必须晚于开始时间")

    win = (start, end)
    result = audit.integrate_window(series, occ, win)
    curve = audit.cumulative_curve(series, occ, win, step_hours=step_hours)

    return {
        "exhibit": {
            "id": exhibit["id"],
            "name": exhibit["name"],
            "material": exhibit["material"],
            "annual_limit_lux_hours": exhibit["annual_limit_lux_hours"],
        },
        "window": {"start_ts": audit.iso(start), "end_ts": audit.iso(end)},
        "occupancies": occ_detail,
        "cases": cases,
        "totals": {
            "total_lux_hours": result.total_lux_hours,
            "audited_lux_hours": result.audited_lux_hours,
            "breakpoint_lux_hours": result.breakpoint_lux_hours,
            "occupied_hours": result.occupied_hours,
            "covered_hours": result.covered_hours,
        },
        "by_case": list(result.by_case.values()),
        "breakpoint_evidence": result.breakpoint_evidence,
        "coverage_gaps": result.coverage_gaps,
        "cumulative_curve": curve,
        "method": {
            "integration": "trapezoidal on adjacent readings",
            "dose_unit": "lux*hour",
            "breakpoint_rule": "reading gap > case gap_threshold_minutes",
            "timezone": "UTC (naive timestamps assumed UTC)",
        },
    }


@app.post("/api/audit")
def run_audit(body: AuditQuery):
    start = _parse_ts_or_400(body.start_ts, "start_ts")
    end = _parse_ts_or_400(body.end_ts, "end_ts")
    return _build_audit(body.exhibit_id, start, end, body.step_hours)


@app.get("/api/audit")
def run_audit_get(exhibit_id: int, start_ts: Optional[str] = None,
                  end_ts: Optional[str] = None, step_hours: float = 1.0):
    start = _parse_ts_or_400(start_ts, "start_ts")
    end = _parse_ts_or_400(end_ts, "end_ts")
    return _build_audit(exhibit_id, start, end, step_hours)


def _simulate(exhibit_id: int, target_case_id: int,
              move_ts: Optional[datetime],
              annual_start: Optional[datetime],
              as_of: Optional[datetime]) -> dict:
    with db.get_conn() as conn:
        exhibit = _row_or_404(conn, "exhibits", exhibit_id)
        target = _row_or_404(conn, "cases", target_case_id)
        occ = _load_occupancies(conn, exhibit_id)
        series = _load_series(conn)
        if as_of is None:
            as_of = _latest_ts(conn)

    if annual_start is None:
        annual_start = datetime(as_of.year, 1, 1, tzinfo=audit.timezone.utc)
    annual_end = annual_start + timedelta(days=365)
    annual_window = (annual_start, annual_end)

    if move_ts is None:
        move_ts = as_of
    if move_ts < annual_start:
        raise HTTPException(400, "轮换时间早于年度窗口起点")

    # Stay scenario: real schedule (current open-ended occupancy unchanged).
    stay_occ = occ
    # Move scenario: truncate the occupancy active at move_ts and append an
    # open-ended occupancy in the target case.
    move_occ: list[tuple[datetime, Optional[datetime], int]] = []
    moved = False
    for s, e, cid in occ:
        if not moved and s < move_ts and (e is None or e > move_ts):
            move_occ.append((s, move_ts, cid))
            moved = True
        else:
            move_occ.append((s, e, cid))
    move_occ.append((move_ts, None, target_case_id))

    # Post-move projection rates are derived from each case's own historical
    # light curve (mean lux over the annual window so far), so moving into a
    # case the object never occupied is still projectable.
    rates = audit.ambient_rates(series, (annual_start, min(as_of, annual_end)))

    stay = audit.simulate_scenario(
        "stay", series, stay_occ, annual_window,
        exhibit["annual_limit_lux_hours"], rates, as_of)
    moved_sc = audit.simulate_scenario(
        "move", series, move_occ, annual_window,
        exhibit["annual_limit_lux_hours"], rates, as_of)

    def rate_segments_json(sc: audit.ScenarioResult) -> list[dict]:
        return [
            {
                "case_id": rs.case_id,
                "case_name": rs.case_name,
                "from_ts": rs.start_ts,
                "to_ts": rs.end_ts,
                "mean_lux": rs.mean_lux,
                "reference_window": {"start_ts": rs.reference_start,
                                     "end_ts": rs.reference_end,
                                     "hours": rs.reference_hours},
                "source": rs.source,
            }
            for rs in sc.rate_segments
        ]

    def scenario_json(sc: audit.ScenarioResult, current_case_row) -> dict:
        return {
            "label": sc.label,
            "current_case_id": current_case_row["id"],
            "current_case_name": current_case_row["name"],
            "mean_lux_case_historical": sc.mean_lux,
            "rate_lux_hours_per_occupied_hour": sc.rate_lux_hours_per_occ_hour,
            "annual_dose_ytd_lux_hours": sc.annual_dose_ytd,
            "remaining_budget_lux_hours": sc.remaining_budget_lux_hours,
            "projected_limit_ts": sc.projected_limit_ts,
            "projected_limit_crosses_annual_reset": sc.projected_limit_crosses_reset,
            "occupied_hours_ytd": sc.occupied_hours_ytd,
            "covered_hours_ytd": sc.covered_hours_ytd,
            "note": sc.note,
            "rate_basis": rate_segments_json(sc),
            "projected_curve": sc.projected_curve,
        }

    # current case row for stay
    stay_case_id = None
    for s, e, cid in occ:
        if s < as_of < (e or annual_end):
            stay_case_id = cid
    with db.get_conn() as conn:
        stay_row = _row_or_404(conn, "cases", stay_case_id) if stay_case_id else \
            {"id": None, "name": "(无在柜记录)"}
        stay_row = dict(stay_row) if stay_case_id else stay_row

    return {
        "exhibit": {
            "id": exhibit["id"], "name": exhibit["name"], "material": exhibit["material"],
            "annual_limit_lux_hours": exhibit["annual_limit_lux_hours"],
        },
        "annual_window": {"start_ts": audit.iso(annual_start), "end_ts": audit.iso(annual_end)},
        "as_of_ts": audit.iso(as_of),
        "move_ts": audit.iso(move_ts),
        "projection_method": "post-as_of dose = case historical mean lux (24 h/day occupancy)",
        "case_historical_rates": list(rates.values()),
        "stay": scenario_json(stay, stay_row),
        "move": scenario_json(moved_sc, dict(target)),
    }


@app.post("/api/simulate")
def simulate(body: SimulateIn):
    as_of = _parse_ts_or_400(body.as_of, "as_of")
    move_ts = _parse_ts_or_400(body.move_ts, "move_ts")
    annual_start = _parse_ts_or_400(body.annual_start, "annual_start")
    return _simulate(body.exhibit_id, body.target_case_id, move_ts, annual_start, as_of)


@app.get("/api/simulate")
def simulate_get(exhibit_id: int, target_case_id: int,
                 move_ts: Optional[str] = None, annual_start: Optional[str] = None,
                 as_of: Optional[str] = None):
    return _simulate(
        exhibit_id, target_case_id,
        _parse_ts_or_400(move_ts, "move_ts"),
        _parse_ts_or_400(annual_start, "annual_start"),
        _parse_ts_or_400(as_of, "as_of"),
    )


# ---------------------------------------------------------------------------
# Rotation scheduling (batch drafts, conflict scan, accept/revoke versions)
# ---------------------------------------------------------------------------

def _rotation_analysis(conn, batch_id: int) -> dict:
    batch = rotation_store.load_batch(conn, batch_id)
    if batch is None:
        raise HTTPException(404, f"rotation_batches id={batch_id} 不存在")
    cap_rows = rotation_store.load_capacities(conn, batch_id)
    drafts = rotation_store.load_drafts(conn, batch_id)
    exhibit_ids = {d.exhibit_id for d in drafts}
    limits = {
        r["id"]: r["annual_limit_lux_hours"]
        for r in conn.execute("SELECT id, annual_limit_lux_hours FROM exhibits")
    }
    committed = {eid: _load_occupancies(conn, eid) for eid in exhibit_ids}
    series = _load_series(conn)

    result = rotation.analyze(
        drafts,
        {r["case_id"]: r["capacity"] for r in cap_rows},
        series, limits, committed,
    )
    result["batch"] = {
        "id": batch["id"], "name": batch["name"],
        "note": batch["note"], "created_at": batch["created_at"],
    }
    result["capacities"] = cap_rows
    return result


@app.post("/api/rotation/batches", status_code=201)
def create_rotation_batch(body: RotationBatchIn):
    """Submit one rotation batch: case capacities + any number of draft plans.

    The batch is persisted (drafts start as ``pending`` with a ``submit``
    version event) and the conflict/dose analysis is returned immediately.
    """
    parsed: list[tuple[RotationDraftIn, datetime, Optional[datetime]]] = []
    for i, dr in enumerate(body.drafts, 1):
        start = _parse_ts_or_400(dr.start_ts, f"drafts[{i}].start_ts")
        end = _parse_ts_or_400(dr.end_ts, f"drafts[{i}].end_ts")
        if start is None:
            raise HTTPException(400, f"第 {i} 份草案缺少 start_ts")
        if end and end <= start:
            raise HTTPException(400, f"第 {i} 份草案 end_ts 必须晚于 start_ts")
        parsed.append((dr, start, end))
    seen_cap: set[int] = set()
    for cap in body.capacities:
        if cap.case_id in seen_cap:
            raise HTTPException(400, f"展柜 id={cap.case_id} 的容量在同一批次中重复定义")
        seen_cap.add(cap.case_id)

    with db.get_conn() as conn:
        for dr, _, _ in parsed:
            _row_or_404(conn, "exhibits", dr.exhibit_id)
            _row_or_404(conn, "cases", dr.case_id)
        for cap in body.capacities:
            _row_or_404(conn, "cases", cap.case_id)
        batch_id = rotation_store.create_batch(
            conn,
            body.name,
            body.note,
            [(c.case_id, c.capacity) for c in body.capacities],
            [(dr.exhibit_id, dr.case_id, start, end, dr.note)
             for dr, start, end in parsed],
        )
        analysis = _rotation_analysis(conn, batch_id)
    return {"id": batch_id, "analysis": analysis}


@app.get("/api/rotation/batches")
def list_rotation_batches():
    with db.get_conn() as conn:
        return rotation_store.list_batches(conn)


@app.get("/api/rotation/batches/{batch_id}")
def get_rotation_batch(batch_id: int):
    with db.get_conn() as conn:
        return _rotation_analysis(conn, batch_id)


def _set_draft_status(draft_id: int, action: str, note: str) -> dict:
    with db.get_conn() as conn:
        try:
            return rotation_store.set_draft_status(conn, draft_id, action, note)
        except rotation_store.DraftNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except rotation_store.StatusConflict as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/rotation/drafts/{draft_id}/accept")
def accept_rotation_draft(draft_id: int, body: Optional[DraftActionIn] = None):
    return _set_draft_status(draft_id, "accept", (body.note if body else "") or "接受草案")


@app.post("/api/rotation/drafts/{draft_id}/revoke")
def revoke_rotation_draft(draft_id: int, body: Optional[DraftActionIn] = None):
    return _set_draft_status(draft_id, "revoke", (body.note if body else "") or "撤销草案")


@app.get("/api/rotation/drafts/{draft_id}/history")
def rotation_draft_history(draft_id: int):
    with db.get_conn() as conn:
        try:
            return rotation_store.get_draft_history(conn, draft_id)
        except rotation_store.DraftNotFound as exc:
            raise HTTPException(404, str(exc)) from exc


@app.post("/api/audit/export")
def export_audit(
    exhibit_id: int,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    step_hours: float = 1.0,
    simulate_target_case_id: Optional[int] = None,
):
    """Full audit bundle (optionally with rotation simulation) as JSON.

    All parameters are query parameters so the browser can trigger the
    download directly from a form/fetch without a JSON body.
    """
    start = _parse_ts_or_400(start_ts, "start_ts")
    end = _parse_ts_or_400(end_ts, "end_ts")
    bundle = _build_audit(exhibit_id, start, end, step_hours)
    if simulate_target_case_id:
        bundle["simulation"] = _simulate(
            exhibit_id, simulate_target_case_id, None, None, None)
    bundle["exported_at"] = db.utc_now_iso()
    payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    bundle["checksum_sha256"] = hashlib.sha256(payload).hexdigest()
    return JSONResponse(bundle, headers={
        "Content-Disposition": 'attachment; filename="light_audit.json"'
    })


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

@app.post("/api/admin/seed", status_code=201)
def seed_demo(overwrite: bool = Form(default=False)):
    """Generate sample data files and import them (idempotent upserts)."""
    with db.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] > 0 and not overwrite:
            raise HTTPException(409, "数据库已有读数；如需覆盖示例请使用 overwrite=true")
    seed_mod.write_sample_files()
    case_specs = [
        ("case-a", "Silk Hall 展柜-A（约 50 lux，含一次传感器断电）", 60),
        ("case-b", "Paper Gallery 展柜-B（约 20 lux，数据完整）", 60),
    ]
    with db.get_conn() as conn:
        for name, desc, gap in case_specs:
            conn.execute(
                "INSERT INTO cases(name, description, gap_threshold_minutes, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, gap_threshold_minutes=excluded.gap_threshold_minutes",
                (name, desc, gap, db.utc_now_iso()))
        conn.execute(
            "INSERT INTO exhibits(name, material, annual_limit_lux_hours, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "material=excluded.material, annual_limit_lux_hours=excluded.annual_limit_lux_hours",
            ("silk-scroll", "古代丝织品（高敏感，UK CIE 等级）", 50000.0, db.utc_now_iso()))
        cmap, emap = _case_map(conn), _exhibit_map(conn)
        n_read = 0
        for rec in _parse_jsonl(seed_mod.SEED_READINGS.read_bytes()):
            n_read += 1
            _insert_readings(conn, cmap[rec["case"]], [rec])
        # replace demo occupancies for the scroll to stay idempotent
        conn.execute("DELETE FROM occupancies WHERE exhibit_id=?", (emap["silk-scroll"],))
        n_occ = 0
        for rec in _parse_jsonl(seed_mod.SEED_OCCUPANCIES.read_bytes()):
            s = audit.parse_ts(rec["start_ts"])
            e = audit.parse_ts(rec["end_ts"]) if rec["end_ts"] else None
            conn.execute(
                "INSERT INTO occupancies(exhibit_id, case_id, start_ts, end_ts, note) "
                "VALUES (?,?,?,?,?)",
                (emap[rec["exhibit"]], cmap[rec["case"]], audit.iso(s),
                 audit.iso(e) if e else None, rec.get("note", "")))
            n_occ += 1
    return {"readings_upserted": n_read, "occupancies_inserted": n_occ,
            "sample_files": [str(seed_mod.SEED_READINGS), str(seed_mod.SEED_OCCUPANCIES)]}


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api routes win)
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
