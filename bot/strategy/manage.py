"""S2b position management: stop/TP/time-exit decisions and the monitor loop (spec §3, §4)."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ExitAction(str, Enum):
    HOLD = "hold"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    TIME_EXIT = "time_exit"


@dataclass
class ManageConfig:
    tp_pct: float = 0.50       # take profit when 50% of credit captured
    stop_mult: float = 2.0     # stop when loss reaches 2x credit
    time_exit_dte: int = 1     # exit at or under 1 DTE


def decide_exit(current_value, credit, dte, cfg) -> ExitAction:
    """current_value = debit-to-close the spread now. STOP has priority (risk first)."""
    if current_value >= credit * (1 + cfg.stop_mult):
        return ExitAction.STOP
    if current_value <= credit * (1 - cfg.tp_pct):
        return ExitAction.TAKE_PROFIT
    if dte <= cfg.time_exit_dte:
        return ExitAction.TIME_EXIT
    return ExitAction.HOLD


@dataclass
class ManagedPosition:
    ticker: str
    short_strike: float
    long_strike: float
    credit: float
    qty: int
    expiry: str        # YYYY-MM-DD


def spread_value_mid(short_q, long_q) -> float:
    """Mid debit-to-close a bull put spread = short_mid - long_mid."""
    short_mid = (short_q.bid + short_q.ask) / 2.0
    long_mid = (long_q.bid + long_q.ask) / 2.0
    return round(short_mid - long_mid, 2)


def dte_from_expiry(expiry: str, today: str) -> int:
    d0 = datetime.strptime(today, "%Y-%m-%d")
    d1 = datetime.strptime(expiry, "%Y-%m-%d")
    return (d1 - d0).days
