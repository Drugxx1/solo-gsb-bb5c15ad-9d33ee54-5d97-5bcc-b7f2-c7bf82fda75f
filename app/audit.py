"""Light-dose audit math.

Dose is accumulated with the trapezoidal rule on adjacent lux readings::

    segment dose [lux*h] = (lux_a + lux_b) / 2 * (t_b - t_a) [hours]

A reading gap longer than the case's ``gap_threshold_minutes`` is a sensor
breakpoint: the trapezoid straddling it is still computed, but it is flagged
"breakpoint" and can be excluded from the audited total. Dose is only counted
while the exhibit is physically inside the case (occupancy intervals).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Series / segments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    ts: datetime
    lux: float


@dataclass(frozen=True)
class Segment:
    t0: datetime
    t1: datetime
    lux0: float
    lux1: float
    dose: float          # trapezoidal lux*hours over the full reading interval
    breakpoint: bool     # gap exceeds the sensor breakpoint threshold


def build_segments(
    series: Iterable[tuple[datetime, float]],
    gap_threshold: timedelta = timedelta(minutes=60),
) -> list[Segment]:
    """Turn (timestamp, lux) rows into trapezoid segments.

    Duplicate timestamps keep the last reading. Input need not be sorted.
    """
    by_ts: dict[datetime, float] = {}
    for ts, lux in series:
        by_ts[ts] = float(lux)
    pts = sorted((Point(ts, lux) for ts, lux in by_ts.items()), key=lambda p: p.ts)

    segs: list[Segment] = []
    for a, b in zip(pts, pts[1:]):
        if b.ts <= a.ts:
            continue
        h = hours(a.ts, b.ts)
        segs.append(
            Segment(
                t0=a.ts,
                t1=b.ts,
                lux0=a.lux,
                lux1=b.lux,
                dose=(a.lux + b.lux) / 2.0 * h,
                breakpoint=(b.ts - a.ts) > gap_threshold,
            )
        )
    return segs


@dataclass
class CaseSeries:
    case_id: int
    case_name: str
    gap_threshold: timedelta
    segments: list[Segment]
    starts: list[datetime]
    ends: list[datetime]

    @property
    def coverage_start(self) -> Optional[datetime]:
        return self.segments[0].t0 if self.segments else None

    @property
    def coverage_end(self) -> Optional[datetime]:
        return self.segments[-1].t1 if self.segments else None

    def lux_at(self, t: datetime) -> Optional[tuple[float, bool]]:
        """Interpolated (lux, breakpoint_adjusted?) at time ``t``.

        Returns None when ``t`` lies outside the measured coverage window.
        A point exactly at a reading timestamp returns that reading.
        """
        if not self.segments:
            return None
        if t < self.segments[0].t0 or t > self.segments[-1].t1:
            return None
        # Segment whose t1 (ends[i]) is the first end >= t; at an exact knot
        # timestamp this selects the segment ending at that timestamp.
        i = bisect.bisect_left(self.ends, t)
        if i >= len(self.segments):
            i = len(self.segments) - 1
        seg = self.segments[i]
        if t < seg.t0 or t > seg.t1:
            return None
        span = hours(seg.t0, seg.t1)
        if span <= 0:
            return (seg.lux1, seg.breakpoint)
        frac = hours(seg.t0, t) / span
        return (seg.lux0 + (seg.lux1 - seg.lux0) * frac, seg.breakpoint)


def make_case_series(
    case_id: int,
    case_name: str,
    rows: Iterable[tuple[datetime, float]],
    gap_threshold_minutes: int,
) -> CaseSeries:
    segs = build_segments(rows, timedelta(minutes=gap_threshold_minutes))
    return CaseSeries(
        case_id=case_id,
        case_name=case_name,
        gap_threshold=timedelta(minutes=gap_threshold_minutes),
        segments=segs,
        starts=[s.t0 for s in segs],
        ends=[s.t1 for s in segs],
    )


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------

def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [ordered[0]]
    for s, e in ordered[1:]:
        ms, me = merged[-1]
        if s <= me:
            if e > me:
                merged[-1] = (ms, e)
        else:
            merged.append((s, e))
    return merged


def intersect(
    a: tuple[datetime, datetime], b: tuple[datetime, datetime]
) -> Optional[tuple[datetime, datetime]]:
    s, e = max(a[0], b[0]), min(a[1], b[1])
    return (s, e) if s < e else None


def invert_intervals(
    within: tuple[datetime, datetime], occupied: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Complement of (merged) occupied intervals inside ``within``."""
    gaps: list[tuple[datetime, datetime]] = []
    cursor = within[0]
    for s, e in merge_intervals(occupied):
        if s > cursor:
            gaps.append((cursor, min(s, within[1])))
        if e > cursor:
            cursor = e
        if cursor >= within[1]:
            break
    if cursor < within[1]:
        gaps.append((cursor, within[1]))
    return gaps


