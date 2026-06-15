# S2b Bot — Plan 3: S2b Strategy + Cushion-Aware Strike Selection (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The S2b entry strategy — decide *if* today is an entry (Monday), select a short put at the target delta that *also* clears the ≥1 ATR cushion, build the bull-put `SpreadOrder` (the same dataclass the Plan-1 risk gate consumes), and render it to a Tradier multileg payload (for the Plan-2 adapter).

**Architecture:** `bot/strategy/s2b.py` holds pure functions: `is_entry_day` (Monday gate), `select_short_put` (delta-target + cushion-constrained pick from a supplied option chain), `build_spread_order` (short+long → `SpreadOrder` with credit/max-loss), and `to_tradier_payload`. The strategy takes the option chain as an **input** (a list of `OptionQuote`) so it's pure and hermetically testable — chain fetching is the adapter's job, wired later. Strike selection enforces cushion at selection time; the Plan-1 gate re-checks it (defense in depth).

**Tech Stack:** Python 3.11, pytest, dataclasses (stdlib). Reuses `bot.risk_gate.SpreadOrder`.

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §3 (Monday SPY bull put, ~30–40Δ, $5–10 wing, weekly ≥4 DTE, ≥1 ATR cushion), §9 (defined-risk credit only).

---

### Task 1: Strategy package + Monday entry gate

**Files:** Create `bot/strategy/__init__.py`, `bot/strategy/s2b.py`, `bot/tests/test_s2b.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_s2b.py`:
```python
from bot.strategy.s2b import is_entry_day


def test_monday_is_entry_day():
    assert is_entry_day("2026-06-15") is True   # Monday


def test_other_days_not_entry():
    assert is_entry_day("2026-06-16") is False   # Tuesday
    assert is_entry_day("2026-06-19") is False   # Friday
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.strategy.s2b`.

- [ ] **Step 3: Implement** — `bot/strategy/__init__.py`:
```python
"""Trading strategies for the S2b bot."""
```
`bot/strategy/s2b.py`:
```python
"""S2b entry strategy: Monday SPY bull put spread, cushion-aware (spec §3)."""
from datetime import datetime


def is_entry_day(date_str: str) -> bool:
    """True iff date_str (YYYY-MM-DD) is a Monday (the validated S2b entry day)."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 0
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_s2b.py -v` → 2 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/__init__.py bot/strategy/s2b.py bot/tests/test_s2b.py
git commit -m "feat: S2b Monday entry-day gate"
```

---

### Task 2: OptionQuote/S2bConfig types + cushion-aware short-put selection

**Files:** Modify `bot/strategy/s2b.py`, `bot/tests/test_s2b.py`

- [ ] **Step 1: Add failing tests** (append to `bot/tests/test_s2b.py`):
```python
from bot.strategy.s2b import OptionQuote, S2bConfig, select_short_put


def _chain():
    # SPY puts; abs delta; strike, delta, bid, ask. spot assumed 575, ATR 6.
    return [
        OptionQuote(strike=572.0, delta=0.45, bid=4.50, ask=4.60),  # cushion 0.5 ATR -> excluded
        OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.50),  # cushion 1.17 ATR, delta near 0.35
        OptionQuote(strike=565.0, delta=0.30, bid=2.80, ask=2.90),  # cushion 1.67 ATR
        OptionQuote(strike=560.0, delta=0.22, bid=2.00, ask=2.10),  # cushion 2.5 ATR
    ]


def test_select_short_put_respects_cushion_and_delta():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    pick = select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg)
    # 572 (delta 0.45, closest to nothing) is excluded by cushion; among cushion-OK,
    # 568 (delta 0.36) is closest to target 0.35
    assert pick.strike == 568.0


def test_select_short_put_none_when_no_cushion():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=5.0)  # demand 5 ATR
    assert select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg) is None
```

- [ ] **Step 2: Run, expect fail** — `ImportError: OptionQuote`.

- [ ] **Step 3: Implement** (append to `bot/strategy/s2b.py`):
```python
from dataclasses import dataclass


@dataclass
class OptionQuote:
    strike: float
    delta: float    # absolute delta of the put (0..1)
    bid: float
    ask: float


