"""Production data adapters: pure parsers/calculators injected into Deps (spec §3,§5,§7)."""
from datetime import datetime, timedelta


def pick_weekly_expiry(today: str, min_dte: int = 4) -> str:
    """Nearest Friday at least min_dte calendar days out (YYYY-MM-DD)."""
    d = datetime.strptime(today, "%Y-%m-%d")
    days_to_fri = (4 - d.weekday()) % 7        # 4 = Friday
    friday = d + timedelta(days=days_to_fri)
    while (friday - d).days < min_dte:
        friday += timedelta(days=7)
    return friday.strftime("%Y-%m-%d")
