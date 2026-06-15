# S2b Bot — Plan 4: Stop-Monitor + Management Loop (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The management loop that watches every open spread each cycle and **actually fires** TP / stop / time exits — verifying the close via the Plan-2 order state machine. This is the direct fix for bot B's fatal flaw (stops configured but never executed → +$772 ran to −$1,405).

**Architecture:** `bot/strategy/manage.py` holds pure decision + helpers: `decide_exit` (STOP-priority decision from current spread value, credit, DTE), `spread_value_mid` (mark a spread from two `OptionQuote`s), `dte_from_expiry`, `build_close_payload` (Tradier debit multileg to close a bull put), and `monitor_positions` (the loop — injected `mark_fn`/`dte_fn`/`close_fn` so it's hermetic). The loop returns every fired exit and flags any close that did NOT reach a FILLED state, so a stop that fails to execute is *surfaced*, never silent.

**Tech Stack:** Python 3.11, pytest, dataclasses/enum (stdlib). Reuses `bot.broker.submit.submit_and_verify`, `bot.broker.order_state.OrderState`, and `bot.strategy.s2b._occ`.

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §3 (TP 50% / stop 2× / 1-DTE exit), §4 (continuous mark monitoring; stop fires and fill is verified; dead-man if a stopped position isn't closed).

---

### Task 1: ExitAction + decide_exit (STOP-priority decision)

**Files:** Create `bot/strategy/manage.py`, `bot/tests/test_manage.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_manage.py`:
```python
from bot.strategy.manage import ExitAction, ManageConfig, decide_exit

# credit = 3.0; stop level = 3.0*(1+2.0)=9.0; tp level = 3.0*(1-0.5)=1.5
CFG = ManageConfig(tp_pct=0.50, stop_mult=2.0, time_exit_dte=1)


def test_stop_fires_at_and_above_level():
    assert decide_exit(current_value=9.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.STOP
    assert decide_exit(current_value=12.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.STOP


def test_take_profit_at_and_below_level():
    assert decide_exit(current_value=1.5, credit=3.0, dte=5, cfg=CFG) == ExitAction.TAKE_PROFIT
    assert decide_exit(current_value=0.8, credit=3.0, dte=5, cfg=CFG) == ExitAction.TAKE_PROFIT


def test_time_exit_when_dte_low_and_midrange():
    assert decide_exit(current_value=3.0, credit=3.0, dte=1, cfg=CFG) == ExitAction.TIME_EXIT


def test_hold_midrange_high_dte():
    assert decide_exit(current_value=3.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.HOLD


def test_stop_takes_priority_over_time_exit():
    # at a loss past the stop AND near expiry -> STOP wins (risk first)
    assert decide_exit(current_value=10.0, credit=3.0, dte=1, cfg=CFG) == ExitAction.STOP
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.strategy.manage`.

- [ ] **Step 3: Implement** — `bot/strategy/manage.py`:
```python
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
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_manage.py -v` → 5 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/manage.py bot/tests/test_manage.py
git commit -m "feat: decide_exit (STOP-priority TP/stop/time decision)"
```

---

### Task 2: ManagedPosition + spread_value_mid + dte_from_expiry

**Files:** Modify `bot/strategy/manage.py`, `bot/tests/test_manage.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.strategy.manage import ManagedPosition, spread_value_mid, dte_from_expiry
from bot.strategy.s2b import OptionQuote


def test_spread_value_mid_cost_to_close():
    # bull put: short higher strike, long lower. value = short_mid - long_mid
    short_q = OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.60)   # mid 3.50
    long_q = OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.80)    # mid 1.70
    assert spread_value_mid(short_q, long_q) == 1.80   # 3.50 - 1.70


def test_dte_from_expiry():
    assert dte_from_expiry("2026-06-19", today="2026-06-15") == 4
    assert dte_from_expiry("2026-06-15", today="2026-06-15") == 0
```

- [ ] **Step 2: Run, expect fail** — `ImportError: ManagedPosition`.

- [ ] **Step 3: Implement** (append to `bot/strategy/manage.py`):
```python
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
```

- [ ] **Step 4: Run, expect pass** — 7 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/manage.py bot/tests/test_manage.py
git commit -m "feat: ManagedPosition, spread mid-mark, dte_from_expiry"
```

