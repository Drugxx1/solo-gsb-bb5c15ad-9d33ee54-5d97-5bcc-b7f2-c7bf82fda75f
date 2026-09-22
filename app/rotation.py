"""轮换排期：批量在柜草案的冲突扫描与年度剂量估算。

一个「批次」= 一次提交的多件展品在柜计划草案 + 展柜容量表。扫描全部
基于**半开区间** ``[start, end)``（一端结束与另一端开始恰好相接不算重叠）：

* 同柜重叠 ``case_overlap``：同一展柜内同时在柜的草案数超过该柜容量
  （容量未配置的展柜默认 1）
* 同一展品多柜冲突 ``exhibit_double_booking``：同一展品在同一时间被
  安排进两个及以上不同展柜（同一展柜的重复草案不算多柜冲突，但会
  被同柜容量扫描捕获）

冲突链 = 冲突关系图的连通分量；链与链内草案均按开始时间**稳定排序**
（开始时间相同则保持提交顺序，即草案 id 升序）。

每份有效草案（pending / accepted，已撤销的不参与）的年度剂量复用
:mod:`app.audit` 的梯形积分：读数覆盖内按真实照度曲线积分，覆盖之外
按该柜历史平均照度外推；按 展品 × 日历年 汇总后与展品年度上限比较，
输出超限计划。年度窗口沿用既有约定：草案开始时刻所在公历年
1 月 1 日（UTC）起 365 天。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import audit

UTC = timezone.utc

ACTIVE_STATUSES = ("pending", "accepted")
DEFAULT_CASE_CAPACITY = 1


@dataclass
class Draft:
    id: int
    exhibit_id: int
    exhibit_name: str
    case_id: int
    case_name: str
    start: datetime
    end: Optional[datetime]          # None = 开放（计划仍在柜）
    status: str = "pending"
    note: str = ""

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


# ---------------------------------------------------------------------------
# Half-open interval sweep
# ---------------------------------------------------------------------------

def _over_capacity_runs(
    items: list[tuple[datetime, Optional[datetime], object, int]],
    capacity: int,
) -> list[tuple[datetime, Optional[datetime], list[int], int]]:
    """Maximal half-open runs where distinct active keys exceed ``capacity``.

    ``items`` are ``(start, end_or_None, key, draft_id)``. Occupants sharing
    a key count once (for the exhibit sweep the key is the case id, so two
    drafts in the *same* case are not a multi-case conflict). Returns
    ``(start, end_or_None, sorted_draft_ids, peak_count)`` tuples; ``end``
    is None when the run never closes (open-ended drafts).
    """
    events: list[tuple[datetime, int, object, int]] = []
    for s, e, key, did in items:
        events.append((s, +1, key, did))
        if e is not None:
            events.append((e, -1, key, did))
    # Half-open: at one timestamp the end-event and start-event cancel out;
    # applying all events at a time together yields exactly that semantics.
    events.sort(key=lambda ev: (ev[0], ev[1]))

    active: dict[object, set[int]] = {}
    runs: list[tuple[datetime, Optional[datetime], list[int], int]] = []
    run_start: Optional[datetime] = None
    run_ids: set[int] = set()
    peak = 0
    i, n = 0, len(events)
    while i < n:
        t = events[i][0]
        while i < n and events[i][0] == t:
            _, kind, key, did = events[i]
            ids = active.setdefault(key, set())
            if kind > 0:
                ids.add(did)
            else:
                ids.discard(did)
            if not ids:
                active.pop(key, None)
            i += 1
        if len(active) > capacity:
            if run_start is None:
                run_start = t
                run_ids = set()
                peak = 0
            peak = max(peak, len(active))
            for ids in active.values():
                run_ids |= ids
        elif run_start is not None:
            runs.append((run_start, t, sorted(run_ids), peak))
            run_start = None
            run_ids = set()
    if run_start is not None:
        runs.append((run_start, None, sorted(run_ids), peak))
    return runs


# ---------------------------------------------------------------------------
# Annual dose estimation (reuses the audit trapezoid math)
# ---------------------------------------------------------------------------

def _year_window(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, tzinfo=UTC)
    return start, start + timedelta(days=365)


def estimate_segment_dose(
    series_by_case: dict[int, audit.CaseSeries],
    mean_lux_by_case: dict[int, Optional[float]],
    case_id: int,
    start: datetime,
    end: datetime,
) -> dict:
    """Estimate one segment's dose [lux*h] from the case's lux curve.

    Measured dose = trapezoid integral of real readings inside the segment;
    occupied time outside reading coverage is filled with the case's
    historical mean lux. ``estimated_lux_hours`` is None when the case has
    no usable readings at all.
    """
    res = audit.integrate_window(series_by_case, [(start, end, case_id)], (start, end))
    measured = res.total_lux_hours
    covered_h = res.covered_hours
    uncovered_h = max(0.0, round(res.occupied_hours - covered_h, 6))
    mean_lux = mean_lux_by_case.get(case_id)
    extrapolated = uncovered_h * mean_lux if mean_lux is not None else None
    estimated = None if extrapolated is None else measured + extrapolated
    return {
        "window": {"start_ts": audit.iso(start), "end_ts": audit.iso(end)},
        "measured_lux_hours": round(measured, 4),
        "covered_hours": round(covered_h, 4),
        "uncovered_hours": round(uncovered_h, 4),
        "mean_lux_reference": mean_lux,
        "extrapolated_lux_hours": round(extrapolated, 4) if extrapolated is not None else None,
        "estimated_lux_hours": round(estimated, 4) if estimated is not None else None,
        "estimable": estimated is not None,
    }


def _case_mean_lux(series_by_case: dict[int, audit.CaseSeries]) -> dict[int, Optional[float]]:
    """Historical mean lux of every case over its own full reading coverage."""
    covered = [cs for cs in series_by_case.values() if cs.segments]
    if not covered:
        return {}
    lo = min(cs.coverage_start for cs in covered)
    hi = max(cs.coverage_end for cs in covered)
    rates = audit.ambient_rates(series_by_case, (lo, hi))
    return {cid: r["mean_lux"] for cid, r in rates.items()}


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------

def analyze(
    drafts: list[Draft],
    capacities: dict[int, int],
    series_by_case: dict[int, audit.CaseSeries],
    annual_limits: dict[int, float],
    committed: Optional[dict[int, list[tuple[datetime, Optional[datetime], int]]]] = None,
) -> dict:
    """Scan a batch of drafts and estimate annual doses.

    ``drafts`` must be in submission order (ascending id) — the stable
    start-time ordering of chains/drafts keeps that order on ties.
    ``committed`` maps exhibit_id to its existing occupancy intervals and is
    added into the annual budget check (real readings only).
    """
    committed = committed or {}
    mean_lux = _case_mean_lux(series_by_case)
    active = [d for d in drafts if d.active]
    draft_by_id = {d.id: d for d in drafts}

    # --- conflicts ---------------------------------------------------------
    conflicts: list[dict] = []

    by_case: dict[int, list[Draft]] = {}
    for d in active:
        by_case.setdefault(d.case_id, []).append(d)
    for cid, ds in sorted(by_case.items()):
        cap = capacities.get(cid, DEFAULT_CASE_CAPACITY)
        items = [(d.start, d.end, d.id, d.id) for d in ds]
        for s, e, ids, peak in _over_capacity_runs(items, cap):
            conflicts.append({
                "type": "case_overlap",
                "case_id": cid,
                "case_name": ds[0].case_name,
                "start_ts": audit.iso(s),
                "end_ts": audit.iso(e) if e else None,
                "capacity": cap,
                "peak_occupants": peak,
                "draft_ids": ids,
                "detail": f"{peak} 份草案同时在柜，超出展柜容量 {cap}",
            })

    by_exhibit: dict[int, list[Draft]] = {}
    for d in active:
        by_exhibit.setdefault(d.exhibit_id, []).append(d)
    for eid, ds in sorted(by_exhibit.items()):
        items = [(d.start, d.end, d.case_id, d.id) for d in ds]
        for s, e, ids, peak in _over_capacity_runs(items, 1):
            conflicts.append({
                "type": "exhibit_double_booking",
                "exhibit_id": eid,
                "exhibit_name": ds[0].exhibit_name,
                "start_ts": audit.iso(s),
                "end_ts": audit.iso(e) if e else None,
                "peak_cases": peak,
                "draft_ids": ids,
                "detail": f"同一展品同时被安排进 {peak} 个不同展柜",
            })

    # Stable ordering by start time; ties keep detection order (case scan
    # before exhibit scan, ascending ids inside each).
    conflicts.sort(key=lambda c: c["start_ts"])

    # --- conflict chains (connected components over drafts) ----------------
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in conflicts:
        ids = c["draft_ids"]
        for other in ids[1:]:
            ra, rb = find(ids[0]), find(other)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    chain_map: dict[int, dict] = {}
    for c in conflicts:
        root = find(c["draft_ids"][0])
        ch = chain_map.setdefault(root, {"draft_ids": set(), "conflicts": []})
        ch["draft_ids"].update(c["draft_ids"])
        ch["conflicts"].append(c)

    chains: list[dict] = []
    for ch in chain_map.values():
        members = sorted(ch["draft_ids"], key=lambda did: (draft_by_id[did].start, did))
        exhibits, cases, seen_e, seen_c = [], [], set(), set()
        for did in members:
            d = draft_by_id[did]
            if d.exhibit_id not in seen_e:
                seen_e.add(d.exhibit_id)
                exhibits.append({"id": d.exhibit_id, "name": d.exhibit_name})
            if d.case_id not in seen_c:
                seen_c.add(d.case_id)
                cases.append({"id": d.case_id, "name": d.case_name})
        chains.append({
            "start_ts": audit.iso(draft_by_id[members[0]].start),
            "draft_ids": members,
            "exhibits": exhibits,
            "cases": cases,
            "conflicts": ch["conflicts"],
        })
    chains.sort(key=lambda ch: (ch["start_ts"], ch["draft_ids"][0]))
    for i, ch in enumerate(chains, 1):
        ch["chain_id"] = i

    conflicted_ids: set[int] = set()
    for c in conflicts:
        conflicted_ids.update(c["draft_ids"])

    # --- per-draft annual dose estimates ------------------------------------
    estimates: dict[int, dict] = {}
    for d in active:
        ys, ye = _year_window(d.start.year)
        dose_end = min(d.end, ye) if d.end else ye
        est = estimate_segment_dose(series_by_case, mean_lux, d.case_id, d.start, dose_end)
        est["year"] = d.start.year
        est["clipped_to_calendar_year"] = d.end is None or d.end > ye
        estimates[d.id] = est

    draft_json: list[dict] = []
    for d in sorted(drafts, key=lambda x: (x.start, x.id)):
        draft_json.append({
            "id": d.id,
            "exhibit_id": d.exhibit_id,
            "exhibit_name": d.exhibit_name,
            "case_id": d.case_id,
            "case_name": d.case_name,
            "start_ts": audit.iso(d.start),
            "end_ts": audit.iso(d.end) if d.end else None,
            "status": d.status,
            "note": d.note,
            "in_conflict": d.id in conflicted_ids,
            "annual_estimate": estimates.get(d.id),
        })

    # --- annual totals vs. limits -------------------------------------------
    annual: dict[tuple[int, int], dict] = {}
    for d in active:
        est = estimates[d.id]
        agg = annual.setdefault((d.exhibit_id, d.start.year), {
            "exhibit_id": d.exhibit_id,
            "exhibit_name": d.exhibit_name,
            "year": d.start.year,
            "planned_lux_hours": 0.0,
            "has_unestimable_segments": False,
            "draft_ids": [],
        })
        agg["draft_ids"].append(d.id)
        if est["estimable"]:
            agg["planned_lux_hours"] += est["estimated_lux_hours"]
        else:
            agg["has_unestimable_segments"] = True

    annual_totals: list[dict] = []
    exceeding: list[dict] = []
    for (eid, year), agg in annual.items():
        agg["draft_ids"].sort(key=lambda did: (draft_by_id[did].start, did))
        limit = annual_limits.get(eid)
        committed_dose = 0.0
        occ = committed.get(eid)
        if occ:
            committed_dose = audit.integrate_window(
                series_by_case, occ, _year_window(year)).total_lux_hours
        total = committed_dose + agg["planned_lux_hours"]
        entry = {
            **agg,
            "annual_limit_lux_hours": limit,
            "committed_lux_hours": round(committed_dose, 4),
            "planned_lux_hours": round(agg["planned_lux_hours"], 4),
            "projected_total_lux_hours": round(total, 4),
            "exceeds_limit": bool(limit is not None and total > limit),
            "over_by_lux_hours": (round(max(0.0, total - limit), 4)
                                  if limit is not None else None),
        }
        annual_totals.append(entry)
        if entry["exceeds_limit"]:
            exceeding.append(entry)
    annual_totals.sort(key=lambda e: (e["year"], e["exhibit_name"]))
    exceeding.sort(key=lambda e: (e["year"], e["exhibit_name"]))

    return {
        "drafts": draft_json,
        "conflicts": conflicts,
        "conflict_chains": chains,
        "annual_totals": annual_totals,
        "exceeding_plans": exceeding,
        "summary": {
            "draft_count": len(drafts),
            "active_draft_count": len(active),
            "revoked_draft_count": len(drafts) - len(active),
            "conflict_count": len(conflicts),
            "conflict_chain_count": len(chains),
            "exceeding_plan_count": len(exceeding),
        },
        "method": {
            "interval_semantics": "half-open [start, end); touching endpoints do not overlap",
            "default_case_capacity": DEFAULT_CASE_CAPACITY,
            "active_statuses": list(ACTIVE_STATUSES),
            "dose_estimate": "读数覆盖内梯形积分 + 覆盖外按该柜历史平均照度外推",
            "annual_window": "草案开始时刻所在公历年 1 月 1 日（UTC）起 365 天",
            "committed_dose": "年度预算核对计入该展品已有在柜记录的真实读数剂量",
            "ordering": "冲突链与草案按开始时间稳定排序；并列时保持提交顺序（草案 id 升序）",
        },
    }
