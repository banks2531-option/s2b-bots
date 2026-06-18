# S2b Bot — Plan 6: Orchestrator (tick = reconcile → manage → enter)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Compose modules 1–5 into one bot **tick**: each invocation reconciles against broker truth (halt on drift), manages open positions (fire verified exits, halt on a failed close), then — only if not halted and it's a Monday entry — opens one S2b spread. All orchestration logic is hermetically tested via injected `Deps` (no network, no clock); the live scheduler + data feeds are a thin wiring layer noted at the end.

**Architecture:** `bot/app/orchestrator.py` holds `BotState` (open positions + halt flag), a `Deps` dataclass of injected callables (data feeds, broker actions, alert sink, configs), three cycle functions (`run_reconcile_cycle`, `run_management_cycle`, `run_entry_cycle`), and `tick(state, deps, now)` that sequences them with halt-gating. The halt flag is sticky within a tick: a reconcile/management halt prevents entry that same tick.

**Tech Stack:** Python 3.11, pytest, dataclasses (stdlib). Reuses every prior module: `sizing`, `risk_gate`, `strategy.s2b`, `strategy.manage`, `ops.ledger`, `ops.monitor`.

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §3 (Monday entry), §4 (manage/stop), §7 (reconcile/halt), §5 (sizing + regime cut). S2b holds **one position at a time** (weekly Monday), so at entry the book is flat → open_risk/concurrency are simple.

---

### Task 1: app package + BotState + Deps

**Files:** Create `bot/app/__init__.py`, `bot/app/orchestrator.py`, `bot/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_orchestrator.py`:
```python
from bot.app.orchestrator import BotState, Deps


def test_botstate_defaults():
    s = BotState()
    assert s.open_positions == [] and s.halted is False and s.halt_reason == ""


def test_deps_is_constructible_with_callables():
    d = Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    assert d.base_risk_pct == 0.10 and d.account_equity == 20_000.0
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.app.orchestrator`.

- [ ] **Step 3: Implement** — `bot/app/__init__.py`:
```python
"""Application orchestration for the S2b bot."""
```
`bot/app/orchestrator.py`:
```python
"""Bot orchestrator: tick = reconcile -> manage -> enter, with halt-gating (spec §3,4,5,7)."""
from dataclasses import dataclass, field

from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig
from bot.strategy.s2b import is_entry_day, build_spread_order, S2bConfig, to_tradier_payload
from bot.strategy.manage import (monitor_positions, ManageConfig, ManagedPosition)
from bot.ops.ledger import reconcile
from bot.ops.monitor import alerts_for_cycle, should_halt_new_entries


@dataclass
class BotState:
    open_positions: list = field(default_factory=list)   # list[ManagedPosition]
    halted: bool = False
    halt_reason: str = ""


@dataclass
class Deps:
    get_spot: callable            # (symbol) -> float
    get_atr: callable             # (symbol) -> float
    get_chain: callable           # (symbol, expiry) -> list[OptionQuote]
    pick_expiry: callable         # (today_str) -> expiry_str
    get_vix_regime: callable      # () -> (vix_pct_rank, vix_1d_change)
    account_state: callable       # (today, concurrent) -> AccountState
    mark_position: callable       # (ManagedPosition) -> float (debit to close)
    dte_of: callable              # (ManagedPosition, today) -> int
    open_spread: callable         # (payload) -> status str ("filled" on success)
    close_spread: callable        # (ManagedPosition, action) -> status str
    broker_positions: callable    # () -> list[ManagedPosition]
    broker_equity: callable       # () -> float
    bot_equity: callable          # () -> float
    alert_sink: callable          # (list[Alert]) -> None
    s2b_cfg: object = field(default_factory=S2bConfig)
    manage_cfg: object = field(default_factory=ManageConfig)
    risk_cfg: object = field(default_factory=RiskConfig)
    base_risk_pct: float = 0.10
    account_equity: float = 20_000.0
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_orchestrator.py -v` → 2 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/app/__init__.py bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "feat: orchestrator BotState + Deps"
```

---

### Task 2: run_reconcile_cycle (halt on drift)

**Files:** Modify `bot/app/orchestrator.py`, `bot/tests/test_orchestrator.py`

- [ ] **Step 1: Add failing tests** (append to `bot/tests/test_orchestrator.py`):
```python
from bot.app.orchestrator import run_reconcile_cycle
from bot.strategy.manage import ManagedPosition