---

### Task 3: build_close_payload (Tradier debit multileg to close)

**Files:** Modify `bot/strategy/manage.py`, `bot/tests/test_manage.py`

- [ ] **Step 1: Add failing test** (append):
```python
from bot.strategy.manage import build_close_payload


def test_build_close_payload_buys_back_short_sells_long():
    pos = ManagedPosition(ticker="SPY", short_strike=568.0, long_strike=558.0,
                          credit=3.0, qty=2, expiry="2026-06-19")
    payload = build_close_payload(pos, limit_price=1.20)
    assert payload["class"] == "multileg"
    assert payload["type"] == "debit"      # closing a credit spread costs a debit
    assert payload["price"] == 1.20
    assert payload["option_symbol[0]"] == "SPY260619P00568000"
    assert payload["side[0]"] == "buy_to_close" and payload["quantity[0]"] == 2
    assert payload["option_symbol[1]"] == "SPY260619P00558000"
    assert payload["side[1]"] == "sell_to_close" and payload["quantity[1]"] == 2
```

- [ ] **Step 2: Run, expect fail** — `ImportError: build_close_payload`.

- [ ] **Step 3: Implement** (append; reuse the OCC helper from the strategy module):
```python
from bot.strategy.s2b import _occ


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
```

- [ ] **Step 4: Run, expect pass** — 8 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/manage.py bot/tests/test_manage.py
git commit -m "feat: build_close_payload (debit multileg close)"
```

---

### Task 4: monitor_positions loop (fires exits, flags failed closes)

**Files:** Modify `bot/strategy/manage.py`, `bot/tests/test_manage.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.strategy.manage import monitor_positions, ExitResult


def _pos(**kw):
    base = dict(ticker="SPY", short_strike=568.0, long_strike=558.0, credit=3.0, qty=1, expiry="2026-06-19")
    base.update(kw)
    return ManagedPosition(**base)


def test_monitor_fires_stop_and_records_filled():
    pos = _pos()
    marks = {id(pos): 10.0}    # past stop (>=9.0)
    closes = []

    def mark_fn(p): return marks[id(p)]
    def dte_fn(p): return 5
    def close_fn(p, action): closes.append((p, action)); return "filled"

    results = monitor_positions([pos], mark_fn, dte_fn, close_fn, CFG)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ExitResult)
    assert r.action == ExitAction.STOP and r.close_status == "filled" and r.failed is False
    assert closes == [(pos, ExitAction.STOP)]


def test_monitor_holds_when_no_exit():
    pos = _pos()
    results = monitor_positions([pos], lambda p: 3.0, lambda p: 5, lambda p, a: "filled", CFG)
    assert results == []   # HOLD -> no close attempted


def test_monitor_flags_failed_close():
    # a stop that does NOT reach 'filled' must be surfaced as failed (the bot-B lesson)
    pos = _pos()
    results = monitor_positions([pos], lambda p: 10.0, lambda p: 5,
                                lambda p, a: "timeout", CFG)
    assert len(results) == 1
    assert results[0].failed is True and results[0].close_status == "timeout"
```

- [ ] **Step 2: Run, expect fail** — `ImportError: monitor_positions`.

- [ ] **Step 3: Implement** (append):
```python
@dataclass
class ExitResult:
    position: "ManagedPosition"
    action: ExitAction
    close_status: str       # e.g. "filled", "timeout", "rejected"
    failed: bool            # True if the close did not reach "filled"


def monitor_positions(positions, mark_fn, dte_fn, close_fn, cfg):
    """Check each open position; on a non-HOLD decision, close via close_fn and record result.
    close_fn(position, action) -> status string (e.g. 'filled'). A close that is not 'filled'
    is flagged failed=True so the caller can alert (a stop that didn't execute is never silent)."""
    results = []
    for p in positions:
        action = decide_exit(mark_fn(p), p.credit, dte_fn(p), cfg)
        if action == ExitAction.HOLD:
            continue
        status = close_fn(p, action)
        results.append(ExitResult(position=p, action=action, close_status=status,
                                  failed=(status != "filled")))
    return results
