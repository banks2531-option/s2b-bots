"""S2b position management: stop/TP/time-exit decisions and the monitor loop (spec §3, §4)."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from bot.strategy.s2b import _occ


class ExitAction(str, Enum):
    HOLD = "hold"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    TIME_EXIT = "time_exit"
    ERROR = "error"


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
    # negative DTE (already expired) also triggers TIME_EXIT -> close attempt surfaces it
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


def build_close_payload(pos: ManagedPosition, limit_price: float) -> dict:
    """Tradier debit multileg to CLOSE a bull put spread: buy back short, sell long."""
    sym = pos.ticker
    return {
        "class": "multileg", "symbol": sym, "type": "debit", "duration": "day",
        "price": round(limit_price, 2),
        "option_symbol[0]": _occ(sym, pos.expiry, "P", pos.short_strike),
        "side[0]": "buy_to_close", "quantity[0]": pos.qty,
        "option_symbol[1]": _occ(sym, pos.expiry, "P", pos.long_strike),
        "side[1]": "sell_to_close", "quantity[1]": pos.qty,
    }


@dataclass
class ExitResult:
    position: ManagedPosition
    action: ExitAction
    close_status: str       # e.g. "filled", "timeout", "rejected"
    failed: bool            # True if the close did not reach "filled"
    value: float = None     # the debit-to-close mark that triggered the exit (for P&L logging)


def monitor_positions(positions, mark_fn, dte_fn, close_fn, cfg):
    """Check each open position; on a non-HOLD decision, close via close_fn and record result.
    close_fn(position, action) -> status string (e.g. 'filled'). A close that is not 'filled'
    is flagged failed=True so the caller can alert (a stop that didn't execute is never silent)."""
    results = []
    for p in positions:
        try:
            value = mark_fn(p)
            action = decide_exit(value, p.credit, dte_fn(p), cfg)
            if action == ExitAction.HOLD:
                continue
            status = close_fn(p, action)
            results.append(ExitResult(position=p, action=action, close_status=status,
                                      failed=(str(status).lower() != "filled"), value=value))
        except Exception as exc:  # one position's error must NOT block the others' stops
            results.append(ExitResult(position=p, action=ExitAction.ERROR,
                                      close_status=f"error: {exc}", failed=True))
            continue
    return results