# ---------------------------------------------------------------------------
# Dose integration across cases and occupancy intervals
# ---------------------------------------------------------------------------

@dataclass
class WindowResult:
    total_lux_hours: float
    audited_lux_hours: float      # excluding trapezoids flagged as breakpoints
    breakpoint_lux_hours: float
    occupied_hours: float
    covered_hours: float          # occupied time actually covered by readings
    breakpoint_evidence: list[dict]
    coverage_gaps: list[dict]     # occupied time with no readings at all
    by_case: dict[int, dict]


def integrate_window(
    series_by_case: dict[int, CaseSeries],
    occupancies: list[tuple[datetime, Optional[datetime], int]],
    window: tuple[datetime, datetime],
) -> WindowResult:
    """Integrate dose over ``window`` for the given occupancy schedule.

    Each occupancy is ``(start, end_or_None, case_id)``. An open-ended
    occupancy runs to the window end. Breakpoint-trapezoid dose is included in
    ``total_lux_hours`` but excluded from ``audited_lux_hours``.
    """
    occ_intervals: list[tuple[datetime, datetime]] = []
    for s, e, cid in occupancies:
        iv = intersect((s, e or window[1]), window)
        if iv:
            occ_intervals.append((iv[0], iv[1], cid))

    total = audited = bp_dose = occ_hours = covered_hours = 0.0
    evidence: list[dict] = []
    gaps: list[dict] = []
    by_case: dict[int, dict] = {}

    def case_bucket(cid: int) -> dict:
        if cid not in by_case:
            name = series_by_case[cid].case_name if cid in series_by_case else f"#{cid}"
            by_case[cid] = {
                "case_id": cid,
                "case_name": name,
                "total_lux_hours": 0.0,
                "audited_lux_hours": 0.0,
                "breakpoint_lux_hours": 0.0,
                "occupied_hours": 0.0,
                "covered_hours": 0.0,
            }
        return by_case[cid]

    # --- trapezoid contributions, clipped to occupancy ∩ window ----------
    for s, e, cid in occ_intervals:
        bucket = case_bucket(cid)
        occ_hours += hours(s, e)
        bucket["occupied_hours"] += hours(s, e)

        cs = series_by_case.get(cid)
        if cs is None or not cs.segments:
            gaps.append(
                {
                    "case_id": cid,
                    "case_name": f"#{cid}",
                    "start": iso(s),
                    "end": iso(e),
                    "reason": "no_readings",
                    "hours": round(hours(s, e), 4),
                }
            )
            continue

        lo = bisect.bisect_left(cs.ends, s)
        hi = bisect.bisect_right(cs.starts, e)
        if lo > 0:
            lo -= 1  # segment straddling the occupancy start

        local_covered = 0.0
        for seg in cs.segments[lo:hi]:
            clip = intersect((seg.t0, seg.t1), (s, e))
            if clip is None:
                continue
            local_covered += hours(*clip)
            if clip == (seg.t0, seg.t1):
                dose = seg.dose
            else:
                # Re-integrate the clipped trapezoid with linear interpolation.
                l0, _ = cs.lux_at(clip[0])  # type: ignore[misc]
                l1, _ = cs.lux_at(clip[1])  # type: ignore[misc]
                dose = (l0 + l1) / 2.0 * hours(clip[0], clip[1])
            total += dose
            bucket["total_lux_hours"] += dose
            if seg.breakpoint:
                bp_dose += dose
                bucket["breakpoint_lux_hours"] += dose
                evidence.append(
                    {
                        "case_id": cid,
                        "case_name": cs.case_name,
                        "reading_start": iso(seg.t0),
                        "reading_end": iso(seg.t1),
                        "occupied_start": iso(s),
                        "occupied_end": iso(e),
                        "gap_hours": round(hours(seg.t0, seg.t1), 4),
                        "threshold_hours": round(cs.gap_threshold.total_seconds() / 3600, 4),
                        "lux_start": round(seg.lux0, 3),
                        "lux_end": round(seg.lux1, 3),
                        "lux_hours_in_occupancy": round(dose, 4),
                        "status": "flagged: counted in total, excluded from audited",
                    }
                )
            else:
                audited += dose
                bucket["audited_lux_hours"] += dose
        # The audited view is recomputed below as total - breakpoint dose.
        covered_hours += local_covered
        bucket["covered_hours"] += local_covered

        # Occupied time outside the case's measurement coverage = data gap.
        cover_iv = intersect((cs.coverage_start, cs.coverage_end), (s, e))  # type: ignore[arg-type]
        missing = invert_intervals(
            (s, e), [cover_iv] if cover_iv else []
        )
        for gs, ge in missing:
            gaps.append(
                {
                    "case_id": cid,
                    "case_name": cs.case_name,
                    "start": iso(gs),
                    "end": iso(ge),
                    "reason": "outside_reading_coverage",
                    "hours": round(hours(gs, ge), 4),
                }
            )

    # audited view excludes breakpoint dose entirely
    audited_total = total - bp_dose

    evidence.sort(key=lambda x: x["reading_start"])
    gaps.sort(key=lambda x: x["start"])
    for b in by_case.values():
        b["audited_lux_hours"] = b["total_lux_hours"] - b["breakpoint_lux_hours"]
        for k in ("total_lux_hours", "audited_lux_hours", "breakpoint_lux_hours",
                  "occupied_hours", "covered_hours"):
            b[k] = round(b[k], 4)

    return WindowResult(
        total_lux_hours=round(total, 4),
        audited_lux_hours=round(audited_total, 4),
        breakpoint_lux_hours=round(bp_dose, 4),
        occupied_hours=round(occ_hours, 4),
        covered_hours=round(covered_hours, 4),
        breakpoint_evidence=evidence,
        coverage_gaps=gaps,
        by_case=by_case,
    )


