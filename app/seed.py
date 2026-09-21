"""Deterministic demo data: two display cases, one silk scroll exhibit.

Case A  ("Silk Hall 展柜-A"): ~50 lux daytime, has a sensor outage on
2026-09-03 (a multi-hour gap that becomes breakpoint evidence).
Case B  ("Paper Gallery 展柜-B"): ~20 lux daytime, complete series.

The scroll sits in case A from 2026-08-01 through "now" (2026-09-21). The
rotation simulation therefore predicts moving it to case B substantially
extends the time before the 50,000 lux*h/year conservation limit is reached.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED_READINGS = Path(__file__).resolve().parent.parent / "sample_data" / "readings.jsonl"
SEED_OCCUPANCIES = Path(__file__).resolve().parent.parent / "sample_data" / "occupancies.jsonl"

START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=30)

# Simulated sensor outage (gap > the 60-minute breakpoint threshold).
GAP_FROM = datetime(2026, 9, 3, 9, 0, 0, tzinfo=timezone.utc)
GAP_TO = datetime(2026, 9, 3, 16, 0, 0, tzinfo=timezone.utc)


def lux_for(ts: datetime, base: float) -> float:
    """Deterministic day/night curve: lights on 08:00-20:00 UTC."""
    h = ts.hour + ts.minute / 60.0
    if 8.0 <= h <= 20.0:
        # smooth bell around 14:00
        x = (h - 14.0) / 6.0
        factor = 0.85 + 0.15 * math.cos(x * math.pi / 2) ** 2
        noise = (1.2 * math.sin(ts.timestamp() / 911.0)
                 + 0.8 * math.sin(ts.timestamp() / 137.0))
        return round(max(0.0, base * factor + noise), 2)
    # night: gallery blackout, tiny residual
    return round(0.5 + 0.2 * math.sin(ts.timestamp() / 503.0), 2)


def write_sample_files() -> None:
    SEED_READINGS.parent.mkdir(parents=True, exist_ok=True)
    with SEED_READINGS.open("w", encoding="utf-8") as f:
        t = START
        while t <= END:
            if not (GAP_FROM <= t < GAP_TO):
                f.write(json.dumps({"ts": t.isoformat(), "case": "case-a", "lux": lux_for(t, 50.0)}, ensure_ascii=False) + "\n")
                f.write(json.dumps({"ts": t.isoformat(), "case": "case-b", "lux": lux_for(t, 20.0)}, ensure_ascii=False) + "\n")
            t += STEP

    SEED_OCCUPANCIES.parent.mkdir(parents=True, exist_ok=True)
    with SEED_OCCUPANCIES.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "exhibit": "silk-scroll",
            "case": "case-a",
            "start_ts": START.isoformat(),
            "end_ts": None,
            "note": "文物自 8/1 起在 A 柜展出（示例数据，截至 2026-09-21）",
        }, ensure_ascii=False) + "\n")
