# S2b Bot — Plan 1: Risk Gate + Position Sizing (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-logic safety core of the S2b execution bot — position sizing (incl. regime-aware VIX size cuts) and the single pre-trade risk gate every order must pass — fully unit-tested, with zero broker/network dependencies.

**Architecture:** Two small, pure-Python modules under a new `bot/` package. `bot/sizing.py` computes contract counts and regime-adjusted risk %. `bot/risk_gate.py` is the single chokepoint (`RiskGate.is_order_allowed`) enforcing structure/cushion/risk-cap/concurrency/daily-loss/settled-cash/cooldown rules from the spec. No I/O, no broker — callers pass in plain dataclasses, so everything is deterministic and trivially testable.

**Tech Stack:** Python 3.11, pytest, dataclasses (stdlib only).

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` (§5 sizing, §6 settled-funds, §7 risk gate, §9 exclusions).

---

### Task 1: Package skeleton + pytest

**Files:**
- Create: `bot/__init__.py`
- Create: `bot/tests/__init__.py`
- Create: `pytest.ini`

- [ ] **Step 1: Create the package files**

`bot/__init__.py`:
```python
"""S2b execution bot."""
```

`bot/tests/__init__.py`:
```python
```

`pytest.ini`:
```ini
[pytest]
testpaths = bot/tests
python_files = test_*.py
addopts = -q
```

- [ ] **Step 2: Verify pytest discovers an empty suite**

Run: `python -m pytest`
Expected: exits 0 with "no tests ran" (collected 0 items).

- [ ] **Step 3: Commit**

```bash
git add bot/__init__.py bot/tests/__init__.py pytest.ini
git commit -m "chore: bot package skeleton + pytest config"
```

---

### Task 2: `contracts_for_risk` sizing

**Files:**
- Create: `bot/sizing.py`
- Test: `bot/tests/test_sizing.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_sizing.py`:
```python
import pytest
from bot.sizing import contracts_for_risk


def test_contracts_floor_division():
    # $20k equity, 10% risk = $2000 budget; $700 max-loss/contract -> 2 contracts
    assert contracts_for_risk(20_000, 700, 0.10) == 2


def test_contracts_minimum_one():
    # budget smaller than one contract's max loss still returns 1
    assert contracts_for_risk(5_000, 900, 0.10) == 1