# ---------------------------------------------------------------------------
# Cumulative dose curve
# ---------------------------------------------------------------------------

def cumulative_curve(
    series_by_case: dict[int, CaseSeries],
    occupancies: list[tuple[datetime, Optional[datetime], int]],
    window: tuple[datetime, datetime],
    *,
    step_hours: float = 1.0,
) -> list[dict]:
    """Cumulative lux*hours sampled on a grid + every event timestamp.

    Between samples we use the midpoint-trapezoid rule on the piecewise-linear
    lux signal, which integrates a linear function exactly.
    """
    # Build per-case occupied intervals, clipped to the window.
    by_case_ivs: dict[int, list[tuple[datetime, datetime]]] = {}
    for s, e, cid in occupancies:
        iv = intersect((s, e or window[1]), window)
        if iv:
            by_case_ivs.setdefault(cid, []).append(iv)
    for cid in by_case_ivs:
        by_case_ivs[cid] = merge_intervals(by_case_ivs[cid])

    events = {window[0], window[1]}
    occupied_union: list[tuple[datetime, datetime]] = []
    for cid, ivs in by_case_ivs.items():
        cs = series_by_case.get(cid)
        occupied_union.extend(ivs)
        for s, e in ivs:
            events.add(s)
            events.add(e)
            if cs:
                # Every reading knot inside an occupied span must be an event:
                # the midpoint rule is only exact on a linear piece, so knots
                # of the piecewise-linear lux signal become grid points.
                i0 = bisect.bisect_left(cs.starts, s)
                i1 = bisect.bisect_right(cs.ends, e)
                for seg in cs.segments[max(0, i0 - 1): min(len(cs.segments), i1 + 1)]:
                    if s <= seg.t0 <= e:
                        events.add(seg.t0)
                    if s <= seg.t1 <= e:
                        events.add(seg.t1)

    # Step grid is only laid over occupied time, so long pre-occupancy gaps
    # don't bloat the curve with flat zero points.
    step = timedelta(hours=step_hours)
    for s, e in merge_intervals(occupied_union):
        t = s
        while t <= e:
            events.add(t)
            t += step
    times = sorted(t for t in events if window[0] <= t <= window[1])

    def state_at(cid: int, ivs: list[tuple[datetime, datetime]], t: datetime) -> tuple[float, bool]:
        """Return lux for a case at time t if occupied and covered, else 0."""
        idx = bisect.bisect_right(ivs, (t, datetime.max.replace(tzinfo=timezone.utc))) - 1
        if idx < 0 or not (ivs[idx][0] <= t <= ivs[idx][1]):
            return 0.0
        cs = series_by_case.get(cid)
        if cs is None:
            return 0.0
        found = cs.lux_at(t)
        return found[0] if found else 0.0

    cumulative = 0.0
    out: list[dict] = []
    prev = times[0]
    for t in times:
        if t > prev:
            mid = prev + (t - prev) / 2
            # midpoint rule on a linear segment == exact trapezoid integral
            dose_step = 0.0
            for cid, ivs in by_case_ivs.items():
                lux_mid = state_at(cid, ivs, mid)
                if lux_mid:
                    dose_step += lux_mid * hours(prev, t)
            cumulative += dose_step
        out.append({"ts": iso(t), "cumulative_lux_hours": round(cumulative, 3)})
        prev = t
    return out