@dataclass
class S2bConfig:
    target_delta: float = 0.35
    wing_width: float = 10.0
    min_cushion_atr: float = 1.0


def select_short_put(chain, spot, atr, cfg):
    """Pick the OTM put nearest target_delta that is also >= min_cushion_atr OTM. None if none qualify."""
    if atr <= 0:
        return None
    eligible = [o for o in chain
                if o.strike < spot and (spot - o.strike) / atr >= cfg.min_cushion_atr]
    if not eligible:
        return None
    return min(eligible, key=lambda o: abs(o.delta - cfg.target_delta))
```

- [ ] **Step 4: Run, expect pass** — 4 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/s2b.py bot/tests/test_s2b.py
git commit -m "feat: cushion-aware short-put selection"
```

---

### Task 3: build_spread_order → SpreadOrder

**Files:** Modify `bot/strategy/s2b.py`, `bot/tests/test_s2b.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.strategy.s2b import build_spread_order
from bot.risk_gate import SpreadOrder


def test_build_spread_order_builds_bull_put():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    # add the long wing (568 short -> 558 long) to the chain
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70)]
    order = build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg)
    assert isinstance(order, SpreadOrder)
    assert order.structure == "bull_put_spread"
    assert order.short_strike == 568.0 and order.long_strike == 558.0
    # credit = short.bid - long.ask = 3.40 - 1.70 = 1.70
    assert order.credit == 1.70
    # max loss per contract = (10 - 1.70) * 100 = 830
    assert order.max_loss_per_contract == 830.0
    assert order.qty == 1   # sizing sets real qty later


def test_build_spread_order_none_when_long_wing_missing():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    order = build_spread_order(spot=575.0, atr=6.0, chain=_chain(), cfg=cfg)  # no 558 strike
    assert order is None


def test_build_spread_order_none_when_nonpositive_credit():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    # long wing priced higher than short bid -> credit <= 0
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=3.50, ask=3.60)]
    assert build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg) is None
```

- [ ] **Step 2: Run, expect fail** — `ImportError: build_spread_order`.

- [ ] **Step 3: Implement** (append):
```python
from bot.risk_gate import SpreadOrder


def build_spread_order(spot, atr, chain, cfg):
    """Select short+long puts and build a bull_put_spread SpreadOrder. None if not buildable."""
    short = select_short_put(chain, spot, atr, cfg)
    if short is None:
        return None
    long_strike = short.strike - cfg.wing_width
    longs = [o for o in chain if o.strike == long_strike]
    if not longs:
        return None
    long = longs[0]
    credit = round(short.bid - long.ask, 2)
    if credit <= 0:
        return None
    max_loss = round((cfg.wing_width - credit) * 100, 2)
    return SpreadOrder(ticker="SPY", structure="bull_put_spread",
                       short_strike=short.strike, long_strike=long_strike,
                       credit=credit, spot=spot, atr=atr,
                       max_loss_per_contract=max_loss, qty=1)
```

- [ ] **Step 4: Run, expect pass** — 7 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/s2b.py bot/tests/test_s2b.py
git commit -m "feat: build_spread_order produces bull-put SpreadOrder"
```

---

### Task 4: to_tradier_payload

**Files:** Modify `bot/strategy/s2b.py`, `bot/tests/test_s2b.py`

- [ ] **Step 1: Add failing test** (append):
```python
from bot.strategy.s2b import to_tradier_payload


def test_to_tradier_payload_multileg_bull_put():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70)]
    order = build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg)
    payload = to_tradier_payload(order, expiry="2026-06-19", qty=2)
    assert payload["class"] == "multileg"
    assert payload["symbol"] == "SPY"
    assert payload["type"] == "credit"
    assert payload["duration"] == "day"
    # short put leg = sell to open; long put leg = buy to open
    assert payload["option_symbol[0]"] == "SPY260619P00568000"
    assert payload["side[0]"] == "sell_to_open" and payload["quantity[0]"] == 2
    assert payload["option_symbol[1]"] == "SPY260619P00558000"
    assert payload["side[1]"] == "buy_to_open" and payload["quantity[1]"] == 2