```

- [ ] **Step 4: Run, expect pass** — 11 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/manage.py bot/tests/test_manage.py
git commit -m "feat: monitor_positions loop (fires exits, flags failed closes)"
```

---

### Task 5: Integration — a STOP fires and is verified through the order state machine

**Files:** Create `bot/tests/test_manage_integration.py`

- [ ] **Step 1: Write the integration test** — `bot/tests/test_manage_integration.py`:
```python
"""The bot-B lesson, pinned: when a stop triggers, the close is executed AND verified filled."""
from bot.strategy.manage import (ManagedPosition, ManageConfig, monitor_positions,
                                 build_close_payload, spread_value_mid, ExitAction)
from bot.strategy.s2b import OptionQuote
from bot.broker.submit import submit_and_verify
from bot.broker.order_state import OrderState


def _clock():
    t = {"now": 0.0}
    return (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))


class _FakeBroker:
    """Records the submitted close order; reports place -> filled across two get_order calls."""
    def __init__(self, fills):
        self._fills = fills
        self._seq = iter([{"id": 777, "status": "ok"},        # place ack
                          {"id": 777, "status": "filled"}])    # poll -> filled
    def place_order(self, payload):
        self._fills.append(payload)
        return "777"
    def get_order(self, oid):
        return next(self._seq)
    def cancel_order(self, oid):
        pass


def test_stop_triggers_close_and_verifies_fill():
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    cfg = ManageConfig()
    # mark the spread from quotes: short blew out -> spread mid 9.00 (== the 9.0 stop level)
    short_q = OptionQuote(568.0, 0.80, 10.40, 10.60)   # mid 10.50
    long_q = OptionQuote(558.0, 0.40, 1.40, 1.60)      # mid 1.50

    fills = []

    def mark_fn(p):
        return spread_value_mid(short_q, long_q)        # 9.0 -> STOP

    def dte_fn(p):
        return 5

    def close_fn(p, action):
        now, sleep = _clock()
        payload = build_close_payload(p, limit_price=9.10)
        state, _ = submit_and_verify(_FakeBroker(fills), payload,
                                     poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
        return state.value                               # "filled"

    results = monitor_positions([pos], mark_fn, dte_fn, close_fn, cfg)
    assert len(results) == 1
    assert results[0].action == ExitAction.STOP
    assert results[0].close_status == OrderState.FILLED.value
    assert results[0].failed is False
    # the close order was actually built and submitted as a debit buy-to-close
    assert fills and fills[0]["type"] == "debit" and fills[0]["side[0]"] == "buy_to_close"
```

- [ ] **Step 2: Run, expect pass** — `python -m pytest bot/tests/test_manage_integration.py -v` → 1 passed.

- [ ] **Step 3: Run the FULL suite** — `python -m pytest` → expected 56 (Plans 1–3) + 12 (Plan 4) = 68 passed.

- [ ] **Step 4: Commit**
```bash
git add bot/tests/test_manage_integration.py
git commit -m "test: STOP fires, closes, and verifies fill end-to-end (the bot-B fix)"
```

---

## Self-review notes (done)
- **Spec coverage:** §3 TP 50% / stop 2× / 1-DTE → Task 1; mark monitoring → Tasks 2, 4; close execution → Task 3; §4 stop fires + fill verified + failed-close surfaced → Tasks 4, 5.
- **No placeholders:** complete code + expected counts throughout.
- **Type consistency:** `ExitAction`/`ManageConfig` (Task 1) reused in `decide_exit`/`monitor_positions`; `ManagedPosition` (Task 2) used in Tasks 3–5; `ExitResult` (Task 4) returned by `monitor_positions`; `close_fn` returns a status string and `monitor_positions` flags `failed = (status != "filled")`; integration wires `submit_and_verify` (Plan 2) so a fired stop is *verified*, not assumed.
- **The bot-B fix is the integration test:** a stop triggers → close is built and submitted → fill is verified → `failed=False`; the `test_monitor_flags_failed_close` unit test proves a non-filled close is surfaced, never silent.

## Next: Plan 5 — truth ledger (broker-truth reconcile, drift halt) + monitoring (dead-man on the failed-close flag) + chaos drills (kill-mid-position, force-a-stop, order-timeout) — the deployment gate.
```