# ---------------------------------------------------------------------------
# Rotation simulation / limit prediction
# ---------------------------------------------------------------------------

@dataclass
class RateSegment:
    case_id: int
    case_name: str
    start_ts: str
    end_ts: Optional[str]      # None => open-ended
    mean_lux: Optional[float]
    reference_start: Optional[str]
    reference_end: Optional[str]
    reference_hours: Optional[float]
    source: str                # "case_historical_curve"


@dataclass
class ScenarioResult:
    label: str
    rate_lux_hours_per_occ_hour: Optional[float]
    mean_lux: Optional[float]
    annual_dose_ytd: float
    remaining_budget_lux_hours: float
    projected_limit_ts: Optional[str]
    projected_limit_crosses_reset: bool
    projected_curve: list[dict]
    occupied_hours_ytd: float
    covered_hours_ytd: float
    note: str
    rate_segments: list[RateSegment]


def ambient_rates(
    series_by_case: dict[int, CaseSeries],
    window: tuple[datetime, datetime],
) -> dict[int, dict]:
    """Time-averaged lux of each case's *own* historical light regime.

    Independent of any exhibit occupancy: a hypothetical object staying in
    the case 24 h/day over the window would accumulate the case's raw-series
    dose. The rate (lux*h per occupied hour = mean lux) is what drives the
    post-move projection ("according to both cases' historical curves").
    Only the overlap of ``window`` with the case's reading coverage counts.
    """
    rates: dict[int, dict] = {}
    for cid, cs in series_by_case.items():
        if not cs.segments:
            continue
        lo = max(window[0], cs.coverage_start)
        hi = min(window[1], cs.coverage_end)
        if hi <= lo:
            continue
        # Trapezoid integral over the clipped window (linear interpolation at
        # the clipping boundaries keeps this consistent with the audit math).
        total = 0.0
        bp_dose = 0.0
        start_i = bisect.bisect_left(cs.ends, lo)
        end_i = bisect.bisect_right(cs.starts, hi)
        for seg in cs.segments[start_i:end_i]:
            clip = intersect((seg.t0, seg.t1), (lo, hi))
            if clip is None:
                continue
            if clip == (seg.t0, seg.t1):
                dose = seg.dose
            else:
                l0, _ = cs.lux_at(clip[0])  # type: ignore[misc]
                l1, _ = cs.lux_at(clip[1])  # type: ignore[misc]
                dose = (l0 + l1) / 2.0 * hours(clip[0], clip[1])
            total += dose
            if seg.breakpoint:
                bp_dose += dose
        cov_h = hours(lo, hi)
        rates[cid] = {
            "case_id": cid,
            "case_name": cs.case_name,
            "reference_start": iso(lo),
            "reference_end": iso(hi),
            "reference_hours": round(cov_h, 4),
            "dose_lux_hours": round(total, 4),
            "breakpoint_lux_hours": round(bp_dose, 4),
            "breakpoint_count": sum(
                1 for seg in cs.segments[start_i:end_i]
                if seg.breakpoint and intersect((seg.t0, seg.t1), (lo, hi))
            ),
            "mean_lux": round(total / cov_h, 4) if cov_h > 0 else None,
            "rate_lux_hours_per_occ_hour": round(total / cov_h, 4) if cov_h > 0 else None,
        }
    return rates


