"""S2b entry strategy: Monday SPY bull put spread, cushion-aware (spec §3)."""
from datetime import datetime


def is_entry_day(date_str: str) -> bool:
    """True iff date_str (YYYY-MM-DD) is a Monday (the validated S2b entry day)."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 0