def _pos():
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


def test_reconcile_clean_no_halt():
    state = BotState(open_positions=[_pos()])
    d = _deps(broker_positions=lambda: [_pos()])
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is False


def test_reconcile_untracked_broker_position_halts_and_alerts():
    sent = []
    state = BotState(open_positions=[])               # bot thinks flat
    d = _deps(broker_positions=lambda: [_pos()],      # broker still holds one
              alert_sink=lambda alerts: sent.append(alerts))
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is True and "reconcile" in state.halt_reason
    assert sent and sent[0][0].message  # a critical alert was emitted
```

- [ ] **Step 2: Run, expect fail** — `ImportError: run_reconcile_cycle`.

- [ ] **Step 3: Implement** (append to `bot/app/orchestrator.py`):
```python
def run_reconcile_cycle(state: BotState, deps: Deps) -> tuple:
    drift = reconcile(state.open_positions, deps.broker_positions(),
                      deps.bot_equity(), deps.broker_equity())
    alerts = alerts_for_cycle([], drift_report=drift)
    if alerts:
        deps.alert_sink(alerts)
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "reconcile drift"
    return state, drift
```

- [ ] **Step 4: Run, expect pass** — 4 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "feat: run_reconcile_cycle (halt + alert on drift)"
```

---

### Task 3: run_management_cycle (fire exits, drop closed, halt on failed close)

**Files:** Modify `bot/app/orchestrator.py`, `bot/tests/test_orchestrator.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.app.orchestrator import run_management_cycle


def test_management_closes_filled_position_and_removes_it():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0,           # past stop
              close_spread=lambda p, a: "filled")
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert len(results) == 1 and results[0].action.value == "stop"
    assert state.open_positions == []                 # filled close -> removed
    assert state.halted is False


def test_management_failed_close_keeps_position_and_halts():
    pos = _pos()
    sent = []
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0, close_spread=lambda p, a: "timeout",
              alert_sink=lambda alerts: sent.append(alerts))
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results[0].failed is True
    assert state.open_positions == [pos]              # NOT removed (close didn't fill)
    assert state.halted is True and "close" in state.halt_reason
    assert sent  # alerted


def test_management_hold_keeps_position():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.0, dte_of=lambda p, today: 5)  # midrange, high dte
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results == [] and state.open_positions == [pos]
```

- [ ] **Step 2: Run, expect fail** — `ImportError: run_management_cycle`.

- [ ] **Step 3: Implement** (append):
```python
def run_management_cycle(state: BotState, deps: Deps, today: str) -> tuple:
    results = monitor_positions(
        state.open_positions,
        mark_fn=deps.mark_position,
        dte_fn=lambda p: deps.dte_of(p, today),
        close_fn=deps.close_spread,
        cfg=deps.manage_cfg,
    )
    alerts = alerts_for_cycle(results, drift_report=None)
    if alerts:
        deps.alert_sink(alerts)
    # remove only positions whose close actually filled
    closed_ok = {id(r.position) for r in results if not r.failed}
    state.open_positions = [p for p in state.open_positions if id(p) not in closed_ok]
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "failed close"
    return state, results
```

- [ ] **Step 4: Run, expect pass** — 7 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "feat: run_management_cycle (fire exits, drop filled, halt on failed close)"
```

---

### Task 4: run_entry_cycle (Monday + 10:00 + flat + not-halted → build/size/gate/submit)

**Files:** Modify `bot/app/orchestrator.py`, `bot/tests/test_orchestrator.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from datetime import datetime
from bot.app.orchestrator import run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def test_entry_opens_position_on_monday_after_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled")
    now = datetime(2026, 6, 15, 10, 5)   # Monday 10:05
    state, info = run_entry_cycle(state, d, now)
    assert len(state.open_positions) == 1
    assert state.open_positions[0].short_strike == 568.0


