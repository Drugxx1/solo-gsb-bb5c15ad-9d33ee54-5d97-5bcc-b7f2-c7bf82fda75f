"""Rotation scheduling: multi-exhibit draft plans with conflict scanning.

One *batch* submission carries several exhibits' in-case plan drafts plus
per-case capacities. The scanner works on half-open intervals [start, end):
a draft ending at T and another starting at T do **not** overlap. Two
conflict types are detected:

* ``exhibit_double_booked`` — the same exhibit is planned in more than one
  place at the same time (two cases, or twice in the same case);
* ``case_over_capacity`` — more drafts are simultaneously active in one case
  than the capacity submitted with the batch.

Drafts linked by conflict windows form *conflict chains* (connected
components), reported in stable start-time order together with the involved
exhibits. Each draft segment's annual dose is estimated from the case's
existing lux curve: trapezoidal integration where readings cover the segment,
historical mean-lux extrapolation (``audit.ambient_rates``) where they do
not. Per-exhibit totals (baseline from real occupancies + planned drafts) are
compared with the exhibit's annual limit to flag over-limit plans.

Drafts can be accepted / revoked individually; every transition is stored as
a new row in ``rotation_draft_versions`` so the full history is preserved.
Only drafts in ``ACTIVE_STATUSES`` take part in scanning and dose estimation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit

ACTIVE_STATUSES = ("draft", "accepted")

CONFLICT_EXHIBIT = "exhibit_double_booked"
CONFLICT_CAPACITY = "case_over_capacity"


@dataclass(frozen=True)
class DraftInterval:
    draft_id: int
    exhibit_id: int
    case_id: int
    start: datetime        # submitted start
    end: datetime          # effective end (open-ended drafts: annual window end)
    open_ended: bool
    status: str
    note: str = ""


# ---------------------------------------------------------------------------
# Half-open interval sweep
# ---------------------------------------------------------------------------

def _over_threshold_windows(
    entries: list[tuple[datetime, datetime, int]],
    threshold: int,
) -> list[tuple[datetime, datetime, set[int], int]]:
    """Maximal windows where more than ``threshold`` intervals are active.

    ``entries`` are ``(start, end, key)`` half-open intervals. At equal
    timestamps ends are processed before starts, so [a, b) and [b, c) never
    coexist. Returns ``(start, end, member_keys, peak_active)`` tuples.
    """
    events: list[tuple[datetime, int, int]] = []
    for s, e, k in entries:
        if s < e:
            events.append((s, 1, k))
            events.append((e, -1, k))
    # sort by time; at the same timestamp ends (-1) come before starts (+1)
    events.sort(key=lambda ev: (ev[0], ev[1]))

    active: set[int] = set()
    windows: list[tuple[datetime, datetime, set[int], int]] = []
    open_from: Optional[datetime] = None
    members: set[int] = set()
    peak = 0
    last_ts: Optional[datetime] = None
    for ts, delta, k in events:
        if last_ts is not None and ts > last_ts:
            # segment [last_ts, ts) ran with the current active set
            if len(active) > threshold:
                if open_from is None:
                    open_from = last_ts
                    members = set()
                    peak = 0
                members |= active
                peak = max(peak, len(active))
            elif open_from is not None:
                windows.append((open_from, last_ts, set(members), peak))
                open_from = None
        if delta == 1:
            active.add(k)
        else:
            active.discard(k)
        last_ts = ts
    if open_from is not None:
        windows.append((open_from, last_ts, set(members), peak))  # type: ignore[arg-type]
    return windows


# ---------------------------------------------------------------------------
# Conflict chains
# ---------------------------------------------------------------------------

def _ex_name(exhibits: dict, ex_id: int) -> str:
    return (exhibits.get(ex_id) or {}).get("name", f"#{ex_id}")


def _case_name(cases: dict, case_id: int) -> str:
    return (cases.get(case_id) or {}).get("name", f"#{case_id}")


def _build_chains(
    drafts: list[DraftInterval],
    clip_of: dict[int, tuple[datetime, datetime]],
    windows: list[dict],
    exhibits: dict,
    cases: dict,
) -> list[dict]:
    """Connected components of drafts linked by conflict windows.

    Chains are sorted by (clipped start, lowest draft id); drafts inside a
    chain are sorted by (clipped start, draft id) — a stable start-time order.
    """
    by_id = {d.draft_id: d for d in drafts}
    parent = {d.draft_id: d.draft_id for d in drafts}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for w in windows:
        members = sorted(w["members"])
        for m in members[1:]:
            ra, rb = find(members[0]), find(m)
            if ra != rb:
                parent[rb] = ra

    groups: dict[int, dict] = {}
    for w in windows:
        root = find(sorted(w["members"])[0])
        g = groups.setdefault(root, {"windows": [], "draft_ids": set()})
        g["windows"].append(w)
        g["draft_ids"].update(w["members"])

    chains = []
    for g in groups.values():
        ds = sorted((by_id[i] for i in g["draft_ids"]),
                    key=lambda d: (clip_of[d.draft_id][0], d.draft_id))
        ws = sorted(g["windows"], key=lambda w: (w["start"], w["type"]))
        chains.append({"drafts": ds, "windows": ws,
                       "start": clip_of[ds[0].draft_id][0], "first_id": ds[0].draft_id})
    chains.sort(key=lambda c: (c["start"], c["first_id"]))

    out = []
    for i, c in enumerate(chains, 1):
        ex_first: dict[int, datetime] = {}
        for d in c["drafts"]:
            s = clip_of[d.draft_id][0]
            if d.exhibit_id not in ex_first or s < ex_first[d.exhibit_id]:
                ex_first[d.exhibit_id] = s
        chain_exhibits = sorted(ex_first.items(), key=lambda kv: (kv[1], kv[0]))

        periods = []
        for w in c["windows"]:
            p = {
                "type": w["type"],
                "start_ts": audit.iso(w["start"]),
                "end_ts": audit.iso(w["end"]),
                "active": w["active"],
                "draft_ids": sorted(w["members"]),
            }
            if w["type"] == CONFLICT_EXHIBIT:
                p["exhibit_id"] = w["exhibit_id"]
                p["exhibit_name"] = _ex_name(exhibits, w["exhibit_id"])
            else:
                p["case_id"] = w["case_id"]
                p["case_name"] = _case_name(cases, w["case_id"])
                p["capacity"] = w["capacity"]
            periods.append(p)

        out.append({
            "chain_no": i,
            "start_ts": audit.iso(c["start"]),
            "types": sorted({w["type"] for w in c["windows"]}),
            "exhibits": [{"id": ex, "name": _ex_name(exhibits, ex)}
                         for ex, _ in chain_exhibits],
            "cases": [{"id": cid, "name": _case_name(cases, cid)}
                      for cid in sorted({d.case_id for d in c["drafts"]})],
            "drafts": [
                {
                    "draft_id": d.draft_id,
                    "exhibit_id": d.exhibit_id,
                    "exhibit_name": _ex_name(exhibits, d.exhibit_id),
                    "case_id": d.case_id,
                    "case_name": _case_name(cases, d.case_id),
                    "start_ts": audit.iso(d.start),
                    "end_ts": audit.iso(d.end),
                    "open_ended": d.open_ended,
                    "status": d.status,
                    "note": d.note,
                }
                for d in c["drafts"]
            ],
            "periods": periods,
        })
    return out


def _involved_exhibits(chains: list[dict]) -> list[dict]:
    """Union of exhibits appearing in any chain, ordered by first conflict."""
    first: dict[int, str] = {}
    names: dict[int, str] = {}
    chain_nos: dict[int, list[int]] = {}
    for ch in chains:
        for ex in ch["exhibits"]:
            eid = ex["id"]
            names[eid] = ex["name"]
            if eid not in first or ch["start_ts"] < first[eid]:
                first[eid] = ch["start_ts"]
            chain_nos.setdefault(eid, []).append(ch["chain_no"])
    return [
        {"id": eid, "name": names[eid], "first_conflict_ts": ts,
         "chains": chain_nos[eid]}
        for eid, ts in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))
    ]


# ---------------------------------------------------------------------------
# Dose estimation
# ---------------------------------------------------------------------------

def estimate_interval_dose(
    series_by_case: dict[int, audit.CaseSeries],
    rates: dict[int, dict],
    case_id: int,
    start: datetime,
    end: datetime,
) -> dict:
    """Estimate lux*hours for one in-case segment.

    Measured dose comes from trapezoidal integration over real readings;
    occupied hours without reading coverage are extrapolated at the case's
    historical mean lux (``rates`` from ``audit.ambient_rates``).
    """
    total_h = audit.hours(start, end)
    cs = series_by_case.get(case_id)
    if cs is None or not cs.segments or total_h <= 0:
        return {"measured": 0.0, "estimated": 0.0, "covered_hours": 0.0,
                "hours": max(0.0, total_h), "note": "no_readings"}
    res = audit.integrate_window(series_by_case, [(start, end, case_id)], (start, end))
    covered = res.covered_hours
    uncovered = max(0.0, total_h - covered)
    rate = (rates.get(case_id) or {}).get("rate_lux_hours_per_occ_hour")
    if uncovered > 1e-9:
        if rate is not None:
            estimated = uncovered * rate
            note = ("measured+extrapolated" if covered > 1e-9
                    else "extrapolated_from_case_history")
        else:
            estimated = 0.0
            note = "uncovered_not_estimable"
    else:
        estimated = 0.0
        note = "measured"
    return {"measured": res.total_lux_hours, "estimated": estimated,
            "covered_hours": covered, "hours": total_h, "note": note}


# ---------------------------------------------------------------------------
# Full batch analysis
# ---------------------------------------------------------------------------

def analyze(
    drafts: list[DraftInterval],
    capacities: dict[int, int],
    series_by_case: dict[int, audit.CaseSeries],
    annual_window: tuple[datetime, datetime],
    exhibits: dict[int, dict],
    cases: dict[int, dict],
    baseline_occupancies: Optional[dict[int, list]] = None,
) -> dict:
    """Scan conflicts and estimate annual doses for one batch of drafts.

    Drafts are clipped to ``annual_window``; drafts not intersecting it are
    reported separately. ``capacities`` defaults to 1 for cases without an
    explicit entry. ``baseline_occupancies`` maps exhibit_id to real
    ``(start, end_or_None, case_id)`` occupancies already consuming the
    exhibit's annual budget.
    """
    rates = audit.ambient_rates(series_by_case, annual_window)

    clipped: list[tuple[DraftInterval, datetime, datetime]] = []
    outside: list[DraftInterval] = []
    for d in drafts:
        iv = audit.intersect((d.start, d.end), annual_window)
        if iv is None:
            outside.append(d)
        else:
            clipped.append((d, iv[0], iv[1]))
    clip_of = {d.draft_id: (s, e) for d, s, e in clipped}

    # --- conflict windows --------------------------------------------------
    windows: list[dict] = []
    by_exhibit: dict[int, list] = {}
    by_case: dict[int, list] = {}
    for d, s, e in clipped:
        by_exhibit.setdefault(d.exhibit_id, []).append((s, e, d.draft_id))
        by_case.setdefault(d.case_id, []).append((s, e, d.draft_id))
    for ex_id, entries in by_exhibit.items():
        for ws, we, members, peak in _over_threshold_windows(entries, 1):
            windows.append({"type": CONFLICT_EXHIBIT, "exhibit_id": ex_id,
                            "start": ws, "end": we, "members": members, "active": peak})
    for case_id, entries in by_case.items():
        cap = capacities.get(case_id, 1)
        for ws, we, members, peak in _over_threshold_windows(entries, cap):
            windows.append({"type": CONFLICT_CAPACITY, "case_id": case_id, "capacity": cap,
                            "start": ws, "end": we, "members": members, "active": peak})

    active_drafts = [d for d, _, _ in clipped]
    chains = _build_chains(active_drafts, clip_of, windows, exhibits, cases)

    # --- per-segment annual dose -------------------------------------------
    dose_of: dict[int, dict] = {}
    for d, s, e in clipped:
        dose_of[d.draft_id] = estimate_interval_dose(series_by_case, rates, d.case_id, s, e)

    # --- per-exhibit plans ---------------------------------------------------
    plans = []
    drafts_by_exhibit: dict[int, list] = {}
    for d, s, e in clipped:
        drafts_by_exhibit.setdefault(d.exhibit_id, []).append((d, s, e))
    for ex_id, items in drafts_by_exhibit.items():
        items.sort(key=lambda x: (x[1], x[0].draft_id))
        info = exhibits.get(ex_id) or {}
        limit = info.get("annual_limit_lux_hours")
        segments = []
        planned = 0.0
        for d, s, e in items:
            dose = dose_of[d.draft_id]
            seg_dose = dose["measured"] + dose["estimated"]
            planned += seg_dose
            segments.append({
                "draft_id": d.draft_id,
                "case_id": d.case_id,
                "case_name": _case_name(cases, d.case_id),
                "start_ts": audit.iso(s),
                "end_ts": audit.iso(e),
                "open_ended": d.open_ended,
                "status": d.status,
                "hours": round(dose["hours"], 4),
                "measured_lux_hours": round(dose["measured"], 4),
                "estimated_lux_hours": round(dose["estimated"], 4),
                "dose_lux_hours": round(seg_dose, 4),
                "estimate_note": dose["note"],
            })
        baseline = 0.0
        for (bs, be, bcid) in (baseline_occupancies or {}).get(ex_id, []):
            iv = audit.intersect((bs, be or annual_window[1]), annual_window)
            if iv is None:
                continue
            b = estimate_interval_dose(series_by_case, rates, bcid, iv[0], iv[1])
            baseline += b["measured"] + b["estimated"]
        total = baseline + planned
        exceeds = limit is not None and total > limit + 1e-9
        plans.append({
            "exhibit_id": ex_id,
            "exhibit_name": _ex_name(exhibits, ex_id),
            "annual_limit_lux_hours": limit,
            "baseline_lux_hours": round(baseline, 4),
            "planned_lux_hours": round(planned, 4),
            "total_lux_hours": round(total, 4),
            "exceeds_limit": exceeds,
            "excess_lux_hours": (round(max(0.0, total - limit), 4)
                                 if limit is not None else None),
            "first_segment_start_ts": audit.iso(items[0][1]),
            "segments": segments,
        })
    plans.sort(key=lambda p: (p["first_segment_start_ts"], p["exhibit_id"]))

    over_limit = [
        {
            "exhibit_id": p["exhibit_id"],
            "exhibit_name": p["exhibit_name"],
            "annual_limit_lux_hours": p["annual_limit_lux_hours"],
            "total_lux_hours": p["total_lux_hours"],
            "excess_lux_hours": p["excess_lux_hours"],
            "first_segment_start_ts": p["first_segment_start_ts"],
        }
        for p in plans if p["exceeds_limit"]
    ]

    return {
        "annual_window": {"start_ts": audit.iso(annual_window[0]),
                          "end_ts": audit.iso(annual_window[1])},
        "statuses_included": list(ACTIVE_STATUSES),
        "conflict_period_counts": {
            CONFLICT_EXHIBIT: sum(1 for w in windows if w["type"] == CONFLICT_EXHIBIT),
            CONFLICT_CAPACITY: sum(1 for w in windows if w["type"] == CONFLICT_CAPACITY),
        },
        "conflict_chains": chains,
        "involved_exhibits": _involved_exhibits(chains),
        "exhibit_plans": plans,
        "over_limit_plans": over_limit,
        "outside_annual_window": [
            {"draft_id": d.draft_id, "exhibit_id": d.exhibit_id,
             "exhibit_name": _ex_name(exhibits, d.exhibit_id),
             "case_id": d.case_id, "case_name": _case_name(cases, d.case_id),
             "start_ts": audit.iso(d.start), "end_ts": audit.iso(d.end),
             "open_ended": d.open_ended, "status": d.status}
            for d in sorted(outside, key=lambda x: (x.start, x.draft_id))
        ],
    }


# ---------------------------------------------------------------------------
# Persistence helpers (sqlite3 connection from app.db)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_batch(conn, *, name: str, note: str,
                 annual_start: Optional[datetime],
                 capacities: list[dict], drafts: list[dict]) -> int:
    """Insert one batch with capacities and drafts (each draft at version 1)."""
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO rotation_batches(name, note, annual_start, created_at) VALUES (?,?,?,?)",
        (name, note, audit.iso(annual_start) if annual_start else None, now))
    batch_id = cur.lastrowid
    for c in capacities:
        conn.execute(
            "INSERT INTO rotation_case_capacities(batch_id, case_id, capacity) VALUES (?,?,?)",
            (batch_id, c["case_id"], c["capacity"]))
    for d in drafts:
        cur = conn.execute(
            "INSERT INTO rotation_drafts(batch_id, exhibit_id, case_id, start_ts, end_ts,"
            " note, status, version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'draft',1,?,?)",
            (batch_id, d["exhibit_id"], d["case_id"], audit.iso(d["start"]),
             audit.iso(d["end"]) if d["end"] else None, d.get("note", ""), now, now))
        conn.execute(
            "INSERT INTO rotation_draft_versions(draft_id, version, status, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (cur.lastrowid, 1, "draft", "创建草案", now))
    return batch_id


def list_batches(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT b.*,"
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id = b.id) AS draft_count,"
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id = b.id"
        "   AND d.status = 'accepted') AS accepted_count,"
        " (SELECT COUNT(*) FROM rotation_drafts d WHERE d.batch_id = b.id"
        "   AND d.status = 'revoked') AS revoked_count"
        " FROM rotation_batches b ORDER BY b.id DESC").fetchall()
    return [dict(r) for r in rows]


def set_draft_status(conn, draft_id: int, new_status: str, note: str = "") -> dict:
    """Accept/revoke one draft; every transition appends a version row."""
    row = conn.execute("SELECT * FROM rotation_drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise LookupError(f"rotation_drafts id={draft_id} 不存在")
    if new_status not in ("accepted", "revoked"):
        raise ValueError(f"非法状态: {new_status}")
    if row["status"] == new_status:
        raise ValueError(f"草案 #{draft_id} 已处于 {new_status} 状态")
    version = row["version"] + 1
    now = _now_iso()
    conn.execute(
        "UPDATE rotation_drafts SET status=?, version=?, updated_at=? WHERE id=?",
        (new_status, version, now, draft_id))
    conn.execute(
        "INSERT INTO rotation_draft_versions(draft_id, version, status, note, created_at)"
        " VALUES (?,?,?,?,?)", (draft_id, version, new_status, note, now))
    return {"id": draft_id, "batch_id": row["batch_id"], "status": new_status,
            "version": version, "updated_at": now}


def list_draft_versions(conn, draft_id: int) -> list[dict]:
    row = conn.execute("SELECT id FROM rotation_drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise LookupError(f"rotation_drafts id={draft_id} 不存在")
    return [dict(r) for r in conn.execute(
        "SELECT * FROM rotation_draft_versions WHERE draft_id=? ORDER BY version",
        (draft_id,)).fetchall()]


def load_case_series(conn) -> dict[int, audit.CaseSeries]:
    out: dict[int, audit.CaseSeries] = {}
    for c in conn.execute("SELECT * FROM cases").fetchall():
        rows = [
            (audit.parse_ts(r["ts"]), r["lux"])
            for r in conn.execute(
                "SELECT ts, lux FROM readings WHERE case_id = ? ORDER BY ts", (c["id"],))
        ]
        if rows:
            out[c["id"]] = audit.make_case_series(
                c["id"], c["name"], rows, c["gap_threshold_minutes"])
    return out


def batch_payload(conn, batch_id: int) -> dict:
    """Batch detail: meta, capacities, all drafts (any status) + fresh analysis."""
    batch = conn.execute("SELECT * FROM rotation_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise LookupError(f"rotation_batches id={batch_id} 不存在")
    capacities = [dict(r) for r in conn.execute(
        "SELECT cc.case_id, c.name AS case_name, cc.capacity"
        " FROM rotation_case_capacities cc JOIN cases c ON c.id = cc.case_id"
        " WHERE cc.batch_id=? ORDER BY c.name", (batch_id,)).fetchall()]
    drafts = [dict(r) for r in conn.execute(
        "SELECT d.*, e.name AS exhibit_name, c.name AS case_name"
        " FROM rotation_drafts d"
        " JOIN exhibits e ON e.id = d.exhibit_id"
        " JOIN cases c ON c.id = d.case_id"
        " WHERE d.batch_id=? ORDER BY d.start_ts, d.id", (batch_id,)).fetchall()]
    return {
        "batch": dict(batch),
        "capacities": capacities,
        "drafts": drafts,
        "analysis": analyze_batch(conn, batch_id),
    }


def analyze_batch(conn, batch_id: int) -> dict:
    """Load one batch from the DB and run the conflict/dose analysis."""
    batch = conn.execute("SELECT * FROM rotation_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise LookupError(f"rotation_batches id={batch_id} 不存在")
    rows = conn.execute(
        "SELECT * FROM rotation_drafts WHERE batch_id=? ORDER BY start_ts, id",
        (batch_id,)).fetchall()
    capacities = {r["case_id"]: r["capacity"] for r in conn.execute(
        "SELECT case_id, capacity FROM rotation_case_capacities WHERE batch_id=?",
        (batch_id,)).fetchall()}
    exhibits = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM exhibits").fetchall()}
    cases = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM cases").fetchall()}

    starts = [audit.parse_ts(r["start_ts"]) for r in rows if r["status"] in ACTIVE_STATUSES]
    if not starts:
        starts = [audit.parse_ts(r["start_ts"]) for r in rows]
    if batch["annual_start"]:
        annual_start = audit.parse_ts(batch["annual_start"])
    elif starts:
        annual_start = datetime(min(starts).year, 1, 1, tzinfo=timezone.utc)
    else:
        annual_start = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)
    annual_window = (annual_start, annual_start + timedelta(days=365))

    intervals = []
    for r in rows:
        if r["status"] not in ACTIVE_STATUSES:
            continue
        s = audit.parse_ts(r["start_ts"])
        e = audit.parse_ts(r["end_ts"]) if r["end_ts"] else None
        intervals.append(DraftInterval(
            draft_id=r["id"], exhibit_id=r["exhibit_id"], case_id=r["case_id"],
            start=s, end=e or annual_window[1], open_ended=e is None,
            status=r["status"], note=r["note"]))

    baseline: dict[int, list] = {}
    for ex_id in {d.exhibit_id for d in intervals}:
        baseline[ex_id] = [
            (audit.parse_ts(o["start_ts"]),
             audit.parse_ts(o["end_ts"]) if o["end_ts"] else None,
             o["case_id"])
            for o in conn.execute(
                "SELECT * FROM occupancies WHERE exhibit_id=? ORDER BY start_ts",
                (ex_id,)).fetchall()]

    result = analyze(intervals, capacities, load_case_series(conn), annual_window,
                     exhibits, cases, baseline)
    result["generated_at"] = _now_iso()
    return result