```

- [ ] **Step 2: Run, expect fail** — `ImportError: to_tradier_payload`.

- [ ] **Step 3: Implement** (append):
```python
def _occ(symbol, expiry, right, strike):
    """OCC option symbol, e.g. SPY260619P00568000."""
    yymmdd = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    return f"{symbol}{yymmdd}{right}{strike_int:08d}"


def to_tradier_payload(order, expiry, qty):
    """Render a bull_put_spread SpreadOrder to a Tradier multileg credit order payload."""
    sym = order.ticker
    return {
        "class": "multileg", "symbol": sym, "type": "credit", "duration": "day",
        "option_symbol[0]": _occ(sym, expiry, "P", order.short_strike),
        "side[0]": "sell_to_open", "quantity[0]": qty,
        "option_symbol[1]": _occ(sym, expiry, "P", order.long_strike),
        "side[1]": "buy_to_open", "quantity[1]": qty,
    }
```

- [ ] **Step 4: Run, expect pass** — 8 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/strategy/s2b.py bot/tests/test_s2b.py
git commit -m "feat: to_tradier_payload (multileg credit order)"
```

---

### Task 5: Integration — strategy → risk gate → payload

**Files:** Create `bot/tests/test_s2b_integration.py`

- [ ] **Step 1: Write the integration test** — `bot/tests/test_s2b_integration.py`:
```python
from bot.strategy.s2b import S2bConfig, OptionQuote, build_spread_order, to_tradier_payload, is_entry_day
from bot.risk_gate import RiskGate, RiskConfig, AccountState
from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct


def _chain():
    return [
        OptionQuote(572.0, 0.45, 4.50, 4.60),
        OptionQuote(568.0, 0.36, 3.40, 3.50),
        OptionQuote(565.0, 0.30, 2.80, 2.90),
        OptionQuote(560.0, 0.22, 2.00, 2.10),
        OptionQuote(558.0, 0.18, 1.60, 1.70),
    ]


def test_full_entry_pipeline_monday():
    assert is_entry_day("2026-06-15")
    cfg = S2bConfig()
    order = build_spread_order(spot=575.0, atr=6.0, chain=_chain(), cfg=cfg)
    assert order is not None
    # size it (calm VIX), then gate it
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.01)
    order.qty = contracts_for_risk(20_000.0, order.max_loss_per_contract, risk)
    state = AccountState(20_000.0, 20_000.0, 0.0, 0, 0.0, {}, "2026-06-15")
    decision = RiskGate(RiskConfig()).is_order_allowed(order, state)
    assert decision.allowed is True
    payload = to_tradier_payload(order, expiry="2026-06-19", qty=order.qty)
    assert payload["class"] == "multileg" and payload["quantity[0]"] == order.qty
```

- [ ] **Step 2: Run, expect pass** — `python -m pytest bot/tests/test_s2b_integration.py -v` → 1 passed (no new code; validates the modules compose).

- [ ] **Step 3: Run the FULL suite** — `python -m pytest` → expected 45 (Plans 1–2) + 9 (Plan 3) = 54 passed.

- [ ] **Step 4: Commit**
```bash
git add bot/tests/test_s2b_integration.py
git commit -m "test: full S2b entry pipeline (strategy -> size -> gate -> payload)"
```

---

## Self-review notes (done)
- **Spec coverage:** §3 Monday entry → Task 1; ~30–40Δ short with ≥1 ATR cushion → Task 2; $X wing + bull-put SpreadOrder + positive-credit/defined-risk → Task 3; broker payload → Task 4; end-to-end with sizing+gate → Task 5.
- **No placeholders:** complete code + expected counts throughout.
- **Type consistency:** `OptionQuote`/`S2bConfig` defined Task 2, reused Tasks 3–5; `build_spread_order` returns `bot.risk_gate.SpreadOrder` (same type the gate consumes); `to_tradier_payload` output keys match the Tradier multileg shape the Plan-2 `place_order` forwards.
- **Cushion defense-in-depth:** selection enforces cushion (Task 2) AND the gate re-checks it (Plan 1) — intentional.

## Next: Plan 4 — stop-monitor + management loop (the critical "stops that fire" piece): poll open-position marks, fire TP/stop/time exits via `submit_and_verify`.