def test_no_entry_on_tuesday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 16, 10, 5))  # Tuesday
    assert state.open_positions == []


def test_no_entry_before_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 9, 45))  # Mon 09:45
    assert state.open_positions == []


def test_no_entry_when_halted():
    state = BotState(halted=True, halt_reason="x")
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.open_positions == []


def test_no_entry_when_already_holding():
    state = BotState(open_positions=[_pos()])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1   # unchanged, no second entry
```

- [ ] **Step 2: Run, expect fail** — `ImportError: run_entry_cycle`.

- [ ] **Step 3: Implement** (append):
```python
def run_entry_cycle(state: BotState, deps: Deps, now) -> tuple:
    today = now.strftime("%Y-%m-%d")
    if state.halted or not is_entry_day(today) or now.hour < 10 or state.open_positions:
        return state, None
    spot = deps.get_spot("SPY")
    atr = deps.get_atr("SPY")
    expiry = deps.pick_expiry(today)
    order = build_spread_order(spot, atr, deps.get_chain("SPY", expiry), deps.s2b_cfg)
    if order is None:
        return state, "no_order"
    pct_rank, change = deps.get_vix_regime()
    risk = regime_adjusted_risk_pct(deps.base_risk_pct, pct_rank, change)
    order.qty = contracts_for_risk(deps.account_equity, order.max_loss_per_contract, risk)
    acct = deps.account_state(today, len(state.open_positions))
    decision = RiskGate(deps.risk_cfg).is_order_allowed(order, acct)
    if not decision.allowed:
        return state, decision.reason
    status = deps.open_spread(to_tradier_payload(order, expiry, order.qty))
    if status == "filled":
        state.open_positions.append(ManagedPosition(
            "SPY", order.short_strike, order.long_strike, order.credit, order.qty, expiry))
    return state, status
```

- [ ] **Step 4: Run, expect pass** — 12 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "feat: run_entry_cycle (Monday/10:00/flat/not-halted gates -> size -> gate -> submit)"
```

---

### Task 5: tick (reconcile → manage → enter) + halt-gating

**Files:** Modify `bot/app/orchestrator.py`, `bot/tests/test_orchestrator.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.app.orchestrator import tick


def test_tick_enters_on_clean_monday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [], open_spread=lambda payload: "filled")
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))   # Monday, flat, clean reconcile
    assert len(state.open_positions) == 1


def test_tick_reconcile_halt_blocks_entry_same_tick():
    # broker shows an untracked position -> reconcile halts -> no entry even though it's Monday 10:05
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [_pos()], open_spread=lambda payload: "filled")
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.halted is True and state.open_positions == []


def test_tick_manages_then_holds_no_new_entry_when_holding():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [_pos()], mark_position=lambda p: 3.0,
              dte_of=lambda p, today: 5)            # HOLD
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1          # still holding the one, no second entry
```

- [ ] **Step 2: Run, expect fail** — `ImportError: tick`.

- [ ] **Step 3: Implement** (append):
```python
def tick(state: BotState, deps: Deps, now) -> BotState:
    """One bot cycle: reconcile (may halt) -> manage open positions -> enter if eligible."""
    today = now.strftime("%Y-%m-%d")
    state, _ = run_reconcile_cycle(state, deps)
    state, _ = run_management_cycle(state, deps, today)
    state, _ = run_entry_cycle(state, deps, now)   # internally no-ops if halted / not eligible
    return state
```

- [ ] **Step 4: Run, expect pass** — 15 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "feat: tick orchestrator (reconcile -> manage -> enter, halt-gated)"
```

---

### Task 6: Full-lifecycle integration (enter Monday → stop fires next tick → halt on failed close)

**Files:** Create `bot/tests/test_orchestrator_lifecycle.py`

- [ ] **Step 1: Write the lifecycle test** — `bot/tests/test_orchestrator_lifecycle.py`:
```python
"""A position's whole life through the orchestrator: open -> stop fires -> closed; and the
failure mode: a stop that can't close halts the bot."""
from datetime import datetime
from bot.app.orchestrator import BotState, Deps, tick
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _chain():
    return [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(565.0, 0.30, 2.80, 2.90),
            OptionQuote(560.0, 0.22, 2.00, 2.10), OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _deps(mark, close_status, broker_positions):
    return Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(), pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=mark, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: close_status,
        broker_positions=broker_positions, broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )


