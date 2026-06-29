"""CSV sink for shadow-logging RegimeState each tick (Phase 0 dataset for Phase 1 calibration)."""
import csv as _csv
import os as _os
from dataclasses import asdict, fields

from bot.regime.state import RegimeState

_FIELDS = ["ts", "event"] + [f.name for f in fields(RegimeState)]


def make_regime_logger(path_prefix):
    """Return log(state, ts, event) that appends one flat row to a per-DAY CSV
    `{path_prefix}_{YYYYMMDD}.csv` — the date is taken from `ts` (the tick timestamp), so the
    shadow log rotates daily instead of growing into one unbounded file. Header written once per file."""
    def log(state: RegimeState, ts: str, event: str = "TICK"):
        day = (ts or "")[:10].replace("-", "") or "unknown"     # "2026-06-29T10:00" -> "20260629"
        path = f"{path_prefix}_{day}.csv"
        exists = _os.path.exists(path)
        row = {"ts": ts, "event": event, **asdict(state)}
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)
    return log
