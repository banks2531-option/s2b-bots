"""S2b position management: stop/TP/time-exit decisions and the monitor loop (spec §3, §4)."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from bot.broker.order_state import result_status
from bot.strategy.s2b import _occ
from bot.errors import MarketDataUnavailable


class ExitAction(str, Enum):
    HOLD = "hold"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    TIME_EXIT = "time_exit"
    DEGROSS = "degross"        # Phase 1.5: defensive de-gross of a held position in a risk_off downtrend
    FLOW_DEGROSS = "flow_degross"  # Flow-flip de-gross: close a same-day, not-yet-profitable position on a bull->bear flow flip
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
    entry_date: str = ""   # YYYY-MM-DD the position was opened (default "" keeps back-compat with old state files)
    opening_fees: float = 0.0   # commissions+regulatory fees on the OPENING fill (spec §8); 0.0 default
                                 # keeps back-compat with old state files and the flag-off path


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
    close_result: object = None   # raw close_fn return (ExecutionResult when actual_fill_accounting
                                   # is on; may be a bare status string from a legacy test double)
    mark_unavailable: bool = False  # True when the ERROR came from mark_fn failing (couldn't PRICE the
                                     # position -> no close order was ever attempted). Lets the manager
                                     # treat a transient quote/data gap as retry-not-halt, while a close
                                     # order that actually failed still halts. (2026-07-24 Bot C fix.)


def monitor_positions(positions, mark_fn, dte_fn, close_fn, cfg):
    """Check each open position; on a non-HOLD decision, close via close_fn and record result.
    close_fn(position, action) -> an ExecutionResult (spec §8) or a legacy bare status string
    (e.g. 'filled'). A close that is not 'filled' is flagged failed=True so the caller can alert
    (a stop that didn't execute is never silent)."""
    results = []
    for p in positions:
        # Price the position FIRST, separately: a mark (quote) failure means we could not even value the
        # spread, so NO close order is attempted -- it is a transient data gap (flagged mark_unavailable
        # so the manager retries rather than halting), not a stuck close. (2026-07-24 Bot C root cause.)
        try:
            value = mark_fn(p)
        except Exception as exc:      # one position's data gap must NOT block the others' stops
            results.append(ExitResult(position=p, action=ExitAction.ERROR,
                                      close_status=f"error: {exc}", failed=True, mark_unavailable=True))
            continue
        try:
            action = decide_exit(value, p.credit, dte_fn(p), cfg)
            if action == ExitAction.HOLD:
                continue
            close_result = close_fn(p, action)
            status = result_status(close_result)
            results.append(ExitResult(position=p, action=action, close_status=status,
                                      failed=(str(status).lower() != "filled"), value=value,
                                      close_result=close_result))
        except MarketDataUnavailable as exc:
            # close_fn declined to submit because it could not VALIDLY price the close (invalid/crossed
            # quotes, or a non-positive debit limit). No order reached the broker -- so this is a data
            # gap to RETRY, not a stuck close to halt on (2026-07-27 Bot C root cause: a transient
            # crossed quote produced a -0.33 debit which, if submitted, Tradier rejects with HTTP 400).
            results.append(ExitResult(position=p, action=ExitAction.ERROR,
                                      close_status=f"error: {exc}", failed=True, value=value,
                                      mark_unavailable=True))
            continue
        except Exception as exc:  # a close order actually SUBMITTED and FAILED -> stuck close (may halt)
            results.append(ExitResult(position=p, action=ExitAction.ERROR,
                                      close_status=f"error: {exc}", failed=True, value=value))
            continue
    return results