def test_open_then_stop_closes_clean():
    state = BotState()
    # tick 1: Monday 10:05, flat, mark irrelevant (no positions) -> opens
    d_open = _deps(mark=lambda p: 3.0, close_status="filled", broker_positions=lambda: [])
    state = tick(state, d_open, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1

    # tick 2 (a later day): the spread blew out past the stop; broker confirms the position; close fills
    held = list(state.open_positions)
    d_stop = _deps(mark=lambda p: 10.0, close_status="filled", broker_positions=lambda: held)
    state = tick(state, d_stop, datetime(2026, 6, 17, 11, 0))   # Wednesday
    assert state.open_positions == [] and state.halted is False


def test_open_then_stop_cannot_close_halts():
    state = BotState()
    d_open = _deps(mark=lambda p: 3.0, close_status="filled", broker_positions=lambda: [])
    state = tick(state, d_open, datetime(2026, 6, 15, 10, 5))
    held = list(state.open_positions)

    # stop triggers but the close does NOT fill -> position retained, bot halted (the bot-B fix, top level)
    d_fail = _deps(mark=lambda p: 10.0, close_status="timeout", broker_positions=lambda: held)
    state = tick(state, d_fail, datetime(2026, 6, 17, 11, 0))
    assert state.halted is True and len(state.open_positions) == 1
```

- [ ] **Step 2: Run, expect pass** — `python -m pytest bot/tests/test_orchestrator_lifecycle.py -v` → 2 passed.

- [ ] **Step 3: Run the FULL suite** — `python -m pytest` → expected 88 (Plans 1–5) + 17 (Plan 6) = 105 passed.

- [ ] **Step 4: Commit**
```bash
git add bot/tests/test_orchestrator_lifecycle.py
git commit -m "test: full orchestrator lifecycle (open -> stop closes; failed stop halts)"
```

---

## Self-review notes (done)
- **Spec coverage:** §7 reconcile/halt → Task 2; §4 manage/stop/verify → Task 3; §3 Monday-10:00-flat entry + §5 sizing/regime/gate → Task 4; full sequencing with halt-gating → Task 5; end-to-end lifecycle incl. the failed-stop halt → Task 6.
- **No placeholders:** complete code + expected counts throughout.
- **Type consistency:** `BotState`/`Deps` (Task 1) used by all cycles; `run_reconcile_cycle`/`run_management_cycle`/`run_entry_cycle` (Tasks 2–4) composed by `tick` (Task 5); cycles reuse the exact module APIs (`monitor_positions`, `build_spread_order`, `reconcile`, `alerts_for_cycle`, `contracts_for_risk`, `regime_adjusted_risk_pct`, `RiskGate.is_order_allowed`) unchanged.
- **Halt is sticky within a tick:** reconcile sets `halted` before management/entry; `run_entry_cycle` early-returns when `state.halted`, so a drift or failed-close halt blocks new entries in the same tick (Task 5's `test_tick_reconcile_halt_blocks_entry_same_tick`).
- **One-position-at-a-time** invariant (S2b weekly): entry early-returns if `state.open_positions`, so concurrency/open-risk stay trivial.

## After Plan 6 — the bot is a complete, tested, composable core. Final go-live wiring (a thin, mostly-untestable layer, separate effort): a `bot/app/wiring.py` that builds production `Deps` (Tradier `make_http_from_env` → chain/quote/positions/equity feeds; ATR from price history; VIX/VIX3M regime; `now()` in ET) and a `runner` that calls `tick()` on the stop-monitor poll cadence and once at Monday 10:00 — pointed at **sandbox** first, then the spec §8 paper→live gates (incl. a stop demonstrably firing in a live drill).
```
