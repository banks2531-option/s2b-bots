"""CSV sink for shadow-logging RegimeState each tick (Phase 0 dataset for Phase 1 calibration)."""
import csv as _csv
import os as _os
from dataclasses import asdict, fields

from bot.regime.state import RegimeState

_FIELDS = ["ts", "event"] + [f.name for f in fields(RegimeState)]


def make_regime_logger(path):
    """Return log(state, ts, event) that appends one flat row per call (header written once)."""
    def log(state: RegimeState, ts: str, event: str = "TICK"):
        exists = _os.path.exists(path)
        row = {"ts": ts, "event": event, **asdict(state)}
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)
    return log
