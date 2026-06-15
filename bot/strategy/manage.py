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