def simulate_scenario(
    label: str,
    series_by_case: dict[int, CaseSeries],
    schedule: list[tuple[datetime, Optional[datetime], int]],
    annual_window: tuple[datetime, datetime],
    limit: float,
    rates: dict[int, dict],
    as_of: datetime,
    *,
    horizon_days: int = 400,
) -> ScenarioResult:
    """Project a (possibly modified post-move) schedule to the annual limit.

    History (before ``as_of``) is integrated from real readings. Future dose
    rate segments come from each case's *own historical light curve*
    (ambient_rates): this is what makes a move into a case the object never
    occupied before projectable. Open-ended occupancy after as_of is assumed
    permanent (24 h/day in-case).
    """
    past_window = (annual_window[0], min(as_of, annual_window[1]))
    past = integrate_window(series_by_case, schedule, past_window)
    ytd = past.total_lux_hours
    remaining0 = max(0.0, limit - ytd)

    # Future schedule pieces after as_of, clipped to the annual window.
    # Consecutive pieces in the same case are joined; overlapping pieces of
    # different cases are split at the later entry's start timestamp.
    raw = sorted(
        [(max(s, as_of), min(e or annual_window[1], annual_window[1]), cid)
         for s, e, cid in schedule if (e or annual_window[1]) > as_of and s < annual_window[1]],
        key=lambda x: x[0],
    )
    future: list[tuple[datetime, datetime, int]] = []
    for s, e, cid in raw:
        if s >= e:
            continue
        if future and future[-1][2] == cid and s <= future[-1][1]:
            future[-1] = (future[-1][0], max(e, future[-1][1]), cid)
        else:
            if future and s < future[-1][1]:
                # overlapping different cases: let the later schedule entry win
                future[-1] = (future[-1][0], s, future[-1][2])
            future.append((s, e, cid))

    rate_segments: list[RateSegment] = []
    for s, e, cid in future:
        info = rates.get(cid)
        rate_segments.append(RateSegment(
            case_id=cid,
            case_name=(info or {}).get("case_name", f"#{cid}"),
            start_ts=iso(s),
            end_ts=iso(e) if e < annual_window[1] else None,
            mean_lux=(info or {}).get("mean_lux"),
            reference_start=(info or {}).get("reference_start"),
            reference_end=(info or {}).get("reference_end"),
            reference_hours=(info or {}).get("reference_hours"),
            source="case_historical_curve",
        ))

    def seg_rate(cid: int) -> Optional[float]:
        info = rates.get(cid)
        return info.get("rate_lux_hours_per_occ_hour") if info else None

    note_parts: list[str] = []
    projected_ts: Optional[datetime] = None
    current_rate: Optional[float] = None
    current_cid: Optional[int] = None
    out_of_window = False  # budget remains when the annual window closes
    if remaining0 <= 0:
        projected_ts = as_of
        note_parts.append("annual limit already reached")
    else:
        budget = remaining0
        hit = False
        for s, e, cid in future:
            rate = seg_rate(cid)
            if rate is None:
                note_parts.append(
                    f"case #{cid} has no historical readings overlapping the annual window; "
                    "projection stops")
                break
            s = max(s, as_of)
            seg_h = hours(s, e)
            if budget > 0:
                current_cid, current_rate = cid, rate
            if rate > 0:
                need_h = budget / rate
                if need_h <= seg_h:
                    projected_ts = s + timedelta(hours=need_h)
                    hit = True
                    break
            budget -= rate * seg_h
        if not hit and projected_ts is None:
            if not future:
                note_parts.append(
                    "no occupancy scheduled after as_of; object not in any case, "
                    "no further dose projected")
            elif not note_parts:
                out_of_window = budget > 0
                if out_of_window:
                    note_parts.append("limit not reached before annual window reset")

    # Projected curve: real cumulative to as_of, piecewise-linear extrapolation.
    hist_curve = cumulative_curve(series_by_case, schedule, (annual_window[0], min(as_of, annual_window[1])))
    curve = [dict(p) for p in hist_curve]
    if not curve or parse_ts(curve[-1]["ts"]) < as_of:
        curve.append({"ts": iso(as_of), "cumulative_lux_hours": round(ytd, 3)})
    if remaining0 > 0:
        horizon = min(as_of + timedelta(days=horizon_days), annual_window[1])
        step = timedelta(hours=6)
        t = as_of
        last = ytd
        fi = 0
        while t < horizon and fi < len(future):
            fs, fe, cid = future[fi]
            if t < fs:
                fi += 1
                continue
            rate = seg_rate(cid) or 0.0
            nt = min(t + step, fe, horizon)
            last += rate * hours(t, nt)
            curve.append({"ts": iso(nt), "cumulative_lux_hours": round(last, 3)})
            t = nt
            if t >= fe:
                fi += 1

    remaining = max(0.0, limit - ytd)
    crosses_reset = out_of_window or (projected_ts is not None and projected_ts > annual_window[1])
    current_info = rates.get(current_cid, {}) if current_cid else {}
    return ScenarioResult(
        label=label,
        rate_lux_hours_per_occ_hour=current_rate,
        mean_lux=current_info.get("mean_lux"),
        annual_dose_ytd=round(ytd, 4),
        remaining_budget_lux_hours=round(remaining, 4),
        projected_limit_ts=iso(projected_ts) if projected_ts else None,
        projected_limit_crosses_reset=crosses_reset,
        projected_curve=curve,
        occupied_hours_ytd=past.occupied_hours,
        covered_hours_ytd=past.covered_hours,
        note="; ".join(note_parts),
        rate_segments=rate_segments,
    )