def test_contracts_rejects_nonpositive_maxloss():
    with pytest.raises(ValueError):
        contracts_for_risk(20_000, 0, 0.10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_sizing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.sizing'`.

- [ ] **Step 3: Write minimal implementation**

`bot/sizing.py`:
```python
"""Position sizing for the S2b bot (spec §5)."""
import math


def contracts_for_risk(equity: float, max_loss_per_contract: float, risk_pct: float) -> int:
    """Number of spreads to trade: floor(equity*risk_pct / max_loss), min 1."""
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be > 0")
    budget = equity * risk_pct
    return max(1, math.floor(budget / max_loss_per_contract))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_sizing.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/sizing.py bot/tests/test_sizing.py
git commit -m "feat: contracts_for_risk position sizing"
```

---

### Task 3: `regime_adjusted_risk_pct` (VIX size cut)

**Files:**
- Modify: `bot/sizing.py`
- Test: `bot/tests/test_sizing.py`

- [ ] **Step 1: Add the failing test** (append to `bot/tests/test_sizing.py`)

```python
from bot.sizing import regime_adjusted_risk_pct


def test_regime_normal_keeps_base():
    # calm vol: percentile 0.5, no spike -> base unchanged
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.02) == 0.10


def test_regime_high_percentile_halves():
    # VIX above 80th percentile -> halve
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.85, vix_1d_change=0.0) == 0.05


def test_regime_spike_halves():
    # 1-day VIX change > +15% -> halve even if percentile low
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.40, vix_1d_change=0.20) == 0.05


def test_regime_none_inputs_keep_base():
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=None, vix_1d_change=None) == 0.10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_sizing.py -v`
Expected: FAIL with `ImportError: cannot import name 'regime_adjusted_risk_pct'`.

- [ ] **Step 3: Add the implementation** (append to `bot/sizing.py`)

```python
def regime_adjusted_risk_pct(base_pct: float, vix_pct_rank, vix_1d_change) -> float:
    """Halve size when VIX is elevated (pct_rank>0.80) or spiking (>+15% 1-day). Spec §5."""
    if vix_pct_rank is not None and vix_pct_rank > 0.80:
        return base_pct / 2.0
    if vix_1d_change is not None and vix_1d_change > 0.15:
        return base_pct / 2.0
    return base_pct
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_sizing.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/sizing.py bot/tests/test_sizing.py
git commit -m "feat: regime-adjusted risk pct (VIX size cut)"
```

---

### Task 4: Risk-gate data types + structure rule

**Files:**
- Create: `bot/risk_gate.py`
- Test: `bot/tests/test_risk_gate.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_risk_gate.py`:
```python
from bot.risk_gate import RiskGate, RiskConfig, SpreadOrder, AccountState


def _ok_order(**kw):
    base = dict(ticker="SPY", structure="bull_put_spread", short_strike=560.0,
                long_strike=550.0, credit=3.0, spot=575.0, atr=6.0,
                max_loss_per_contract=700.0, qty=2)
    base.update(kw)
    return SpreadOrder(**base)


def _ok_state(**kw):
    base = dict(equity=20_000.0, settled_cash=20_000.0, open_risk=0.0,
                concurrent_positions=0, realized_pnl_today=0.0,
                recent_losses={}, current_date="2026-06-15")
    base.update(kw)
    return AccountState(**base)


def test_allowed_structure_passes_structure_check():
    gate = RiskGate(RiskConfig())
    assert gate.is_order_allowed(_ok_order(), _ok_state()).allowed is True


def test_directional_debit_spread_rejected():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(structure="bull_call_spread"), _ok_state())
    assert d.allowed is False
    assert "structure" in d.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.risk_gate'`.

- [ ] **Step 3: Write minimal implementation**

`bot/risk_gate.py`:
```python
"""Single pre-trade risk chokepoint for the S2b bot (spec §7, §9)."""
from dataclasses import dataclass, field

ALLOWED_STRUCTURES = {"bull_put_spread", "bear_call_spread", "iron_condor"}


@dataclass
class SpreadOrder:
    ticker: str
    structure: str
    short_strike: float
    long_strike: float
    credit: float
    spot: float
    atr: float                       # ATR(14) of the underlying
    max_loss_per_contract: float     # dollars
    qty: int


@dataclass
class AccountState:
    equity: float
    settled_cash: float              # cash-account settled funds available
    open_risk: float                 # sum of max-loss across open positions ($)
    concurrent_positions: int
    realized_pnl_today: float
    recent_losses: dict              # ticker -> sessions since last loss in that ticker
    current_date: str


@dataclass
class RiskConfig:
    max_risk_pct: float = 0.10
    max_total_risk_pct: float = 0.30
    max_concurrent: int = 3
    daily_loss_halt_pct: float = 0.02
    cushion_min_atr: float = 1.0
    cooldown_sessions: int = 5
    allowed_structures: set = field(default_factory=lambda: set(ALLOWED_STRUCTURES))


@dataclass
class Decision:
    allowed: bool
    reason: str = "ok"


class RiskGate:
    def __init__(self, config: RiskConfig):
        self.cfg = config

    def is_order_allowed(self, order: SpreadOrder, state: AccountState) -> Decision:
        if order.structure not in self.cfg.allowed_structures:
            return Decision(False, f"structure {order.structure} not allowed")
        return Decision(True, "ok")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/risk_gate.py bot/tests/test_risk_gate.py
git commit -m "feat: risk-gate types + structure rule (no directional debits)"
```

---

### Task 5: Cushion rule (≥1 ATR OTM)

**Files:**
- Modify: `bot/risk_gate.py`
- Test: `bot/tests/test_risk_gate.py`

- [ ] **Step 1: Add the failing test** (append to `bot/tests/test_risk_gate.py`)

```python
from bot.risk_gate import RiskConfig as _RC  # alias to build tight configs


def test_bull_put_thin_cushion_rejected():
    # spot 575, short 572, ATR 6 -> cushion 0.5 ATR < 1.0 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(short_strike=572.0), _ok_state())
    assert d.allowed is False
    assert "cushion" in d.reason


def test_bull_put_fat_cushion_allowed():
    # spot 575, short 560, ATR 6 -> cushion 2.5 ATR -> ok
    gate = RiskGate(RiskConfig())
    assert gate.is_order_allowed(_ok_order(short_strike=560.0), _ok_state()).allowed is True


def test_bear_call_cushion_uses_other_side():
    # bear_call: cushion = (short - spot)/atr; short 590, spot 575, atr 6 -> 2.5 ATR ok
    gate = RiskGate(RiskConfig())
    o = _ok_order(structure="bear_call_spread", short_strike=590.0, long_strike=600.0)
    assert gate.is_order_allowed(o, _ok_state()).allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: FAIL — `test_bull_put_thin_cushion_rejected` fails (thin cushion currently allowed).

- [ ] **Step 3: Extend the implementation** — replace the `is_order_allowed` body and add `_cushion_atr` in `bot/risk_gate.py`:

```python
    def is_order_allowed(self, order: SpreadOrder, state: AccountState) -> Decision:
        if order.structure not in self.cfg.allowed_structures:
            return Decision(False, f"structure {order.structure} not allowed")
        cushion = self._cushion_atr(order)
        if cushion is not None and cushion < self.cfg.cushion_min_atr:
            return Decision(False, f"cushion {cushion:.2f} ATR < {self.cfg.cushion_min_atr}")
        return Decision(True, "ok")

    def _cushion_atr(self, order: SpreadOrder):
        """Distance from spot to short strike, in ATRs. None if not applicable."""
        if order.atr <= 0:
            return None
        if order.structure == "bull_put_spread":
            dist = order.spot - order.short_strike
        elif order.structure == "bear_call_spread":
            dist = order.short_strike - order.spot
        else:
            return None  # iron_condor: per-side cushion handled by the strategy, not here
        return dist / order.atr
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/risk_gate.py bot/tests/test_risk_gate.py
git commit -m "feat: >=1 ATR cushion gate on short strikes"
```

---

### Task 6: Risk caps (per-trade + total open risk)

**Files:**
- Modify: `bot/risk_gate.py`
- Test: `bot/tests/test_risk_gate.py`

- [ ] **Step 1: Add the failing test** (append to `bot/tests/test_risk_gate.py`)

```python
def test_per_trade_risk_cap_rejects():
    # 10% of $20k = $2000 cap; order risk = 700*4 = $2800 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=4), _ok_state())
    assert d.allowed is False
    assert "per-trade risk" in d.reason


def test_total_open_risk_cap_rejects():
    # 30% of $20k = $6000 total cap; already $5000 open + $1400 new = $6400 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=2), _ok_state(open_risk=5000.0))
    assert d.allowed is False
    assert "total open risk" in d.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: FAIL — both new tests fail (caps not yet enforced).

- [ ] **Step 3: Extend `is_order_allowed`** — insert these checks before the final `return Decision(True, "ok")` in `bot/risk_gate.py`:

```python
        trade_risk = order.max_loss_per_contract * order.qty
        if trade_risk > state.equity * self.cfg.max_risk_pct + 1e-9:
            return Decision(False, "per-trade risk exceeds cap")
        if state.open_risk + trade_risk > state.equity * self.cfg.max_total_risk_pct + 1e-9:
            return Decision(False, "total open risk exceeds cap")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/risk_gate.py bot/tests/test_risk_gate.py
git commit -m "feat: per-trade and total-open-risk caps"
```

---

### Task 7: Concurrency, daily-loss halt, settled cash, cooldown

**Files:**
- Modify: `bot/risk_gate.py`
- Test: `bot/tests/test_risk_gate.py`

- [ ] **Step 1: Add the failing tests** (append to `bot/tests/test_risk_gate.py`)

```python
def test_max_concurrent_rejects():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(), _ok_state(concurrent_positions=3))
    assert d.allowed is False and "concurrent" in d.reason


def test_daily_loss_halt_rejects():
    # 2% of $20k = $400 loss halt; today -$450 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(), _ok_state(realized_pnl_today=-450.0))
    assert d.allowed is False and "daily loss" in d.reason


def test_insufficient_settled_cash_rejects():
    # trade risk 700*2=1400 but only $1000 settled -> reject (cash account)
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=2), _ok_state(settled_cash=1000.0))
    assert d.allowed is False and "settled cash" in d.reason


def test_same_ticker_cooldown_rejects():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(ticker="SPY"), _ok_state(recent_losses={"SPY": 2}))
    assert d.allowed is False and "cooldown" in d.reason


def test_cooldown_expired_allows():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(ticker="SPY"), _ok_state(recent_losses={"SPY": 5}))
    assert d.allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: FAIL — the 4 reject tests fail (rules not enforced).

- [ ] **Step 3: Extend `is_order_allowed`** — insert before the final `return Decision(True, "ok")`, after the risk-cap checks, in `bot/risk_gate.py`:

```python
        if state.concurrent_positions >= self.cfg.max_concurrent:
            return Decision(False, "max concurrent positions reached")
        if state.realized_pnl_today <= -self.cfg.daily_loss_halt_pct * state.equity:
            return Decision(False, "daily loss halt active")
        if trade_risk > state.settled_cash + 1e-9:
            return Decision(False, "insufficient settled cash")
        sessions_since = state.recent_losses.get(order.ticker)
        if sessions_since is not None and sessions_since < self.cfg.cooldown_sessions:
            return Decision(False, f"{order.ticker} in cooldown")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_risk_gate.py -v`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/risk_gate.py bot/tests/test_risk_gate.py
git commit -m "feat: concurrency, daily-loss halt, settled-cash, cooldown rules"
```

---

### Task 8: End-to-end sizing→gate integration test

**Files:**
- Test: `bot/tests/test_integration.py`

- [ ] **Step 1: Write the integration test**

`bot/tests/test_integration.py`:
```python
from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig, SpreadOrder, AccountState


def test_size_then_gate_happy_path():
    equity = 20_000.0
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.01)  # calm -> 0.10
    qty = contracts_for_risk(equity, max_loss_per_contract=700.0, risk_pct=risk)  # -> 2
    order = SpreadOrder("SPY", "bull_put_spread", 560.0, 550.0, 3.0, 575.0, 6.0, 700.0, qty)
    state = AccountState(equity, equity, 0.0, 0, 0.0, {}, "2026-06-15")
    assert RiskGate(RiskConfig()).is_order_allowed(order, state).allowed is True


def test_vix_spike_downsizes_and_still_passes():
    equity = 20_000.0
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.9, vix_1d_change=0.0)  # elevated -> 0.05
    qty = contracts_for_risk(equity, 700.0, risk)  # floor(1000/700)=1
    assert qty == 1
    order = SpreadOrder("SPY", "bull_put_spread", 560.0, 550.0, 3.0, 575.0, 6.0, 700.0, qty)
    state = AccountState(equity, equity, 0.0, 0, 0.0, {}, "2026-06-15")
    assert RiskGate(RiskConfig()).is_order_allowed(order, state).allowed is True
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_integration.py -v`
Expected: PASS (2 passed). (No new code — this validates the two modules compose.)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest`
Expected: PASS (21 passed total).

- [ ] **Step 4: Commit**

```bash
git add bot/tests/test_integration.py
git commit -m "test: sizing->risk-gate integration (calm + VIX-spike paths)"
```

---

## Self-review notes (done)

- **Spec coverage:** §5 sizing → Tasks 2–3; §5 regime cut → Task 3; §6 settled-cash → Task 7; §7 risk-gate caps/concurrency/daily-loss/cooldown → Tasks 6–7; §9 no-directional-debits → Task 4; ≥1 ATR cushion (§3/§5) → Task 5. Stops, broker, strategy, ledger, monitoring are **out of scope for Plan 1** (Plans 2–5).
- **Placeholder scan:** none — every step has complete code/commands/expected output.
- **Type consistency:** `SpreadOrder`, `AccountState`, `RiskConfig`, `Decision` defined in Task 4 and used unchanged in Tasks 5–8; field names (`max_loss_per_contract`, `settled_cash`, `recent_losses`, `concurrent_positions`) consistent throughout.

## Next plans (write after Plan 1 lands)
2. Tradier broker adapter + order state machine · 3. S2b strategy + cushion gate (produces the `SpreadOrder` fed to this gate) · 4. Stop monitor + management loop · 5. Truth ledger + monitoring + chaos drills.
